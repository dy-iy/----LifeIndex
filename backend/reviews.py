"""Owned cloud drafts and atomically enqueued, versioned evaluation inputs."""
from db import IdempotencyKey, lock_reader, mutate, owned
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from time import monotonic

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, or_, select, exists

from auth import CurrentUser, Db
from models import Review, StudySegment, StudySession
from rules import CORRECTION_WINDOW_DAYS
from review_flow import QUESTION_VERSION, ReviewContext
from review_records import REVIEW_RETENTION, ensure_recent_reviews
from study_sessions import StudyResponse, close_expired_sessions, session_result, session_segments

router = APIRouter()
Answer = Annotated[str, Field(strict=True, max_length=8000)]


def utcnow():
    return datetime.now(timezone.utc)


class ReviewInput(BaseModel):
    # Preserve the actual answer, including meaningful whitespace, across retries.
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=0)
    question_version: Literal["class-review-v1", "class-review-v2"]
    answers: list[Answer] = Field(min_length=1, max_length=3)
    review_context: ReviewContext = Field(default_factory=ReviewContext)

    @model_validator(mode="after")
    def match_questions(self):
        expected = 1 if self.question_version == QUESTION_VERSION else 3
        if len(self.answers) != expected:
            raise ValueError("答案数量与复盘版本不一致。")
        if self.question_version != QUESTION_VERSION and self.review_context != ReviewContext():
            raise ValueError("旧版三问不接受新版参与情况。")
        if self.question_version != QUESTION_VERSION and any(len(answer) > 2000 for answer in self.answers):
            raise ValueError("旧版每项答案最多 2000 字。")
        return self


def validate_submission(body):
    if body.question_version == QUESTION_VERSION and not body.answers[0].strip():
        raise HTTPException(422, "请填写当前问题的回答。")
    if body.question_version != QUESTION_VERSION and any(not answer.strip() for answer in body.answers):
        raise HTTPException(422, "请填写全部三项复盘后再提交，或切换到新版情况记录。")


def input_context(body):
    context = body.review_context.model_dump() if body.question_version == QUESTION_VERSION else {}
    # Preserve older input identities when optional choices were not recorded.
    if context.get("focus") == "unspecified":
        context.pop("focus")
    if context.get("untimed_reason") == "unspecified":
        context.pop("untimed_reason")
    return context


class ReviewResponse(BaseModel):
    session: StudyResponse
    question_version: str = QUESTION_VERSION
    version: int
    answers: list[str]
    status: Literal["draft", "pending_evaluation"]
    updated_at: datetime | None
    submitted_at: datetime | None
    can_submit: bool
    incomplete: bool
    correction_deadline: datetime
    review_context: dict = Field(default_factory=dict)
    evaluation_summary: dict | None = None
    note: str = ""
    note_version: int = 0
    voided: bool = False
    evaluation: dict | None = None


class ReviewSessionSummary(BaseModel):
    id: str
    name: str
    local_date: str
    start_time: str
    end_time: str
    ends_at: str
    finished_at: str | None
    effective_seconds: int
    version: int
    source: str


class ReviewSummary(BaseModel):
    session: ReviewSessionSummary
    version: int
    status: str
    question_version: str
    summary: str
    voided: bool
    evaluation_summary: dict | None


class ReviewListResponse(BaseModel):
    items: list[ReviewResponse | ReviewSummary]
    next_cursor: str | None


def result(item, review, segments, now, job=None):
    return ReviewResponse(
        question_version=review.question_version if review else QUESTION_VERSION,
        session=session_result(item, segments, now, review), version=review.version if review else 0,
        answers=review.answers if review else [""], status=review.status if review else "draft",
        updated_at=review.updated_at if review else None, submitted_at=review.submitted_at if review else None,
        can_submit=not item.voided and (review is None or review.status == "draft") and item.ends_at <= now <= item.ends_at + timedelta(days=CORRECTION_WINDOW_DAYS),
        incomplete=(review is None or review.status == "draft" or job is not None and job.status == "invalid_answer") and now >= item.ends_at + timedelta(hours=48),
        correction_deadline=item.ends_at + timedelta(days=CORRECTION_WINDOW_DAYS),
        review_context=review.review_context if review else {}, note=review.note if review else "",
        note_version=review.note_version if review else 0,
        voided=item.voided,
        evaluation_summary={"id": str(job.id), "status": job.status, "version": job.version,
                            "review_version": job.review_version} if job else None,
    ).model_dump(mode="json")


def finished(db, user_id, session_id, *, reading=False):
    item = owned(db, StudySession, user_id, session_id)
    if item.voided and not reading:
        raise HTTPException(409, "这节课已改为缺席、请假或停课，请查看最新课后记录。")
    if item.phase != "finished":
        raise HTTPException(409, "请先结束计时，再填写复盘。")
    return item


def find_review(db, user_id, session_id):
    return db.scalar(select(Review).where(Review.user_id == user_id, Review.session_id == session_id)
                     .execution_options(populate_existing=True))


def timed_read_lock(db, user_id, request):
    started = monotonic()
    lock_reader(db, user_id)
    request.state.review_lock_ms = (monotonic() - started) * 1000


@router.get("/api/reviews", response_model=ReviewListResponse)
def list_reviews(user: CurrentUser, db: Db, request: Request, status: Literal["all", "pending", "submitted"] = "all",
                 limit: Annotated[int, Query(ge=1, le=50)] = 20,
                 cursor: Annotated[str | None, Query(max_length=100)] = None, compact: bool = False):
    timed_read_lock(db, user.id, request)
    now = utcnow()
    close_expired_sessions(db, user.id, now)
    ensure_recent_reviews(db, user.id, now)
    query = select(StudySession, Review).outerjoin(Review, and_(
        Review.session_id == StudySession.id, Review.user_id == StudySession.user_id,
    )).where(StudySession.user_id == user.id, StudySession.phase == "finished",
             StudySession.ends_at >= now - REVIEW_RETENTION)
    from evaluation_models import EvaluationJob
    invalid = exists(select(EvaluationJob.id).where(EvaluationJob.session_id == StudySession.id,
        EvaluationJob.user_id == user.id, EvaluationJob.review_version == Review.version,
        EvaluationJob.session_version == StudySession.version, EvaluationJob.status == "invalid_answer"))
    if status == "pending":
        query = query.where(StudySession.voided.is_(False), or_(Review.session_id.is_(None), Review.status == "draft", invalid))
    elif status == "submitted":
        query = query.where(Review.status == "pending_evaluation", ~invalid)
    if cursor:
        try:
            timestamp, identifier = cursor.split("|", 1)
            before, before_id = datetime.fromisoformat(timestamp), uuid.UUID(identifier)
            if before.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "历史记录游标无效，请刷新列表。") from None
        query = query.where(or_(StudySession.finished_at < before,
                                and_(StudySession.finished_at == before, StudySession.id < before_id)))
    rows = db.execute(query.order_by(StudySession.finished_at.desc(), StudySession.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    segments = {item.id: [] for item, _ in page}
    if segments:
        for row in db.scalars(select(StudySegment).where(
            StudySegment.user_id == user.id, StudySegment.session_id.in_(segments),
        ).order_by(StudySegment.start_at, StudySegment.id)):
            segments[row.session_id].append(row)
    last = page[-1][0] if len(rows) > limit else None
    from evaluation_models import EvaluationJob
    jobs = {(job.session_id, job.review_version, job.session_version): job for job in db.execute(
        select(EvaluationJob.id, EvaluationJob.session_id, EvaluationJob.review_version, EvaluationJob.session_version,
               EvaluationJob.version, EvaluationJob.status).join(Review, and_(Review.session_id == EvaluationJob.session_id,
               Review.version == EvaluationJob.review_version)).join(StudySession, and_(StudySession.id == EvaluationJob.session_id,
               StudySession.version == EvaluationJob.session_version)).where(EvaluationJob.user_id == user.id,
               EvaluationJob.session_id.in_(segments)))} if segments else {}
    linked_ids = [review.outcome_evaluation_id for _, review in page if review and review.outcome_evaluation_id]
    linked = {job.id: job for job in db.execute(select(
        EvaluationJob.id, EvaluationJob.version, EvaluationJob.status, EvaluationJob.review_version).where(
        EvaluationJob.user_id == user.id, EvaluationJob.id.in_(linked_ids), EvaluationJob.status == "succeeded"))} if linked_ids else {}
    items = []
    for item, review in page:
        job = (linked.get(review.outcome_evaluation_id) or jobs.get((item.id, review.version, item.version))) if review else None
        if compact:
            summary = session_result(item, segments[item.id], now, review, include_segments=False)
            items.append(ReviewSummary(
                session=ReviewSessionSummary(**summary), version=review.version if review else 0,
                status=review.status if review else "draft", question_version=review.question_version if review else QUESTION_VERSION,
                summary=" ".join(review.answers).strip()[:80] if review else "", voided=item.voided,
                evaluation_summary={"id": str(job.id), "status": job.status, "version": job.version,
                                    "review_version": job.review_version} if job else None,
            ).model_dump(mode="json"))
        else:
            items.append(result(item, review, segments[item.id], now, job))
    next_cursor = f"{last.finished_at.isoformat()}|{last.id}" if last else None
    db.commit()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/api/reviews/{session_id}", response_model=ReviewResponse)
def get_review(session_id: uuid.UUID, user: CurrentUser, db: Db, request: Request):
    item = finished(db, user.id, session_id, reading=True)
    review = find_review(db, user.id, session_id)
    from evaluation_api import latest_job, response
    job = latest_job(db, user.id, item, review)
    now = utcnow()
    return {**result(item, review, session_segments(db, user.id, session_id), now, job),
            "evaluation": response(item, review, job, now)}


def write_review(db, user, session_id, body, submit):
    item = finished(db, user.id, session_id)
    review = find_review(db, user.id, session_id)
    if body.version != (review.version if review else 0):
        raise HTTPException(409, "云端复盘已更新，请核对两份答案后再保存。")
    if review and review.status != "draft":
        raise HTTPException(409, "复盘已提交，当前不支持修改已提交的答案。")
    if submit:
        validate_submission(body)
    now = utcnow()
    if submit:
        if now < item.ends_at:
            raise HTTPException(409, "课程尚未结束，请等待下课后提交复盘；当前仍可继续计时。")
        from evaluation_api import require_window
        require_window(item, now)
    if review is None:
        review = Review(session_id=session_id, user_id=user.id, version=0, question_version=body.question_version)
        db.add(review)
    review.answers = body.answers
    review.question_version = body.question_version
    review.review_context = input_context(body)
    review.version += 1
    review.status = "pending_evaluation" if submit else "draft"
    review.updated_at = now
    review.submitted_at = now if submit else None
    db.flush()
    if submit:
        from evaluation_api import enqueue_evaluation
        from learning_lifecycle import reopen_lesson
        enqueue_evaluation(db, user.id, review, item, now)
        reopen_lesson(db, user.id, item.lesson_id, now)
    return result(item, review, session_segments(db, user.id, session_id), now)


@router.put("/api/reviews/{session_id}", response_model=ReviewResponse)
def save_review(session_id: uuid.UUID, body: ReviewInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    return mutate(db, user, request, key, body, 200, lambda: write_review(db, user, session_id, body, False))


@router.post("/api/reviews/{session_id}/submit", response_model=ReviewResponse)
def submit_review(session_id: uuid.UUID, body: ReviewInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    return mutate(db, user, request, key, body, 200, lambda: write_review(db, user, session_id, body, True))


class ReflectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=0)
    note: str = Field(max_length=2000)


@router.patch("/api/reviews/{session_id}/note", response_model=ReviewResponse)
def save_reflection(session_id: uuid.UUID, body: ReflectionInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        item = owned(db, StudySession, user.id, session_id)
        review = find_review(db, user.id, session_id)
        if not review:
            raise HTTPException(409, "请先保存本次记录，再补充心得。")
        if review.note_version != body.version:
            raise HTTPException(409, "补充心得已更新，请刷新后核对。")
        review.note, review.note_version = body.note, review.note_version + 1
        db.flush()
        return result(item, review, session_segments(db, user.id, session_id), utcnow())
    return mutate(db, user, request, key, body, 200, action)
