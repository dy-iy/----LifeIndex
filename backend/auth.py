import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db import get_db
from models import AuthSession, User

router = APIRouter()
bearer = HTTPBearer(auto_error=False)
Db = Annotated[Session, Depends(get_db)]


def utcnow():
    return datetime.now(timezone.utc)


def hash_token(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


def unauthorized():
    return HTTPException(401, "登录已失效，请重新登录。", headers={"WWW-Authenticate": "Bearer"})


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    display_name: str | None
    avatar_url: str | None
    timezone: str
    created_at: datetime


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(min_length=1, max_length=256)


class SessionResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class UserPatch(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=60)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai。") from None
        return value


def get_auth_session(
    db: Db,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if credentials is None or len(credentials.credentials) > 512:
        raise unauthorized()
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(credentials.credentials)))
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        raise unauthorized()
    return session


CurrentSession = Annotated[AuthSession, Depends(get_auth_session)]


def get_current_user(session: CurrentSession, db: Db):
    user = db.get(User, session.user_id)
    if user is None:
        raise unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def issue_session(db: Session, user: User, ttl: int, session: AuthSession | None = None):
    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(seconds=ttl)
    if session is None:
        db.add(AuthSession(user_id=user.id, token_hash=hash_token(token), expires_at=expires_at))
    else:
        session.token_hash = hash_token(token)
        session.expires_at = expires_at
    return SessionResponse(access_token=token, expires_at=expires_at, user=UserResponse.model_validate(user))


@router.post("/api/auth/login", response_model=SessionResponse)
def login(body: LoginRequest, request: Request, response: Response, db: Db):
    settings = request.app.state.settings
    if not settings.wechat_app_id or not settings.wechat_app_secret:
        raise HTTPException(503, "微信登录尚未配置。")
    try:
        upstream = request.app.state.wechat_client.get(
            "/sns/jscode2session",
            params={"appid": settings.wechat_app_id, "secret": settings.wechat_app_secret,
                    "js_code": body.code, "grant_type": "authorization_code"},
        )
        if not upstream.is_success:
            raise HTTPException(502, "微信登录服务暂时不可用。")
        payload = upstream.json()
    except (httpx.TimeoutException, httpx.RequestError, ValueError):
        raise HTTPException(502, "微信登录服务暂时不可用。") from None
    if not isinstance(payload, dict):
        raise HTTPException(502, "微信登录服务返回无效数据。")
    if payload.get("errcode") in (40029, 40163):
        raise HTTPException(401, "微信登录凭据无效或已使用，请重新登录。")
    if payload.get("errcode", 0) != 0:
        raise HTTPException(502, "微信登录暂时失败，请稍后重试。")
    open_id = payload.get("openid")
    if not isinstance(open_id, str) or not open_id.strip() or len(open_id) > 128:
        raise HTTPException(502, "微信登录服务返回无效数据。")
    db.execute(insert(User).values(wechat_app_id=settings.wechat_app_id, wechat_open_id=open_id)
               .on_conflict_do_nothing(constraint="uq_users_wechat_identity"))
    user = db.scalar(select(User).where(User.wechat_app_id == settings.wechat_app_id, User.wechat_open_id == open_id))
    result = issue_session(db, user, settings.session_ttl_seconds)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/api/auth/refresh", response_model=SessionResponse)
def refresh(request: Request, response: Response, auth: CurrentSession, db: Db):
    # Keep the expected hash before populate_existing updates the identity-map object.
    expected_token_hash = auth.token_hash
    session = db.scalar(select(AuthSession).where(AuthSession.id == auth.id)
                        .with_for_update().execution_options(populate_existing=True))
    if (session is None or session.revoked_at is not None or session.expires_at <= utcnow()
            or session.token_hash != expected_token_hash):
        raise unauthorized()
    user = db.get(User, session.user_id)
    if user is None:
        raise unauthorized()
    # Rotation preserves the session id so an already authenticated logout can
    # revoke the replacement token, even when refresh wins this row lock first.
    result = issue_session(db, user, request.app.state.settings.session_ttl_seconds, session=session)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/api/auth/logout", status_code=204)
def logout(auth: CurrentSession, db: Db):
    session = db.scalar(select(AuthSession).where(AuthSession.id == auth.id)
                        .with_for_update().execution_options(populate_existing=True))
    if session is None:
        raise unauthorized()
    session.revoked_at = utcnow()
    db.commit()
    return Response(status_code=204)


@router.get("/api/users/me", response_model=UserResponse)
def me(user: CurrentUser):
    return user


@router.patch("/api/users/me", response_model=UserResponse)
def update_me(body: UserPatch, user: CurrentUser, db: Db):
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(user, name, value)
    db.commit()
    return user
