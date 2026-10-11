"""Persist timer snapshots and server-authored activity segments."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_study_sessions"
down_revision = "0004_schedule_agent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "study_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("location", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.String(12), nullable=False, server_default="running"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_makeup", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("user_id", "id", name="uq_study_sessions_owner"),
        sa.UniqueConstraint("lesson_id", name="uq_study_sessions_lesson"),
        sa.ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], ondelete="CASCADE", name="fk_study_sessions_lesson"),
        sa.CheckConstraint("phase IN ('running', 'paused', 'finished')", name="ck_study_sessions_phase"),
        sa.CheckConstraint("version > 0", name="ck_study_sessions_version"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_study_sessions_plan"),
        sa.CheckConstraint("(phase = 'finished') = (finished_at IS NOT NULL)", name="ck_study_sessions_finished"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="ck_study_sessions_times"),
    )
    op.create_index("uq_study_sessions_active", "study_sessions", ["user_id"], unique=True,
                    postgresql_where=sa.text("phase IN ('running', 'paused')"))
    op.create_index("ix_study_sessions_recent", "study_sessions", ["user_id", "finished_at"])
    op.create_table(
        "study_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id", "session_id"], ["study_sessions.user_id", "study_sessions.id"], ondelete="CASCADE", name="fk_study_segments_session"),
        sa.CheckConstraint("end_at IS NULL OR end_at >= start_at", name="ck_study_segments_times"),
    )
    op.create_index("ix_study_segments_session", "study_segments", ["session_id", "start_at"])
    op.create_index("uq_study_segments_open", "study_segments", ["session_id"], unique=True,
                    postgresql_where=sa.text("end_at IS NULL"))


def downgrade():
    op.drop_table("study_segments")
    op.drop_table("study_sessions")
