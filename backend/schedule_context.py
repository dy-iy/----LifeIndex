"""Read-only timetable facts for the structured schedule assistant."""
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from models import CourseTemplate, Lesson, Semester, User
from timetable_actions import recorded_lessons
import timetable_actions as tt


def _uuid(value):
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _records(db, user_id, semester_id):
    """Collect owned timetable records, including soft deletions and versions."""
    user_id = _uuid(user_id)
    semesters = db.scalars(select(Semester).where(Semester.user_id == user_id, Semester.id == semester_id).order_by(Semester.id)
                          .execution_options(populate_existing=True)).all()
    courses = db.scalars(select(CourseTemplate).where(CourseTemplate.user_id == user_id,
        CourseTemplate.semester_id == semester_id).order_by(CourseTemplate.id)
                        .execution_options(populate_existing=True)).all()
    lessons = db.scalars(select(Lesson).where(Lesson.user_id == user_id,
        Lesson.semester_id == semester_id).order_by(Lesson.id)
                        .execution_options(populate_existing=True)).all()
    used = set(db.scalars(select(CourseTemplate.semester_id).where(
        CourseTemplate.user_id == user_id, CourseTemplate.semester_id == semester_id).distinct()))
    semester_values = {}
    for row in semesters:
        values = tt.semester_result(row, row.id in used)
        values.pop("today")
        semester_values[str(row.id)] = values
    return {
        "semesters": semester_values,
        "courses": {str(row.id): tt.course_result(row) for row in courses},
        "lessons": {str(row.id): tt.lesson_result(row) for row in lessons},
    }


def context_snapshot(db, user_id, semester_id):
    user_id = _uuid(user_id)
    user = db.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(404, "未找到该账户。")
    if semester_id is None:
        raise HTTPException(422, "请先设置并选择学期。")
    semester = tt.owned(db, Semester, user_id, _uuid(semester_id))
    records = _records(db, user_id, semester_id)
    semesters = list(records["semesters"].values())
    for item in semesters:
        item["today"] = tt.utcnow().astimezone(ZoneInfo(item["timezone"])).date().isoformat()
    target = str(semester.id) if semester is not None else None
    recorded = {str(item) for item in recorded_lessons(db, user_id)}
    weeks = []
    if semester:
        for number in range(1, tt.total_weeks(semester) + 1):
            start = semester.first_week_monday + timedelta(weeks=number - 1)
            weeks.append({"number": number, "start_date": max(start, semester.start_date).isoformat(),
                          "end_date": min(start + timedelta(days=6), semester.end_date).isoformat()})
    return {
        "today": tt.utcnow().astimezone(ZoneInfo(user.timezone)).date().isoformat(),
        "timezone": user.timezone,
        "semesters": semesters,
        "semester": next((item for item in semesters if item["id"] == target), None),
        "week_dates": weeks,
        "courses": [item for item in records["courses"].values()
                    if item["semester_id"] == target and not item["is_deleted"]],
        "lessons": [{**item, "has_learning_record": item["id"] in recorded}
                    for item in records["lessons"].values() if item["semester_id"] == target and not item["is_deleted"]],
    }
