"""Missed timer confirmations and explicit self-reported duration evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_lesson_outcomes"
down_revision = "0006_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("study_sessions", sa.Column("reported_seconds", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_study_sessions_reported", "study_sessions",
        "reported_seconds IS NULL OR (reported_seconds >= 0 AND reported_seconds <= EXTRACT(EPOCH FROM (ends_at - starts_at)) AND phase = 'finished' AND is_makeup)")
    op.create_table("lesson_outcomes",
        sa.Column("lesson_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(1000), nullable=False, server_default=""),
        sa.Column("note", sa.String(1000), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], ondelete="CASCADE", name="fk_lesson_outcomes_owner"),
        sa.CheckConstraint("decision IN ('pending', 'makeup', 'absent', 'leave', 'cancelled')", name="ck_lesson_outcomes_decision"),
        sa.CheckConstraint("version > 0", name="ck_lesson_outcomes_version"),
        sa.CheckConstraint("(decision = 'pending') = (confirmed_at IS NULL)", name="ck_lesson_outcomes_confirmed"),
    )
    op.create_index("ix_lesson_outcomes_owner", "lesson_outcomes", ["user_id", "decision"])


def downgrade():
    op.drop_table("lesson_outcomes")
    op.drop_constraint("ck_study_sessions_reported", "study_sessions", type_="check")
    op.drop_column("study_sessions", "reported_seconds")
