"""Remove deleted arrangements from the timetable without erasing learning records."""
from alembic import op
import sqlalchemy as sa

revision = "0011_timetable_deletion"
down_revision = "0010_learning_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("lessons", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("schedule_runs", sa.Column("confirmation_text", sa.String(4000), nullable=False, server_default=""))
    # These templates were explicitly deleted already. Repair their leftover
    # occurrences; actual attendance, reviews and ledger sources stay intact.
    op.execute("""
        SELECT id FROM users
        WHERE EXISTS (SELECT 1 FROM course_templates c WHERE c.user_id = users.id AND c.is_deleted)
        ORDER BY id FOR UPDATE
    """)
    op.execute("""
        UPDATE lessons SET is_deleted = true, version = version + 1
        WHERE EXISTS (SELECT 1 FROM course_templates c
                      WHERE c.id = lessons.course_id AND c.user_id = lessons.user_id AND c.is_deleted)
          AND NOT EXISTS (SELECT 1 FROM study_sessions s
                          WHERE s.lesson_id = lessons.id AND s.user_id = lessons.user_id)
          AND NOT EXISTS (SELECT 1 FROM lesson_outcomes o
                          WHERE o.lesson_id = lessons.id AND o.user_id = lessons.user_id)
    """)


def downgrade():
    op.drop_column("schedule_runs", "confirmation_text")
    op.drop_column("lessons", "is_deleted")
