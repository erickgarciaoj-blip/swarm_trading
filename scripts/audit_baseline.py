"""
Read-only baseline snapshot of the swarm's financial and operational state.

    python scripts/audit_baseline.py
    python scripts/audit_baseline.py --json

STRICTLY READ-ONLY. Every database statement issued here is a SELECT, the
engine is opened without any schema step, and nothing is written, migrated,
created or deleted. Safe to run against the live VPS database while the swarm
is trading.

WHY THIS EXISTS
---------------
The dashboard's PnL and `SELECT sum(pnl) FROM trades` disagree, and the gap
is not a rounding artifact. The dashboard shows realized equity rebuilt by
SwarmFactory._restore_agent_state(), which replays only trades whose agent_id
belongs to an agent in the *current* swarm composition. Every trade written
under an older id scheme, or by an agent index that no longer exists, stays
in the database and never re-enters equity.

So the database is a superset and the dashboard shows a restorable subset.
This script quantifies both, plus the difference, so the two numbers can be
compared deliberately instead of being assumed equal.

WHAT IT CANNOT KNOW
-------------------
Floating (mark-to-market) PnL lives in the running orchestrator process and
depends on the current tick price. It is not persisted anywhere. This script
reports it as N/A rather than inventing a value; query the live process at
GET /swarm/summary for that figure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from swarm_trading.agents.templates.swarm_factory import (
    HEDGER_PER_SYMBOL,
    NEWS_PER_SYMBOL,
    RL_PER_SYMBOL,
    SCALPERS_PER_SYMBOL,
    SWING_PER_SYMBOL,
)
from swarm_trading.core.config import settings
from swarm_trading.core.models import Symbol
from swarm_trading.data.historic.repository import normalize_async_url

# Agent counts per symbol, read from SwarmFactory rather than duplicated here
# — the whole point of the id classification below is to compare the database
# against the composition the swarm actually builds today.
CURRENT_COMPOSITION: dict[str, int] = {
    "SCALPER": SCALPERS_PER_SYMBOL,
    "SWING": SWING_PER_SYMBOL,
    "NEWS_REACTIVE": NEWS_PER_SYMBOL,
    "HEDGER": HEDGER_PER_SYMBOL,
    "RL": RL_PER_SYMBOL,
}

AGENT_TYPES = tuple(CURRENT_COMPOSITION)
SYMBOL_NAMES = tuple(s.value for s in Symbol)

# SwarmFactory's stable id format: "{TYPE}_{SYMBOL}_{index}".
# NEWS_REACTIVE contains an underscore itself, so the type alternation is
# ordered longest-first to stop "NEWS" matching before "NEWS_REACTIVE".
_STABLE_ID = re.compile(
    r"^(?P<type>" + "|".join(sorted(AGENT_TYPES, key=len, reverse=True)) + r")"
    r"_(?P<symbol>" + "|".join(SYMBOL_NAMES) + r")"
    r"_(?P<index>\d+)$"
)

NOT_AVAILABLE = "N/A"

# Classification of an agent_id relative to the swarm running today.
IN_SWARM = "in_swarm"  # restorable — its PnL is in the dashboard's equity
OUT_OF_RANGE = "out_of_range"  # stable format, index beyond current composition
LEGACY = "legacy"  # random-UUID era ids, unmatched by the stable format


def classify_agent_id(agent_id: str) -> str:
    """IN_SWARM / OUT_OF_RANGE / LEGACY for one agent_id.

    Only IN_SWARM ids are replayed by SwarmFactory._restore_agent_state(), so
    only their trades contribute to the equity the dashboard reports.

    OUT_OF_RANGE is a real category, not a theoretical one: an id like
    "SCALPER_OIL_32899139" matches the stable format (a UUID hex fragment
    that happens to be all digits) but names an agent index the factory never
    builds, so it is just as orphaned as a LEGACY id. Classifying on format
    alone would silently count it as restorable.
    """
    match = _STABLE_ID.match(agent_id)
    if match is None:
        return LEGACY
    agent_type = match.group("type")
    index = int(match.group("index"))
    if index < CURRENT_COMPOSITION[agent_type]:
        return IN_SWARM
    return OUT_OF_RANGE


def agent_type_of(agent_id: str) -> str:
    """Strategy family for an agent_id, from its prefix — the `agents` table
    only ever holds "UNKNOWN" placeholder rows (they are upserted by
    save_trade() purely to satisfy the trades.agent_id foreign key), so the
    id itself is the only available source."""
    for agent_type in sorted(AGENT_TYPES, key=len, reverse=True):
        if agent_id.startswith(agent_type + "_"):
            return agent_type
    return "UNKNOWN"


def symbol_of(agent_id: str, fallback: str) -> str:
    match = _STABLE_ID.match(agent_id)
    return match.group("symbol") if match else fallback


@dataclass
class TradeRow:
    """Only the columns this script reads. Deliberately not the ORM model —
    nothing here should be able to write."""

    agent_id: str
    symbol: str
    pnl: float
    status: str
    opened_at: datetime | None
    closed_at: datetime | None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass
class Reconciliation:
    """The dashboard-vs-database gap, decomposed. `unexplained` must be 0.0;
    anything else means a source of divergence this model does not know
    about, and is reported rather than absorbed."""

    in_swarm_pnl: float = 0.0
    in_swarm_count: int = 0
    out_of_range_pnl: float = 0.0
    out_of_range_count: int = 0
    legacy_pnl: float = 0.0
    legacy_count: int = 0
    observed_dashboard_pnl: float | None = None

    @property
    def db_total_pnl(self) -> float:
        return self.in_swarm_pnl + self.out_of_range_pnl + self.legacy_pnl

    @property
    def orphaned_pnl(self) -> float:
        """In the database, invisible to the dashboard."""
        return self.out_of_range_pnl + self.legacy_pnl

    @property
    def orphaned_count(self) -> int:
        return self.out_of_range_count + self.legacy_count

    @property
    def unexplained(self) -> float | None:
        """Observed dashboard PnL minus what this model predicts it should
        be. None when no observed figure was supplied to compare against."""
        if self.observed_dashboard_pnl is None:
            return None
        return self.observed_dashboard_pnl - self.in_swarm_pnl


def reconcile(trades: list[TradeRow], observed_dashboard_pnl: float | None = None) -> Reconciliation:
    """Split realized PnL by whether the swarm running today can restore it."""
    result = Reconciliation(observed_dashboard_pnl=observed_dashboard_pnl)
    for trade in trades:
        kind = classify_agent_id(trade.agent_id)
        if kind == IN_SWARM:
            result.in_swarm_pnl += trade.pnl
            result.in_swarm_count += 1
        elif kind == OUT_OF_RANGE:
            result.out_of_range_pnl += trade.pnl
            result.out_of_range_count += 1
        else:
            result.legacy_pnl += trade.pnl
            result.legacy_count += 1
    return result


@dataclass
class Baseline:
    git_sha: str
    git_dirty: bool | None
    app_env: str
    db_dialect: str
    db_reachable: bool
    db_error: str | None = None

    initial_capital: float = 0.0
    trades: list[TradeRow] = field(default_factory=list)
    latest_trade_at: datetime | None = None
    latest_snapshot_at: datetime | None = None
    latest_snapshot_equity: float | None = None
    latest_snapshot_trades: int | None = None
    agent_rows: int = 0
    agent_types_in_db: dict[str, int] = field(default_factory=dict)


def git_sha() -> tuple[str, bool | None]:
    """(sha, dirty). Read-only: rev-parse and status --porcelain never modify
    the working tree or the index."""
    repo_root = Path(__file__).resolve().parent.parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        return sha, bool(status)
    except (subprocess.SubprocessError, OSError):
        return NOT_AVAILABLE, None


def missing_sqlite_file(url: str) -> str | None:
    """Path of a SQLite database file named by `url` that does not exist yet,
    or None (URL is not file-backed SQLite, or the file is already there).

    Guards the one way this script could write anything: SQLite creates an
    empty database file on connect, so pointing it at a wrong or not-yet-
    created path would leave a new 0-byte file on disk AND report the
    database as reachable-but-empty. Postgres has no equivalent behaviour.
    """
    if not url.startswith("sqlite"):
        return None
    _, _, location = url.partition(":///")
    if not location or location.startswith(":memory:"):
        return None
    path = Path(location.split("?", 1)[0])
    return None if path.exists() else str(path)


async def load_baseline() -> Baseline:
    sha, dirty = git_sha()
    # normalize_async_url only fills in the async driver; it never reveals or
    # logs the URL. The URL itself is never printed by this script.
    url = normalize_async_url(settings.database_url)
    engine = create_async_engine(url)
    baseline = Baseline(
        git_sha=sha,
        git_dirty=dirty,
        app_env=settings.app_env,
        db_dialect=engine.dialect.name,
        db_reachable=False,
        initial_capital=settings.swarm_total_capital_usd,
    )

    if missing_sqlite_file(url) is not None:
        # Reported as unreachable rather than connected-and-empty: connecting
        # would create the file, and "0 trades" from a database this script
        # just invented is a worse answer than "not there".
        baseline.db_error = "SQLiteFileNotFound"
        await engine.dispose()
        return baseline

    try:
        async with engine.connect() as conn:  # connect(), not begin() — no transaction to commit
            baseline.db_reachable = True

            rows = await conn.execute(text("SELECT agent_id, symbol, pnl, status, opened_at, closed_at FROM trades"))
            baseline.trades = [
                TradeRow(
                    agent_id=r[0],
                    symbol=r[1],
                    pnl=float(r[2] or 0.0),
                    status=r[3],
                    opened_at=_as_datetime(r[4]),
                    closed_at=_as_datetime(r[5]),
                )
                for r in rows
            ]
            closed_at_values = [t.closed_at for t in baseline.trades if t.closed_at]
            baseline.latest_trade_at = max(closed_at_values) if closed_at_values else None

            snapshot = (
                await conn.execute(
                    text(
                        "SELECT timestamp, total_equity, total_trades FROM swarm_snapshots "
                        "ORDER BY timestamp DESC LIMIT 1"
                    )
                )
            ).first()
            if snapshot is not None:
                baseline.latest_snapshot_at = _as_datetime(snapshot[0])
                baseline.latest_snapshot_equity = float(snapshot[1])
                baseline.latest_snapshot_trades = int(snapshot[2])

            agent_rows = await conn.execute(text("SELECT agent_type, COUNT(*) FROM agents GROUP BY agent_type"))
            baseline.agent_types_in_db = {r[0]: int(r[1]) for r in agent_rows}
            baseline.agent_rows = sum(baseline.agent_types_in_db.values())
    except Exception as exc:  # any driver/connection failure means "not reachable"
        # Only the exception TYPE is kept. Driver messages routinely embed the
        # full DSN including credentials, and this output is meant to be
        # pasteable.
        baseline.db_error = type(exc).__name__
    finally:
        await engine.dispose()

    return baseline


def _as_datetime(value: Any) -> datetime | None:
    """Postgres returns datetimes; SQLite may hand back ISO strings."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _fmt(value: float | None, width: int = 14, places: int = 4) -> str:
    return NOT_AVAILABLE.rjust(width) if value is None else f"{value:>{width},.{places}f}"


def _fmt_dt(value: datetime | None) -> str:
    return NOT_AVAILABLE if value is None else value.isoformat(sep=" ", timespec="seconds")


def render(baseline: Baseline) -> str:
    out: list[str] = []
    add = out.append

    add("=== TRADING SWARM BASELINE ===")
    add("")
    dirty = NOT_AVAILABLE if baseline.git_dirty is None else ("yes" if baseline.git_dirty else "no")
    add(f"Git SHA:                  {baseline.git_sha}")
    add(f"Working tree dirty:       {dirty}")
    add(f"APP_ENV:                  {baseline.app_env}")
    add(f"DB dialect:               {baseline.db_dialect}")
    add(f"DB reachable:             {'yes' if baseline.db_reachable else f'no ({baseline.db_error})'}")

    if not baseline.db_reachable:
        add("")
        add("Database unreachable — every figure below requires it.")
        return "\n".join(out)

    trades = baseline.trades
    closed = [t for t in trades if not t.is_open]
    open_trades = [t for t in trades if t.is_open]
    rec = reconcile(closed, observed_dashboard_pnl=None)

    add("")
    add("--- Capital ---")
    add(f"Initial capital:        {_fmt(baseline.initial_capital)}")
    add(f"Realized PnL (restorable):{_fmt(rec.in_swarm_pnl, width=12)}")
    add(f"Realized equity:        {_fmt(baseline.initial_capital + rec.in_swarm_pnl)}")
    add(f"Floating PnL:           {NOT_AVAILABLE:>14}   (live-process only — GET /swarm/summary)")
    add(f"Total equity:           {NOT_AVAILABLE:>14}   (needs floating PnL)")
    add("")
    add("--- Reconciliation: database total vs what the swarm can restore ---")
    add(f"  in-swarm agent ids    {rec.in_swarm_count:>7d} trades {_fmt(rec.in_swarm_pnl)}   <- dashboard sees this")
    add(f"  stable id, index gone {rec.out_of_range_count:>7d} trades {_fmt(rec.out_of_range_pnl)}   <- orphaned")
    add(f"  legacy/UUID agent ids {rec.legacy_count:>7d} trades {_fmt(rec.legacy_pnl)}   <- orphaned")
    add(f"  {'':22}{'':>7}        {'-' * 14}")
    add(f"  DB total              {len(closed):>7d} trades {_fmt(rec.db_total_pnl)}")
    add("")
    add(f"  Orphaned (in DB, never restored into equity): {_fmt(rec.orphaned_pnl, width=12)}")
    if baseline.latest_snapshot_equity is not None:
        observed = baseline.latest_snapshot_equity - baseline.initial_capital
        checked = reconcile(closed, observed_dashboard_pnl=observed)
        add(f"  Last snapshot PnL (observed):                {_fmt(observed, width=12)}")
        add(f"  Predicted (in-swarm only):                   {_fmt(rec.in_swarm_pnl, width=12)}")
        add(f"  UNEXPLAINED:                                 {_fmt(checked.unexplained, width=12)}")

    add("")
    add("--- Trades ---")
    add(f"Total:                  {len(trades):>14,d}")
    add(f"Open (closed_at NULL):  {len(open_trades):>14,d}")
    add(f"Closed:                 {len(closed):>14,d}")
    add(f"Latest closed trade:    {_fmt_dt(baseline.latest_trade_at)}")

    add("")
    add("--- Realized PnL by strategy (closed trades) ---")
    add(_table(closed, key=lambda t: agent_type_of(t.agent_id), order=AGENT_TYPES))

    add("")
    add("--- Realized PnL by symbol (closed trades) ---")
    add(_table(closed, key=lambda t: symbol_of(t.agent_id, t.symbol), order=SYMBOL_NAMES))

    add("")
    add("--- Agent ids ---")
    unique = {t.agent_id for t in trades}
    kinds: dict[str, int] = defaultdict(int)
    for agent_id in unique:
        kinds[classify_agent_id(agent_id)] += 1
    expected = sum(CURRENT_COMPOSITION.values()) * len(SYMBOL_NAMES)
    add(f"Unique (ever traded):   {len(unique):>14,d}")
    add(f"  in current swarm:     {kinds[IN_SWARM]:>14,d}   (composition builds {expected})")
    add(f"  stable, index gone:   {kinds[OUT_OF_RANGE]:>14,d}")
    add(f"  legacy/UUID-like:     {kinds[LEGACY]:>14,d}")
    add("")
    add("--- Agents table ---")
    add(f"Rows:                   {baseline.agent_rows:>14,d}")
    for agent_type, count in sorted(baseline.agent_types_in_db.items()):
        add(f"  {agent_type:<20s}  {count:>14,d}")
    add(f"Latest snapshot:        {_fmt_dt(baseline.latest_snapshot_at)}")
    add(f"Snapshot equity:        {_fmt(baseline.latest_snapshot_equity)}")

    return "\n".join(out)


def _table(trades: list[TradeRow], key: Any, order: tuple[str, ...]) -> str:
    buckets: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        buckets[key(trade)].append(trade.pnl)

    lines = [f"  {'':<16}{'trades':>9}{'PnL':>14}{'avg':>12}{'win rate':>11}"]
    # `order` first so absent strategies still show as a zero row — an agent
    # type with no trades at all is exactly what this baseline needs to make
    # visible, and omitting the row would hide it.
    for name in list(order) + sorted(set(buckets) - set(order)):
        pnls = buckets.get(name, [])
        if not pnls:
            lines.append(f"  {name:<16}{0:>9d}{NOT_AVAILABLE:>14}{NOT_AVAILABLE:>12}{NOT_AVAILABLE:>11}")
            continue
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        lines.append(f"  {name:<16}{len(pnls):>9d}{total:>14,.4f}{total / len(pnls):>12,.5f}{wins / len(pnls):>10.1%} ")
    return "\n".join(lines)


def to_json(baseline: Baseline) -> dict[str, Any]:
    closed = [t for t in baseline.trades if not t.is_open]
    rec = reconcile(closed)
    observed = (
        baseline.latest_snapshot_equity - baseline.initial_capital
        if baseline.latest_snapshot_equity is not None
        else None
    )
    return {
        "git_sha": baseline.git_sha,
        "git_dirty": baseline.git_dirty,
        "app_env": baseline.app_env,
        "db_dialect": baseline.db_dialect,
        "db_reachable": baseline.db_reachable,
        "db_error": baseline.db_error,
        "initial_capital": baseline.initial_capital,
        "trades_total": len(baseline.trades),
        "trades_open": len(baseline.trades) - len(closed),
        "trades_closed": len(closed),
        "floating_pnl": None,
        "reconciliation": {
            "in_swarm_pnl": rec.in_swarm_pnl,
            "in_swarm_count": rec.in_swarm_count,
            "out_of_range_pnl": rec.out_of_range_pnl,
            "out_of_range_count": rec.out_of_range_count,
            "legacy_pnl": rec.legacy_pnl,
            "legacy_count": rec.legacy_count,
            "db_total_pnl": rec.db_total_pnl,
            "orphaned_pnl": rec.orphaned_pnl,
            "observed_dashboard_pnl": observed,
            "unexplained": reconcile(closed, observed).unexplained,
        },
        "latest_trade_at": _fmt_dt(baseline.latest_trade_at),
        "latest_snapshot_at": _fmt_dt(baseline.latest_snapshot_at),
    }


async def _amain(as_json: bool) -> int:
    baseline = await load_baseline()
    if as_json:
        print(json.dumps(to_json(baseline), indent=2, default=str))
    else:
        print(render(baseline))
    return 0 if baseline.db_reachable else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    return asyncio.run(_amain(as_json=args.json))


if __name__ == "__main__":
    sys.exit(main())
