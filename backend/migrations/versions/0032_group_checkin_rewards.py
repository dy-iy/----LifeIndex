"""Award a persistent 3% group-index bonus for first daily full check-in."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0032_group_checkin_rewards"
down_revision = "0031_group_daily_checkins"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("study_group_checkin_rewards",
        sa.Column("group_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("study_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("local_date", sa.Date(), primary_key=True),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_close", sa.Numeric(36, 8), nullable=False),
        sa.Column("reward_points", sa.Numeric(36, 8), nullable=False),
        sa.Column("rate", sa.Numeric(6, 5), nullable=False),
        sa.Column("member_ids", postgresql.JSONB(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.String(80), nullable=False),
        sa.CheckConstraint("local_date = (awarded_at AT TIME ZONE 'Asia/Shanghai')::date",
                           name="ck_group_checkin_rewards_local_date"),
        sa.CheckConstraint("previous_close >= 1 AND previous_close <> 'NaN'::numeric",
                           name="ck_group_checkin_rewards_previous_close"),
        sa.CheckConstraint("rate = 0.03", name="ck_group_checkin_rewards_rate"),
        sa.CheckConstraint("reward_points > 0 AND reward_points <> 'NaN'::numeric "
                           "AND reward_points = round(previous_close * rate, 8)",
                           name="ck_group_checkin_rewards_points"),
        sa.CheckConstraint("member_count > 0 AND jsonb_typeof(member_ids) = 'array' "
                           "AND jsonb_array_length(member_ids) = member_count",
                           name="ck_group_checkin_rewards_members"),
        sa.CheckConstraint("char_length(btrim(rule_version)) BETWEEN 1 AND 80 "
                           "AND rule_version = btrim(rule_version)",
                           name="ck_group_checkin_rewards_rule"),
    )
    op.create_index("ix_group_checkin_rewards_time", "study_group_checkin_rewards",
                    ["group_id", "awarded_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_group_checkin_rewards)")):
        raise RuntimeError("Full check-in rewards contain data; retain group-index achievement history")
    op.drop_index("ix_group_checkin_rewards_time", table_name="study_group_checkin_rewards")
    op.drop_table("study_group_checkin_rewards")
