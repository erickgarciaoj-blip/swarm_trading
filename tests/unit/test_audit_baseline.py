"""
Unit tests for scripts/audit_baseline.py's pure functions.

The script's I/O half (load_baseline) is deliberately untested here — it does
nothing but issue SELECTs and is exercised by running the script. What
matters for correctness is the classification and reconciliation logic, which
is pure and is what turns "the dashboard and the database disagree" into a
number.
"""

import dataclasses
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

# scripts/ is not a package (no __init__.py) and is not on sys.path, so the
# module is loaded by file location rather than by import name.
_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "audit_baseline.py"
_spec = importlib.util.spec_from_file_location("audit_baseline", _SCRIPT)
assert _spec is not None and _spec.loader is not None
audit_baseline = importlib.util.module_from_spec(_spec)
# Registered before exec_module: the script uses `from __future__ import
# annotations`, so @dataclass resolves its field types by looking the module
# up in sys.modules — which fails if it isn't there yet.
sys.modules["audit_baseline"] = audit_baseline
_spec.loader.exec_module(audit_baseline)

IN_SWARM = audit_baseline.IN_SWARM
OUT_OF_RANGE = audit_baseline.OUT_OF_RANGE
LEGACY = audit_baseline.LEGACY
TradeRow = audit_baseline.TradeRow
classify_agent_id = audit_baseline.classify_agent_id
reconcile = audit_baseline.reconcile


def _trade(agent_id: str, pnl: float, *, closed: bool = True, symbol: str = "XAUUSD"):
    """Returns an audit_baseline.TradeRow. Deliberately unannotated: the class
    is loaded at runtime from a file path, so it is a module-level *variable*
    here, and mypy rejects a variable used as a type annotation."""
    return TradeRow(
        agent_id=agent_id,
        symbol=symbol,
        pnl=pnl,
        status="FILLED",
        opened_at=datetime(2026, 8, 1, 12, 0, 0),
        closed_at=datetime(2026, 8, 1, 12, 30, 0) if closed else None,
    )


# ─── agent id classification ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_id",
    [
        "SCALPER_XAUUSD_0",
        "SCALPER_OIL_5",
        "SWING_NAS100_3",
        "NEWS_REACTIVE_US100_0",
        "NEWS_REACTIVE_PLTR_3",
        "HEDGER_XAUUSD_1",
        "RL_OIL_1",
    ],
)
def test_ids_within_current_composition_are_in_swarm(agent_id):
    assert classify_agent_id(agent_id) == IN_SWARM


@pytest.mark.parametrize(
    "agent_id",
    [
        "SCALPER_XAUUSD_6",  # composition builds indices 0..5
        "SWING_OIL_9",
        "NEWS_REACTIVE_PLTR_4",  # builds 0..3
        "HEDGER_NAS100_2",  # builds 0..1
        "RL_US100_2",
        # A UUID hex fragment that happens to be all digits. Format-only
        # classification would call this restorable; it is not.
        "SCALPER_OIL_32899139",
        "SWING_XAUUSD_70280050",
    ],
)
def test_stable_format_with_index_beyond_composition_is_out_of_range(agent_id):
    assert classify_agent_id(agent_id) == OUT_OF_RANGE


@pytest.mark.parametrize(
    "agent_id",
    [
        "SCALPER_OIL_841a357a",  # real random-UUID-era id
        "SCALPER_OIL_df7331fc",
        "SWING_XAUUSD_ab12cd34",
        "scalper_XAUUSD_0",  # lowercase: not the factory's format
        "SCALPER_BTCUSD_0",  # symbol outside the Symbol enum
        "UNKNOWN_THING_0",
        "",
    ],
)
def test_unrecognized_ids_are_legacy(agent_id):
    assert classify_agent_id(agent_id) == LEGACY


def test_news_reactive_prefix_is_not_shadowed_by_a_shorter_type():
    """NEWS_REACTIVE contains an underscore; a naive alternation would let a
    shorter prefix match first and mis-parse the symbol/index."""
    assert classify_agent_id("NEWS_REACTIVE_XAUUSD_0") == IN_SWARM
    assert audit_baseline.agent_type_of("NEWS_REACTIVE_XAUUSD_0") == "NEWS_REACTIVE"


def test_agent_type_of_falls_back_to_unknown():
    assert audit_baseline.agent_type_of("SCALPER_OIL_841a357a") == "SCALPER"
    assert audit_baseline.agent_type_of("garbage") == "UNKNOWN"


def test_symbol_of_falls_back_to_the_trade_row_symbol():
    """Legacy ids can't be parsed, so the trade's own symbol column is used."""
    assert audit_baseline.symbol_of("SCALPER_OIL_0", "IGNORED") == "OIL"
    assert audit_baseline.symbol_of("SCALPER_OIL_841a357a", "OIL") == "OIL"


# ─── reconciliation ───────────────────────────────────────────────────────


def test_reconcile_splits_restorable_from_orphaned_pnl():
    trades = [
        _trade("SCALPER_XAUUSD_0", 10.0),
        _trade("SWING_OIL_1", 6.0805),
        _trade("SCALPER_OIL_32899139", 0.2302),  # stable format, index gone
        _trade("SCALPER_OIL_841a357a", -13.6686),  # legacy
    ]

    result = reconcile(trades)

    assert result.in_swarm_count == 2
    assert result.in_swarm_pnl == pytest.approx(16.0805)
    assert result.out_of_range_count == 1
    assert result.out_of_range_pnl == pytest.approx(0.2302)
    assert result.legacy_count == 1
    assert result.legacy_pnl == pytest.approx(-13.6686)
    assert result.db_total_pnl == pytest.approx(2.6421)
    assert result.orphaned_pnl == pytest.approx(-13.4384)
    assert result.orphaned_count == 2


def test_unexplained_is_zero_when_dashboard_matches_restorable_pnl():
    """The reconciliation model in one assertion: the dashboard reports
    exactly the PnL of trades whose agent_id the current swarm rebuilds."""
    trades = [
        _trade("SCALPER_XAUUSD_0", 16.0805),
        _trade("SCALPER_OIL_841a357a", -13.6686),
    ]

    result = reconcile(trades, observed_dashboard_pnl=16.0805)

    assert result.unexplained == pytest.approx(0.0)


def test_unexplained_surfaces_a_gap_rather_than_absorbing_it():
    trades = [_trade("SCALPER_XAUUSD_0", 10.0)]

    result = reconcile(trades, observed_dashboard_pnl=42.0)

    assert result.unexplained == pytest.approx(32.0)


def test_unexplained_is_none_without_an_observed_figure():
    assert reconcile([_trade("SCALPER_XAUUSD_0", 1.0)]).unexplained is None


def test_reconcile_of_no_trades_is_all_zeros():
    result = reconcile([])
    assert result.db_total_pnl == 0.0
    assert result.orphaned_pnl == 0.0
    assert result.in_swarm_count == 0


# ─── TradeRow ─────────────────────────────────────────────────────────────


def test_open_trade_is_detected_by_null_closed_at():
    assert _trade("SCALPER_XAUUSD_0", 0.0, closed=False).is_open is True
    assert _trade("SCALPER_XAUUSD_0", 0.0, closed=True).is_open is False


# ─── rendering ────────────────────────────────────────────────────────────


def test_render_reports_na_instead_of_inventing_floating_pnl():
    """Floating PnL is not persisted anywhere, so the baseline must not
    produce a number for it."""
    baseline = audit_baseline.Baseline(
        git_sha="abc123",
        git_dirty=False,
        app_env="development",
        db_dialect="sqlite",
        db_reachable=True,
        initial_capital=100_000.0,
        trades=[_trade("SCALPER_XAUUSD_0", 1.0)],
    )

    output = audit_baseline.render(baseline)

    assert "Floating PnL:" in output
    assert "N/A" in output
    assert "Total equity:" in output


def test_render_lists_strategies_with_zero_trades():
    """An agent type that has never traded is exactly what this baseline
    exists to surface — it must appear as a row, not be omitted."""
    baseline = audit_baseline.Baseline(
        git_sha="abc123",
        git_dirty=False,
        app_env="development",
        db_dialect="sqlite",
        db_reachable=True,
        initial_capital=100_000.0,
        trades=[_trade("SCALPER_XAUUSD_0", 1.0)],
    )

    output = audit_baseline.render(baseline)

    assert "NEWS_REACTIVE" in output
    assert "HEDGER" in output


def test_render_does_not_leak_the_database_url():
    baseline = audit_baseline.Baseline(
        git_sha="abc123",
        git_dirty=False,
        app_env="paper",
        db_dialect="postgresql",
        db_reachable=False,
        db_error="ConnectionError",
        initial_capital=100_000.0,
    )

    output = audit_baseline.render(baseline)

    assert "postgresql://" not in output
    assert "@" not in output
    assert "ConnectionError" in output


def test_to_json_marks_floating_pnl_as_null():
    baseline = audit_baseline.Baseline(
        git_sha="abc123",
        git_dirty=False,
        app_env="development",
        db_dialect="sqlite",
        db_reachable=True,
        initial_capital=100_000.0,
        trades=[_trade("SCALPER_XAUUSD_0", 1.0), _trade("SCALPER_XAUUSD_1", 2.0, closed=False)],
    )

    payload = audit_baseline.to_json(baseline)

    assert payload["floating_pnl"] is None
    assert payload["trades_total"] == 2
    assert payload["trades_open"] == 1
    assert payload["trades_closed"] == 1
    assert payload["reconciliation"]["in_swarm_pnl"] == pytest.approx(1.0)


# ─── read-only guarantees ─────────────────────────────────────────────────
# The script is meant to be safe to run against the live VPS database while
# the swarm is trading. These prove it two ways: by intercepting every
# statement it actually issues, and by scanning its source so a future edit
# can't quietly introduce a write.


class _FakeResult:
    """Supports both `for row in result` and `result.first()`, the two shapes
    load_baseline() consumes."""

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _RecordingConnection:
    def __init__(self, statements):
        self._statements = statements

    async def execute(self, statement):
        sql = str(statement)
        self._statements.append(sql)
        if "FROM trades" in sql:
            return _FakeResult([("SCALPER_XAUUSD_0", "XAUUSD", 1.5, "FILLED", None, None)])
        if "swarm_snapshots" in sql:
            return _FakeResult([(None, 100_001.5, 1)])
        if "FROM agents" in sql:
            return _FakeResult([("UNKNOWN", 3)])
        return _FakeResult([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingEngine:
    """Records every statement and fails loudly on any transactional or
    schema-level entry point — begin(), run_sync(), execute() on the engine
    itself — which is how DDL and writes would have to be issued."""

    def __init__(self, statements):
        self._statements = statements
        self.dialect = type("Dialect", (), {"name": "postgresql"})()
        self.disposed = False

    def connect(self):
        return _RecordingConnection(self._statements)

    def begin(self):
        raise AssertionError("audit_baseline must not open a write transaction")

    async def run_sync(self, *args, **kwargs):
        raise AssertionError("audit_baseline must not run schema operations")

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def recorded_statements(monkeypatch):
    statements: list[str] = []
    engine = _RecordingEngine(statements)
    monkeypatch.setattr(audit_baseline, "create_async_engine", lambda *a, **kw: engine)
    # Postgres-shaped URL so the SQLite file guard is not what stops it.
    monkeypatch.setattr(
        audit_baseline.settings, "database_url", "postgresql+asyncpg://u:p@localhost:5432/db", raising=False
    )
    return statements, engine


@pytest.mark.asyncio
async def test_baseline_issues_only_select_statements(recorded_statements):
    statements, _ = recorded_statements

    await audit_baseline.load_baseline()

    assert statements, "expected the baseline to query something"
    for sql in statements:
        assert sql.strip().upper().startswith("SELECT"), f"non-SELECT statement issued: {sql}"


@pytest.mark.asyncio
async def test_baseline_issues_no_write_or_ddl_keywords(recorded_statements):
    statements, _ = recorded_statements

    await audit_baseline.load_baseline()

    forbidden = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE", "COMMIT", "GRANT", "REPLACE")
    joined = " ".join(statements).upper()
    for keyword in forbidden:
        assert keyword not in joined, f"{keyword} appeared in an issued statement"


@pytest.mark.asyncio
async def test_baseline_disposes_the_engine(recorded_statements):
    _, engine = recorded_statements

    await audit_baseline.load_baseline()

    assert engine.disposed is True


@pytest.mark.asyncio
async def test_baseline_never_opens_a_write_transaction(recorded_statements):
    """_RecordingEngine.begin()/run_sync() raise AssertionError, so this
    passing means neither was reached."""
    await audit_baseline.load_baseline()  # must not raise


def test_source_contains_no_write_or_ddl_sql():
    """Static guard for future edits: the script's own source must not
    contain write or schema SQL, nor the schema-creation helpers."""
    source = _SCRIPT.read_text().upper()
    # Split off the module docstring, which legitimately names these words
    # while explaining that the script does not do them.
    body = source.split('"""', 2)[-1]
    for keyword in (
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "CREATE TABLE",
        "DROP ",
        "ALTER ",
        "TRUNCATE",
        "CREATE_ALL",
        "DROP_ALL",
        ".COMMIT(",
        "ALEMBIC",
        "UPGRADE HEAD",
    ):
        assert keyword not in body, f"forbidden SQL/DDL construct in audit_baseline.py: {keyword}"


def test_source_does_not_build_swarm_or_connect_brokers():
    """Running the baseline must not start the system it is measuring."""
    source = _SCRIPT.read_text()
    # NOTE: engine.connect() is expected and correct — it is the read-only
    # SELECT connection. What must be absent are the constructors that would
    # start the trading system or dial a broker.
    for construct in (
        "build_swarm(",
        "IBKRBroker(",
        "MT5Broker(",
        "SwarmOrchestrator(",
        "MarketFeed(",
        "NewsFeed(",
        "broker.connect(",
        "mt5.initialize(",
    ):
        assert construct not in source, f"audit_baseline.py must not use {construct}"


def test_sqlite_guard_prevents_creating_a_database_file(tmp_path):
    """SQLite creates an empty database file on connect. Pointing the script
    at a path that does not exist must be reported, not materialized."""
    missing = tmp_path / "does_not_exist.db"

    assert audit_baseline.missing_sqlite_file(f"sqlite+aiosqlite:///{missing}") == str(missing)
    assert not missing.exists()

    existing = tmp_path / "real.db"
    existing.write_bytes(b"")
    assert audit_baseline.missing_sqlite_file(f"sqlite+aiosqlite:///{existing}") is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://user:pw@host:5432/db",
        "sqlite+aiosqlite:///:memory:",
    ],
)
def test_sqlite_guard_ignores_non_file_backed_urls(url):
    assert audit_baseline.missing_sqlite_file(url) is None


@pytest.mark.asyncio
async def test_missing_sqlite_file_is_reported_as_unreachable(monkeypatch, tmp_path):
    """Not "reachable with 0 trades" — a database this script just invented
    would be a worse answer than "not there"."""
    monkeypatch.setattr(
        audit_baseline.settings,
        "database_url",
        f"sqlite+aiosqlite:///{tmp_path / 'nope.db'}",
        raising=False,
    )

    baseline = await audit_baseline.load_baseline()

    assert baseline.db_reachable is False
    assert baseline.db_error == "SQLiteFileNotFound"
    assert not (tmp_path / "nope.db").exists()


# ─── secret hygiene ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_driver_error_never_leaks_the_dsn(monkeypatch):
    """Driver exceptions routinely embed the full DSN including the
    password. Only the exception TYPE may survive into the report."""
    secret_dsn = "postgresql+asyncpg://swarm:sup3r-s3cret@10.0.0.5:5432/swarm_prod"

    class _ExplodingEngine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            raise RuntimeError(f"could not connect to {secret_dsn}: password authentication failed")

        async def dispose(self):
            pass

    monkeypatch.setattr(audit_baseline, "create_async_engine", lambda *a, **kw: _ExplodingEngine())
    monkeypatch.setattr(audit_baseline.settings, "database_url", secret_dsn, raising=False)

    baseline = await audit_baseline.load_baseline()
    output = audit_baseline.render(baseline)
    payload = json.dumps(audit_baseline.to_json(baseline))

    assert baseline.db_error == "RuntimeError"
    for leak in (secret_dsn, "sup3r-s3cret", "10.0.0.5", "swarm_prod", "password authentication"):
        assert leak not in output, f"{leak!r} leaked into rendered output"
        assert leak not in payload, f"{leak!r} leaked into JSON output"


@pytest.mark.asyncio
async def test_successful_run_does_not_print_the_database_url(recorded_statements, monkeypatch):
    secret_dsn = "postgresql+asyncpg://dbuser:another-s3cret@10.0.0.9:5432/swarm_prod_db"
    monkeypatch.setattr(audit_baseline.settings, "database_url", secret_dsn, raising=False)

    baseline = await audit_baseline.load_baseline()
    output = audit_baseline.render(baseline)
    payload = json.dumps(audit_baseline.to_json(baseline))

    # Only genuinely-secret tokens are asserted on. Short generic substrings
    # (e.g. "swarm:") collide with the report's own labels — "in current
    # swarm:" is a heading, not a leaked credential.
    for leak in (secret_dsn, "another-s3cret", "10.0.0.9", "dbuser", "swarm_prod_db", "asyncpg"):
        assert leak not in output, f"{leak!r} leaked into rendered output"
        assert leak not in payload, f"{leak!r} leaked into JSON output"


def test_baseline_dataclass_has_no_field_holding_the_url():
    """Structural guarantee: the URL is never carried into the reportable
    object in the first place, so it cannot be printed by accident."""
    field_names = {f.name for f in dataclasses.fields(audit_baseline.Baseline)}

    assert not any("url" in name or "dsn" in name or "password" in name for name in field_names)
