"""Groups, hashed invitations, consent-filtered shares and transactional outbox."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0021_social_groups"
down_revision = "0020_semester_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reviews", sa.Column("first_submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE reviews SET first_submitted_at=submitted_at WHERE submitted_at IS NOT NULL")
    # A later lifecycle repair may have cleared submitted_at back to a draft.
    # Durable evaluation jobs retain the earliest known submission attempt.
    op.execute(sa.text("""WITH first_jobs AS (
        SELECT session_id,user_id,min(created_at) AS submitted_at
        FROM evaluation_jobs GROUP BY session_id,user_id
    ) UPDATE reviews AS r SET first_submitted_at=LEAST(r.submitted_at,j.submitted_at)
        FROM first_jobs AS j WHERE r.session_id=j.session_id AND r.user_id=j.user_id"""))
    op.create_table("social_groups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activity_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','dissolved')", name="ck_social_groups_status"),
        sa.CheckConstraint("version > 0", name="ck_social_groups_version"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 60", name="ck_social_groups_name"))
    op.create_table("social_group_members",
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("social_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("role", sa.String(12), nullable=False, server_default="member"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("share_course", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("share_focus", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_share_reviews", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sharing_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("public_display_name", sa.String(60), nullable=False, server_default="群成员"),
        sa.Column("display_name_hash", sa.String(64), nullable=True),
        sa.Column("display_name_status", sa.String(12), nullable=False, server_default="pending"),
        sa.CheckConstraint("status IN ('active','left','banned')", name="ck_social_members_status"),
        sa.CheckConstraint("role IN ('owner','member')", name="ck_social_members_role"),
        sa.CheckConstraint("NOT auto_share_reviews OR sharing_enabled_at IS NOT NULL", name="ck_social_members_consent"))
    op.create_index("ix_social_members_user_status", "social_group_members", ["user_id", "status"])
    op.create_table("social_group_invites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("social_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inviter_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("max_uses > 0 AND used_count >= 0 AND used_count <= max_uses", name="ck_social_invites_uses"))
    op.create_index("ix_social_invites_group", "social_group_invites", ["group_id", "expires_at"])
    op.create_table("review_group_shares",
        sa.Column("review_id", UUID(as_uuid=True), sa.ForeignKey("reviews.session_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("social_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared_revision", sa.Integer(), nullable=False),
        sa.Column("selected_answer_indexes", JSONB(), nullable=False),
        sa.Column("shared_text_hash", sa.String(64), nullable=False),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visibility_status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("share_source", sa.String(12), nullable=False, server_default="manual"),
        sa.Column("moderation_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_moderation_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("visibility_status IN ('pending','approved','rejected','withdrawn')", name="ck_review_group_shares_visibility"),
        sa.CheckConstraint("shared_revision > 0", name="ck_review_group_shares_revision"),
        sa.CheckConstraint("jsonb_typeof(selected_answer_indexes) = 'array'", name="ck_review_group_shares_answers"))
    op.create_index("ix_review_group_shares_feed", "review_group_shares", ["group_id", "visibility_status", "expires_at"])
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
    op.create_index("ix_social_outbox_pending", "social_outbox_events", ["created_at"], postgresql_where=sa.text("published_at IS NULL"))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM social_groups)")):
        raise RuntimeError("Group membership and shared history cannot be discarded; restore a pre-upgrade backup")
    for table in ("social_outbox_events", "review_group_shares", "social_group_invites", "social_group_members", "social_groups"):
        op.drop_table(table)
    op.drop_column("reviews", "first_submitted_at")
