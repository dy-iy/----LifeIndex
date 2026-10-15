"""Member-only chat and frozen review results with transactional send receipts."""
import json
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select

from auth import CurrentUser, Db, hash_token, unauthorized, utcnow
from db import IdempotencyKey, mutate
from models import AuthSession
from study_group_models import StudyGroup, StudyGroupMember, StudyGroupMessage
from study_groups import group_not_found, lock_group

router = APIRouter()
MESSAGES_PER_MINUTE = 30
MAX_SEQUENCE = 2 ** 63 - 1
Cursor = Annotated[str | None, Query(max_length=19, pattern=r"^(0|[1-9][0-9]{0,18})$")]


class GroupMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text"] = "text"
    content: str = Field(strict=True, min_length=1, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def validate_unicode(cls, value):
        try:
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            # FastAPI's standard validation response includes the raw input;
            # isolated surrogates cannot be encoded there or saved by PostgreSQL.
            raise HTTPException(422, "消息包含无效字符。") from None
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value):
        if value is None:
            return value
        if not value.strip():
            raise ValueError("消息不能为空。")
        if any((ord(char) < 32 or 127 <= ord(char) <= 159) and char not in "\n\t"
               for char in value):
            raise ValueError("消息不能包含无效控制字符。")
        return value

class GroupMessageResponse(BaseModel):
    id: uuid.UUID
    sequence: str
    sender_id: uuid.UUID
    sender_name: str
    sender_avatar_url: str | None
    client_key: str | None
    content: str
    kind: Literal["text", "review"] = "text"
    review_summary: dict | None = None
    created_at: datetime
    is_self: bool


class GroupMessagePage(BaseModel):
    items: list[GroupMessageResponse]
    next_before: str | None
    next_after: str | None
    has_more: bool


def message_result(message, user_id):
    return GroupMessageResponse(
        id=message.id, sequence=str(message.sequence), sender_id=message.sender_id,
        sender_name=message.sender_name, sender_avatar_url=message.sender_avatar_url,
        client_key=message.client_key if message.sender_id == user_id else None,
        content=message.content, created_at=message.created_at,
        kind=message.kind, review_summary=message.review_summary,
        is_self=message.sender_id == user_id,
    ).model_dump(mode="json")


def require_membership(db, group_id, user_id):
    # get_db supplies a repeatable-read snapshot to GET requests, so membership
    # and message reads cannot observe different versions of the group.
    accessible = db.scalar(select(StudyGroup.id).join(
        StudyGroupMember, StudyGroupMember.group_id == StudyGroup.id).where(
        StudyGroup.id == group_id, StudyGroup.status == "active",
        StudyGroupMember.user_id == user_id, StudyGroupMember.status == "active"))
    if accessible is None:
        raise group_not_found()


def lock_current_auth(db, user_id, request):
    """Hold the authenticated token against logout/rotation through commit."""
    authorization = request.headers.get("authorization", "").split(" ", 1)
    if len(authorization) != 2 or authorization[0].lower() != "bearer":
        raise unauthorized()
    session = db.scalar(select(AuthSession).where(
        AuthSession.user_id == user_id, AuthSession.token_hash == hash_token(authorization[1]))
        .with_for_update(read=True).execution_options(populate_existing=True))
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        raise unauthorized()


def shared_review_snapshot(db, user_id, session_id, review_version, now):
    """Reuse personal result calculations, then freeze only the shared UI fields.

    The completion transaction holds the sender's User lock, so the review,
    timer and settled evaluation cannot change while this snapshot is built.
    No personal review note, timer segments, location, agent evidence or private
    model/workflow context is stored in a group message.
    """
    from evaluation_api import records, response
    from reviews import result
    from study_sessions import session_segments

    study, review, job = records(db, user_id, session_id, reading=True)
    if review is None or review.status != "pending_evaluation":
        raise HTTPException(409, "请先完成并提交这节课的记录。")
    if review.version != review_version:
        raise HTTPException(409, "记录已更新，请刷新后重新选择。")
    if job is None or job.status != "succeeded":
        raise HTTPException(409, "记录结果尚未完成，请等待结算。")

    evaluation = response(study, review, job, now)["evaluation"]
    exempt = (evaluation.get("outcome") or {}).get("decision") in {"leave", "cancelled"}
    if not evaluation.get("settlement") and not exempt:
        raise HTTPException(409, "记录尚未结算，请稍后再发送。")
    personal = result(study, review, session_segments(db, user_id, session_id), now, job, db=db)
    session = personal["session"]
    summary = {"session_id": str(study.id), "review_version": review.version,
               "evaluation_id": str(job.id), "evaluation_version": job.version,
               "course_name": session["name"], "local_date": session["local_date"],
               "start_time": session["start_time"], "end_time": session["end_time"],
               "starts_at": session["starts_at"], "ends_at": session["ends_at"],
               "effective_seconds": session["effective_seconds"]}
    shared = {name: personal[name] for name in (
        "question_version", "answers", "review_context", "voided", "version", "status")}
    shared["session"] = {name: session[name] for name in (
        "id", "name", "local_date", "start_time", "end_time", "effective_seconds", "source")}
    safe_evaluation = {name: evaluation[name] for name in (
        "id", "session_id", "review_version", "version", "status")}
    if evaluation.get("result_review_version") is not None:
        safe_evaluation["result_review_version"] = evaluation["result_review_version"]
    safe_evaluation["outcome"] = evaluation.get("outcome")
    safe_evaluation["signal"] = {"direction": evaluation["signal"].get("direction")} if evaluation.get("signal") else None
    report = (evaluation.get("managers") or {}).get("long_short")
    safe_evaluation["managers"] = {"long_short": {"direction": report.get("direction")}} if report else None
    settlement = evaluation.get("settlement")
    settlement_fields = (
        "revision", "kind", "is_correction", "business_date", "timezone", "created_at", "lesson_name",
        "price_before", "price_after", "previous_close", "change_points", "change_percent", "raw_points",
        "clipped_points", "rounding_points", "raw_rate", "quality", "duration_ratio", "participation_score",
        "makeup", "target_points", "previous_target_points", "effective_seconds", "planned_seconds",
        "lesson_starts_at", "lesson_ends_at", "original_price_before", "original_previous_close",
        "cumulative_actual_points", "cumulative_discarded_points")
    safe_evaluation["settlement"] = {name: settlement[name] for name in settlement_fields if name in settlement} if settlement else None
    return summary, {"review": shared, "evaluation": {"evaluation": safe_evaluation, "can_revise": False}}


@router.get("/api/groups/{group_id}/messages/{message_id}/review")
def get_group_message_review(group_id: uuid.UUID, message_id: uuid.UUID, user: CurrentUser, db: Db,
                             response: Response):
    require_membership(db, group_id, user.id)
    message = db.scalar(select(StudyGroupMessage).where(
        StudyGroupMessage.id == message_id, StudyGroupMessage.group_id == group_id,
        StudyGroupMessage.kind == "review"))
    if message is None or message.review_snapshot is None:
        raise HTTPException(404, "记录消息不存在。")
    response.headers["Cache-Control"] = "no-store"
    return {**message.review_snapshot,
            "sender": {"id": str(message.sender_id), "name": message.sender_name,
                       "avatar_url": message.sender_avatar_url},
            "shared_at": message.created_at.isoformat()}


@router.get("/api/groups/{group_id}/messages", response_model=GroupMessagePage)
def list_group_messages(group_id: uuid.UUID, user: CurrentUser, db: Db,
                        limit: Annotated[int, Query(ge=1, le=100)] = 50,
                        before: Cursor = None, after: Cursor = None):
    if before is not None and after is not None:
        raise HTTPException(422, "历史翻页与新消息刷新不能同时使用。")
    cursor = int(before if before is not None else after) if before is not None or after is not None else None
    if cursor is not None and cursor > MAX_SEQUENCE:
        raise HTTPException(422, "消息游标无效。")
    require_membership(db, group_id, user.id)
    query = select(StudyGroupMessage).where(StudyGroupMessage.group_id == group_id)
    if after is not None:
        query = query.where(StudyGroupMessage.sequence > cursor).order_by(StudyGroupMessage.sequence)
    else:
        if before is not None:
            query = query.where(StudyGroupMessage.sequence < cursor)
        query = query.order_by(StudyGroupMessage.sequence.desc())
    messages = db.scalars(query.limit(limit + 1)).all()
    has_more = len(messages) > limit
    messages = messages[:limit]
    if after is None:
        messages.reverse()
    return {"items": [message_result(message, user.id) for message in messages],
            "next_before": str(messages[0].sequence) if messages else None,
            "next_after": str(messages[-1].sequence) if messages else None,
            "has_more": has_more}


@router.post("/api/groups/{group_id}/messages", status_code=201, response_model=GroupMessageResponse)
def send_group_message(group_id: uuid.UUID, body: GroupMessageCreate, request: Request,
                       user: CurrentUser, db: Db, key: IdempotencyKey):
    # Reserve the automatic receipt namespace even for guessed or stale clients.
    if key.startswith("auto-review:"):
        raise HTTPException(422, "消息请求标识使用了保留前缀。")

    def action():
        group, membership = lock_group(db, group_id, user.id, request)
        lock_current_auth(db, user.id, request)
        now = utcnow()
        # mutate owns the sender's user lock, so this database-backed limit is
        # shared by all workers and also serializes sends to different groups.
        recent = db.scalar(select(func.count()).select_from(StudyGroupMessage).where(
            StudyGroupMessage.sender_id == user.id,
            StudyGroupMessage.kind == "text",
            StudyGroupMessage.created_at >= now - timedelta(minutes=1)))
        if recent >= MESSAGES_PER_MINUTE:
            raise HTTPException(429, "发送太频繁，请稍后再试。", headers={"Retry-After": "60"})
        message = StudyGroupMessage(
            group_id=group.id, sender_id=user.id, client_key=key,
            content=body.content, kind="text",
            sender_name=membership.nickname or user.display_name or "群成员",
            sender_avatar_url=user.avatar_url, created_at=now,
        )
        db.add(message)
        db.flush()
        # Keep only a pointer in the persisted receipt, not readable message data.
        return {"message_id": str(message.id)}

    def render(result, replayed):
        # Replays must lose access after leave, removal, dissolution, or logout.
        lock_group(db, group_id, user.id, request)
        lock_current_auth(db, user.id, request)
        message = db.scalar(select(StudyGroupMessage).where(
            StudyGroupMessage.id == uuid.UUID(result["message_id"]),
            StudyGroupMessage.group_id == group_id))
        if message is None:
            raise HTTPException(404, "消息不存在。")
        return message_result(message, user.id)

    # Chat remains available independently of the group-management write switch.
    return mutate(db, user, request, key, body, 201, action,
                  no_key_update=True, render_response=render)
