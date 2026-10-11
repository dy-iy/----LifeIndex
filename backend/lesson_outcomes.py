"""Explicit post-class decisions. No activity segments or scores are invented."""
from db import IdempotencyKey, Input, Version, check_version, lock_reader, mutate, owned
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, or_, select

from auth import CurrentUser, Db
from models import Lesson, LessonOutcome, Review, StudySession
from learning_lifecycle import record_outcome
from rules import CORRECTION_WINDOW_DAYS
from timetable_actions import LessonResponse, lesson_result

router = APIRouter()


def utcnow():
    return datetime.now(timezone.utc)


class OutcomeInput(Input):
    lesson_version: Version
    version: int = Field(strict=True, ge=0)
    decision: Literal["makeup", "absent", "leave", "cancelled"]
    reason: str = Field(min_length=1, max_length=1000)
    reported_seconds: int | None = Field(default=None, strict=True, ge=0, le=86400)
    session_version: Version | None = None

    @model_validator(mode="after")
    def duration_matches_decision(self):
        if (self.decision == "makeup") != (self.reported_seconds is not None):
            raise ValueError("只有人工补记必须且可以提供自报时长。")
        return self


class NoteInput(Input):
    lesson_version: Version
    version: int = Field(strict=True, ge=0)
    note: str = Field(min_length=1, max_length=1000)


class OutcomeResponse(BaseModel):
    lesson: LessonResponse
    version: int
    decision: Literal["pending", "makeup", "absent", "leave", "cancelled"]
    reason: str
    note: str
    confirmed_at: datetime | None
    updated_at: datetime | None
    deadline: datetime
    deadline_local: str
    server_now: datetime
    can_confirm: bool
    can_correct: bool
    incomplete: bool
    input_version: int
    session_id: str | None
    reported_seconds: int | None
    session_version: int | None = None
    timed_record: bool = False
    settlement: dict | None = None


class OutcomeListResponse(BaseModel):
    items: list[OutcomeResponse]
    next_cursor: str | None


def response(lesson, outcome, study, now, settlement=None):
    deadline = lesson.ends_at + timedelta(days=CORRECTION_WINDOW_DAYS)
    return OutcomeResponse(lesson=lesson_result(lesson), version=outcome.version if outcome else 0,
        decision=outcome.decision if outcome else "pending", reason=outcome.reason if outcome else "",
        note=outcome.note if outcome else "", confirmed_at=outcome.confirmed_at if outcome else None,
        updated_at=outcome.updated_at if outcome else None, deadline=deadline, server_now=now,
        deadline_local=deadline.astimezone(ZoneInfo(lesson.timezone)).strftime("%Y-%m-%d %H:%M"),
        can_confirm=lesson.ends_at <= now <= deadline and (study is None or study.phase == "finished") and (outcome is None or outcome.decision == "pending"),
        can_correct=lesson.ends_at <= now <= deadline and bool(outcome and outcome.decision != "pending"),
        incomplete=now >= lesson.ends_at + timedelta(hours=48) and (outcome is None or outcome.decision == "pending"),
        input_version=outcome.input_version if outcome else 0,
        session_id=str(study.id) if study and not study.voided else None,
        reported_seconds=study.reported_seconds if study and not study.voided else None,
        session_version=study.version if study else None,
        timed_record=bool(study and study.reported_seconds is None and study.finish_reason != "untimed"),
        settlement=settlement,
    ).model_dump(mode="json")


def records(db, user_id, lesson_id):
    lesson = owned(db, Lesson, user_id, lesson_id)
    outcome = db.scalar(select(LessonOutcome).where(LessonOutcome.user_id == user_id, LessonOutcome.lesson_id == lesson_id)
                        .execution_options(populate_existing=True))
    study = db.scalar(select(StudySession).where(StudySession.user_id == user_id, StudySession.lesson_id == lesson_id))
    return lesson, outcome, study


def eligible_lesson(lesson, outcome, study, now):
    if now < lesson.ends_at:
        raise HTTPException(409, "课程尚未结束，请使用正常计时或课表调整。")
    if lesson.status != "scheduled":
        raise HTTPException(409, "该课程已在课表中请假或停课，无需重复确认。")
    if study is not None and study.phase != "finished":
        raise HTTPException(409, "请先结束本次计时，再确认或更正参与情况。")


@router.get("/api/lesson-outcomes", response_model=OutcomeListResponse)
def list_outcomes(user: CurrentUser, db: Db, status: Literal["pending", "confirmed", "all"] = "pending",
                  limit: Annotated[int, Query(ge=1, le=50)] = 20,
                  cursor: Annotated[str | None, Query(max_length=100)] = None):
    lock_reader(db, user.id)
    now = utcnow()
    query = select(Lesson, LessonOutcome, StudySession).outerjoin(LessonOutcome, and_(
        LessonOutcome.lesson_id == Lesson.id, LessonOutcome.user_id == Lesson.user_id,
    )).outerjoin(StudySession, and_(StudySession.lesson_id == Lesson.id, StudySession.user_id == Lesson.user_id)).where(
        Lesson.user_id == user.id, Lesson.status == "scheduled", Lesson.is_deleted.is_(False), Lesson.ends_at <= now,
        or_(StudySession.id.is_(None), LessonOutcome.lesson_id.is_not(None)),
    )
    if status == "pending":
        query = query.where(or_(LessonOutcome.lesson_id.is_(None), LessonOutcome.decision == "pending"))
    elif status == "confirmed":
        query = query.where(LessonOutcome.decision != "pending")
    if cursor:
        try:
            stamp, identifier = cursor.split("|", 1)
            before, before_id = datetime.fromisoformat(stamp), uuid.UUID(identifier)
            if before.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "记录游标无效，请刷新。") from None
        query = query.where(or_(Lesson.ends_at < before, and_(Lesson.ends_at == before, Lesson.id < before_id)))
    rows = db.execute(query.order_by(Lesson.ends_at.desc(), Lesson.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    last = page[-1][0] if len(rows) > limit else None
    return {"items": [response(lesson, outcome, study, now) for lesson, outcome, study in page],
            "next_cursor": f"{last.ends_at.isoformat()}|{last.id}" if last else None}


@router.get("/api/lessons/{lesson_id}/outcome", response_model=OutcomeResponse)
def get_outcome(lesson_id: uuid.UUID, user: CurrentUser, db: Db):
    lock_reader(db, user.id)
    lesson, outcome, study = records(db, user.id, lesson_id)
    now = utcnow()
    eligible_lesson(lesson, outcome, study, now)
    return response(lesson, outcome, study, now)


def base_write(db, user, lesson_id, body):
    lesson, outcome, study = records(db, user.id, lesson_id)
    now = utcnow()
    eligible_lesson(lesson, outcome, study, now)
    check_version(lesson, body.lesson_version)
    if body.version != (outcome.version if outcome else 0):
        raise HTTPException(409, "课后记录已更新，请刷新核对后再操作。")
    return lesson, outcome, study, now


def save_record(db, user, lesson, outcome, now):
    if outcome is None:
        outcome = LessonOutcome(lesson_id=lesson.id, user_id=user.id, version=0, input_version=0, decision="pending", reason="", note="")
        db.add(outcome)
    outcome.version += 1
    outcome.updated_at = now
    # Also invalidates old timetable/agent previews; history stays immutable.
    lesson.version += 1
    return outcome


def decide(db, user, lesson_id, body, correcting):
    lesson, outcome, study, now = base_write(db, user, lesson_id, body)
    confirmed = bool(outcome and outcome.decision != "pending")
    if correcting != confirmed:
        raise HTTPException(409, "课后记录状态已变化，请刷新后使用确认或纠错入口。")
    if now > lesson.ends_at + timedelta(days=CORRECTION_WINDOW_DAYS):
        raise HTTPException(409, "已超过课后 168 小时，仅可保存备注，不能更改参与情况。")
    if (study and study.reported_seconds is None and study.finish_reason != "untimed"
            and body.session_version != study.version):
        raise HTTPException(409, "该课程已有计时，请核对原计时版本后明确确认更正参与情况。")
    if study and body.session_version is not None and body.session_version != study.version:
        raise HTTPException(409, "原计时已更新，请刷新后再更正。")
    if body.reported_seconds is not None and body.reported_seconds > int((lesson.ends_at - lesson.starts_at).total_seconds()):
        raise HTTPException(422, "自报时长不能超过课程计划时长。")
    if confirmed and outcome.decision == body.decision and (
            body.decision != "makeup" or study.reported_seconds == body.reported_seconds):
        raise HTTPException(409, "参与情况和时长没有变化；文字说明请通过补充备注保存，无需重新评分。")
    return apply_decision(db, user, lesson, outcome, study, now, body.decision, body.reason, body.reported_seconds)


def apply_decision(db, user, lesson, outcome, study, now, decision, reason, reported_seconds=None, *, automatic_job=None):
    """Shared atomic write; caller owns the User lock and validates its input versions."""
    outcome = save_record(db, user, lesson, outcome, now)
    outcome.input_version += 1
    outcome.decision, outcome.reason = decision, reason
    outcome.confirmed_at = outcome.confirmed_at or now
    review = db.get(Review, study.id) if study else None
    restoring_automatic_absence = bool(review and review.outcome_evaluation_id and study.voided
                                      and decision == "makeup" and automatic_job is None)
    if study:
        study.version += 1
        study.voided = decision != "makeup"
        if decision == "makeup":
            study.is_makeup = True
            study.reported_seconds = reported_seconds
        if review:
            review.version += 1
            review.updated_at = now
            review.outcome_evaluation_id = automatic_job.id if automatic_job else None
            if restoring_automatic_absence:
                # The saved absence reason is not a new learning answer. Its
                # original text remains in the frozen job/outcome revision.
                review.status, review.submitted_at = "draft", None
                review.answers, review.review_context = [""], {}
        # Preserve audit records; an in-flight model response cannot restore a
        # withdrawn or changed duration because both input versions are fenced.
        from evaluation_models import EvaluationJob
        for job in db.scalars(select(EvaluationJob).where(EvaluationJob.session_id == study.id,
                EvaluationJob.status.in_(["queued", "running", "needs_input", "hold", "failed", "invalid_answer"]))):
            if automatic_job is not None and job.id == automatic_job.id:
                continue
            job.status, job.error = "superseded", "课后记录已纠错，请查看当前版本。"
            job.version += 1
            job.lease_owner = job.lease_until = None
            job.updated_at = now
    elif decision == "makeup":
        study = StudySession(user_id=user.id, lesson_id=lesson.id, name=lesson.name, location=lesson.location,
            timezone=lesson.timezone, starts_at=lesson.starts_at, ends_at=lesson.ends_at, phase="finished", version=1,
            started_at=now, finished_at=now, is_makeup=True, reported_seconds=reported_seconds, voided=False)
        db.add(study)
    db.flush()
    evaluation = None if automatic_job is None else {
        "id": str(automatic_job.id), "source": "review_agent", "input_hash": automatic_job.input_hash,
        "input_snapshot": automatic_job.input_snapshot, "assessment": automatic_job.assessment,
        "model_version": automatic_job.model_version, "prompt_version": automatic_job.prompt_version,
        "workflow_version": automatic_job.workflow_version, "agent_context": automatic_job.agent_context}
    settlement = record_outcome(db, lesson, outcome, study, now, evaluation=evaluation)
    if review and decision == "makeup" and review.status == "pending_evaluation":
        from evaluation_api import enqueue_evaluation
        enqueue_evaluation(db, user.id, review, study, now)
    db.flush()
    return response(lesson, outcome, study, now, settlement)


@router.post("/api/lessons/{lesson_id}/outcome", response_model=OutcomeResponse)
def confirm_outcome(lesson_id: uuid.UUID, body: OutcomeInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    return mutate(db, user, request, key, body, 200, lambda: decide(db, user, lesson_id, body, False))


@router.patch("/api/lessons/{lesson_id}/outcome", response_model=OutcomeResponse)
def correct_outcome(lesson_id: uuid.UUID, body: OutcomeInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    return mutate(db, user, request, key, body, 200, lambda: decide(db, user, lesson_id, body, True))


@router.patch("/api/lessons/{lesson_id}/outcome/note", response_model=OutcomeResponse)
def save_note(lesson_id: uuid.UUID, body: NoteInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        lesson, outcome, study, now = base_write(db, user, lesson_id, body)
        outcome = save_record(db, user, lesson, outcome, now)
        outcome.note = body.note
        db.flush()
        return response(lesson, outcome, study, now)
    return mutate(db, user, request, key, body, 200, action)
