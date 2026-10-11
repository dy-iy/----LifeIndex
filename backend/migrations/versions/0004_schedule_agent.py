"""Persistent timetable conversations, durable jobs, private images and AI budget."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0004_schedule_agent"
down_revision = "0003_timetables"
branch_labels = None
depends_on = None


def identity():
    return [sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)]


def timestamps():
    return [sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
            for name in ("created_at", "updated_at")]


def upgrade():
    op.create_table("schedule_threads", *identity(),
        sa.Column("semester_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"), *timestamps(),
        sa.UniqueConstraint("user_id", "id", name="uq_schedule_threads_owner"),
        sa.ForeignKeyConstraint(["user_id", "semester_id"], ["semesters.user_id", "semesters.id"], name="fk_schedule_thread_semester"),
        sa.CheckConstraint("version > 0", name="ck_schedule_thread_version"))
    op.create_index("ix_schedule_threads_owner_updated", "schedule_threads", ["user_id", "updated_at"])
    op.create_table("schedule_runs", *identity(),
        sa.Column("thread_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("input_text", sa.String(4000), nullable=False),
        sa.Column("image_ids", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reply", sa.String(12000), nullable=False, server_default=""),
        sa.Column("plan", pg.JSONB(), nullable=True),
        sa.Column("preview", pg.JSONB(), nullable=True),
        sa.Column("result", pg.JSONB(), nullable=True),
        sa.Column("error", sa.String(1000), nullable=False, server_default=""),
        sa.Column("decision", sa.String(10), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True), *timestamps(),
        sa.ForeignKeyConstraint(["user_id", "thread_id"], ["schedule_threads.user_id", "schedule_threads.id"], ondelete="CASCADE", name="fk_schedule_run_thread"),
        sa.UniqueConstraint("user_id", "id", name="uq_schedule_runs_owner"),
        sa.CheckConstraint("attempts >= 0", name="ck_schedule_run_attempts"))
    op.create_index("ix_schedule_runs_queue", "schedule_runs", ["status", "lease_until", "created_at"])
    op.create_index("ix_schedule_runs_thread", "schedule_runs", ["thread_id", "created_at"])
    op.create_table("schedule_images", *identity(),
        sa.Column("thread_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("mime_type", sa.String(30), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id", "thread_id"], ["schedule_threads.user_id", "schedule_threads.id"], ondelete="CASCADE", name="fk_schedule_image_thread"))
    op.create_index("ix_schedule_images_expiry", "schedule_images", ["expires_at"])
    op.create_table("schedule_daily_usage",
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("usage_date", sa.Date(), primary_key=True),
        sa.Column("reserved_micro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("charged_micro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("reserved_micro >= 0 AND charged_micro >= 0 AND request_count >= 0", name="ck_schedule_daily_usage_nonnegative"))
    op.create_table("schedule_model_calls", *identity(),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reserved_micro", sa.Integer(), nullable=False),
        sa.Column("charged_micro", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("input_rate", sa.Integer(), nullable=False),
        sa.Column("output_rate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id", "run_id"], ["schedule_runs.user_id", "schedule_runs.id"], ondelete="CASCADE", name="fk_schedule_call_run"))


def downgrade():
    for table in ("schedule_model_calls", "schedule_daily_usage", "schedule_images", "schedule_runs", "schedule_threads"):
        op.drop_table(table)
