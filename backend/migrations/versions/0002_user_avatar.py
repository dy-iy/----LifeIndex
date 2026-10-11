"""Store each user's current normalized avatar and its public identifier."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_user_avatar"
down_revision = "0001_identity_chat_quota"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("avatar_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("users", sa.Column("avatar_data", sa.LargeBinary(), nullable=True))
    op.create_unique_constraint("uq_users_avatar_id", "users", ["avatar_id"])


def downgrade():
    op.drop_constraint("uq_users_avatar_id", "users", type_="unique")
    op.drop_column("users", "avatar_data")
    op.drop_column("users", "avatar_id")
