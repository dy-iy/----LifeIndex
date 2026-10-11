"""Cloud review drafts and immutable submitted input versions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_reviews"
down_revision = "0005_study_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reviews",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=False),
        sa.Column("question_version", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id", "session_id"], ["study_sessions.user_id", "study_sessions.id"],
                                ondelete="CASCADE", name="fk_reviews_owned_session"),
        sa.CheckConstraint("version > 0", name="ck_reviews_version"),
        sa.CheckConstraint("question_version = 'class-review-v1'", name="ck_reviews_questions"),
        sa.CheckConstraint("status IN ('draft', 'pending_evaluation')", name="ck_reviews_status"),
        sa.CheckConstraint("(status = 'pending_evaluation') = (submitted_at IS NOT NULL)", name="ck_reviews_submitted"),
        sa.CheckConstraint("jsonb_typeof(answers) = 'array' AND jsonb_array_length(answers) = 3", name="ck_reviews_answers"),
    )
    op.create_index("ix_reviews_owner_status", "reviews", ["user_id", "status"])


def downgrade():
    op.drop_table("reviews")
