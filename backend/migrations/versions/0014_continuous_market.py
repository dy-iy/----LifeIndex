"""Persistent continuous quotes and eight-place class-v2.0 prices."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0014_continuous_market"
down_revision = "0013_timer_finish"
branch_labels = depends_on = None

EVENT_PRICES = ("price_before", "price_after", "clipped_points", "target_points",
                "previous_target_points", "original_price_before", "cumulative_discarded_points")


def owner():
    return sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


def upgrade():
    op.alter_column("class_index_accounts", "price", type_=sa.Numeric(36, 8), existing_type=sa.Numeric(30, 2))
    for column in EVENT_PRICES:
        op.alter_column("class_index_events", column, type_=sa.Numeric(36, 8), existing_type=sa.Numeric(30, 2))
    op.create_table("class_market_states", owner(),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("seed", sa.String(64), nullable=False),
        sa.Column("mood", sa.Numeric(40, 24), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("rule_snapshot", pg.JSONB(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("session_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("running", sa.Boolean(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_class_market_states_cursor_at", "class_market_states", ["cursor_at"])
    op.create_table("class_market_actions", owner(),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("user_id", "source_key", name="uq_class_market_action_source"))
    op.create_table("class_market_points", owner(),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(36, 8), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("event_id", pg.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id", "event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_market_point_event"),
        sa.CheckConstraint("price >= 1", name="ck_class_market_point_price"))
    op.create_index("ix_class_market_point_day", "class_market_points", ["user_id", "business_date", "sequence"])
    op.create_table("class_market_days", owner(),
        sa.Column("business_date", sa.Date(), primary_key=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("previous_close", sa.Numeric(30, 2), nullable=False),
        *[sa.Column(name, sa.Numeric(36, 8), nullable=False) for name in ("open", "high", "low", "close")],
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False))


def downgrade():
    # Rounding active v2 ledgers back to cents would silently destroy evidence.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM class_market_states)")):
        raise RuntimeError("Cannot downgrade activated continuous markets; restore a pre-upgrade backup instead")
    for table in ("class_market_days", "class_market_points", "class_market_actions", "class_market_states"):
        op.drop_table(table)
    for column in EVENT_PRICES:
        op.alter_column("class_index_events", column, type_=sa.Numeric(30, 2), existing_type=sa.Numeric(36, 8))
    op.alter_column("class_index_accounts", "price", type_=sa.Numeric(30, 2), existing_type=sa.Numeric(36, 8))
