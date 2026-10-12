"""Mutable timetable rows and independent learning snapshots."""
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from db import lock_reader
from models import Lesson, LessonOutcome, StudySession


def snapshot_lesson(lesson):
    result = {}
    for column in Lesson.__table__.columns:
        value = getattr(lesson, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, uuid.UUID) else value
    return result


def learning_lesson(db, user_id, lesson_id):
    """Resolve learning history without restoring a row into the timetable."""
    study = db.scalar(select(StudySession).where(StudySession.user_id == user_id, StudySession.lesson_id == lesson_id))
    outcome = db.scalar(select(LessonOutcome).where(LessonOutcome.user_id == user_id, LessonOutcome.lesson_id == lesson_id))
    live = db.scalar(select(Lesson).where(Lesson.user_id == user_id, Lesson.id == lesson_id))
    saved = (outcome.lesson_snapshot if outcome and outcome.lesson_snapshot else
             study.lesson_snapshot if study and study.lesson_snapshot else None)
    if saved:
        values = dict(saved)
        for name in ("id", "user_id", "course_id", "semester_id"):
            values[name] = uuid.UUID(values[name]) if isinstance(values.get(name), str) else values.get(name)
        for name in ("starts_at", "ends_at", "original_start_at"):
            values[name] = datetime.fromisoformat(values[name]) if isinstance(values.get(name), str) else values.get(name)
        if values.get("id") != lesson_id or values.get("user_id") != user_id:
            raise HTTPException(404, "学习记录不存在。")
        if study:
            values.update(name=study.name, location=study.location, timezone=study.timezone,
                          starts_at=study.starts_at, ends_at=study.ends_at)
        values["is_deleted"] = False
        return Lesson(**{column.name: values[column.name] for column in Lesson.__table__.columns if column.name in values})
    if live is None or live.is_deleted:
        raise HTTPException(404, "课程或学习记录不存在。")
    return live


def assert_not_timing(db, user_id, lessons, operation="delete", now=None):
    """Serialize with timer start/resume; only unfinished timers block edits."""
    lessons = list(lessons)
    if any(lesson.user_id != user_id for lesson in lessons):
        raise HTTPException(404, "课程不存在。")
    lock_reader(db, user_id)
    from study_sessions import close_expired_sessions
    close_expired_sessions(db, user_id, now or datetime.now(timezone.utc))
    identifiers = [lesson.id for lesson in lessons if lesson.id is not None]
    if identifiers and db.scalar(select(StudySession.id).where(StudySession.user_id == user_id,
            StudySession.lesson_id.in_(identifiers), StudySession.phase.in_(["running", "paused"])).limit(1)):
        verb = "删除" if operation == "delete" else "修改"
        raise HTTPException(409, f"当前课程正处于计时中，无法{verb}，请计时结束后重试。")
    # Imported/legacy learning records can predate the snapshot backfill. Freeze
    # missing snapshots before either a delete or an allowed timetable edit.
    by_id = {lesson.id: lesson for lesson in lessons if lesson.id is not None}
    if by_id:
        for model in (StudySession, LessonOutcome):
            for record in db.scalars(select(model).where(model.user_id == user_id, model.lesson_id.in_(by_id))):
                if not record.lesson_snapshot:
                    record.lesson_snapshot = snapshot_lesson(by_id[record.lesson_id])


def remove_lessons(db, user_id, lessons, now=None):
    lessons = list(lessons)
    assert_not_timing(db, user_id, lessons, now=now)
    by_id = {lesson.id: lesson for lesson in lessons}
    if not by_id:
        return
    for lesson in lessons:
        db.delete(lesson)
    db.flush()
