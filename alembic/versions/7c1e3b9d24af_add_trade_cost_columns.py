"""add trade cost columns (Fase 1A — paper trading cost model)

Adds the gross/cost/net decomposition and effective fill prices to `trades`
so a closed trade can be audited after the fact: what it earned before
costs, what each leg cost, and what was actually transacted versus the
reference price the strategy acted on.

`trades.pnl` keeps its column and its name but its meaning is tightened: it
is now NET of every transaction cost. Existing rows need no backfill — they
were written by a cost-free simulator, so their gross equals their net, and
the new columns default to 0.0. That makes the invariant
`pnl == gross_pnl - entry_costs - exit_costs` hold on historical rows too,
with one caveat: for those rows `gross_pnl` reads 0.0 rather than repeating
`pnl`, so aggregate gross figures are only meaningful from this migration
forward. Deliberately not backfilled — copying `pnl` into `gross_pnl` would
assert that pre-cost trades were measured against a cost model that did not
exist, which is exactly the kind of retroactive fiction this phase is
meant to remove.

Revision ID: 7c1e3b9d24af
Revises: 41cde2ea07b5
Create Date: 2026-08-21 09:14:22.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e3b9d24af"
down_revision: str | Sequence[str] | None = "41cde2ea07b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (column, type, nullable, server_default)
#
# server_default on the non-nullable columns is what lets this run against a
# populated `trades` table without a separate backfill step: existing rows
# get 0.0 as the statement executes. exit_price/exit_fill_price stay
# nullable because an OPEN trade genuinely has no exit yet — 0.0 would be a
# lie that reads as "exited at zero".
_COST_COLUMNS = (
    ("gross_pnl", sa.Float(), False, "0"),
    ("entry_costs", sa.Float(), False, "0"),
    ("exit_costs", sa.Float(), False, "0"),
    ("commission", sa.Float(), False, "0"),
    ("entry_fill_price", sa.Float(), False, "0"),
    ("exit_price", sa.Float(), True, None),
    ("exit_fill_price", sa.Float(), True, None),
)


def upgrade() -> None:
    """Upgrade schema."""
    # batch_alter_table: SQLite cannot ALTER TABLE ADD COLUMN with every
    # constraint form, so Alembic rebuilds the table there. On PostgreSQL
    # (the production target, see ADR-0008) this compiles to plain
    # ADD COLUMN statements.
    with op.batch_alter_table("trades") as batch_op:
        for name, type_, nullable, server_default in _COST_COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=nullable, server_default=server_default))


def downgrade() -> None:
    """Downgrade schema.

    Dropping these columns discards the cost breakdown permanently — the
    `pnl` figures themselves survive, but which portion of each was spread,
    slippage or commission does not.
    """
    with op.batch_alter_table("trades") as batch_op:
        for name, _type, _nullable, _default in reversed(_COST_COLUMNS):
            batch_op.drop_column(name)
