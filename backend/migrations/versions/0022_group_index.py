"""Official-settlement group index, membership baselines and natural-day OHLC."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022_group_index"
down_revision = "0021_social_groups"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    group_price = sa.Numeric(78, 36)
    personal_price = sa.Numeric(36, 8)
    op.create_table("group_index_state",
        sa.Column("group_id", uid, sa.ForeignKey("social_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("price", group_price, nullable=False),
        sa.Column("basis_price", group_price, nullable=False),
        sa.Column("previous_close", group_price, nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("formula_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price > 0 AND basis_price > 0 AND previous_close > 0 AND sequence >= 0", name="ck_group_index_state_values"),
    )
    op.create_index("ix_group_index_state_business_date", "group_index_state", ["business_date"])
    op.create_table("group_index_components",
        sa.Column("group_id", uid, sa.ForeignKey("social_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("basis_price", personal_price, nullable=False),
        sa.Column("current_price", personal_price, nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.CheckConstraint("basis_price > 0 AND current_price > 0 AND source_sequence >= 0", name="ck_group_index_component_values"),
    )
    op.create_table("group_index_events",
        sa.Column("id", uid, primary_key=True),
        sa.Column("group_id", uid, sa.ForeignKey("social_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_event_id", uid, sa.ForeignKey("class_index_events.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("price_before", group_price, nullable=False),
        sa.Column("price_after", group_price, nullable=False),
        sa.Column("component_count", sa.Integer(), nullable=False),
        sa.Column("formula_version", sa.String(40), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("group_id", "sequence", name="uq_group_index_event_sequence"),
        sa.UniqueConstraint("group_id", "source_event_id", name="uq_group_index_event_source"),
        sa.CheckConstraint("sequence > 0 AND price_before > 0 AND price_after > 0 AND component_count >= 0", name="ck_group_index_event_values"),
        sa.CheckConstraint("kind IN ('initial', 'settlement', 'membership', 'rebalance')", name="ck_group_index_event_kind"),
    )
    op.create_index("ix_group_index_events_group_id", "group_index_events", ["group_id"])
    op.create_table("group_index_daily",
        sa.Column("group_id", uid, sa.ForeignKey("social_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("business_date", sa.Date(), primary_key=True),
        sa.Column("previous_close", group_price, nullable=False),
        sa.Column("open", group_price, nullable=False),
        sa.Column("high", group_price, nullable=False),
        sa.Column("low", group_price, nullable=False),
        sa.Column("close", group_price, nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("previous_close > 0 AND low > 0 AND low <= open AND low <= close AND high >= open AND high >= close AND event_count >= 0", name="ck_group_index_daily_values"),
    )


def downgrade():
    op.drop_table("group_index_daily")
    op.drop_table("group_index_events")
    op.drop_table("group_index_components")
    op.drop_table("group_index_state")
