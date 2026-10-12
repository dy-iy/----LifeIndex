import asyncio
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from auth import CurrentUser, Db, router as auth_router
from avatars import router as avatars_router
from config import Settings
from db import make_engine
from diagnostics import log_failure
from quota import reserve_chat_request
from timetables import router as timetables_router
from schedule_api import router as schedule_router
from study_sessions import router as study_sessions_router
from reviews import router as reviews_router
from lesson_outcomes import router as lesson_outcomes_router
from evaluation_api import router as evaluation_router
from class_index import router as class_index_router
from learning_lifecycle import router as lifecycle_router
from pydantic import BaseModel, ConfigDict, Field, model_validator

load_dotenv(Path(__file__).with_name(".env"))
MODEL = "deepseek-flash"
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None):
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured = settings or Settings.from_env()
        engine = make_engine(configured.database_url)
        application.state.settings = configured
        application.state.session_factory = sessionmaker(engine, expire_on_commit=False) if engine is not None else None
        application.state.chat_requests = deque()
        application.state.chat_slots = asyncio.Semaphore(2)
        try:
            async with httpx.AsyncClient(base_url="https://api.deepseek.com", timeout=40, trust_env=False) as ai_client:
                with httpx.Client(base_url="https://api.weixin.qq.com", timeout=10, trust_env=False) as wechat_client:
                    application.state.ai_client = ai_client
                    application.state.wechat_client = wechat_client
                    yield
        finally:
            if engine is not None:
                engine.dispose()

    application = FastAPI(lifespan=lifespan)
    application.include_router(auth_router)
    application.include_router(avatars_router)
    application.include_router(timetables_router)
    application.include_router(schedule_router)
    application.include_router(study_sessions_router)
    application.include_router(reviews_router)
    application.include_router(lesson_outcomes_router)
    application.include_router(evaluation_router)
    application.include_router(class_index_router)
    application.include_router(lifecycle_router)

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError):
        log_failure(logger, "Database request failed: %s %s", exc, request.method, request.url.path)
        return JSONResponse(status_code=503, content={"detail": "数据服务暂时不可用，请稍后重试。"})

    @application.middleware("http")
    async def private_response_headers(request: Request, call_next):
        started = monotonic()
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/api/reviews"):
            timing = f"app;dur={(monotonic() - started) * 1000:.2f}"
            lock_ms = getattr(request.state, "review_lock_ms", None)
            if lock_ms is not None:
                timing += f", user_lock;dur={lock_ms:.2f}"
            response.headers["Server-Timing"] = timing
        return response

    application.add_api_route("/health", health, methods=["GET"])
    application.add_api_route("/api/chat", chat, methods=["POST"], response_model=ChatResponse)
    return application


class ChatMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=6000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=19)

    @model_validator(mode="after")
    def validate_conversation(self):
        roles = [message.role for message in self.messages]
        if roles[-1] != "user" or any(
            role != ("user" if index % 2 == 0 else "assistant")
            for index, role in enumerate(roles)
        ):
            raise ValueError("消息必须按 user、assistant 交替，并以 user 结束。")
        if len(self.messages[-1].content) > 2000:
            raise ValueError("单次输入不能超过 2000 字。")
        if sum(len(message.content) for message in self.messages) > 24000:
            raise ValueError("对话内容过长，请新建对话。")
        return self


class ChatResponse(BaseModel):
    reply: str
    model: str = MODEL


def health():
    return {"status": "ok"}


async def chat(body: ChatRequest, request: Request, user: CurrentUser, db: Db):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "后端尚未配置 DEEPSEEK_API_KEY。")
    now = monotonic()
    recent = request.app.state.chat_requests
    while recent and now - recent[0] >= 60:
        recent.popleft()
    if len(recent) >= 20 or request.app.state.chat_slots.locked():
        raise HTTPException(429, "测试接口繁忙，请稍后重试。")
    recent.append(now)

    async with request.app.state.chat_slots:
        await run_in_threadpool(reserve_chat_request, db, user.id, request.app.state.settings.ai_daily_request_limit)
        try:
            response = await asyncio.wait_for(
                request.app.state.ai_client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": MODEL,
                        "messages": [
                            {"role": "system", "content": "你是 Lifefund 的 AI 助手。请用中文简洁、清楚地回答用户的问题。"},
                            *[message.model_dump() for message in body.messages],
                        ],
                        "thinking": {"type": "disabled"},
                        "stream": False,
                        "max_tokens": 1024,
                    },
                ), timeout=45,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, "AI 回复超时，请稍后重试。") from None
        except httpx.RequestError:
            raise HTTPException(502, "暂时无法连接 AI 服务，请稍后重试。") from None

    # Never forward upstream bodies or credentials to the mini program.
    if response.status_code in (401, 403):
        raise HTTPException(502, "AI 服务认证失败，请检查后端 API Key。")
    if response.status_code == 402:
        raise HTTPException(502, "AI 服务额度不足，请检查 DeepSeek 账户余额。")
    if response.status_code == 429:
        raise HTTPException(429, "AI 服务请求过于频繁，请稍后重试。")
    if not response.is_success:
        raise HTTPException(502, "AI 服务暂时不可用，请稍后重试。")
    try:
        reply = response.json()["choices"][0]["message"]["content"]
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("empty reply")
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(502, "AI 未返回有效文本，请重试。") from None
    return ChatResponse(reply=reply.strip())


app = create_app()
