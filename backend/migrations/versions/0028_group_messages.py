"""Persist group text messages with global cursors and sender retry keys."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028_group_messages"
down_revision = "0027_group_initial_open"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table("study_group_messages",
        sa.Column("id", uid, primary_key=True),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("group_id", uid, sa.ForeignKey("study_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_id", uid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_key", sa.String(128), nullable=False),
        sa.Column("content", sa.String(2000), nullable=False),
        sa.Column("sender_name", sa.String(60), nullable=False),
        sa.Column("sender_avatar_url", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("sequence", name="uq_study_group_messages_sequence"),
        sa.UniqueConstraint("sender_id", "client_key", name="uq_study_group_messages_sender_key"),
        sa.CheckConstraint("sequence > 0", name="ck_study_group_messages_sequence"),
        sa.CheckConstraint("char_length(content) BETWEEN 1 AND 2000 AND content ~ '[^[:space:]]'",
                           name="ck_study_group_messages_content"),
    )
    op.create_index("ix_study_group_messages_group_sequence", "study_group_messages", ["group_id", "sequence"])
    op.create_index("ix_study_group_messages_sender_time", "study_group_messages", ["sender_id", "created_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_group_messages)")):
        raise RuntimeError("Group messages contain data; retain them instead of discarding chat history")
    op.drop_table("study_group_messages")
