"""Archive incomplete records, preserve corrections and fence withdrawn makeup."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0010_learning_lifecycle"
down_revision = "0009_class_index"
branch_labels = depends_on = None


def upgrade():
    op.add_column("study_sessions", sa.Column("voided", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("lesson_outcomes", sa.Column("input_version", sa.Integer(), nullable=False, server_default="0"))
    op.execute("UPDATE lesson_outcomes SET input_version = 1 WHERE decision <> 'pending'")
    op.create_check_constraint("ck_lesson_outcomes_input_version", "lesson_outcomes", "input_version >= 0")
    op.create_table("learning_archives",
        sa.Column("lesson_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], ondelete="CASCADE", name="fk_learning_archives_owner"))
    op.create_table("outcome_revisions",
        sa.Column("lesson_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("input_version", sa.Integer(), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], ondelete="CASCADE", name="fk_outcome_revisions_owner"))
    op.execute("""INSERT INTO outcome_revisions (lesson_id,input_version,user_id,snapshot,created_at)
        SELECT o.lesson_id, o.input_version, o.user_id,
        jsonb_build_object('decision',o.decision,'reason',o.reason,'reported_seconds',s.reported_seconds,
                           'session_id',s.id,'input_version',o.input_version,'confirmed_at',o.confirmed_at),o.confirmed_at
        FROM lesson_outcomes o LEFT JOIN study_sessions s ON s.lesson_id=o.lesson_id
        WHERE o.decision <> 'pending'""")


def downgrade():
    op.drop_table("outcome_revisions")
    op.drop_table("learning_archives")
    op.drop_constraint("ck_lesson_outcomes_input_version", "lesson_outcomes", type_="check")
    op.drop_column("lesson_outcomes", "input_version")
    op.drop_column("study_sessions", "voided")
