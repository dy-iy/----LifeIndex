"""Direct review results and the final evaluation of an automatic outcome."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0017_review_agent_v5"
down_revision = "0016_untimed_reviews"
branch_labels = depends_on = None


def upgrade():
    op.add_column("reviews", sa.Column("outcome_evaluation_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_reviews_outcome_evaluation", "reviews", "evaluation_jobs", ["outcome_evaluation_id"], ["id"])
    op.drop_constraint("ck_evaluation_jobs_status", "evaluation_jobs", type_="check")
    op.create_check_constraint("ck_evaluation_jobs_status", "evaluation_jobs",
        "status IN ('queued','running','needs_input','hold','failed','succeeded','superseded','invalid_answer')")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM evaluation_jobs WHERE prompt_version IN ('class-quality-v5', 'class-summary-v6', 'class-summary-v7'))")):
        raise RuntimeError("Cannot discard v5 review results; restore a pre-upgrade backup instead")
    op.drop_constraint("ck_evaluation_jobs_status", "evaluation_jobs", type_="check")
    op.create_check_constraint("ck_evaluation_jobs_status", "evaluation_jobs",
        "status IN ('queued','running','needs_input','hold','failed','succeeded','superseded')")
    op.drop_constraint("fk_reviews_outcome_evaluation", "reviews", type_="foreignkey")
    op.drop_column("reviews", "outcome_evaluation_id")
