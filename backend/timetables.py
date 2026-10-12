"""HTTP bindings for timetable actions."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from pydantic import Field

from auth import CurrentUser, Db
from db import IdempotencyKey, Input, Version, mutate, owned, check_version
from models import CourseTemplate, Lesson, Semester
import timetable_actions as actions
from timetable_actions import SemesterCreate, SemesterPatch, CourseCreate, CoursePatch, LessonPatch, SemesterResponse, CourseResponse, LessonResponse, semester_result, course_result, lesson_result, validate_semester, assert_no_overlap

router = APIRouter()


class StructuredCourseCreate(Input):
    document_id: uuid.UUID
    data: dict[str, Any]


class StructuredCoursePatch(Input):
    version: Version
    name: str | None = Field(default=None, max_length=100)
    schedule_updates: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    schedule_additions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    schedule_removals: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class StructuredBind(Input):
    version: Version
    semester_id: uuid.UUID


@router.get("/api/timetables/structured")
def get_structured(user: CurrentUser, db: Db, semester_id: uuid.UUID | None = None,
                   thread_id: uuid.UUID | None = None):
    from timetable_structured import structured_snapshot
    if (semester_id is None) == (thread_id is None):
        raise HTTPException(422, "请指定学期或未关联学期的对话。")
    return structured_snapshot(db, user.id, semester_id=semester_id, thread_id=thread_id)


@router.post("/api/timetables/structured/courses", status_code=201)
def create_structured(body: StructuredCourseCreate, request: Request, user: CurrentUser, db: Db,
                      key: IdempotencyKey):
    from timetable_structured import create_structured_course
    return mutate(db, user, request, key, body, 201,
                  lambda: create_structured_course(db, user, body.document_id, body.data))


@router.patch("/api/timetables/structured/courses/{course_id}")
def patch_structured(course_id: uuid.UUID, body: StructuredCoursePatch, request: Request,
                     user: CurrentUser, db: Db, key: IdempotencyKey):
    from timetable_structured import patch_structured_course
    return mutate(db, user, request, key, body, 200,
                  lambda: patch_structured_course(db, user, course_id, body.model_dump(mode="json", exclude_unset=True)))


@router.post("/api/timetables/structured/documents/{document_id}/bind")
def bind_structured(document_id: uuid.UUID, body: StructuredBind, request: Request,
                    user: CurrentUser, db: Db, key: IdempotencyKey):
    from models import TimetableDocument
    from timetable_structured import associate_document, structured_snapshot
    def action():
        document = owned(db, TimetableDocument, user.id, document_id)
        check_version(document, body.version)
        owned(db, Semester, user.id, body.semester_id)
        associate_document(db, user, document, body.semester_id)
        return structured_snapshot(db, user.id, semester_id=body.semester_id)
    return mutate(db, user, request, key, body, 200, action)


def _capture_periods(db, user, semester):
    """Save the current semester clocks and retime its period-based courses."""
    from copy import deepcopy
    from timetable_structured import get_document, sync_period_axis, _apply_period_table_changes, _current_period_table
    document = get_document(db, user.id, semester_id=semester.id, create=True)
    data = deepcopy(document.data or {})
    tables = data.setdefault("period_tables", [])
    table = _current_period_table(data)
    if table is None:
        table = {"id": str(uuid.uuid4()), "weeks": None, "source": "manual"}
        tables.append(table)
    table["source"] = "manual"
    table["periods"] = deepcopy(semester.periods or [])
    data["display_period_table_id"] = table["id"]
    document.data = data
    document.version += 1
    document.updated_at = datetime.now(timezone.utc)
    sync_period_axis(db, user, document, preferred_id=table["id"])
    unapplied = []
    _apply_period_table_changes(db, user, document, [{"action": "update", "id": table["id"],
        "periods": deepcopy(table["periods"])}], {}, unapplied)
    if unapplied:
        raise HTTPException(409, unapplied[0]["reason"])
    db.flush()

@router.get("/api/timetables/semesters", response_model=list[SemesterResponse])
def list_semesters(user: CurrentUser, db: Db):
    used_semesters = set(db.scalars(select(CourseTemplate.semester_id).where(
        CourseTemplate.user_id == user.id,
    ).distinct()).all())
    return [semester_result(item, item.id in used_semesters) for item in db.scalars(select(Semester).where(
        Semester.user_id == user.id,
    ).order_by(Semester.start_date.desc(), Semester.id)).all()]


@router.post("/api/timetables/semesters", response_model=SemesterResponse, status_code=201)
def create_semester(body: SemesterCreate, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    from timetable_actions import create_semester_record
    def action():
        item = create_semester_record(db, user, body)
        _capture_periods(db, user, item)
        return semester_result(item)
    return mutate(db, user, request, key, body, 201, action)


@router.patch("/api/timetables/semesters/{semester_id}", response_model=SemesterResponse)
def patch_semester(semester_id: uuid.UUID, body: SemesterPatch, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        item = owned(db, Semester, user.id, semester_id)
        check_version(item, body.version)
        values = body.model_dump(exclude_unset=True, exclude={"version"})
        protected = {"start_date", "end_date", "first_week_monday", "timezone"}
        has_courses = db.scalar(select(CourseTemplate.id).where(
            CourseTemplate.user_id == user.id, CourseTemplate.semester_id == item.id,
        ).limit(1)) is not None
        if any(name in values and values[name] != getattr(item, name) for name in protected):
            if has_courses:
                raise HTTPException(409, "该学期已有课程，不能修改学期日期、周次起算或时区；请新建学期。")
        for name, value in values.items():
            setattr(item, name, value)
        validate_semester(item)
        item.version += 1
        db.flush()
        if "periods" in values:
            _capture_periods(db, user, item)
        return semester_result(item, has_courses)
    return mutate(db, user, request, key, body, 200, action)


@router.get("/api/timetables/courses", response_model=list[CourseResponse])
def list_courses(semester_id: uuid.UUID, user: CurrentUser, db: Db):
    owned(db, Semester, user.id, semester_id)
    return [course_result(item) for item in db.scalars(select(CourseTemplate).where(
        CourseTemplate.user_id == user.id, CourseTemplate.semester_id == semester_id,
        CourseTemplate.is_deleted.is_(False),
    ).order_by(CourseTemplate.weekday, CourseTemplate.start_time, CourseTemplate.id)).all()]


@router.post("/api/timetables/courses", response_model=CourseResponse, status_code=201)
def create_course(body: CourseCreate, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    from timetable_actions import create_course_record
    def action():
        course = create_course_record(db, user, body)
        assert_no_overlap(db, user.id)
        return course_result(course)
    return mutate(db, user, request, key, body, 201, action)


@router.patch("/api/timetables/courses/{course_id}", response_model=CourseResponse)
def patch_course(course_id: uuid.UUID, body: CoursePatch, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    from timetable_actions import update_course_record
    def action():
        course = update_course_record(db, user, course_id, body, now=actions.utcnow())
        assert_no_overlap(db, user.id)
        return course_result(course)
    return mutate(db, user, request, key, body, 200, action)


@router.delete("/api/timetables/courses/{course_id}", status_code=204)
def delete_course(course_id: uuid.UUID, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey,
                  version: Annotated[int, Query(ge=1)]):
    from timetable_actions import delete_course_record
    def action():
        delete_course_record(db, user, course_id, version, now=actions.utcnow())
        return None
    return mutate(db, user, request, key, {"version": version}, 204, action)


@router.get("/api/lessons", response_model=list[LessonResponse])
def list_lessons(semester_id: uuid.UUID, date_from: date, date_to: date, user: CurrentUser, db: Db):
    semester = owned(db, Semester, user.id, semester_id)
    if not 0 <= (date_to - date_from).days < 93:
        raise HTTPException(422, "查询日期范围需从早到晚，且不能超过 93 天。")
    # Broad UTC bounds followed by snapshot-local dates also handle historical
    # midnight timezone transitions without treating an ambiguous midnight as a lesson.
    if date_from.year < 2 or date_to.year > 9998:
        raise HTTPException(422, "查询日期超出支持范围。")
    start = datetime.combine(date_from - timedelta(days=1), time.min, timezone.utc)
    end = datetime.combine(date_to + timedelta(days=2), time.min, timezone.utc)
    rows = db.scalars(select(Lesson).where(
        Lesson.user_id == user.id, Lesson.semester_id == semester.id, Lesson.is_deleted.is_(False),
        Lesson.starts_at >= start, Lesson.starts_at < end,
    ).order_by(Lesson.starts_at, Lesson.id)).all()
    return [lesson_result(item) for item in rows
            if date_from <= item.starts_at.astimezone(ZoneInfo(item.timezone)).date() <= date_to]


@router.patch("/api/lessons/{lesson_id}", response_model=LessonResponse)
def patch_lesson(lesson_id: uuid.UUID, body: LessonPatch, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    from timetable_actions import update_lesson_record
    def action():
        item = update_lesson_record(db, user, lesson_id, body, now=actions.utcnow())
        assert_no_overlap(db, user.id)
        return lesson_result(item)
    return mutate(db, user, request, key, body, 200, action)


@router.delete("/api/lessons/{lesson_id}", status_code=204)
def delete_lesson(lesson_id: uuid.UUID, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey,
                  version: Annotated[int, Query(ge=1)]):
    from timetable_actions import delete_lesson_record
    def action():
        delete_lesson_record(db, user, lesson_id, version, now=actions.utcnow())
        return None
    return mutate(db, user, request, key, {"version": version}, 204, action)
