"""Branching reviews, bounded followups and separate later reflections."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0015_review_flow"
down_revision = "0014_continuous_market"
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint("ck_reviews_questions", "reviews")
    op.create_check_constraint("ck_reviews_questions", "reviews", "question_version IN ('class-review-v1', 'class-review-v2')")
    op.drop_constraint("ck_reviews_answers", "reviews")
    op.create_check_constraint("ck_reviews_answers", "reviews", "jsonb_typeof(answers) = 'array' AND ((question_version = 'class-review-v1' AND jsonb_array_length(answers) = 3) OR (question_version = 'class-review-v2' AND jsonb_array_length(answers) = 1))")
    op.add_column("reviews", sa.Column("review_context", pg.JSONB(), nullable=False, server_default="{}"))
    op.add_column("reviews", sa.Column("note", sa.String(2000), nullable=False, server_default=""))
    op.add_column("reviews", sa.Column("note_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("evaluation_jobs", sa.Column("dialogue", pg.JSONB(), nullable=False, server_default="[]"))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM reviews WHERE question_version = 'class-review-v2' OR note <> '')")):
        raise RuntimeError("Cannot discard branching reviews or notes; restore a pre-upgrade backup instead")
    op.drop_column("evaluation_jobs", "dialogue")
    for name in ("note_version", "note", "review_context"):
        op.drop_column("reviews", name)
    op.drop_constraint("ck_reviews_answers", "reviews")
    op.create_check_constraint("ck_reviews_answers", "reviews", "jsonb_typeof(answers) = 'array' AND jsonb_array_length(answers) = 3")
    op.drop_constraint("ck_reviews_questions", "reviews")
    op.create_check_constraint("ck_reviews_questions", "reviews", "question_version = 'class-review-v1'")
