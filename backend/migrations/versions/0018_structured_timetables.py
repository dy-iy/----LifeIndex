"""JSONB timetable documents, logical courses, and exact week projections."""
import uuid
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0018_structured_timetables"
down_revision = "0017_review_agent_v5"
branch_labels = depends_on = None


def upgrade():
    op.create_table("timetable_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("semester_id", UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=True),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "id", name="uq_timetable_documents_owner"),
        sa.UniqueConstraint("user_id", "semester_id", name="uq_timetable_documents_semester"),
        sa.UniqueConstraint("user_id", "thread_id", name="uq_timetable_documents_thread"),
        sa.ForeignKeyConstraint(["user_id", "semester_id"], ["semesters.user_id", "semesters.id"], ondelete="CASCADE", name="fk_timetable_documents_semester"),
        sa.ForeignKeyConstraint(["user_id", "thread_id"], ["schedule_threads.user_id", "schedule_threads.id"], ondelete="CASCADE", name="fk_timetable_documents_thread"),
        sa.CheckConstraint("(semester_id IS NULL) <> (thread_id IS NULL)", name="ck_timetable_documents_scope"),
        sa.CheckConstraint("version > 0", name="ck_timetable_documents_version"))
    op.create_table("timetable_courses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "id", name="uq_timetable_courses_owner"),
        sa.ForeignKeyConstraint(["user_id", "document_id"], ["timetable_documents.user_id", "timetable_documents.id"], ondelete="CASCADE", name="fk_timetable_courses_document"),
        sa.CheckConstraint("version > 0", name="ck_timetable_courses_version"),
        sa.CheckConstraint("jsonb_typeof(data) = 'object'", name="ck_timetable_courses_data"))
    op.create_index("ix_timetable_courses_document", "timetable_courses", ["user_id", "document_id"])
    op.add_column("course_templates", sa.Column("weeks", JSONB, nullable=True))
    op.add_column("course_templates", sa.Column("source_course_id", UUID(as_uuid=True), nullable=True))
    op.add_column("course_templates", sa.Column("source_schedule_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_course_templates_structured", "course_templates", "timetable_courses", ["source_course_id"], ["id"], ondelete="SET NULL")
    _backfill()
    # Workers must be stopped during migration. Previously confirmed/applying runs
    # are allowed to finish first; remaining incompatible graph work stays readable.
    op.execute(sa.text("""WITH superseded AS (
        UPDATE schedule_runs SET status='superseded', lease_owner=NULL,
        lease_until=NULL, updated_at=now(), error='课表识别流程已升级，原输入和图片已保留。'
        WHERE status IN ('queued','running','needs_input','awaiting_confirmation','confirm_queued','confirming','stale','failed')
        RETURNING thread_id
        ) UPDATE schedule_threads SET version=version+1, updated_at=now()
        WHERE id IN (SELECT thread_id FROM superseded)"""))


def _backfill():
    bind = op.get_bind()
    documents = sa.table("timetable_documents", sa.column("id", UUID), sa.column("user_id", UUID), sa.column("semester_id", UUID), sa.column("data", JSONB))
    courses = sa.table("timetable_courses", sa.column("id", UUID), sa.column("user_id", UUID), sa.column("document_id", UUID), sa.column("data", JSONB), sa.column("version", sa.Integer), sa.column("is_deleted", sa.Boolean))
    semesters = bind.execute(sa.text("SELECT id,user_id,periods FROM semesters ORDER BY id")).mappings().all()
    for semester in semesters:
        document_id = uuid.uuid5(uuid.NAMESPACE_URL, "lifefund:semester:" + str(semester["id"]))
        table_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "lifefund:periods:" + str(semester["id"])))
        periods = [{"number": item["number"], "start_time": item.get("start_time"), "end_time": item.get("end_time")}
                   for item in semester["periods"] or []]
        bind.execute(documents.insert().values(id=document_id, user_id=semester["user_id"], semester_id=semester["id"],
            data={"schema_version": "structured-timetable-v1", "period_tables": [{"id": table_id, "weeks": None, "periods": periods}] if periods else [], "sources": [{"type": "legacy_migration"}]}))
        templates = bind.execute(sa.text("SELECT * FROM course_templates WHERE semester_id=:id ORDER BY id"), {"id": semester["id"]}).mappings().all()
        for course in templates:
            schedule_id = uuid.uuid5(uuid.NAMESPACE_URL, "lifefund:schedule:" + str(course["id"]))
            weeks = [week for week in range(course["start_week"], course["end_week"] + 1)
                     if course["week_pattern"] == "all" or (week % 2 == 1 if course["week_pattern"] == "odd" else week % 2 == 0)]
            exceptions = []
            lessons = bind.execute(sa.text("""SELECT * FROM lessons WHERE course_id=:id
                AND (is_override OR is_deleted OR status<>'scheduled') ORDER BY week_number"""), {"id": course["id"]}).mappings().all()
            for lesson in lessons:
                local_start = lesson["starts_at"].astimezone(ZoneInfo(lesson["timezone"]))
                local_end = lesson["ends_at"].astimezone(ZoneInfo(lesson["timezone"]))
                exceptions.append({"schedule_id": str(schedule_id), "original_week": lesson["week_number"],
                    "date": local_start.date().isoformat(), "start_time": local_start.strftime("%H:%M"), "end_time": local_end.strftime("%H:%M"),
                    "status": lesson["status"], "note": lesson["note"], "is_deleted": lesson["is_deleted"]})
            bind.execute(courses.insert().values(id=course["id"], user_id=course["user_id"], document_id=document_id,
                version=course["version"], is_deleted=course["is_deleted"], data={"name": course["name"], "schedules": [{"id": str(schedule_id),
                    "weekday": course["weekday"], "period_table_id": table_id if periods else None, "period_numbers": None,
                    "start_time": course["start_time"], "end_time": course["end_time"], "weeks": weeks, "teacher": None,
                    "location": course["location"] or None}], "exceptions": exceptions}))
            bind.execute(sa.text("""UPDATE course_templates SET weeks=CAST(:weeks AS jsonb), source_course_id=:id, source_schedule_id=:schedule
                WHERE id=:id"""), {"id": course["id"], "schedule": schedule_id, "weeks": __import__("json").dumps(weeks)})


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM timetable_courses)")):
        raise RuntimeError("Structured timetable data cannot be discarded; restore a pre-upgrade backup instead")
    op.drop_constraint("fk_course_templates_structured", "course_templates", type_="foreignkey")
    op.drop_column("course_templates", "source_schedule_id")
    op.drop_column("course_templates", "source_course_id")
    op.drop_column("course_templates", "weeks")
    op.drop_index("ix_timetable_courses_document", table_name="timetable_courses")
    op.drop_table("timetable_courses")
    op.drop_table("timetable_documents")
