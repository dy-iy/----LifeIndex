import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "session_id"], ["study_sessions.user_id", "study_sessions.id"],
                             ondelete="CASCADE", name="fk_reviews_owned_session"),
        ForeignKeyConstraint(["outcome_evaluation_id"], ["evaluation_jobs.id"],
                             use_alter=True, name="fk_reviews_outcome_evaluation"),
        CheckConstraint("version > 0", name="ck_reviews_version"),
        CheckConstraint("question_version IN ('class-review-v1', 'class-review-v2')", name="ck_reviews_questions"),
        CheckConstraint("status IN ('draft', 'pending_evaluation')", name="ck_reviews_status"),
        CheckConstraint("(status = 'pending_evaluation') = (submitted_at IS NOT NULL)", name="ck_reviews_submitted"),
        CheckConstraint("jsonb_typeof(answers) = 'array' AND ((question_version = 'class-review-v1' AND jsonb_array_length(answers) = 3) OR (question_version = 'class-review-v2' AND jsonb_array_length(answers) = 1))", name="ck_reviews_answers"),
        Index("ix_reviews_owner_status", "user_id", "status"),
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    answers: Mapped[list] = mapped_column(JSONB)
    question_version: Mapped[str] = mapped_column(String(32))
    review_context: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    outcome_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    note: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    note_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("wechat_app_id", "wechat_open_id", name="uq_users_wechat_identity"),
        UniqueConstraint("avatar_id", name="uq_users_avatar_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wechat_app_id: Mapped[str] = mapped_column(String(128))
    wechat_open_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    avatar_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    avatar_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", server_default="Asia/Shanghai")
    review_tracking_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def avatar_url(self) -> str | None:
        return f"/api/avatars/{self.avatar_id}" if self.avatar_id is not None else None


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatDailyUsage(Base):
    __tablename__ = "chat_daily_usage"
    __table_args__ = (CheckConstraint("request_count >= 0", name="ck_chat_daily_usage_nonnegative"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Semester(Base):
    __tablename__ = "semesters"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_semesters_user_id_id"),
        CheckConstraint("end_date >= start_date AND end_date - start_date < 370", name="ck_semesters_dates"),
        CheckConstraint("first_week_monday <= start_date AND start_date - first_week_monday < 7", name="ck_semesters_first_week"),
        CheckConstraint("EXTRACT(ISODOW FROM first_week_monday) = 1", name="ck_semesters_monday"),
        CheckConstraint("version > 0", name="ck_semesters_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    first_week_monday: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    periods: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CourseTemplate(Base):
    __tablename__ = "course_templates"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_courses_user_id_id"),
        UniqueConstraint("user_id", "semester_id", "id", name="uq_courses_user_semester_id"),
        ForeignKeyConstraint(["user_id", "semester_id"], ["semesters.user_id", "semesters.id"], ondelete="CASCADE", name="fk_courses_owned_semester"),
        CheckConstraint("weekday BETWEEN 1 AND 7", name="ck_courses_weekday"),
        CheckConstraint("start_week >= 1 AND end_week >= start_week AND end_week <= 54", name="ck_courses_weeks"),
        CheckConstraint("week_pattern IN ('all', 'odd', 'even')", name="ck_courses_pattern"),
        CheckConstraint("start_time < end_time", name="ck_courses_times"),
        CheckConstraint("version > 0", name="ck_courses_version"),
        Index("ix_courses_user_semester", "user_id", "semester_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(100))
    weekday: Mapped[int] = mapped_column(Integer)
    start_week: Mapped[int] = mapped_column(Integer)
    end_week: Mapped[int] = mapped_column(Integer)
    week_pattern: Mapped[str] = mapped_column(String(4), default="all", server_default="all")
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    location: Mapped[str] = mapped_column(String(200), default="", server_default="")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_lessons_user_id_id"),
        UniqueConstraint("course_id", "week_number", name="uq_lessons_course_week"),
        ForeignKeyConstraint(["user_id", "semester_id", "course_id"], ["course_templates.user_id", "course_templates.semester_id", "course_templates.id"], ondelete="CASCADE", name="fk_lessons_owned_course"),
        CheckConstraint("ends_at > starts_at", name="ck_lessons_times"),
        CheckConstraint("week_number BETWEEN 1 AND 54", name="ck_lessons_week"),
        CheckConstraint("status IN ('scheduled', 'leave', 'cancelled')", name="ck_lessons_status"),
        CheckConstraint("version > 0", name="ck_lessons_version"),
        Index("ix_lessons_user_starts", "user_id", "starts_at"),
        Index("ix_lessons_user_semester_starts", "user_id", "semester_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    week_number: Mapped[int] = mapped_column(Integer)
    original_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    name: Mapped[str] = mapped_column(String(100))
    location: Mapped[str] = mapped_column(String(200), default="", server_default="")
    timezone: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default="scheduled", server_default="scheduled")
    is_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    note: Mapped[str] = mapped_column(String(500), default="", server_default="")


class TimetableMutation(Base):
    __tablename__ = "timetable_mutations"
    __table_args__ = (CheckConstraint("length(key) BETWEEN 1 AND 128", name="ck_timetable_mutation_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudySession(Base):
    __tablename__ = "study_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_study_sessions_owner"),
        UniqueConstraint("lesson_id", name="uq_study_sessions_lesson"),
        ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], ondelete="CASCADE", name="fk_study_sessions_lesson"),
        CheckConstraint("phase IN ('running', 'paused', 'finished')", name="ck_study_sessions_phase"),
        CheckConstraint("version > 0", name="ck_study_sessions_version"),
        CheckConstraint("ends_at > starts_at", name="ck_study_sessions_plan"),
        CheckConstraint("(phase = 'finished') = (finished_at IS NOT NULL)", name="ck_study_sessions_finished"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="ck_study_sessions_times"),
        CheckConstraint("finish_reason IS NULL OR finish_reason IN ('user', 'timeout', 'offline', 'repair', 'untimed')", name="ck_study_sessions_finish_reason"),
        CheckConstraint("reported_seconds IS NULL OR (reported_seconds >= 0 AND reported_seconds <= EXTRACT(EPOCH FROM (ends_at - starts_at)) AND phase = 'finished' AND is_makeup)", name="ck_study_sessions_reported"),
        Index("uq_study_sessions_active", "user_id", unique=True, postgresql_where=text("phase IN ('running', 'paused')")),
        Index("ix_study_sessions_recent", "user_id", "finished_at"),
        Index("ix_study_sessions_expiry", "ends_at", postgresql_where=text("phase IN ('running', 'paused')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(100))
    location: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(12), default="running", server_default="running")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(12), nullable=True)
    finish_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_makeup: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reported_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voided: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class LessonOutcome(Base):
    __tablename__ = "lesson_outcomes"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"],
                             ondelete="CASCADE", name="fk_lesson_outcomes_owner"),
        CheckConstraint("decision IN ('pending', 'makeup', 'absent', 'leave', 'cancelled')", name="ck_lesson_outcomes_decision"),
        CheckConstraint("version > 0", name="ck_lesson_outcomes_version"),
        CheckConstraint("input_version >= 0", name="ck_lesson_outcomes_input_version"),
        CheckConstraint("(decision = 'pending') = (confirmed_at IS NULL)", name="ck_lesson_outcomes_confirmed"),
        Index("ix_lesson_outcomes_owner", "user_id", "decision"),
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    decision: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    reason: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    note: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    input_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudySegment(Base):
    __tablename__ = "study_segments"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "session_id"], ["study_sessions.user_id", "study_sessions.id"], ondelete="CASCADE", name="fk_study_segments_session"),
        CheckConstraint("end_at IS NULL OR end_at >= start_at", name="ck_study_segments_times"),
        Index("ix_study_segments_session", "session_id", "start_at"),
        Index("uq_study_segments_open", "session_id", unique=True, postgresql_where=text("end_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduleThread(Base):
    __tablename__ = "schedule_threads"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_schedule_threads_owner"),
        ForeignKeyConstraint(["user_id", "semester_id"], ["semesters.user_id", "semesters.id"], name="fk_schedule_thread_semester"),
        CheckConstraint("version > 0", name="ck_schedule_thread_version"),
        Index("ix_schedule_threads_owner_updated", "user_id", "updated_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    semester_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(100), default="课表对话")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduleRun(Base):
    __tablename__ = "schedule_runs"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "thread_id"], ["schedule_threads.user_id", "schedule_threads.id"], ondelete="CASCADE", name="fk_schedule_run_thread"),
        UniqueConstraint("user_id", "id", name="uq_schedule_runs_owner"),
        CheckConstraint("attempts >= 0", name="ck_schedule_run_attempts"),
        Index("ix_schedule_runs_queue", "status", "lease_until", "created_at"),
        Index("ix_schedule_runs_thread", "thread_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    input_text: Mapped[str] = mapped_column(String(4000))
    confirmation_text: Mapped[str] = mapped_column(String(4000), default="", server_default="")
    image_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    reply: Mapped[str] = mapped_column(String(12000), default="", server_default="")
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    preview: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduleImage(Base):
    __tablename__ = "schedule_images"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "thread_id"], ["schedule_threads.user_id", "schedule_threads.id"], ondelete="CASCADE", name="fk_schedule_image_thread"),
        Index("ix_schedule_images_expiry", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    mime_type: Mapped[str] = mapped_column(String(30))
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScheduleDailyUsage(Base):
    __tablename__ = "schedule_daily_usage"
    __table_args__ = (CheckConstraint("reserved_micro >= 0 AND charged_micro >= 0 AND request_count >= 0", name="ck_schedule_daily_usage_nonnegative"),)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_micro: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    charged_micro: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    request_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ScheduleModelCall(Base):
    __tablename__ = "schedule_model_calls"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "run_id"], ["schedule_runs.user_id", "schedule_runs.id"], ondelete="CASCADE", name="fk_schedule_call_run"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    usage_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    reserved_micro: Mapped[int] = mapped_column(Integer)
    charged_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_rate: Mapped[int] = mapped_column(Integer)
    output_rate: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Register auxiliary tables after Base and owned models exist.
import evaluation_models  # noqa: E402,F401
import index_models  # noqa: E402,F401
import lifecycle_models  # noqa: E402,F401
import agents.models  # noqa: E402,F401
