"""Server-clock timers with durable segments, ownership and retryable transitions."""
from db import IdempotencyKey, Input, Version, check_version, mutate, owned
from db import lock_reader
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from auth import CurrentUser, Db
from models import Lesson, Review, StudySegment, StudySession, User
from market_engine import reconcile_active_timer, timer_action
from timetable_actions import LessonResponse, lesson_result

router = APIRouter()
TIMING_GRACE = timedelta(minutes=10)


def utcnow():
    return datetime.now(timezone.utc)


class StartInput(Input):
    lesson_id: uuid.UUID


class TransitionInput(Input):
    version: Version


class FinishInput(TransitionInput):
    deferred: bool = Field(default=False, strict=True)
    requested_finish_at: AwareDatetime | None = None


class SegmentResponse(BaseModel):
    start_at: datetime
    end_at: datetime | None


class StudyResponse(BaseModel):
    id: str
    lesson_id: str
    phase: Literal["running", "paused", "finished"]
    version: int
    name: str
    location: str
    timezone: str
    local_date: date
    start_time: str
    end_time: str
    starts_at: datetime
    ends_at: datetime
    planned_seconds: int
    elapsed_seconds: int
    effective_seconds: int
    preparation_seconds: int = 0
    auto_finish_at: datetime | None = None
    finish_reason: Literal["user", "timeout", "offline", "repair", "untimed"] | None = None
    can_resume: bool = False
    server_now: datetime
    started_at: datetime
    finished_at: datetime | None
    is_makeup: bool
    source: Literal["timer", "manual", "untimed"]
    reported_seconds: int | None
    segments: list[SegmentResponse]


class StudyListResponse(BaseModel):
    server_now: datetime
    active: StudyResponse | None
    recent: list[StudyResponse]
    lessons: list[LessonResponse]


def merged_intervals(segments, now):
    """Union half-open intervals so duplicates/overlaps can never double count."""
    intervals = sorted((segment.start_at, min(segment.end_at or now, now)) for segment in segments)
    merged = []
    for start, end in intervals:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def durations(segments, now, starts_at, ends_at):
    intervals = merged_intervals(segments, now)
    elapsed = sum(max(0, (min(end, ends_at + TIMING_GRACE) - max(start, starts_at - TIMING_GRACE)).total_seconds())
                  for start, end in intervals)
    effective = sum(max(0, (min(end, ends_at) - max(start, starts_at)).total_seconds())
                    for start, end in intervals)
    return int(elapsed), min(int(effective), int((ends_at - starts_at).total_seconds()))


def resume_available(item, review, now):
    return (item.phase in ("paused", "finished") and not item.voided
            and item.reported_seconds is None and item.finish_reason != "untimed"
            and item.starts_at - TIMING_GRACE <= now < item.ends_at + TIMING_GRACE
            and (review is None or review.status == "draft"))


def session_result(item, segments, now, review=None, *, include_segments=True):
    elapsed, effective = durations(segments, now, item.starts_at, item.ends_at)
    preparation = int(sum(max(0, (min(end, item.starts_at) - max(start, item.starts_at - TIMING_GRACE)).total_seconds())
                          for start, end in merged_intervals(segments, now)))
    if item.reported_seconds is not None:
        elapsed = effective = item.reported_seconds
        preparation = 0
    local_start = item.starts_at.astimezone(ZoneInfo(item.timezone))
    local_end = item.ends_at.astimezone(ZoneInfo(item.timezone))
    return StudyResponse(
        id=str(item.id), lesson_id=str(item.lesson_id), phase=item.phase, version=item.version,
        name=item.name, location=item.location, timezone=item.timezone,
        local_date=local_start.date(), start_time=local_start.strftime("%H:%M"),
        end_time=local_end.strftime("%H:%M"), starts_at=item.starts_at.astimezone(timezone.utc),
        ends_at=item.ends_at.astimezone(timezone.utc),
        planned_seconds=int((item.ends_at - item.starts_at).total_seconds()),
        elapsed_seconds=elapsed, effective_seconds=effective, server_now=now.astimezone(timezone.utc),
        preparation_seconds=preparation, auto_finish_at=(item.ends_at + TIMING_GRACE).astimezone(timezone.utc),
        finish_reason=item.finish_reason,
        can_resume=resume_available(item, review, now),
        started_at=item.started_at.astimezone(timezone.utc),
        finished_at=item.finished_at.astimezone(timezone.utc) if item.finished_at else None,
        is_makeup=item.is_makeup,
        source="manual" if item.reported_seconds is not None else "untimed" if item.finish_reason == "untimed" else "timer",
        reported_seconds=item.reported_seconds,
        segments=[SegmentResponse(start_at=row.start_at.astimezone(timezone.utc),
                                  end_at=row.end_at.astimezone(timezone.utc) if row.end_at else None)
                  for row in segments] if include_segments else [],
    ).model_dump(mode="json")


def session_segments(db, user_id, session_id):
    return db.scalars(select(StudySegment).where(
        StudySegment.user_id == user_id, StudySegment.session_id == session_id,
    ).order_by(StudySegment.start_at, StudySegment.id)).all()




def check_timing_window(item, now):
    if not item.starts_at - TIMING_GRACE <= now < item.ends_at + TIMING_GRACE:
        raise HTTPException(409, "仅可在该课程开始前 10 分钟至结束后 10 分钟内操作计时。")


def last_event(item, segments):
    return max([item.started_at] + [row.end_at or row.start_at for row in segments])


def close_at_cutoff(item, segments, reason="timeout"):
    cutoff = max(item.started_at, item.ends_at + TIMING_GRACE)
    item.finish_context = {"phase": item.phase, "version": item.version,
                           "last_event_at": last_event(item, segments).isoformat(),
                           "open_segments": [str(row.id) for row in segments if row.end_at is None]}
    for segment in segments:
        if segment.end_at is None:
            segment.end_at = max(segment.start_at, cutoff)
    item.phase, item.finished_at, item.finish_reason = "finished", cutoff, reason
    item.version += 1


def close_expired_sessions(db, user_id, now, reason="timeout"):
    """Caller holds the user lock; reconcile idle/offline timers at their cutoff."""
    expired = db.scalars(select(StudySession).where(
        StudySession.user_id == user_id, StudySession.phase.in_(["running", "paused"]),
        StudySession.ends_at <= now - TIMING_GRACE,
    )).all()
    if expired:
        # Preserve the already-started market even if the first upgraded
        # request is a timer read that closes an expired legacy session.
        reconcile_active_timer(db, db.get(User, user_id), now)
    for item in expired:
        segments = session_segments(db, user_id, item.id)
        close_at_cutoff(item, segments, reason)
        activate_review_tracking(db, item, segments)
    db.flush()
    return len(expired)


def activate_review_tracking(db, item, segments):
    """Persist the first completed real timer; manual/empty records do not enroll users."""
    if (item.phase != "finished" or item.voided or item.reported_seconds is not None
            or item.finish_reason == "untimed"
            or durations(segments, item.finished_at, item.starts_at, item.ends_at)[1] <= 0):
        return
    user = db.scalar(select(User).where(User.id == item.user_id).execution_options(populate_existing=True))
    if user.review_tracking_started_at is None or item.finished_at < user.review_tracking_started_at:
        user.review_tracking_started_at = item.finished_at


def expire_sessions(factory, now=None, limit=100):
    """Bounded maintenance; isolate each owner's transaction and keep processing on failure."""
    now = now or utcnow()
    with factory() as db:
        owners = db.scalars(select(StudySession.user_id).where(
            StudySession.phase.in_(["running", "paused"]), StudySession.ends_at <= now - TIMING_GRACE,
        ).order_by(StudySession.ends_at, StudySession.id).limit(limit)).all()
    count = 0
    for user_id in owners:
        try:
            with factory.begin() as db:
                lock_reader(db, user_id)
                count += close_expired_sessions(db, user_id, now)
        except SQLAlchemyError:
            print("Timer maintenance transaction failed; retrying next round.", flush=True)
    return count


@router.get("/api/sessions", response_model=StudyListResponse)
def list_sessions(user: CurrentUser, db: Db):
    lock_reader(db, user.id)
    now = utcnow()
    close_expired_sessions(db, user.id, now)
    active = db.scalar(select(StudySession).where(
        StudySession.user_id == user.id, StudySession.phase.in_(["running", "paused"]),
    ))
    recent = db.scalars(select(StudySession).where(
        StudySession.user_id == user.id, StudySession.phase == "finished", StudySession.voided.is_(False),
    ).order_by(StudySession.finished_at.desc(), StudySession.id.desc()).limit(20)).all()
    items = ([active] if active is not None else []) + list(recent)
    segments = {item.id: [] for item in items}
    reviews = {}
    if items:
        reviews = {row.session_id: row for row in db.scalars(select(Review).where(
            Review.user_id == user.id, Review.session_id.in_(segments),
        )).all()}
        for row in db.scalars(select(StudySegment).where(
            StudySegment.user_id == user.id, StudySegment.session_id.in_(segments),
        ).order_by(StudySegment.start_at, StudySegment.id)).all():
            segments[row.session_id].append(row)
    result = {"server_now": now,
            "active": session_result(active, segments[active.id], now, reviews.get(active.id)) if active is not None else None,
            "recent": [session_result(item, segments[item.id], now, reviews.get(item.id)) for item in recent],
            "lessons": [lesson_result(item) for item in db.scalars(select(Lesson).where(
                Lesson.user_id == user.id, Lesson.is_deleted.is_(False), Lesson.status == "scheduled",
                Lesson.starts_at <= now + TIMING_GRACE, Lesson.ends_at > now - TIMING_GRACE,
            ).order_by(Lesson.starts_at, Lesson.id))]}
    db.commit()
    return result


@router.get("/api/sessions/{session_id}", response_model=StudyResponse)
def get_session(session_id: uuid.UUID, user: CurrentUser, db: Db):
    lock_reader(db, user.id)
    item = owned(db, StudySession, user.id, session_id)
    if item.voided:
        raise HTTPException(404, "该补记已撤回，请查看课程的课后记录。")
    now = utcnow()
    close_expired_sessions(db, user.id, now)
    result = session_result(item, session_segments(db, user.id, item.id), now, db.get(Review, item.id))
    db.commit()
    return result


@router.post("/api/sessions", response_model=StudyResponse, status_code=201)
def start_session(body: StartInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        lesson = owned(db, Lesson, user.id, body.lesson_id)
        now = utcnow()
        if lesson.status != "scheduled":
            raise HTTPException(409, "请假或停课的课程不能开始计时。")
        check_timing_window(lesson, now)
        close_expired_sessions(db, user.id, now)
        if db.scalar(select(StudySession.id).where(StudySession.lesson_id == lesson.id)) is not None:
            raise HTTPException(409, "这节课已有计时记录，请读取原记录。")
        if db.scalar(select(StudySession.id).where(
            StudySession.user_id == user.id, StudySession.phase.in_(["running", "paused"]),
        )) is not None:
            raise HTTPException(409, "已有进行中或暂停中的计时，请先结束当前记录。")
        item = StudySession(user_id=user.id, lesson_id=lesson.id, name=lesson.name,
                            location=lesson.location, timezone=lesson.timezone,
                            starts_at=lesson.starts_at, ends_at=lesson.ends_at,
                            phase="running", version=1, started_at=now, is_makeup=False)
        # Starting early still locks the occurrence, including stale AI previews.
        lesson.version += 1
        db.add(item)
        db.flush()
        segment = StudySegment(user_id=user.id, session_id=item.id, start_at=now)
        db.add(segment)
        db.flush()
        timer_action(db, user, item, now, "start")
        return session_result(item, [segment], now)
    return mutate(db, user, request, key, body, 201, action)


def transition(db, user, session_id, body, operation):
    item = owned(db, StudySession, user.id, session_id)
    if item.voided:
        raise HTTPException(404, "该补记已撤回，请查看课程的课后记录。")
    if operation == "finish":
        return finish_record(db, item, body, utcnow())
    check_version(item, body.version)
    expected = {"pause": ("running",), "resume": ("paused", "finished")}
    if item.phase not in expected[operation]:
        raise HTTPException(409, "计时状态已变化，请重新加载后操作。")
    now = utcnow()
    check_timing_window(item, now)
    segments = session_segments(db, user.id, item.id)
    review = db.get(Review, item.id)
    # Clamp rare backwards wall-clock adjustments to the last server event.
    now = max([now, item.started_at, item.finished_at or item.started_at]
              + [row.end_at or row.start_at for row in segments])
    if operation == "resume":
        if not resume_available(item, review, now):
            raise HTTPException(409, "该记录已提交复盘、属于人工补记或已超出计时时段，不能继续计时。")
        close_expired_sessions(db, user.id, now)
        if db.scalar(select(StudySession.id).where(
            StudySession.user_id == user.id, StudySession.id != item.id,
            StudySession.phase.in_(["running", "paused"]),
        )) is not None:
            raise HTTPException(409, "已有其他课程正在计时，请先结束当前计时。")
        if any(row.end_at is None for row in segments):
            raise HTTPException(409, "计时片段状态异常，请重新加载后重试。")
        segment = StudySegment(user_id=user.id, session_id=item.id, start_at=now)
        db.add(segment)
        segments.append(segment)
        item.phase = "running"
        item.finished_at, item.finish_reason, item.finish_context = None, None, None
    else:
        if item.phase == "running":
            open_segment = next((row for row in segments if row.end_at is None), None)
            if open_segment is None:
                raise HTTPException(409, "计时片段状态异常，请重新加载后重试。")
            open_segment.end_at = now
        item.phase = "paused"
    item.version += 1
    db.flush()
    timer_action(db, user, item, now, operation)
    return session_result(item, segments, now, review)


def validate_requested_finish(requested, earliest, now, cutoff):
    if not earliest <= requested <= min(now, cutoff):
        raise HTTPException(422, "离线结束时间与服务器记录不一致，已保留点击记录，请核对云端计时。")


def finish_record(db, item, body, now):
    if item.phase != "finished":
        reconcile_active_timer(db, db.get(User, item.user_id), now)
    segments = session_segments(db, item.user_id, item.id)
    review = db.get(Review, item.id)
    if item.finish_reason == "untimed":
        return session_result(item, segments, now, review)
    cutoff = item.ends_at + TIMING_GRACE
    requested = body.requested_finish_at
    use_requested = requested is not None and (body.deferred or now >= cutoff)
    if item.phase == "finished":
        if use_requested and requested < item.finished_at:
            context = item.finish_context or {}
            if (item.finish_reason != "timeout" or context.get("version") != body.version
                    or item.version != body.version + 1 or (review and review.status != "draft")):
                raise HTTPException(409, "云端记录已有后续操作，离线结束未覆盖；请核对已保存的云端记录。")
            validate_requested_finish(requested, datetime.fromisoformat(context["last_event_at"]), now, cutoff)
            for row in segments:
                if str(row.id) in context["open_segments"]:
                    row.end_at = requested
            item.finished_at, item.finish_reason, item.is_makeup = requested, "offline", True
            item.finish_context = None
            item.version += 1
            db.flush()
        activate_review_tracking(db, item, segments)
        timer_action(db, db.get(User, item.user_id), item, now, "finish")
        return session_result(item, segments, now, review)
    check_version(item, body.version)
    if use_requested:
        validate_requested_finish(requested, last_event(item, segments), now, cutoff)
    if now >= cutoff and (not use_requested or requested == cutoff):
        close_at_cutoff(item, segments)
    else:
        stopped_at = requested if use_requested else max(now, last_event(item, segments))
        for row in segments:
            if row.end_at is None:
                row.end_at = stopped_at
        item.phase, item.finished_at = "finished", stopped_at
        item.is_makeup = item.is_makeup or body.deferred or use_requested
        item.finish_reason = "offline" if body.deferred or use_requested else "user"
        item.finish_context = None
        item.version += 1
    db.flush()
    activate_review_tracking(db, item, segments)
    timer_action(db, db.get(User, item.user_id), item, now, "finish")
    return session_result(item, segments, now, review)


@router.post("/api/sessions/{session_id}/pause", response_model=StudyResponse)
def pause_session(session_id: uuid.UUID, body: TransitionInput, request: Request,
                  user: CurrentUser, db: Db, key: IdempotencyKey):
    return mutate(db, user, request, key, body, 200,
                  lambda: transition(db, user, session_id, body, "pause"))


@router.post("/api/sessions/{session_id}/resume", response_model=StudyResponse)
def resume_session(session_id: uuid.UUID, body: TransitionInput, request: Request,
                   user: CurrentUser, db: Db, key: IdempotencyKey):
    return mutate(db, user, request, key, body, 200,
                  lambda: transition(db, user, session_id, body, "resume"))


@router.post("/api/sessions/{session_id}/finish", response_model=StudyResponse)
def finish_session(session_id: uuid.UUID, body: FinishInput, request: Request,
                   user: CurrentUser, db: Db, key: IdempotencyKey):
    # Delivery metadata is deliberately excluded from the idempotency digest.
    # If the first response was lost, a deferred retry replays that success.
    # If the first request never arrived, its retry records a makeup finish.
    digest = {"version": body.version}
    if body.requested_finish_at is not None:
        digest["requested_finish_at"] = body.requested_finish_at.isoformat()
    return mutate(db, user, request, key, digest, 200,
                  lambda: transition(db, user, session_id, body, "finish"))
