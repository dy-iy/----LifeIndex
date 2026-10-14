"""Remove group notification delivery state; keep group and learning records."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0023_remove_group_notifications"
down_revision = "0022_group_index"
branch_labels = None
depends_on = None


def upgrade():
    # These are notification delivery artifacts only. Group membership, shares,
    # private reviews and the official group-index ledger are retained.
    op.drop_table("social_outbox_events")
    op.drop_column("social_groups", "activity_checked_at")


def downgrade():
    # Restore the former empty schema, not deleted notification history or the
    # former scan checkpoints. Business data is unchanged in either direction.
    op.add_column("social_groups", sa.Column("activity_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("social_outbox_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("social_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False, server_default="group.updated"),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
        sa.UniqueConstraint("group_id", "version", name="uq_social_outbox_group_version"))
    op.create_index("ix_social_outbox_pending", "social_outbox_events", ["created_at"],
                    postgresql_where=sa.text("published_at IS NULL"))
