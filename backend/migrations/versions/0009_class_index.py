"""Deterministic class index account and immutable settlement revision ledger."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_class_index"
down_revision = "0008_evaluations"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table("class_index_accounts",
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("price", sa.Numeric(30, 2), nullable=False),
        sa.Column("previous_close", sa.Numeric(30, 2), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("price >= 1 AND previous_close >= 1 AND sequence >= 0", name="ck_class_index_account_values"),
    )
    columns = [
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", uid, nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lesson_name", sa.String(100), nullable=False),
        sa.Column("lesson_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lesson_ends_at", sa.DateTime(timezone=True), nullable=False),
    ]
    for name in ("price_before", "price_after", "previous_close", "actual_points", "clipped_points", "target_points", "previous_target_points", "original_price_before", "original_previous_close", "cumulative_actual_points", "cumulative_discarded_points"):
        columns.append(sa.Column(name, sa.Numeric(30, 2), nullable=False))
    for name in ("raw_points", "rounding_points"):
        columns.append(sa.Column(name, sa.Numeric(38, 12), nullable=False))
    columns.extend([
        sa.Column("raw_rate", sa.Numeric(24, 18), nullable=False),
        sa.Column("quality", sa.Numeric(8, 4), nullable=True),
        sa.Column("duration_ratio", sa.Numeric(24, 18), nullable=True),
        sa.Column("participation_score", sa.Numeric(24, 18), nullable=True),
        sa.Column("effective_seconds", sa.Integer(), nullable=False),
        sa.Column("planned_seconds", sa.Integer(), nullable=False),
        sa.Column("makeup", sa.Boolean(), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("rule_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("original_event_id", uid, nullable=True),
        sa.Column("previous_event_id", uid, nullable=True),
        sa.UniqueConstraint("user_id", "id", name="uq_class_index_event_owner_id"),
        sa.UniqueConstraint("user_id", "sequence", name="uq_class_index_event_sequence"),
        sa.UniqueConstraint("user_id", "source_key", name="uq_class_index_event_source"),
        sa.UniqueConstraint("user_id", "lesson_id", "revision", name="uq_class_index_event_revision"),
        sa.UniqueConstraint("previous_event_id", name="uq_class_index_event_successor"),
        sa.ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], name="fk_class_index_event_lesson", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id", "original_event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_index_event_original"),
        sa.ForeignKeyConstraint(["user_id", "previous_event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_index_event_previous"),
        sa.CheckConstraint("kind IN ('attendance', 'absence', 'excluded')", name="ck_class_index_event_kind"),
        sa.CheckConstraint("revision >= 0 AND sequence > 0 AND price_before >= 1 AND price_after >= 1", name="ck_class_index_event_values"),
        sa.CheckConstraint("(revision = 0 AND original_event_id IS NULL AND previous_event_id IS NULL) OR (revision > 0 AND original_event_id IS NOT NULL AND previous_event_id IS NOT NULL)", name="ck_class_index_event_links"),
    ])
    op.create_table("class_index_events", *columns)
    op.create_index("ix_class_index_event_owner_date", "class_index_events", ["user_id", "business_date", "sequence"])
    op.create_table("class_index_receipts",
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_key", sa.String(200), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("event_id", uid, nullable=True),
        sa.ForeignKeyConstraint(["user_id", "event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_index_receipt_event", ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("class_index_receipts")
    op.drop_table("class_index_events")
    op.drop_table("class_index_accounts")
