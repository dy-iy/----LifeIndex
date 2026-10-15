import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from auth import router as auth_router
from avatars import router as avatars_router
from config import Settings
from db import make_engine
from diagnostics import log_failure
from timetables import router as timetables_router
from schedule_api import router as schedule_router
from study_sessions import router as study_sessions_router
from reviews import router as reviews_router
from lesson_outcomes import router as lesson_outcomes_router
from evaluation_api import router as evaluation_router
from class_index import router as class_index_router
from learning_lifecycle import router as lifecycle_router
from study_groups import router as study_groups_router
from group_index import router as group_index_router
from group_messages import router as group_messages_router
from group_member_status import router as group_member_status_router
from group_checkins import router as group_checkins_router
from worker_runtime import WorkerRuntime

load_dotenv(Path(__file__).with_name(".env"))
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, background_workers: bool = True):
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured = settings or Settings.from_env()
        engine = make_engine(configured.database_url)
        application.state.settings = configured
        application.state.session_factory = sessionmaker(engine, expire_on_commit=False) if engine is not None else None
        runtime = WorkerRuntime(configured) if background_workers and engine is not None else None
        application.state.worker_runtime = runtime
        try:
            with httpx.Client(base_url="https://api.weixin.qq.com", timeout=10, trust_env=False) as wechat_client:
                application.state.wechat_client = wechat_client
                if runtime is not None:
                    await asyncio.to_thread(runtime.start)
                try:
                    yield
                finally:
                    if runtime is not None:
                        await asyncio.to_thread(runtime.stop)
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
    application.include_router(study_groups_router)
    application.include_router(group_index_router)
    application.include_router(group_messages_router)
    application.include_router(group_member_status_router)
    application.include_router(group_checkins_router)

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
    return application


def health(request: Request):
    runtime = request.app.state.worker_runtime
    workers = runtime.status() if runtime is not None else {}
    healthy = all(state == "running" for state in workers.values())
    return JSONResponse(status_code=200 if healthy else 503,
                        content={"status": "ok" if healthy else "degraded", "background_workers": workers})


app = create_app()
