"""Persist semester selection and archive old conversations without a semester."""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0020_semester_scope"
down_revision = "0019_independent_learning"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("semesters", sa.Column("number", sa.Integer(), nullable=True))
    op.execute(sa.text("""WITH numbered AS (
        SELECT id, row_number() OVER (
            PARTITION BY user_id ORDER BY start_date, created_at, id
        ) AS number FROM semesters
    ) UPDATE semesters AS s SET number=n.number FROM numbered AS n WHERE s.id=n.id"""))
    op.alter_column("semesters", "number", nullable=False)
    op.create_unique_constraint("uq_semesters_user_number", "semesters", ["user_id", "number"])
    op.create_check_constraint("ck_semesters_number", "semesters", "number > 0")

    op.add_column("users", sa.Column("active_semester_id", UUID(as_uuid=True), nullable=True))
    op.add_column("users", sa.Column("semester_state_version", sa.Integer(), nullable=False, server_default="1"))
    # Match the former page's default list order; do not guess which semester an
    # old unscoped conversation or course belonged to.
    op.execute(sa.text("""UPDATE users AS u SET active_semester_id=(
        SELECT s.id FROM semesters AS s WHERE s.user_id=u.id
        ORDER BY s.start_date DESC, s.id LIMIT 1
    )"""))
    op.create_foreign_key("fk_users_active_semester", "users", "semesters",
                          ["id", "active_semester_id"], ["user_id", "id"])
    op.create_check_constraint("ck_users_semester_state_version", "users", "semester_state_version >= 1")

    op.drop_index("ix_schedule_threads_owner_updated", table_name="schedule_threads")
    op.create_index("ix_schedule_threads_owner_semester_updated", "schedule_threads",
                    ["user_id", "semester_id", "updated_at"])

    for table, constraint in (
        ("schedule_threads", "ck_schedule_threads_semester_scope"),
        ("timetable_documents", "ck_timetable_documents_semester_scope"),
    ):
        op.add_column(table, sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.execute(sa.text(f"UPDATE {table} SET is_archived=true WHERE semester_id IS NULL"))
        op.create_check_constraint(constraint, table, "semester_id IS NOT NULL OR is_archived")

    # Workers must be stopped while applying schema migrations. Preserve all
    # user input and model results, but do not let old queued work write into a
    # semester chosen only after the request was made.
    op.execute(sa.text("""WITH superseded AS (
        UPDATE schedule_runs AS r SET status='superseded', lease_owner=NULL,
            lease_until=NULL, updated_at=now(),
            error='原对话未设置学期，已封存保留。请先设置学期后使用课表助手。'
        FROM schedule_threads AS t WHERE r.thread_id=t.id AND t.is_archived
            AND r.status IN ('queued','running','needs_input','awaiting_confirmation',
                'confirm_queued','confirming','stale','failed')
        RETURNING r.thread_id
    ) UPDATE schedule_threads SET version=version+1, updated_at=now()
        WHERE id IN (SELECT thread_id FROM superseded)"""))
    _ensure_documents()


def _ensure_documents():
    connection = op.get_bind()
    semesters = connection.execute(sa.text("""SELECT s.id,s.user_id,s.periods
        FROM semesters AS s LEFT JOIN timetable_documents AS d
            ON d.user_id=s.user_id AND d.semester_id=s.id
        WHERE d.id IS NULL ORDER BY s.id""")).mappings()
    documents = sa.table("timetable_documents", sa.column("id", UUID),
        sa.column("user_id", UUID), sa.column("semester_id", UUID), sa.column("data", JSONB))
    for semester in semesters:
        document_id = uuid.uuid5(uuid.NAMESPACE_URL, "lifefund:semester:" + str(semester["id"]))
        data = {"schema_version": "structured-timetable-v1", "period_tables": [], "sources": []}
        if semester["periods"]:
            table_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "lifefund:periods:" + str(semester["id"])))
            data["period_tables"] = [{"id": table_id, "weeks": None, "periods": semester["periods"]}]
            data["display_period_table_id"] = table_id
        connection.execute(documents.insert().values(id=document_id, user_id=semester["user_id"],
            semester_id=semester["id"], data=data))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("""SELECT EXISTS (SELECT 1 FROM semesters)
        OR EXISTS (SELECT 1 FROM users WHERE active_semester_id IS NOT NULL OR semester_state_version <> 1)
        OR EXISTS (SELECT 1 FROM schedule_threads WHERE is_archived)
        OR EXISTS (SELECT 1 FROM timetable_documents WHERE is_archived)""")):
        raise RuntimeError("Semester state cannot be discarded; restore a pre-upgrade backup instead")
    op.drop_index("ix_schedule_threads_owner_semester_updated", table_name="schedule_threads")
    op.create_index("ix_schedule_threads_owner_updated", "schedule_threads", ["user_id", "updated_at"])
    for table, constraint in (
        ("timetable_documents", "ck_timetable_documents_semester_scope"),
        ("schedule_threads", "ck_schedule_threads_semester_scope"),
    ):
        op.drop_constraint(constraint, table, type_="check")
        op.drop_column(table, "is_archived")
    op.drop_constraint("fk_users_active_semester", "users", type_="foreignkey")
    op.drop_constraint("ck_users_semester_state_version", "users", type_="check")
    op.drop_column("users", "semester_state_version")
    op.drop_column("users", "active_semester_id")
    op.drop_constraint("uq_semesters_user_number", "semesters", type_="unique")
    op.drop_constraint("ck_semesters_number", "semesters", type_="check")
    op.drop_column("semesters", "number")
