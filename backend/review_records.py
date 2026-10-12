"""Idempotent review placeholders for elapsed courses after timer enrollment."""
from db import lock_reader
from diagnostics import log_failure
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import exists, select
from sqlalchemy.exc import SQLAlchemyError

from models import Lesson, LessonOutcome, StudySession, User
from study_sessions import TIMING_GRACE

REVIEW_RETENTION = timedelta(days=7)


def missing_lessons(now):
    return select(Lesson).join(User, User.id == Lesson.user_id).where(
        User.review_tracking_started_at.is_not(None), Lesson.starts_at >= User.review_tracking_started_at,
        Lesson.status == "scheduled", Lesson.is_deleted.is_(False),
        Lesson.ends_at >= now - REVIEW_RETENTION, Lesson.ends_at <= now - TIMING_GRACE,
        ~exists(select(StudySession.id).where(StudySession.lesson_id == Lesson.id)),
        ~exists(select(LessonOutcome.lesson_id).where(LessonOutcome.lesson_id == Lesson.id,
                                                     LessonOutcome.decision != "pending")),
    )


def create_record(db, lesson):
    # No segments, attendance decision, or evaluation are invented by this record.
    db.add(StudySession(user_id=lesson.user_id, lesson_id=lesson.id, name=lesson.name,
        location=lesson.location, timezone=lesson.timezone, starts_at=lesson.starts_at, ends_at=lesson.ends_at,
        phase="finished", version=1, started_at=lesson.ends_at, finished_at=lesson.ends_at,
        finish_reason="untimed", is_makeup=False, voided=False))
    lesson.version += 1


def ensure_recent_reviews(db, user_id, now):
    """Caller holds the user lock shared by timers, outcomes and timetable edits."""
    lessons = db.scalars(missing_lessons(now).where(Lesson.user_id == user_id)).all()
    for lesson in lessons:
        create_record(db, lesson)
    db.flush()
    return len(lessons)


def maintain_review_records(factory, now=None, limit=100):
    now = now or datetime.now(timezone.utc)
    with factory() as db:
        candidates = db.execute(missing_lessons(now).with_only_columns(Lesson.id, Lesson.user_id)
                                .order_by(Lesson.ends_at, Lesson.id).limit(limit)).all()
    count = 0
    for lesson_id, user_id in candidates:
        try:
            created = False
            with factory.begin() as db:
                lock_reader(db, user_id)
                lesson = db.scalar(missing_lessons(now).where(Lesson.id == lesson_id, Lesson.user_id == user_id))
                if lesson is not None:
                    create_record(db, lesson)
                    created = True
            count += int(created)
        except SQLAlchemyError as error:
            log_failure(logging.getLogger("lifefund.review.worker"),
                "Review record maintenance failed lesson_id=%s; retrying next round", error, lesson_id)
    return count
