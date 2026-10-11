"""Validate and normalize uploaded avatars before storing their current version."""
import uuid
from io import BytesIO

from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from auth import CurrentSession, Db, UserResponse, unauthorized, utcnow
from models import AuthSession, User

router = APIRouter()
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_PIXELS = 16_000_000
MAX_AVATAR_SIDE = 512
ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp", "application/octet-stream")
ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")


def normalize_avatar(data: bytes) -> bytes:
    """Decode actual image data and emit only newly encoded, metadata-free JPEG."""
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ALLOWED_IMAGE_FORMATS:
                raise HTTPException(415, "头像仅支持 JPEG、PNG 或 WebP 图片。")
            if image.width * image.height > MAX_AVATAR_PIXELS:
                raise HTTPException(413, "头像尺寸不能超过 1600 万像素。")
            image.verify()

        # verify() consumes the decoder; reopen for the actual pixel decode.
        with Image.open(BytesIO(data)) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            oriented.thumbnail((MAX_AVATAR_SIDE, MAX_AVATAR_SIDE), Image.Resampling.LANCZOS)
            rgba = oriented.convert("RGBA")
            # A fresh RGB image strips all source metadata and flattens alpha.
            normalized = Image.new("RGB", rgba.size, "white")
            normalized.paste(rgba, mask=rgba.getchannel("A"))
            output = BytesIO()
            normalized.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()
    except Image.DecompressionBombError:
        raise HTTPException(413, "头像尺寸不能超过 1600 万像素。") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise HTTPException(400, "头像图片无法读取，请重新选择。") from None


def save_avatar(db: Session, session_id: uuid.UUID, expected_token_hash: str, data: bytes) -> UserResponse:
    # Uploads may take time. Recheck the session while holding the same lock
    # used by refresh/logout, before accepting the write.
    session = db.scalar(select(AuthSession).where(AuthSession.id == session_id)
                        .with_for_update().execution_options(populate_existing=True))
    if (session is None or session.revoked_at is not None or session.expires_at <= utcnow()
            or session.token_hash != expected_token_hash):
        raise unauthorized()
    # Update only avatar columns so concurrent nickname/timezone edits survive.
    result = db.execute(update(User).where(User.id == session.user_id)
                        .values(avatar_id=uuid.uuid4(), avatar_data=data))
    if result.rowcount != 1:
        raise unauthorized()
    user = db.scalar(select(User).where(User.id == session.user_id)
                     .execution_options(populate_existing=True))
    response = UserResponse.model_validate(user)
    db.commit()
    return response


@router.post(
    "/api/users/me/avatar",
    response_model=UserResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {mime: {"schema": {"type": "string", "format": "binary"}}
                        for mime in ALLOWED_CONTENT_TYPES},
        },
    },
)
async def upload_avatar(request: Request, auth: CurrentSession, db: Db):
    content_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "头像仅支持 JPEG、PNG 或 WebP 图片。")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            size = int(content_length)
        except ValueError:
            raise HTTPException(400, "无效的头像请求长度。") from None
        if size < 0:
            raise HTTPException(400, "无效的头像请求长度。")
        if size > MAX_AVATAR_BYTES:
            raise HTTPException(413, "头像文件不能超过 2 MiB。")

    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_AVATAR_BYTES:
            raise HTTPException(413, "头像文件不能超过 2 MiB。")
        data.extend(chunk)
    normalized = await run_in_threadpool(normalize_avatar, bytes(data))
    return await run_in_threadpool(save_avatar, db, auth.id, auth.token_hash, normalized)


@router.get("/api/avatars/{avatar_id}", response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "头像不存在"}})
def get_avatar(avatar_id: uuid.UUID, db: Db):
    data = db.scalar(select(User.avatar_data).where(User.avatar_id == avatar_id))
    if data is None:
        raise HTTPException(404, "头像不存在。")
    return Response(content=data, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})
