"""Separate learning snapshots from mutable timetable rows."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019_independent_learning"
down_revision = "0018_structured_timetables"
branch_labels = None
depends_on = None

LINKS = (
    ("study_sessions", "fk_study_sessions_lesson"),
    ("lesson_outcomes", "fk_lesson_outcomes_owner"),
    ("class_index_events", "fk_class_index_event_lesson"),
    ("outcome_revisions", "fk_outcome_revisions_owner"),
)


def upgrade():
    for table in ("study_sessions", "lesson_outcomes"):
        op.add_column(table, sa.Column("lesson_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
        op.execute(sa.text(f"UPDATE {table} AS h SET lesson_snapshot = to_jsonb(l) FROM lessons AS l WHERE h.lesson_id=l.id AND h.user_id=l.user_id"))
    # The source UUID remains stable for existing evaluation fingerprints and
    # index revisions. Ownership is still enforced on every learning record.
    for table, name in LINKS:
        op.drop_constraint(name, table, type_="foreignkey")


def downgrade():
    connection = op.get_bind()
    for table, _ in LINKS:
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} h LEFT JOIN lessons l ON l.id=h.lesson_id AND l.user_id=h.user_id WHERE l.id IS NULL)")):
            raise RuntimeError("Cannot restore timetable foreign keys while independent learning history exists")
    for table, name in LINKS:
        op.create_foreign_key(name, table, "lessons", ["user_id", "lesson_id"], ["user_id", "id"], ondelete="CASCADE")
    for table in ("study_sessions", "lesson_outcomes"):
        op.drop_column(table, "lesson_snapshot")
