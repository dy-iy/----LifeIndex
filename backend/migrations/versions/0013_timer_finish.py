"""Bounded timer completion and recoverable offline stop intents."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0013_timer_finish"
down_revision = "0012_agent_workflows"
branch_labels = depends_on = None


def upgrade():
    op.add_column("study_sessions", sa.Column("finish_reason", sa.String(12), nullable=True))
    op.add_column("study_sessions", sa.Column("finish_context", pg.JSONB(), nullable=True))
    op.create_check_constraint("ck_study_sessions_finish_reason", "study_sessions",
                               "finish_reason IS NULL OR finish_reason IN ('user', 'timeout', 'offline', 'repair')")
    op.create_index("ix_study_sessions_expiry", "study_sessions", ["ends_at"],
                    postgresql_where=sa.text("phase IN ('running', 'paused')"))


def downgrade():
    op.drop_index("ix_study_sessions_expiry", table_name="study_sessions")
    op.drop_constraint("ck_study_sessions_finish_reason", "study_sessions", type_="check")
    op.drop_column("study_sessions", "finish_context")
    op.drop_column("study_sessions", "finish_reason")
