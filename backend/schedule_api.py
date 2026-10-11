"""Authenticated conversation API. Model work is performed by a durable worker."""
from db import IdempotencyKey, Input, Version, check_version, mutate, owned
import uuid
import re
from datetime import timedelta
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field, model_validator
from sqlalchemy import func, select, update
from starlette.concurrency import run_in_threadpool

from auth import CurrentSession, CurrentUser, Db, unauthorized, utcnow
from models import AuthSession, ScheduleImage, ScheduleRun, ScheduleThread, Semester, User

router = APIRouter(prefix="/api/timetable-agent", tags=["课表助手"])
BUSY = {"queued", "running", "confirm_queued", "confirming"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


class ThreadCreate(Input):
    semester_id: uuid.UUID | None = None
    title: str = Field(default="课表对话", min_length=1, max_length=100)


class MessageCreate(Input):
    version: Version
    text: str = Field(default="", max_length=4000)
    image_ids: list[uuid.UUID] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.text and not self.image_ids:
            raise ValueError("请填写说明或选择课表图片。")
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError("请勿重复选择同一图片。")
        return self


class Decision(Input):
    version: Version
    decision: Literal["apply", "cancel"]


class Retry(Input):
    version: Version


def summary(thread):
    return {"id": str(thread.id), "semester_id": str(thread.semester_id) if thread.semester_id else None,
            "title": thread.title, "version": thread.version, "updated_at": thread.updated_at.isoformat()}


def runs_for(db, thread_id, limit=50):
    rows = db.scalars(select(ScheduleRun).where(ScheduleRun.thread_id == thread_id)
                      .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def latest_run(db, thread_id):
    rows = runs_for(db, thread_id, 1)
    return rows[0] if rows else None


def detail(db, thread):
    db.flush()
    rows = runs_for(db, thread.id)
    messages = []
    for row in rows:
        messages.append({"id": str(row.id) + ":user", "role": "user", "text": row.input_text,
                         "image_count": len(row.image_ids), "created_at": row.created_at.isoformat()})
        if row.confirmation_text:
            messages.append({"id": str(row.id) + ":confirmation", "role": "user", "text": row.confirmation_text,
                             "image_count": 0, "created_at": row.updated_at.isoformat()})
        if row.reply:
            messages.append({"id": str(row.id) + ":assistant", "role": "assistant", "text": row.reply,
                             "image_count": 0, "created_at": row.updated_at.isoformat()})
    run = rows[-1] if rows else None
    return {**summary(thread), "messages": messages, "has_more_messages": len(rows) == 50,
            "run": None if run is None else {
                "id": str(run.id), "status": run.status, "reply": run.reply, "error": run.error,
                "preview": run.preview, "created_at": run.created_at.isoformat(),
            }}


def touch(thread):
    thread.version += 1
    thread.updated_at = utcnow()


def confirms_preview(text, run):
    """Only an exact affirmative can approve a concrete, currently visible plan."""
    if not run or run.status != "awaiting_confirmation" or not run.preview:
        return False
    value = re.sub(r"[\s，。！!,.]", "", text)
    if value in {"确认", "确认执行", "确认导入", "确认更新", "执行", "应用", "确认应用",
                 "全部确认", "都确认", "没问题", "可以", "好的", "同意", "按这个方案执行"}:
        return True
    operations = (run.plan or {}).get("operations", [])
    return value in {"删除", "确认删除", "删掉", "直接删除"} and bool(operations) and all(
        item.get("kind") in {"delete_course", "delete_lesson"} for item in operations)


def queue_decision(db, thread, run, decision):
    from schedule_plan import snapshot_hash
    if run.status != "awaiting_confirmation" or not run.preview:
        raise HTTPException(409, "当前没有可以确认的方案，请读取最新对话。")
    if decision == "apply" and run.preview["baseline_hash"] != snapshot_hash(db, run.user_id, thread.semester_id,
            legacy=not run.preview["baseline_hash"].startswith("semester-v1:")):
        raise HTTPException(409, "课表已经改变，这份预览已过期。请发送说明，重新生成方案后确认。")
    run.status, run.decision, run.error = "confirm_queued", decision, ""
    run.updated_at = utcnow()
    touch(thread)


@router.get("/threads")
def list_threads(user: CurrentUser, db: Db, semester_id: uuid.UUID | None = None):
    query = select(ScheduleThread).where(ScheduleThread.user_id == user.id)
    if semester_id is not None:
        owned(db, Semester, user.id, semester_id)
        query = query.where(ScheduleThread.semester_id == semester_id)
    return [summary(item) for item in db.scalars(query.order_by(ScheduleThread.updated_at.desc(), ScheduleThread.id).limit(100)).all()]


@router.post("/threads", status_code=201)
def create_thread(body: ThreadCreate, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        if body.semester_id is not None:
            owned(db, Semester, user.id, body.semester_id)
        thread = ScheduleThread(user_id=user.id, semester_id=body.semester_id, title=body.title)
        db.add(thread)
        db.flush()
        return detail(db, thread)
    return mutate(db, user, request, key, body, 201, action)


@router.get("/threads/{thread_id}")
def get_thread(thread_id: uuid.UUID, user: CurrentUser, db: Db, known_version: int | None = Query(default=None, ge=1)):
    thread = owned(db, ScheduleThread, user.id, thread_id)
    if known_version == thread.version:
        return {"id": str(thread.id), "version": thread.version, "unchanged": True}
    return detail(db, thread)


@router.post("/threads/{thread_id}/messages", status_code=202)
def post_message(thread_id: uuid.UUID, body: MessageCreate, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        thread = owned(db, ScheduleThread, user.id, thread_id)
        check_version(thread, body.version)
        previous = latest_run(db, thread.id)
        if previous and previous.status in BUSY:
            raise HTTPException(409, "上一条请求仍在处理，请稍后读取结果。")
        if not body.image_ids and body.text.strip(" 。！!，,") in {"取消", "取消导入", "取消这次导入", "取消本次任务", "不用了"}:
            # Cancelling an unfinished task must not require model budget, a
            # readable upload, or a valid preview. Keep the original audit rows.
            if previous and previous.status == "awaiting_confirmation":
                previous.status = "superseded"
                previous.updated_at = utcnow()
            run = ScheduleRun(user_id=user.id, thread_id=thread.id, input_text=body.text,
                              image_ids=[], status="cancelled", reply="已取消这次课表修改。")
            db.add(run)
            # Choosing a file uploads it before the client sends a message.
            # Cancelling a task must not discard those unsent attachments.
            references = db.scalars(select(ScheduleRun.image_ids).where(
                ScheduleRun.user_id == user.id, ScheduleRun.thread_id == thread.id)).all()
            referenced_ids = {uuid.UUID(value) for values in references for value in values}
            db.execute(update(ScheduleImage).where(ScheduleImage.user_id == user.id,
                       ScheduleImage.thread_id == thread.id, ScheduleImage.id.in_(referenced_ids)).values(data=None))
            touch(thread)
            return detail(db, thread)
        if not body.image_ids and confirms_preview(body.text, previous):
            queue_decision(db, thread, previous, "apply")
            previous.confirmation_text = body.text
            return detail(db, thread)
        for image_id in body.image_ids:
            image = owned(db, ScheduleImage, user.id, image_id)
            if image.thread_id != thread.id:
                raise HTTPException(404, "未找到该图片。")
            if image.expires_at <= utcnow() or image.data is None:
                raise HTTPException(422, "图片已过期，请重新选择上传。")
        if previous and previous.status == "awaiting_confirmation":
            previous.status = "superseded"
            previous.updated_at = utcnow()
        run = ScheduleRun(user_id=user.id, thread_id=thread.id, input_text=body.text,
                          image_ids=[str(item) for item in body.image_ids], status="queued")
        db.add(run)
        touch(thread)
        return detail(db, thread)
    return mutate(db, user, request, key, body, 202, action)


def selected_run(db, user, thread_id, run_id, version):
    thread = owned(db, ScheduleThread, user.id, thread_id)
    check_version(thread, version)
    run = owned(db, ScheduleRun, user.id, run_id)
    latest = latest_run(db, thread.id)
    if run.thread_id != thread.id or latest is None or latest.id != run.id:
        raise HTTPException(409, "这份方案已被后续对话替代，请读取最新对话。")
    return thread, run


@router.post("/threads/{thread_id}/runs/{run_id}/confirm", status_code=202)
def confirm_run(thread_id: uuid.UUID, run_id: uuid.UUID, body: Decision, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        thread, run = selected_run(db, user, thread_id, run_id, body.version)
        queue_decision(db, thread, run, body.decision)
        return detail(db, thread)
    return mutate(db, user, request, key, body, 202, action)


@router.post("/threads/{thread_id}/runs/{run_id}/retry", status_code=202)
def retry_run(thread_id: uuid.UUID, run_id: uuid.UUID, body: Retry, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        thread, run = selected_run(db, user, thread_id, run_id, body.version)
        if run.status != "failed":
            raise HTTPException(409, "只能重试失败的请求；课表有变化时请发送说明重新生成方案。")
        run.status = "confirm_queued" if run.decision else "queued"
        run.error, run.lease_owner, run.lease_until = "", None, None
        run.attempts = 0
        run.updated_at = utcnow()
        touch(thread)
        return detail(db, thread)
    return mutate(db, user, request, key, body, 202, action)


def normalize_schedule_image(data):
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(415, "课表图片仅支持 JPEG、PNG 或 WebP。")
            if source.width * source.height > 16_000_000:
                raise HTTPException(413, "课表图片不能超过 1600 万像素。")
            source.verify()
        with Image.open(BytesIO(data)) as source:
            source.load()
            rgba = ImageOps.exif_transpose(source).convert("RGBA")
            rgba.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
            output = BytesIO()
            clean.save(output, format="PNG", optimize=True)
            if output.tell() > 8 * 1024 * 1024:
                raise HTTPException(413, "图片处理后过大，请裁剪课表区域再上传。")
            return output.getvalue()
    except Image.DecompressionBombError:
        raise HTTPException(413, "课表图片尺寸过大。") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise HTTPException(400, "无法读取图片，请重新选择。") from None


def save_image(db, auth_id, token_hash, thread_id, raw):
    # No public URL; ownership is checked again after reading and normalizing the upload.
    auth = db.scalar(select(AuthSession).where(AuthSession.id == auth_id).with_for_update().execution_options(populate_existing=True))
    if auth is None or auth.revoked_at or auth.expires_at <= utcnow() or auth.token_hash != token_hash:
        raise unauthorized()
    db.scalar(select(User).where(User.id == auth.user_id).with_for_update())
    owned(db, ScheduleThread, auth.user_id, thread_id)
    count = db.scalar(select(func.count()).select_from(ScheduleImage).where(
        ScheduleImage.user_id == auth.user_id, ScheduleImage.expires_at > utcnow(), ScheduleImage.data.is_not(None)))
    if count >= 20:
        raise HTTPException(429, "待处理图片较多，请先完成已有课表对话。")
    image = ScheduleImage(user_id=auth.user_id, thread_id=thread_id, data=raw,
                          mime_type="image/png", expires_at=utcnow() + timedelta(days=7))
    db.add(image)
    db.flush()
    result = {"id": str(image.id), "mime_type": image.mime_type, "expires_at": image.expires_at.isoformat()}
    db.commit()
    return result


@router.post("/threads/{thread_id}/images", status_code=201,
             openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
async def upload_image(thread_id: uuid.UUID, request: Request, auth: CurrentSession, db: Db):
    owned(db, ScheduleThread, auth.user_id, thread_id)
    kind = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
    if kind not in {"application/octet-stream", "image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "课表图片仅支持 JPEG、PNG 或 WebP。")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "每张课表图片不能超过 2 MiB，请压缩或裁剪后重试。")
        data.extend(chunk)
    normalized = await run_in_threadpool(normalize_schedule_image, bytes(data))
    return await run_in_threadpool(save_image, db, auth.id, auth.token_hash, thread_id, normalized)
