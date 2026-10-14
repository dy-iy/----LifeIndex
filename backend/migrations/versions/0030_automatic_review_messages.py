"""Durably publish newly completed reviews without replaying past results."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_automatic_review_messages"
down_revision = "0029_group_review_messages"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table("study_group_review_completions",
        sa.Column("session_id", uid, primary_key=True),
        sa.Column("review_version", sa.Integer(), primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evaluation_id", uid, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("review_version > 0", name="ck_group_review_completions_version"),
    )
    op.create_table("study_group_review_outbox",
        sa.Column("id", uid, primary_key=True),
        sa.Column("evaluation_id", uid, nullable=False),
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", uid, nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        # No group FK: completion owns the User lock and must not acquire group
        # key-share locks against owner transfer. Delivery checks the target.
        sa.Column("group_id", uid, nullable=False),
        sa.Column("member_joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_name", sa.String(60), nullable=False),
        sa.Column("sender_avatar_url", sa.String(256), nullable=True),
        sa.Column("review_summary", postgresql.JSONB(), nullable=False),
        sa.Column("review_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "review_version", "group_id", name="uq_group_review_outbox_completion"),
        sa.CheckConstraint("review_version > 0 AND attempts >= 0", name="ck_group_review_outbox_versions"),
        sa.CheckConstraint("status IN ('pending', 'sent', 'skipped')", name="ck_group_review_outbox_status"),
        sa.CheckConstraint("(status = 'pending') = (finished_at IS NULL)", name="ck_group_review_outbox_finished"),
        sa.CheckConstraint("jsonb_typeof(review_summary) = 'object' AND jsonb_typeof(review_snapshot) = 'object'",
                           name="ck_group_review_outbox_snapshot"),
    )
    op.create_index("ix_group_review_outbox_due", "study_group_review_outbox", ["available_at", "created_at", "id"],
                    postgresql_where=sa.text("status = 'pending'"))
    # Completion hooks create outbox rows from now on; existing successes are
    # intentionally not backfilled into historical group conversations.


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_group_review_outbox) "
                                   "OR EXISTS (SELECT 1 FROM study_group_review_completions)")):
        raise RuntimeError("Review delivery receipts contain data; retain automatic completion history")
    op.drop_table("study_group_review_outbox")
    op.drop_table("study_group_review_completions")
