"""Durable 48-hour incomplete archives and deterministic outcome recovery."""
from db import lock_reader
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import String, cast, exists, or_, select

from auth import CurrentUser, Db
from class_index import settle_lesson
from index_models import ClassIndexReceipt
from lifecycle_models import LearningArchive, OutcomeRevision
from models import Lesson, LessonOutcome, Review, StudySession

router = APIRouter()


def reopen_lesson(db, user_id, lesson_id, now):
    item = db.get(LearningArchive, lesson_id)
    if item and item.user_id == user_id and item.reopened_at is None:
        item.reopened_at = now


def outcome_evidence(lesson, outcome, study=None):
    return {"lesson_name": lesson.name, "outcome": {
        "decision": outcome.decision, "reason": outcome.reason, "input_version": outcome.input_version,
        "confirmed_at": outcome.confirmed_at.isoformat(),
        "reported_seconds": study.reported_seconds if study and outcome.decision == "makeup" else None,
        "session_id": str(study.id) if study else None,
    }}


def record_outcome(db, lesson, outcome, study, now, *, evaluation=None):
    evidence = outcome_evidence(lesson, outcome, study)
    if evaluation is not None:
        evidence["evaluation"] = evaluation
        evidence["outcome"]["evaluation"] = evaluation
    db.add(OutcomeRevision(user_id=outcome.user_id, lesson_id=lesson.id, input_version=outcome.input_version,
                           snapshot=evidence["outcome"], created_at=now))
    # A corrected attendance claim removes a previous absence while quality is
    # pending. The eventual score appends another adjustment using the same base.
    event = settle_lesson(db, outcome.user_id, lesson.id, f"outcome:{lesson.id}:{outcome.input_version}",
        "absence" if outcome.decision == "absent" else "excluded", evidence=evidence, now=now)
    reopen_lesson(db, outcome.user_id, lesson.id, now)
    return event


def archive_incomplete(factory, now=None, limit=100):
    now = now or datetime.now(timezone.utc)
    from evaluation_models import EvaluationJob
    invalid = exists(select(EvaluationJob.id).where(EvaluationJob.session_id == StudySession.id,
        EvaluationJob.review_version == Review.version, EvaluationJob.session_version == StudySession.version,
        EvaluationJob.status == "invalid_answer"))
    missing = or_(StudySession.id.is_(None), StudySession.phase != "finished",
                  Review.session_id.is_(None), Review.status == "draft", invalid)
    query = select(Lesson.id, Lesson.user_id).outerjoin(StudySession, StudySession.lesson_id == Lesson.id).outerjoin(
        Review, Review.session_id == StudySession.id).outerjoin(LessonOutcome, LessonOutcome.lesson_id == Lesson.id).where(
        Lesson.status == "scheduled", Lesson.is_deleted.is_(False), Lesson.ends_at <= now - timedelta(hours=48), missing,
        or_(LessonOutcome.lesson_id.is_(None), LessonOutcome.decision.in_(["pending", "makeup"])),
        ~exists(select(LearningArchive.lesson_id).where(LearningArchive.lesson_id == Lesson.id,
                                                       LearningArchive.reopened_at.is_(None))),
    ).order_by(Lesson.ends_at, Lesson.id).limit(limit)
    with factory() as db:
        candidates = db.execute(query).all()
    count = 0
    for lesson_id, user_id in candidates:
        with factory.begin() as db:
            lock_reader(db, user_id)
            lesson = db.get(Lesson, lesson_id)
            outcome = db.get(LessonOutcome, lesson_id)
            study = db.scalar(select(StudySession).where(StudySession.lesson_id == lesson_id))
            review = db.get(Review, study.id) if study else None
            if not lesson or lesson.is_deleted or lesson.status != "scheduled" or lesson.ends_at > now - timedelta(hours=48):
                continue
            if outcome and outcome.decision not in ("pending", "makeup"):
                continue
            if study and not study.voided and study.phase == "finished" and review and review.status != "draft":
                invalid_job = db.scalar(select(EvaluationJob.id).where(EvaluationJob.session_id == study.id,
                    EvaluationJob.review_version == review.version, EvaluationJob.session_version == study.version,
                    EvaluationJob.status == "invalid_answer"))
                if invalid_job is None:
                    continue
            reason = "unconfirmed" if not study else "unfinished_timer" if study.phase != "finished" else "missing_review"
            item = db.get(LearningArchive, lesson_id)
            if item is None:
                item = LearningArchive(lesson_id=lesson_id, user_id=user_id, reason=reason,
                                       archived_at=lesson.ends_at + timedelta(hours=48))
                db.add(item)
            item.reason, item.reopened_at = reason, None
            count += 1
    return count


def backfill_outcomes(factory, now=None, limit=100):
    now = now or datetime.now(timezone.utc)
    source = "outcome:" + cast(LessonOutcome.lesson_id, String) + ":" + cast(LessonOutcome.input_version, String)
    with factory() as db:
        candidates = db.execute(select(LessonOutcome.lesson_id, LessonOutcome.user_id).where(
            LessonOutcome.decision.in_(["absent", "leave", "cancelled"]),
            ~exists(select(ClassIndexReceipt.source_key).where(ClassIndexReceipt.user_id == LessonOutcome.user_id,
                                                              ClassIndexReceipt.source_key == source)),
        ).order_by(LessonOutcome.confirmed_at, LessonOutcome.lesson_id).limit(limit)).all()
    count = 0
    for lesson_id, user_id in candidates:
        with factory.begin() as db:
            lock_reader(db, user_id)
            outcome = db.get(LessonOutcome, lesson_id)
            if not outcome or outcome.decision not in ("absent", "leave", "cancelled"):
                continue
            from timetable_lifecycle import learning_lesson
            lesson = learning_lesson(db, user_id, lesson_id)
            key = f"outcome:{lesson.id}:{outcome.input_version}"
            if db.get(ClassIndexReceipt, (user_id, key)):
                continue
            settle_lesson(db, user_id, lesson.id, key, "absence" if outcome.decision == "absent" else "excluded",
                          evidence=outcome_evidence(lesson, outcome), now=now)
            count += 1
    return count


@router.get("/api/learning/incomplete")
def list_incomplete(user: CurrentUser, db: Db, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    rows = db.execute(select(LearningArchive, Lesson).join(Lesson, Lesson.id == LearningArchive.lesson_id).where(
        LearningArchive.user_id == user.id, LearningArchive.reopened_at.is_(None), Lesson.is_deleted.is_(False),
    ).order_by(LearningArchive.archived_at.desc(), Lesson.id.desc()).limit(limit)).all()
    return {"items": [{"lesson_id": str(lesson.id), "name": lesson.name, "reason": archive.reason,
                       "archived_at": archive.archived_at, "correction_deadline": lesson.ends_at + timedelta(days=7)}
                      for archive, lesson in rows]}
