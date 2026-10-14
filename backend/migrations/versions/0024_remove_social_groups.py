"""Remove the withdrawn group feature while preserving private learning data."""
from alembic import op

revision = "0024_remove_social_groups"
down_revision = "0023_remove_group_notifications"
branch_labels = None
depends_on = None


def upgrade():
    # Drop only group-owned data, in dependency order. In particular, dropping
    # the group event ledger does not delete its personal settlement sources.
    for table in (
        "group_index_daily",
        "group_index_events",
        "group_index_components",
        "group_index_state",
        "review_group_shares",
        "social_group_invites",
        "social_group_members",
        "social_groups",
    ):
        op.drop_table(table)
    # This metadata was added exclusively for consent-based group sharing.
    # Personal answers, submission timestamps and evaluation jobs are retained.
    op.drop_column("reviews", "first_submitted_at")


def downgrade():
    raise RuntimeError("Removed group data cannot be reconstructed; restore the pre-removal backup")
