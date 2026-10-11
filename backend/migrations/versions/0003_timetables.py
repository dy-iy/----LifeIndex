"""Persist owned semesters, course templates, lessons and idempotent mutations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_timetables"
down_revision = "0002_user_avatar"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "semesters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("first_week_monday", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("periods", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "id", name="uq_semesters_user_id_id"),
        sa.CheckConstraint("end_date >= start_date AND end_date - start_date < 370", name="ck_semesters_dates"),
        sa.CheckConstraint("first_week_monday <= start_date AND start_date - first_week_monday < 7", name="ck_semesters_first_week"),
        sa.CheckConstraint("EXTRACT(ISODOW FROM first_week_monday) = 1", name="ck_semesters_monday"),
        sa.CheckConstraint("version > 0", name="ck_semesters_version"),
    )
    op.create_table(
        "course_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("semester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_week", sa.Integer(), nullable=False),
        sa.Column("end_week", sa.Integer(), nullable=False),
        sa.Column("week_pattern", sa.String(4), nullable=False, server_default="all"),
        sa.Column("start_time", sa.String(5), nullable=False),
        sa.Column("end_time", sa.String(5), nullable=False),
        sa.Column("location", sa.String(200), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("user_id", "id", name="uq_courses_user_id_id"),
        sa.UniqueConstraint("user_id", "semester_id", "id", name="uq_courses_user_semester_id"),
        sa.ForeignKeyConstraint(["user_id", "semester_id"], ["semesters.user_id", "semesters.id"], ondelete="CASCADE", name="fk_courses_owned_semester"),
        sa.CheckConstraint("weekday BETWEEN 1 AND 7", name="ck_courses_weekday"),
        sa.CheckConstraint("start_week >= 1 AND end_week >= start_week AND end_week <= 54", name="ck_courses_weeks"),
        sa.CheckConstraint("week_pattern IN ('all', 'odd', 'even')", name="ck_courses_pattern"),
        sa.CheckConstraint("start_time < end_time", name="ck_courses_times"),
        sa.CheckConstraint("version > 0", name="ck_courses_version"),
    )
    op.create_index("ix_courses_user_semester", "course_templates", ["user_id", "semester_id"])
    op.create_table(
        "lessons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("semester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("week_number", sa.Integer(), nullable=False),
        sa.Column("original_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("location", sa.String(200), nullable=False, server_default=""),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="scheduled"),
        sa.Column("is_override", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.UniqueConstraint("user_id", "id", name="uq_lessons_user_id_id"),
        sa.UniqueConstraint("course_id", "week_number", name="uq_lessons_course_week"),
        sa.ForeignKeyConstraint(["user_id", "semester_id", "course_id"], ["course_templates.user_id", "course_templates.semester_id", "course_templates.id"], ondelete="CASCADE", name="fk_lessons_owned_course"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_lessons_times"),
        sa.CheckConstraint("week_number BETWEEN 1 AND 54", name="ck_lessons_week"),
        sa.CheckConstraint("status IN ('scheduled', 'leave', 'cancelled')", name="ck_lessons_status"),
        sa.CheckConstraint("version > 0", name="ck_lessons_version"),
    )
    op.create_index("ix_lessons_user_starts", "lessons", ["user_id", "starts_at"])
    op.create_index("ix_lessons_user_semester_starts", "lessons", ["user_id", "semester_id", "starts_at"])
    op.create_table(
        "timetable_mutations",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(key) BETWEEN 1 AND 128", name="ck_timetable_mutation_key"),
    )


def downgrade():
    op.drop_table("timetable_mutations")
    op.drop_table("lessons")
    op.drop_table("course_templates")
    op.drop_table("semesters")
