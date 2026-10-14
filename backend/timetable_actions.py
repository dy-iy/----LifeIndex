"""Timetable contracts and actions backed by structured course data."""
from datetime import date, datetime, time, timezone
from datetime import date as LocalDate
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from db import Input, Version, owned, check_version, lock_reader
from models import CourseTemplate, Lesson, LessonOutcome, Semester, StudySession, TimetableCourse, TimetableDocument
from schedule_conflicts import find_conflicts

ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
WeekPattern = Literal["all", "odd", "even"]
LessonStatus = Literal["scheduled", "leave", "cancelled"]


def utcnow():
    return datetime.now(timezone.utc)


def valid_timezone(value):
    if value is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai。") from None
    return value


class Period(Input):
    number: int = Field(strict=True, ge=1, le=30)
    start_time: ClockTime | None = None
    end_time: ClockTime | None = None

    @model_validator(mode="after")
    def valid_times(self):
        if self.start_time is not None and self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("节次结束时间必须晚于开始时间，不支持跨日节次。")
        return self


def validate_periods(value):
    if value is None:
        return value
    if len({item.number for item in value}) != len(value):
        raise ValueError("节次编号不能重复。")
    ordered = sorted((item for item in value if item.start_time is not None and item.end_time is not None), key=lambda item: item.start_time)
    if any(left.end_time > right.start_time for left, right in zip(ordered, ordered[1:])):
        raise ValueError("节次时间不能重叠。")
    return value


class SemesterCreate(Input):
    name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    first_week_monday: date
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    periods: list[Period] = Field(default_factory=list, max_length=30)

    _timezone = field_validator("timezone")(valid_timezone)
    _periods = field_validator("periods")(validate_periods)


class Patch(Input):
    version: Version

    @model_validator(mode="after")
    def reject_nulls(self):
        if not self.model_fields_set - {"version"}:
            raise ValueError("请至少提供一个需要修改的字段。")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("修改字段不能为 null；清空地点或备注请使用空字符串。")
        return self


class SemesterPatch(Patch):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    first_week_monday: date | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    periods: list[Period] | None = Field(default=None, max_length=30)

    _timezone = field_validator("timezone")(valid_timezone)
    _periods = field_validator("periods")(validate_periods)


class LessonPatch(Patch):
    status: LessonStatus | None = None
    date: LocalDate | None = None
    start_time: ClockTime | None = None
    end_time: ClockTime | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def complete_time_change(self):
        fields = self.model_fields_set & {"date", "start_time", "end_time"}
        if fields and len(fields) != 3:
            raise ValueError("调课请同时提供 date、start_time、end_time。")
        return self


class SemesterResponse(BaseModel):
    id: str
    number: int
    name: str
    start_date: date
    end_date: date
    first_week_monday: date
    timezone: str
    periods: list[Period]
    version: int
    created_at: datetime
    total_weeks: int
    today: date
    has_courses: bool


class CourseResponse(BaseModel):
    id: str
    semester_id: str
    name: str
    weekday: int
    start_week: int
    end_week: int
    week_pattern: WeekPattern
    start_time: str
    end_time: str
    location: str
    version: int
    is_deleted: bool
    weeks: list[int] | None = None
    structured_course_id: str | None = None
    schedule_id: str | None = None


class LessonResponse(BaseModel):
    id: str
    semester_id: str
    course_id: str
    week_number: int
    original_start_at: datetime
    starts_at: datetime
    ends_at: datetime
    name: str
    location: str
    timezone: str
    status: LessonStatus
    is_override: bool
    is_deleted: bool
    version: int
    note: str
    local_date: date
    start_time: str
    end_time: str
    duration_minutes: int


def total_weeks(semester):
    return (semester.end_date - semester.first_week_monday).days // 7 + 1


def semester_result(item, has_courses=False):
    return SemesterResponse(
        id=str(item.id), number=item.number, name=item.name, start_date=item.start_date, end_date=item.end_date,
        first_week_monday=item.first_week_monday, timezone=item.timezone, periods=item.periods,
        version=item.version, created_at=item.created_at, total_weeks=total_weeks(item),
        today=utcnow().astimezone(ZoneInfo(item.timezone)).date(),
        has_courses=has_courses,
    ).model_dump(mode="json")


def course_result(item):
    return CourseResponse(
        id=str(item.id), semester_id=str(item.semester_id), name=item.name, weekday=item.weekday,
        start_week=item.start_week, end_week=item.end_week, week_pattern=item.week_pattern,
        start_time=item.start_time, end_time=item.end_time, location=item.location,
        version=item.version, is_deleted=item.is_deleted,
        weeks=getattr(item, "weeks", None),
        structured_course_id=str(item.source_course_id) if getattr(item, "source_course_id", None) else None,
        schedule_id=str(item.source_schedule_id) if getattr(item, "source_schedule_id", None) else None,
    ).model_dump(mode="json")


def lesson_result(item):
    local_start = item.starts_at.astimezone(ZoneInfo(item.timezone))
    local_end = item.ends_at.astimezone(ZoneInfo(item.timezone))
    return LessonResponse(
        id=str(item.id), semester_id=str(item.semester_id), course_id=str(item.course_id),
        week_number=item.week_number, original_start_at=item.original_start_at.astimezone(timezone.utc),
        starts_at=item.starts_at.astimezone(timezone.utc), ends_at=item.ends_at.astimezone(timezone.utc),
        name=item.name, location=item.location, timezone=item.timezone, status=item.status,
        is_override=item.is_override, is_deleted=item.is_deleted, version=item.version, note=item.note,
        local_date=local_start.date(), start_time=local_start.strftime("%H:%M"), end_time=local_end.strftime("%H:%M"),
        duration_minutes=int((item.ends_at - item.starts_at).total_seconds() // 60),
    ).model_dump(mode="json")


def validate_semester(item):
    if not 0 <= (item.end_date - item.start_date).days < 370:
        raise HTTPException(422, "学期结束日不能早于开始日，且学期不得超过 370 天。")
    if item.first_week_monday.weekday() != 0 or not 0 <= (item.start_date - item.first_week_monday).days < 7:
        raise HTTPException(422, "第一周起算日必须是学期开始日所在周的周一。")
    if item.start_date.year < 2 or item.end_date.year > 9998:
        raise HTTPException(422, "学期日期超出支持范围。")


def local_instant(day, clock, tz_name):
    """A wall clock must map to exactly one UTC instant, including DST transitions."""
    naive = datetime.combine(day, time.fromisoformat(clock))
    tz = ZoneInfo(tz_name)
    candidates = set()
    for fold in (0, 1):
        utc = naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            candidates.add(utc)
    if len(candidates) != 1:
        raise HTTPException(422, f"{day.isoformat()} {clock} 不存在或对应多个时刻，请调整时间。")
    return candidates.pop()


def interval(day, start, end, tz_name):
    if start >= end:
        raise HTTPException(422, "结束时间必须晚于开始时间，不支持跨日课程。")
    starts_at, ends_at = local_instant(day, start, tz_name), local_instant(day, end, tz_name)
    if ends_at <= starts_at:
        raise HTTPException(422, "课程结束时刻必须晚于开始时刻。")
    return starts_at, ends_at


def assert_no_overlap(db, user_id):
    """Return overlap diagnostics; overlapping courses are valid saved data."""
    db.flush()
    items = db.scalars(select(Lesson).where(
        Lesson.user_id == user_id, Lesson.status == "scheduled", Lesson.is_deleted.is_(False),
    ).order_by(Lesson.starts_at, Lesson.id)).all()
    conflicts = find_conflicts((lesson_result(item) for item in items))
    if conflicts:
        # Only query record protection after an overlap is found. Both query
        # branches are scoped to the authenticated owner, including other terms.
        recorded = (set(db.scalars(select(StudySession.lesson_id).where(StudySession.user_id == user_id)).all())
                    | set(db.scalars(select(LessonOutcome.lesson_id).where(LessonOutcome.user_id == user_id)).all()))
        conflicts = find_conflicts((lesson_result(item) for item in items), recorded_lesson_ids=recorded)
    return conflicts


def new_lesson(course, semester, week, starts_at, ends_at):
    return Lesson(user_id=course.user_id, semester_id=semester.id, course_id=course.id,
                  week_number=week, original_start_at=starts_at, starts_at=starts_at, ends_at=ends_at,
                  name=course.name, location=course.location, timezone=semester.timezone,
                  status="scheduled", is_override=False, version=1, note="")


def recorded_lessons(db, user_id):
    return (set(db.scalars(select(StudySession.lesson_id).where(StudySession.user_id == user_id)).all())
            | set(db.scalars(select(LessonOutcome.lesson_id).where(LessonOutcome.user_id == user_id)).all()))


def create_semester_record(db, user, body):
    from semester_state import next_semester_number
    values = body.model_dump()
    values["timezone"] = body.timezone or user.timezone
    item = Semester(user_id=user.id, number=next_semester_number(db, user.id), **values)
    validate_semester(item)
    db.add(item)
    db.flush()
    return item


def _structured_source(db, user, course):
    """Resolve one projected schedule; absent migration data must be repaired explicitly."""
    message = "课程缺少有效的结构化来源，请先完成课表数据迁移后再修改。"
    if course.source_course_id is None or course.source_schedule_id is None:
        raise HTTPException(409, message)
    logical = db.scalar(select(TimetableCourse).where(TimetableCourse.user_id == user.id,
        TimetableCourse.id == course.source_course_id).execution_options(populate_existing=True))
    if logical is None or logical.is_deleted:
        raise HTTPException(409, message)
    document = db.scalar(select(TimetableDocument).where(TimetableDocument.user_id == user.id,
        TimetableDocument.id == logical.document_id, TimetableDocument.semester_id == course.semester_id))
    schedule = next((item for item in logical.data.get("schedules", [])
                     if item.get("id") == str(course.source_schedule_id)), None)
    if document is None or schedule is None:
        raise HTTPException(409, message)
    return logical, schedule


def delete_course_record(db, user, course_id, version, *, now):
    from timetable_structured import patch_structured_course
    lock_reader(db, user.id)
    course = owned(db, CourseTemplate, user.id, course_id)
    check_version(course, version)
    logical, schedule = _structured_source(db, user, course)
    patch_structured_course(db, user, logical.id, {
        "version": logical.version, "schedule_removals": [schedule["id"]],
    }, now=now, delete_if_empty=True, strict=True)
    return course


def delete_lesson_record(db, user, lesson_id, version, *, now=None):
    from timetable_structured import patch_structured_course
    now = now or utcnow()
    lock_reader(db, user.id)
    item = owned(db, Lesson, user.id, lesson_id)
    check_version(item, version)
    template = owned(db, CourseTemplate, user.id, item.course_id)
    logical, schedule = _structured_source(db, user, template)
    patch_structured_course(db, user, logical.id, {
        "version": logical.version, "exceptions": [{"schedule_id": schedule["id"],
            "original_week": item.week_number, "is_deleted": True}],
    }, now=now, strict=True)
    return item


def update_lesson_record(db, user, lesson_id, body, *, now):
    from timetable_structured import patch_structured_course
    lock_reader(db, user.id)
    item = owned(db, Lesson, user.id, lesson_id)
    check_version(item, body.version)
    template = owned(db, CourseTemplate, user.id, item.course_id)
    logical, schedule = _structured_source(db, user, template)
    if item.is_override and not any(exception.get("schedule_id") == schedule["id"]
            and exception.get("original_week") == item.week_number for exception in logical.data.get("exceptions", [])):
        raise HTTPException(409, "该课次缺少有效的单次安排来源，请先完成课表数据迁移后再修改。")
    values = {}
    if body.date is not None:
        semester = owned(db, Semester, user.id, item.semester_id)
        if not semester.start_date <= body.date <= semester.end_date:
            raise HTTPException(422, "调课日期必须在该学期内。")
        interval(body.date, body.start_time, body.end_time, semester.timezone)
        values.update(date=body.date.isoformat(), start_time=body.start_time, end_time=body.end_time)
    if body.status is not None:
        values["status"] = body.status
    if body.note is not None:
        values["note"] = body.note
    patch_structured_course(db, user, logical.id, {
        "version": logical.version, "exceptions": [{"schedule_id": schedule["id"],
            "original_week": item.week_number, **values}],
    }, now=now, touch_lesson_id=item.id, strict=True)
    return owned(db, Lesson, user.id, lesson_id)
