import hashlib
import json
import re
import psycopg
from typing import Annotated

from fastapi import Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from models import CourseTemplate, Lesson, TimetableMutation, User
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str):
    if not database_url:
        return None
    try:
        url = make_url(database_url)
        if url.drivername not in ("postgresql", "postgresql+psycopg"):
            raise ValueError
        url = url.set(drivername="postgresql+psycopg")
    except (ArgumentError, ValueError, TypeError):
        # Do not include a potentially credential-bearing URL in errors.
        raise ValueError("DATABASE_URL must be a valid PostgreSQL psycopg URL") from None
    return create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 5})


def get_db(request: Request):
    factory: sessionmaker[Session] | None = request.app.state.session_factory
    if factory is None:
        raise HTTPException(503, "数据库尚未配置。")
    with factory() as session:
        if request.method == "GET" and (request.url.path == "/api/groups"
                or request.url.path.startswith("/api/groups/")
                or request.url.path.startswith("/api/indices/class")
                or request.url.path == "/api/learning/incomplete"
                or request.url.path.startswith("/api/timetable-agent/threads")
                or request.url.path == "/api/timetables/structured"
                or request.url.path == "/api/timetables/state"
                or request.url.path.startswith("/api/reviews/")):
            # Start the snapshot before authentication issues its first SELECT.
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        yield session


Version = Annotated[int, Field(strict=True, ge=1)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class Input(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


def mutate(db, user, request, key, body, status, action, *, no_key_update=False, render_response=None):
    """Commit a receipt with the write; optionally render it against current permissions.

    Group writes use a non-key user lock to allow foreign-key checks during owner
    transfer. ``render_response(receipt, replayed)`` must not mutate business state;
    it can omit data the caller is no longer authorized to read. The persisted
    receipt can therefore avoid credentials and stale permission-bearing data.
    """
    if not key.strip():
        raise HTTPException(422, "Idempotency-Key 不能为空。")
    payload = body.model_dump(mode="json", exclude_unset=True) if isinstance(body, BaseModel) else body
    digest = hashlib.sha256(json.dumps(
        [request.method, request.url.path, sorted(request.query_params.multi_items()), payload],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    # Refresh the user after locking so semester defaults use the latest saved timezone.
    db.scalar(select(User).where(User.id == user.id).with_for_update(key_share=no_key_update)
              .execution_options(populate_existing=True))
    previous = db.get(TimetableMutation, (user.id, key))
    if previous is not None:
        if previous.request_hash != digest:
            raise HTTPException(409, "该请求标识已用于其他操作，请刷新后重试。")
        response = previous.response_body
        if render_response is not None:
            response = render_response(response, True)
        db.commit()
        return Response(status_code=204) if previous.response_status == 204 else JSONResponse(
            status_code=previous.response_status, content=response,
        )
    result = action()
    db.add(TimetableMutation(user_id=user.id, key=key, request_hash=digest,
                             response_status=status, response_body=result))
    response = result
    if render_response is not None:
        db.flush()
        response = render_response(result, False)
    db.commit()
    return Response(status_code=204) if status == 204 else JSONResponse(status_code=status, content=response)


def owned(db, model, user_id, item_id):
    query = select(model).where(model.id == item_id, model.user_id == user_id)
    if model is CourseTemplate:
        query = query.where(CourseTemplate.is_deleted.is_(False))
    if model is Lesson:
        query = query.where(Lesson.is_deleted.is_(False))
    item = db.scalar(query.execution_options(populate_existing=True))
    if item is None:
        raise HTTPException(404, "未找到该记录。")
    return item


def check_version(item, version):
    if item.version != version:
        raise HTTPException(409, "记录已被其他操作更新，请重新加载后再修改。")


def lock_reader(db, user_id):
    # Reads span records and segments. The same user lock used by timetable
    # writes prevents returning a state from one version with another's segments.
    db.scalar(select(User.id).where(User.id == user_id).with_for_update())


def checkpoint_location(database_url):
    url = make_url(database_url).set(drivername="postgresql")
    options = url.query.get("options", "")
    match = re.search(r"search_path=([A-Za-z_][A-Za-z_0-9]*)", options)
    schema = (match.group(1) + "_graph") if match else "lifefund_schedule_graph"
    if len(schema) > 63:
        raise ValueError("Checkpoint schema name is too long")
    # libpq URIs do not decode '+' as a space; SQLAlchemy URL rendering does.
    # Build a libpq conninfo from parsed values so options and Unix sockets survive.
    parameters = url.translate_connect_args(username="user", database="dbname")
    parameters.update(url.query)
    parameters["options"] = options + f" -csearch_path={schema}"
    return psycopg.conninfo.make_conninfo(**parameters), schema

