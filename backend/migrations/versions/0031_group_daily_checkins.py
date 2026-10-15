"""Persist one self check-in per active group member and Shanghai day."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031_group_daily_checkins"
down_revision = "0030_automatic_review_messages"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table("study_group_checkins",
        sa.Column("group_id", uid, sa.ForeignKey("study_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("local_date", sa.Date(), primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("local_date = (checked_at AT TIME ZONE 'Asia/Shanghai')::date",
                           name="ck_study_group_checkins_local_date"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_group_checkins)")):
        raise RuntimeError("Daily check-in receipts contain data; retain member check-in history")
    op.drop_table("study_group_checkins")
