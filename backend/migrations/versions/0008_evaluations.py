"""Versioned evaluations, leased jobs and conservative model budgets."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0008_evaluations"
down_revision = "0007_lesson_outcomes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("evaluation_jobs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("input_snapshot", pg.JSONB(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(80), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("clarification", sa.String(2000), nullable=True),
        sa.Column("assessment", pg.JSONB(), nullable=True),
        sa.Column("settlement", pg.JSONB(), nullable=True),
        sa.Column("error", sa.String(1000), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "id", name="uq_evaluation_jobs_owner"),
        sa.UniqueConstraint("session_id", "review_version", "session_version", name="uq_evaluation_jobs_input"),
        sa.ForeignKeyConstraint(["user_id", "session_id"], ["study_sessions.user_id", "study_sessions.id"], ondelete="CASCADE", name="fk_evaluation_jobs_session"),
        sa.CheckConstraint("review_version > 0 AND session_version > 0 AND version > 0 AND attempts >= 0", name="ck_evaluation_jobs_versions"),
        sa.CheckConstraint("status IN ('queued','running','needs_input','hold','failed','succeeded','superseded')", name="ck_evaluation_jobs_status"),
    )
    op.create_index("ix_evaluation_jobs_queue", "evaluation_jobs", ["status", "lease_until", "created_at"])
    op.create_table("evaluation_daily_usage",
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("usage_date", sa.Date(), primary_key=True),
        sa.Column("reserved_micro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("charged_micro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("reserved_micro >= 0 AND charged_micro >= 0 AND request_count >= 0", name="ck_evaluation_daily_usage_nonnegative"),
    )
    op.create_table("evaluation_calls",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("reserved_micro", sa.Integer(), nullable=False),
        sa.Column("charged_micro", sa.Integer(), nullable=True),
        sa.Column("input_rate", sa.Integer(), nullable=False),
        sa.Column("output_rate", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("response", pg.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id", "job_id"], ["evaluation_jobs.user_id", "evaluation_jobs.id"], ondelete="CASCADE", name="fk_evaluation_calls_job"),
        sa.CheckConstraint("status IN ('reserved','completed','unknown')", name="ck_evaluation_calls_status"),
    )
    op.create_index("ix_evaluation_calls_input", "evaluation_calls", ["job_id", "input_hash", "status"])


def downgrade():
    op.drop_table("evaluation_calls")
    op.drop_table("evaluation_daily_usage")
    op.drop_table("evaluation_jobs")
