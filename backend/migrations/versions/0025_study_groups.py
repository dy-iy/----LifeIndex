"""Add study group names and memberships without notification infrastructure."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_study_groups"
down_revision = "0024_remove_social_groups"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table("study_groups",
        sa.Column("id", uid, primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("owner_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 60 AND name = btrim(name)",
                           name="ck_study_groups_name"),
    )
    op.create_index("ix_study_groups_owner_id", "study_groups", ["owner_id"])
    op.create_table("study_group_members",
        sa.Column("group_id", uid, sa.ForeignKey("study_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_study_group_members_user_id", "study_group_members", ["user_id", "group_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_groups)")):
        raise RuntimeError("Study groups contain data; back them up before removing these tables")
    op.drop_table("study_group_members")
    op.drop_table("study_groups")
