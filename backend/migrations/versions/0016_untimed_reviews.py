"""Enable recent zero-duration reviews after the first completed real timer."""
from alembic import op
import sqlalchemy as sa

revision = "0016_untimed_reviews"
down_revision = "0015_review_flow"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("review_tracking_started_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_study_sessions_finish_reason", "study_sessions", type_="check")
    op.create_check_constraint("ck_study_sessions_finish_reason", "study_sessions",
        "finish_reason IS NULL OR finish_reason IN ('user', 'timeout', 'offline', 'repair', 'untimed')")
    # Historical real timers qualify too; manual duration claims have no segments.
    op.execute(sa.text("""
        UPDATE users AS u SET review_tracking_started_at = eligible.first_finish
        FROM (
            SELECT s.user_id, MIN(s.finished_at) AS first_finish
            FROM study_sessions AS s
            WHERE s.phase = 'finished' AND NOT s.voided AND s.reported_seconds IS NULL
              AND (SELECT SUM(GREATEST(0, EXTRACT(EPOCH FROM (
                    LEAST(g.end_at, s.finished_at, s.ends_at) - GREATEST(g.start_at, s.starts_at)))))
                   FROM study_segments AS g
                   WHERE g.session_id = s.id AND g.user_id = s.user_id AND g.end_at IS NOT NULL) >= 1
            GROUP BY s.user_id
        ) AS eligible WHERE u.id = eligible.user_id
    """))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_sessions WHERE finish_reason = 'untimed')")):
        raise RuntimeError("Cannot discard untimed review records; restore a pre-upgrade backup instead")
    op.drop_constraint("ck_study_sessions_finish_reason", "study_sessions", type_="check")
    op.create_check_constraint("ck_study_sessions_finish_reason", "study_sessions",
        "finish_reason IS NULL OR finish_reason IN ('user', 'timeout', 'offline', 'repair')")
    op.drop_column("users", "review_tracking_started_at")
