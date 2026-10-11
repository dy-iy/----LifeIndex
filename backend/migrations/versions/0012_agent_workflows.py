"""Version agent workflows, retrieval snapshots, scoped memory and tool traces."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0012_agent_workflows"
down_revision = "0011_timetable_deletion"
branch_labels = depends_on = None


def upgrade():
    op.add_column("schedule_model_calls", sa.Column("input_hash", sa.String(64), nullable=True))
    op.add_column("schedule_model_calls", sa.Column("response", pg.JSONB(), nullable=True))
    op.add_column("evaluation_calls", sa.Column("stage", sa.String(24), nullable=False, server_default="legacy"))
    op.add_column("evaluation_jobs", sa.Column("workflow_version", sa.String(40), nullable=False, server_default="legacy-v1"))
    op.add_column("evaluation_jobs", sa.Column("agent_context", pg.JSONB(), nullable=True))
    op.add_column("evaluation_jobs", sa.Column("quality_result", pg.JSONB(), nullable=True))
    op.create_table("agent_memories",
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("domain", sa.String(24), primary_key=True),
        sa.Column("source_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(80), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("content", pg.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_agent_memories_scope", "agent_memories", ["user_id", "domain", "scope", "updated_at"])
    op.create_table("agent_traces",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow", sa.String(40), nullable=False),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("metadata_json", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_agent_traces_run", "agent_traces", ["user_id", "run_id", "created_at"])


def downgrade():
    op.drop_table("agent_traces")
    op.drop_table("agent_memories")
    for name in ("quality_result", "agent_context", "workflow_version"):
        op.drop_column("evaluation_jobs", name)
    op.drop_column("evaluation_calls", "stage")
    op.drop_column("schedule_model_calls", "response")
    op.drop_column("schedule_model_calls", "input_hash")
