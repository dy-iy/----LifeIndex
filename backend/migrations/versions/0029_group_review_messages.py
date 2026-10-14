"""Freeze completed review cards and group-scoped read-only result snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029_group_review_messages"
down_revision = "0028_group_messages"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("study_group_messages", sa.Column("kind", sa.String(16), server_default="text", nullable=False))
    op.add_column("study_group_messages", sa.Column("review_summary", postgresql.JSONB(), nullable=True))
    op.add_column("study_group_messages", sa.Column("review_snapshot", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("ck_study_group_messages_kind", "study_group_messages", "kind IN ('text', 'review')")
    op.create_check_constraint("ck_study_group_messages_review", "study_group_messages",
        "(kind = 'text' AND review_summary IS NULL AND review_snapshot IS NULL) OR "
        "(kind = 'review' AND review_summary IS NOT NULL AND review_snapshot IS NOT NULL "
        "AND jsonb_typeof(review_summary) = 'object' AND jsonb_typeof(review_snapshot) = 'object')")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_group_messages WHERE kind = 'review')")):
        raise RuntimeError("Shared reviews contain data; retain them instead of discarding completed review cards")
    op.drop_constraint("ck_study_group_messages_review", "study_group_messages", type_="check")
    op.drop_constraint("ck_study_group_messages_kind", "study_group_messages", type_="check")
    op.drop_column("study_group_messages", "review_snapshot")
    op.drop_column("study_group_messages", "review_summary")
    op.drop_column("study_group_messages", "kind")
