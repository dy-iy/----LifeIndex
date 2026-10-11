"""Timetable contracts and the single implementation shared by HTTP and AI plans."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from datetime import date as LocalDate
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from db import Input, Version, owned, check_version
from models import CourseTemplate, Lesson, LessonOutcome, Semester, StudySession
from schedule_conflicts import ScheduleConflictError, find_conflicts

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
    start_time: ClockTime
    end_time: ClockTime

    @model_validator(mode="after")
    def valid_times(self):
        if self.end_time <= self.start_time:
            raise ValueError("节次结束时间必须晚于开始时间，不支持跨日节次。")
        return self


def validate_periods(value):
    if value is None:
        return value
    if len({item.number for item in value}) != len(value):
        raise ValueError("节次编号不能重复。")
    ordered = sorted(value, key=lambda item: item.start_time)
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


class CourseCreate(Input):
    semester_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    weekday: int = Field(strict=True, ge=1, le=7)
    start_week: int = Field(strict=True, ge=1, le=54)
    end_week: int = Field(strict=True, ge=1, le=54)
    week_pattern: WeekPattern = "all"
    start_time: ClockTime
    end_time: ClockTime
    location: str = Field(default="", max_length=200)


class CoursePatch(Patch):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    weekday: int | None = Field(default=None, strict=True, ge=1, le=7)
    start_week: int | None = Field(default=None, strict=True, ge=1, le=54)
    end_week: int | None = Field(default=None, strict=True, ge=1, le=54)
    week_pattern: WeekPattern | None = None
    start_time: ClockTime | None = None
    end_time: ClockTime | None = None
    location: str | None = Field(default=None, max_length=200)


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
        id=str(item.id), name=item.name, start_date=item.start_date, end_date=item.end_date,
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
        raise HTTPException(422, f"{day.isoformat()} {clock} 在 {tz_name} 因时区切换不存在或有歧义，请调整时间。")
    return candidates.pop()


def interval(day, start, end, tz_name):
    if start >= end:
        raise HTTPException(422, "结束时间必须晚于开始时间，不支持跨日课程。")
    starts_at, ends_at = local_instant(day, start, tz_name), local_instant(day, end, tz_name)
    if ends_at <= starts_at:
        raise HTTPException(422, "课程结束时刻必须晚于开始时刻。")
    return starts_at, ends_at


def generate(course, semester):
    if course.start_week > course.end_week or course.end_week > total_weeks(semester):
        raise HTTPException(422, "课程周次必须在学期内，结束周不能早于开始周。")
    if course.start_time >= course.end_time:
        raise HTTPException(422, "结束时间必须晚于开始时间，不支持跨日课程。")
    result = {}
    for week in range(course.start_week, course.end_week + 1):
        if (course.week_pattern == "odd" and week % 2 == 0) or (course.week_pattern == "even" and week % 2 == 1):
            continue
        day = semester.first_week_monday + timedelta(days=(week - 1) * 7 + course.weekday - 1)
        if semester.start_date <= day <= semester.end_date:
            result[week] = interval(day, course.start_time, course.end_time, semester.timezone)
    if not result:
        raise HTTPException(422, "当前星期、周次及单双周设置在学期内没有课程。")
    return result


def assert_no_overlap(db, user_id, *, before_lessons=None, semester_id=None):
    db.flush()
    items = db.scalars(select(Lesson).where(
        Lesson.user_id == user_id, Lesson.status == "scheduled", Lesson.is_deleted.is_(False),
    ).order_by(Lesson.starts_at, Lesson.id)).all()
    if before_lessons is not None and semester_id is not None:
        # The plan can only change its own semester. Foreign rows are current,
        # unchanged conflict candidates, not proposed imports.
        before_lessons = {**{str(item.id): lesson_result(item) for item in items
                             if item.semester_id != semester_id}, **before_lessons}
    conflicts = find_conflicts((lesson_result(item) for item in items), before_lessons=before_lessons)
    if conflicts:
        # Only query record protection after an overlap is found. Both query
        # branches are scoped to the authenticated owner, including other terms.
        recorded = (set(db.scalars(select(StudySession.lesson_id).where(StudySession.user_id == user_id)).all())
                    | set(db.scalars(select(LessonOutcome.lesson_id).where(LessonOutcome.user_id == user_id)).all()))
        conflicts = find_conflicts((lesson_result(item) for item in items), before_lessons=before_lessons,
                                   recorded_lesson_ids=recorded)
        raise ScheduleConflictError(conflicts)


def new_lesson(course, semester, week, starts_at, ends_at):
    return Lesson(user_id=course.user_id, semester_id=semester.id, course_id=course.id,
                  week_number=week, original_start_at=starts_at, starts_at=starts_at, ends_at=ends_at,
                  name=course.name, location=course.location, timezone=semester.timezone,
                  status="scheduled", is_override=False, version=1, note="")


def recorded_lessons(db, user_id):
    return (set(db.scalars(select(StudySession.lesson_id).where(StudySession.user_id == user_id)).all())
            | set(db.scalars(select(LessonOutcome.lesson_id).where(LessonOutcome.user_id == user_id)).all()))


def create_semester_record(db, user, body, *, record_id=None):
    values = body.model_dump()
    values["timezone"] = body.timezone or "Asia/Shanghai"
    item = Semester(user_id=user.id, **values)
    if record_id is not None:
        item.id = record_id
    validate_semester(item)
    db.add(item)
    db.flush()
    return item


def create_course_record(db, user, body, *, record_id=None, lesson_id_factory=None):
    semester = owned(db, Semester, user.id, body.semester_id)
    course = CourseTemplate(user_id=user.id, **body.model_dump())
    if record_id is not None:
        course.id = record_id
    generated = generate(course, semester)
    db.add(course)
    db.flush()
    for week, (starts_at, ends_at) in generated.items():
        item = new_lesson(course, semester, week, starts_at, ends_at)
        if lesson_id_factory is not None:
            item.id = lesson_id_factory(week)
        db.add(item)
    db.flush()
    return course


def update_course_record(db, user, course_id, body, *, now, lesson_id_factory=None):
    course = owned(db, CourseTemplate, user.id, course_id)
    check_version(course, body.version)
    semester = owned(db, Semester, user.id, course.semester_id)
    for name, value in body.model_dump(exclude_unset=True, exclude={"version"}).items():
        setattr(course, name, value)
    generated = generate(course, semester)
    lessons = {item.week_number: item for item in db.scalars(select(Lesson).where(
        Lesson.course_id == course.id, Lesson.user_id == user.id,
    )).all()}
    recorded = recorded_lessons(db, user.id)
    for week, item in lessons.items():
        if item.is_deleted or item.starts_at <= now or item.is_override or item.id in recorded:
            continue
        target = generated.get(week)
        if target is None or target[0] <= now:
            if item.status != "cancelled":
                item.status = "cancelled"
                item.version += 1
            continue
        starts_at, ends_at = target
        changed = (item.starts_at != starts_at or item.ends_at != ends_at or item.name != course.name
                   or item.location != course.location or item.status != "scheduled")
        if changed:
            item.starts_at, item.ends_at = starts_at, ends_at
            item.name, item.location = course.name, course.location
            item.status = "scheduled"
            item.version += 1
    for week, (starts_at, ends_at) in generated.items():
        if week not in lessons and starts_at > now:
            item = new_lesson(course, semester, week, starts_at, ends_at)
            if lesson_id_factory is not None:
                item.id = lesson_id_factory(week)
            db.add(item)
    course.version += 1
    db.flush()
    return course


def delete_course_record(db, user, course_id, version, *, now):
    course = owned(db, CourseTemplate, user.id, course_id)
    check_version(course, version)
    recorded = recorded_lessons(db, user.id)
    for item in db.scalars(select(Lesson).where(
        Lesson.course_id == course.id, Lesson.user_id == user.id,
        Lesson.is_deleted.is_(False),
    )).all():
        if item.id not in recorded:
            item.is_deleted = True
            item.version += 1
    course.is_deleted = True
    course.version += 1
    db.flush()
    return course


def delete_lesson_record(db, user, lesson_id, version):
    item = owned(db, Lesson, user.id, lesson_id)
    check_version(item, version)
    if item.id in recorded_lessons(db, user.id):
        raise HTTPException(409, "这节课已有计时或课后记录，已保留学习记录；如需纠错，请到计时或复盘页修改记录。")
    item.is_deleted = True
    item.is_override = True
    item.version += 1
    db.flush()
    return item


def update_lesson_record(db, user, lesson_id, body, *, now, allow_history=False):
    item = owned(db, Lesson, user.id, lesson_id)
    check_version(item, body.version)
    if db.scalar(select(StudySession.id).where(
        StudySession.user_id == user.id, StudySession.lesson_id == item.id,
    )) is not None or db.scalar(select(LessonOutcome.lesson_id).where(
        LessonOutcome.user_id == user.id, LessonOutcome.lesson_id == item.id,
    )) is not None:
        raise HTTPException(409, "该课程已有计时或课后记录，不能修改课表中的这一次安排。")
    if not allow_history and item.starts_at <= now:
        raise HTTPException(409, "已开始或过去的课程不能在课表中修改，请到计时页查看课后确认与补记。")
    if body.date is not None:
        semester = owned(db, Semester, user.id, item.semester_id)
        if not semester.start_date <= body.date <= semester.end_date:
            raise HTTPException(422, "调课日期必须在该学期内。")
        starts_at, ends_at = interval(body.date, body.start_time, body.end_time, item.timezone)
        if not allow_history and starts_at <= now:
            raise HTTPException(422, "不能调课到已经开始或过去的时刻。")
        item.starts_at, item.ends_at = starts_at, ends_at
    if body.status is not None:
        item.status = body.status
    if body.note is not None:
        item.note = body.note
    item.is_override = True
    item.version += 1
    db.flush()
    return item
