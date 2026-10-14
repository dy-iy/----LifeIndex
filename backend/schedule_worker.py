"""Durable one-pass multimodal timetable worker."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg
from psycopg import sql
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import sessionmaker

from auth import utcnow
from config import Settings
from db import make_engine, checkpoint_location
from diagnostics import log_failure
from models import CourseTemplate, ScheduleImage, ScheduleRun, ScheduleThread, ScheduleModelCall, User
from agents.prompts import prompt_manifest
from agents.schedule_image_reader import SCHEMA_VERSION, normalize_understanding
from agents.tools.registry import Scope, record_trace
from schedule_api import latest_run, touch
from schedule_budget import reserve, settle
from schedule_graph import build_graph
from schedule_model import MODEL_NAME, PROMPT_VERSION, ModelError, prepare_messages, invoke_planner

logger = logging.getLogger(__name__)
CACHE_VERSION = "schedule-understanding-v7"
SEMESTER_SCOPE_VERSION = "semester-bound-v1"


def _context_has_semester(context, semester_id):
    document = context.get("document") or {}
    semester = context.get("semester") or {}
    return (context.get("semester_id") == semester_id
            and document.get("semester_id") == semester_id
            and semester.get("id") == semester_id)


def setup_checkpoints(database_url):
    dsn, schema = checkpoint_location(database_url)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()


@contextmanager
def open_checkpointer(database_url, run_id):
    dsn, _ = checkpoint_location(database_url)
    with PostgresSaver.from_conn_string(dsn) as saver:
        # Fencing covers the provider call without holding a business-row lock.
        lock_id = int.from_bytes(uuid.UUID(str(run_id)).bytes[:8], "big", signed=True)
        acquired = saver.conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_id,)).fetchone()["acquired"]
        if not acquired:
            yield None
            return
        try:
            yield saver
        finally:
            saver.conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))


def model_context(db, user_id, document, snapshot):
    """Project current facts; the audit trail is never fed back into inference."""
    from schedule_context import context_snapshot
    facts = context_snapshot(db, user_id, document.semester_id)
    source = snapshot.get("document", {})
    data = source.get("data", {})
    projected = {**snapshot, "document": {
        key: value for key, value in source.items() if key in {"id", "version", "semester_id"}}}
    projected["document"]["data"] = {key: deepcopy(data[key]) for key in ("period_tables", "display_period_table_id") if key in data}
    projected["courses"] = [{"id": item["id"], "version": item["version"], "data": {
        key: deepcopy(item.get("data", {}).get(key)) for key in ("name", "schedules", "exceptions")}}
        for item in snapshot.get("courses", []) if not item.get("is_deleted")]
    for key in ("today", "timezone", "semester", "semesters", "week_dates"):
        projected[key] = facts.get(key)
    zone = (facts.get("semester") or {}).get("timezone") or facts.get("timezone") or "Asia/Shanghai"
    now = utcnow().astimezone(ZoneInfo(zone))
    projected["now"], projected["today"], projected["timezone"] = now.isoformat(), now.date().isoformat(), zone
    templates = db.scalars(select(CourseTemplate).where(CourseTemplate.user_id == user_id,
        CourseTemplate.semester_id == document.semester_id)).all() if document.semester_id else []
    by_template = {str(item.id): item for item in templates}
    keep = {"id", "course_id", "version", "name", "local_date", "start_time", "end_time", "status", "is_override", "week_number", "location", "has_learning_record"}
    projected["lessons"] = []
    for lesson in facts.get("lessons", []):
        item = {key: value for key, value in lesson.items() if key in keep}
        template = by_template.get(lesson.get("course_id"))
        if template and template.source_course_id and template.source_schedule_id:
            item.update(structured_course_id=str(template.source_course_id), schedule_id=str(template.source_schedule_id),
                        original_week=lesson.get("week_number"))
        projected["lessons"].append(item)
    return projected


def _read_range(state):
    context, text = state.get("context", {}), state.get("input_text", "")
    try:
        today = date.fromisoformat(context["today"])
    except (KeyError, TypeError, ValueError):
        return None
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if dates:
        try:
            start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
            return start, end
        except ValueError:
            return None
    for word, offset in (("后天", 2), ("明天", 1), ("昨天", -1), ("今天", 0)):
        if word in text:
            day = today + timedelta(days=offset)
            return day, day
    for word, offset in (("下周", 7), ("上周", -7), ("本周", 0), ("这周", 0)):
        if word in text:
            monday = today - timedelta(days=today.weekday()) + timedelta(days=offset)
            return monday, monday + timedelta(days=6)
    match = re.search(r"第\s*(\d+)\s*周", text)
    if match:
        row = next((item for item in context.get("week_dates", []) or [] if item.get("number") == int(match[1])), None)
        if row:
            return date.fromisoformat(row["start_date"]), date.fromisoformat(row["end_date"])
    match = re.search(r"(?:周|星期)([一二三四五六日天])", text)
    if match:
        weekday = "一二三四五六日".index("日" if match[1] == "天" else match[1])
        day = today - timedelta(days=today.weekday()) + timedelta(days=weekday)
        return day, day
    return None


def period_time_setup(result, state):
    """An image import can succeed while period clocks still need manual input."""
    empty = {"required": False, "tables": []}
    if (not state.get("image_ids") and not state.get("period_time_setup_pending")) or state.get("plan", {}).get("intent") == "read" or result.get("saved") is False:
        return empty
    structured = result.get("structured") or {}
    tables = structured.get("period_tables", [])
    from timetable_structured import _current_period_table
    document_data = (structured.get("document") or {}).get("data") or {}
    current = _current_period_table({"period_tables": tables,
                                     "display_period_table_id": document_data.get("display_period_table_id")})
    tables = [current] if current is not None else []
    missing = {}
    known = {}
    for table in tables:
        identifier = table.get("id")
        periods = {item["number"]: item for item in table.get("periods", [])}
        known[identifier] = periods
        for number, period in periods.items():
            start, end = period.get("start_time"), period.get("end_time")
            if not start or not end or start >= end:
                missing.setdefault(identifier, set()).add(number)
    # Preserve the prompt even if only a course's period numbers were extracted,
    # without a corresponding period-table row.
    for course in structured.get("courses", []):
        if course.get("is_deleted"):
            continue
        for schedule in course.get("data", {}).get("schedules", []):
            if schedule.get("start_time") and schedule.get("end_time"):
                continue
            identifier = current["id"] if current is not None else None
            periods = known.get(identifier, {})
            for number in schedule.get("period_numbers") or []:
                if number not in periods:
                    missing.setdefault(identifier, set()).add(number)
    rows = [{"id": identifier, "period_numbers": sorted(numbers)} for identifier, numbers in missing.items() if numbers]
    return {"required": bool(rows), "tables": rows}


def saved_reply(result, state):
    if result.get("scope_rejected") or (result.get("saved") is False and state.get("plan", {}).get("intent") != "read"):
        reasons = [str(item.get("reason")) for item in result.get("unapplied_changes", []) if item.get("reason")]
        return "本次课表修改未应用。" + " ".join(dict.fromkeys(reasons))
    if state.get("plan", {}).get("intent") == "read":
        timeframe = _read_range(state)
        if timeframe:
            start, end = timeframe
            lessons = [item for item in state.get("context", {}).get("lessons", [])
                       if start.isoformat() <= item.get("local_date", "") <= end.isoformat()]
            lessons.sort(key=lambda item: (item.get("local_date", ""), item.get("start_time", "")))
            label = start.isoformat() if start == end else f"{start.isoformat()} 至 {end.isoformat()}"
            if not lessons:
                return f"{label} 没有已排定的课程。"
            labels = {"leave": "（请假）", "cancelled": "（停课）"}
            return f"{label} 的课程：\n" + "\n".join(
                f"{item['local_date']} {item.get('start_time', '')}—{item.get('end_time', '')} {item.get('name') or '未命名课程'}"
                + (f" · {item['location']}" if item.get("location") else "")
                + labels.get(item.get("status"), "") for item in lessons)
        courses = result.get("structured", {}).get("courses", [])
        names = [item.get("data", {}).get("name") or "未命名课程" for item in courses if not item.get("is_deleted")]
        return "当前课程：" + "、".join(names) + "。" if names else "当前没有已保存的课程。"
    created = len(result.get("created_courses", []))
    updated = len(result.get("updated_courses", []))
    reply = f"已保存课表信息：新增 {created} 门课程，更新 {updated} 门课程。"
    period_changes = result.get("period_table_changes", [])
    if period_changes:
        labels = {"create": "设置作息时间", "update": "修改作息时间", "delete": "删除作息时间", "clear": "清空作息时间"}
        reply += "作息时间已保存：" + "、".join(f"{label} {sum(item.get('action') == action for item in period_changes)} 项"
            for action, label in labels.items() if any(item.get("action") == action for item in period_changes)) + "。已选节次的课程时间会同步跟随当前作息；缺少对应节次或完整起止时间的安排暂不进入周课表。"
    if result.get("unapplied_changes"):
        reply += "部分修改未应用，详情已保留在处理结果中。"
    if result.get("diagnostics"):
        reply += "缺失信息和时间重叠已标记，可在课程列表查看和编辑。"
    setup = result.get("period_time_setup") or period_time_setup(result, state)
    if setup.get("required"):
        reply += "部分节次缺少对应时间段，请手动设置每节课的开始和结束时间。只有开始和结束时间填写完整的课程安排，才可在对应时段被计时功能识别；时间未填完整的安排暂不会被识别。可在聊天框输入各节时间，也可到学期设置中补充作息时间；设置前保留已识别信息，不使用默认作息。"
    return reply


class Worker:
    def __init__(self, settings, planner=invoke_planner):
        self.settings, self.planner = settings, planner
        self.engine = make_engine(settings.database_url)
        if self.engine is None:
            raise ValueError("DATABASE_URL is required for the schedule worker")
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def close(self):
        self.engine.dispose()

    def claim(self):
        with self.factory() as db:
            now = utcnow()
            db.execute(update(ScheduleImage).where(ScheduleImage.expires_at <= now, ScheduleImage.data.is_not(None)).values(data=None))
            run = db.scalar(select(ScheduleRun).where(or_(
                ScheduleRun.status.in_(["queued", "confirm_queued"]),
                and_(ScheduleRun.status.in_(["running", "confirming"]), ScheduleRun.lease_until < now),
            )).order_by(ScheduleRun.created_at, ScheduleRun.id).with_for_update(skip_locked=True).limit(1))
            if run is None:
                db.commit()
                return None
            if run.decision is not None or run.status in {"confirm_queued", "confirming"}:
                run.status, run.reply = "superseded", "旧版预览任务已被替代，原始输入和图片已保留。"
                run.lease_owner, run.lease_until = None, None
                touch(db.get(ScheduleThread, run.thread_id))
                db.commit()
                return None
            run.attempts += 1
            run.updated_at = now
            db.execute(update(ScheduleThread).where(ScheduleThread.id == run.thread_id).values(
                version=ScheduleThread.version + 1, updated_at=now))
            if run.attempts > 3:
                run.status, run.error = "failed", "任务多次中断，输入和有效识别结果已保留，可主动重试。"
                run.lease_owner, run.lease_until = None, None
                db.commit()
                return None
            lease_owner = uuid.uuid4()
            run.status = "running"
            run.lease_owner, run.lease_until = lease_owner, now + timedelta(minutes=5)
            db.commit()
            return run.id, lease_owner

    def load_state(self, run_id):
        from timetable_structured import get_document, structured_snapshot
        with self.factory() as db:
            run = db.get(ScheduleRun, run_id)
            thread = db.get(ScheduleThread, run.thread_id)
            if thread is None or thread.user_id != run.user_id or thread.is_archived or thread.semester_id is None:
                raise HTTPException(404, "未找到当前课表对话。")
            db.scalar(select(User).where(User.id == run.user_id).with_for_update())
            # Include user words only. Assistant guesses never become evidence;
            # previously applied actions are labelled and cannot be replayed.
            rows = db.scalars(select(ScheduleRun).where(ScheduleRun.user_id == run.user_id,
                ScheduleRun.thread_id == thread.id, ScheduleRun.created_at <= run.created_at)
                .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc()).limit(100)).all()
            rows.reverse()
            previous_write = next((item for item in reversed(rows) if item.id != run.id and item.status == "applied"), None)
            setup_pending = bool(previous_write and (previous_write.result or {}).get("period_time_setup", {}).get("required"))
            history = [{"run_id": str(item.id), "input": item.input_text,
                        "confirmation": item.confirmation_text, "status": item.status,
                        "unapplied_changes": [{key: deepcopy(value) for key, value in change.items()
                            if key in {"reason", "change", "fields", "candidate_course_ids", "candidate_period_table_ids",
                                       "period_table_change", "period_table_changes", "source_text"}}
                            for change in (item.result or {}).get("unapplied_changes", []) if isinstance(change, dict)]}
                       for item in rows if item.id != run.id]
            document = get_document(db, run.user_id, semester_id=thread.semester_id, create=True)
            context = model_context(db, run.user_id, document, structured_snapshot(db, run.user_id, document))
            db.commit()
            return {"run_id": str(run.id), "thread_id": str(thread.id), "user_id": str(run.user_id),
                    "schedule_prompt": PROMPT_VERSION,
                    "semester_id": str(thread.semester_id), "semester_scope": SEMESTER_SCOPE_VERSION,
                    "input_text": run.input_text, "image_ids": list(run.image_ids),
                    "context": context, "history": history, "period_time_setup_pending": setup_pending}

    def interpret(self, state):
        if (state.get("semester_scope") != SEMESTER_SCOPE_VERSION or not state.get("semester_id")
                or not _context_has_semester(state.get("context", {}), state.get("semester_id"))):
            raise ModelError("任务所属学期已变化，原始输入已保留。", status_code=409)
        identifiers = state.get("image_ids", [])
        if len(identifiers) > 3 or len(set(identifiers)) != len(identifiers):
            raise ModelError("每条消息最多处理三张不同的课表图片。", status_code=422)
        # Uploads are immutable and account-scoped. Their IDs identify the image
        # batch without requiring expired bytes to reuse a completed response.
        cache_input = {"version": CACHE_VERSION, "model": MODEL_NAME, "schema": SCHEMA_VERSION,
                       "prompt_hash": prompt_manifest([PROMPT_VERSION])[PROMPT_VERSION],
                       "context": state.get("context", {}), "history": state.get("history", []),
                       "input_text": state.get("input_text", ""), "image_ids": identifiers}
        digest = hashlib.sha256(json.dumps(cache_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.factory() as db:
            cached = db.scalar(select(ScheduleModelCall.response).where(
                ScheduleModelCall.user_id == uuid.UUID(state["user_id"]), ScheduleModelCall.run_id == uuid.UUID(state["run_id"]),
                ScheduleModelCall.input_hash == digest, ScheduleModelCall.response.is_not(None)).limit(1))
            pending = db.scalars(select(ScheduleModelCall.id).where(ScheduleModelCall.run_id == uuid.UUID(state["run_id"]),
                ScheduleModelCall.user_id == uuid.UUID(state["user_id"]), ScheduleModelCall.input_hash == digest,
                ScheduleModelCall.status == "reserved")).all()
        for call in pending:
            settle(self.factory, call, {})
        if isinstance(cached, dict) and cached.get("kind") == CACHE_VERSION:
            return cached["understanding"]
        images = []
        with self.factory() as db:
            for image_id in identifiers:
                item = db.get(ScheduleImage, uuid.UUID(image_id))
                if item is None or str(item.user_id) != state["user_id"] or str(item.thread_id) != state["thread_id"]:
                    raise ModelError("附件不可用，任务输入已保留。", status_code=422)
                if item.expires_at <= utcnow() or item.data is None:
                    raise ModelError("本次附件已过期，任务原始输入已保留。", status_code=422)
                images.append({"mime_type": item.mime_type, "data": item.data})
        prepared = prepare_messages(state, images)
        messages = prepared["messages"]
        record_trace(self.factory, Scope(uuid.UUID(state["user_id"]), uuid.UUID(state["run_id"]), CACHE_VERSION, "context"),
                     "context_schedule", "succeeded", time.monotonic(),
                     {**prepared["manifest"], "context_hash": digest, "cache_hit": False})
        key = os.getenv("ARK_API_KEY", "").strip()
        if not key:
            raise ModelError("课表助手尚未配置模型服务，任务输入已保留。", status_code=503)
        call_id = reserve(self.factory, state["user_id"], state["run_id"], messages, self.settings, input_hash=digest)
        result = None
        try:
            result = self.planner(messages, api_key=key)
            understanding = normalize_understanding(result.get("understanding"))
            # Versions are pinned to the context sent to the LLM, never re-read
            # after inference. Missing model versions cannot disable concurrency.
            versions = {item["id"]: item["version"] for item in state["context"].get("courses", [])}
            for course in understanding.get("courses", []):
                if course.get("id") in versions:
                    course["version"] = versions[course["id"]]
            for change in understanding.get("changes", []):
                if change.get("course_id") in versions:
                    change["version"] = versions[change["course_id"]]
            understanding["document_version"] = state["context"].get("document", {}).get("version", state["context"].get("version"))
            understanding["expected_document_id"] = state["context"].get("document", {}).get("id", state["context"].get("document_id"))
        except ModelError as error:
            settle(self.factory, call_id, result.get("usage", {}) if result else error.usage)
            raise
        except Exception as error:
            log_failure(logger, "Schedule understanding failed run_id=%s", error, state["run_id"])
            settle(self.factory, call_id, {})
            raise ModelError("课表助手处理暂时中断，输入已保留，可重试。") from None
        settle(self.factory, call_id, result.get("usage", {}), {"kind": CACHE_VERSION, "understanding": understanding, "context_hash": digest})
        return understanding

    def apply(self, state, lease_owner):
        from timetable_structured import SEMESTER_FACT_FIELDS, get_document, apply_understanding
        with self.factory() as db:
            user = db.scalar(select(User).where(User.id == uuid.UUID(state["user_id"])).with_for_update())
            run = db.scalar(select(ScheduleRun).where(ScheduleRun.id == uuid.UUID(state["run_id"])).with_for_update())
            if run is None or user is None or run.user_id != user.id or str(run.thread_id) != state["thread_id"]:
                raise HTTPException(404, "未找到当前课表任务。")
            thread = db.get(ScheduleThread, run.thread_id)
            expected = state.get("context", {}).get("document") or {}
            if (thread is None or thread.is_archived or thread.semester_id is None
                    or state.get("semester_scope") != SEMESTER_SCOPE_VERSION
                    or state.get("semester_id") != str(thread.semester_id)
                    or not _context_has_semester(state.get("context", {}), str(thread.semester_id))):
                raise HTTPException(409, "任务所属学期与当前对话不一致，原始输入已保留。")
            if run.status in {"applied", "completed"}:
                return run.result
            if run.lease_owner != lease_owner or run.status != "running":
                raise HTTPException(409, "任务状态已经变化，请读取最新对话。")
            current = latest_run(db, thread.id)
            if current is None or current.id != run.id:
                raise HTTPException(409, "任务已被后续对话替代，请读取最新对话。")
            document = get_document(db, user.id, semester_id=thread.semester_id, create=True)
            if str(document.id) != str(expected.get("id")):
                raise HTTPException(409, "任务对应的课表已变化，原始输入已保留。")
            understanding = deepcopy(state["plan"])
            # These facts are frozen by the server before inference. Neither
            # a model field nor a resumed cache can supply a newer calendar.
            semester = state["context"]["semester"]
            understanding["expected_semester_facts"] = {key: deepcopy(semester.get(key)) for key in SEMESTER_FACT_FIELDS}
            # The semester and document identity remain pinned through inference.
            understanding["expected_document_id"] = state["context"].get("document", {}).get("id", state["context"].get("document_id"))
            understanding.setdefault("sources", []).append({"kind": "user_message", "run_id": str(run.id),
                "input": run.input_text, "image_ids": list(run.image_ids)})
            result = apply_understanding(db, user, document, understanding, run_id=run.id)
            result["period_time_setup"] = period_time_setup(result, state)
            result["reply"] = saved_reply(result, state)[:12000]
            run.status = "completed" if understanding.get("intent") == "read" or result.get("saved") is False else "applied"
            run.result, run.reply = result, result["reply"]
            run.plan, run.preview = state["plan"], None
            run.lease_owner, run.lease_until, run.error = None, None, ""
            run.updated_at = utcnow()
            touch(thread)
            db.commit()
            return result

    def finish(self, run_id, lease_owner, state=None, error=None):
        with self.factory() as db:
            initial = db.get(ScheduleRun, run_id)
            if initial is None:
                return
            db.scalar(select(User).where(User.id == initial.user_id).with_for_update())
            run = db.scalar(select(ScheduleRun).where(ScheduleRun.id == run_id).with_for_update().execution_options(populate_existing=True))
            if run.lease_owner != lease_owner or run.status in {"applied", "completed"}:
                return
            thread = db.get(ScheduleThread, run.thread_id)
            if error is not None:
                run.status, run.error = "failed", str(error)[:1000]
            else:
                run.status = state.get("status", "failed")
                run.reply = state.get("reply", "")[:12000]
                run.plan, run.preview = state.get("plan"), None
                run.result, run.error = state.get("result"), ""
            run.lease_owner, run.lease_until = None, None
            run.updated_at = utcnow()
            touch(thread)
            db.commit()

    def supersede_legacy(self, run_id, lease_owner):
        with self.factory() as db:
            run = db.scalar(select(ScheduleRun).where(ScheduleRun.id == run_id).with_for_update())
            if run.lease_owner != lease_owner:
                return
            run.status, run.reply = "superseded", "旧版课表任务已被替代，原始输入和图片已保留。"
            run.lease_owner, run.lease_until = None, None
            run.updated_at = utcnow()
            touch(db.get(ScheduleThread, run.thread_id))
            db.commit()

    def run_once(self):
        claimed = self.claim()
        if claimed is None:
            return False
        run_id, lease_owner = claimed
        try:
            with open_checkpointer(self.settings.database_url, run_id) as saver:
                if saver is None:
                    return True
                graph = build_graph(saver, self.interpret, lambda state: self.apply(state, lease_owner))
                config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 5}
                snapshot = graph.get_state(config)
                if snapshot.values and (snapshot.values.get("schedule_prompt") != PROMPT_VERSION
                        or snapshot.values.get("semester_scope") != SEMESTER_SCOPE_VERSION):
                    self.supersede_legacy(run_id, lease_owner)
                    return True
                with self.factory() as db:
                    run = db.get(ScheduleRun, run_id)
                    if run.lease_owner != lease_owner or run.status in {"applied", "completed"}:
                        return True
                    thread = db.get(ScheduleThread, run.thread_id)
                    invalid_scope = thread is None or thread.is_archived or thread.semester_id is None
                    if snapshot.values and not invalid_scope:
                        scope = str(thread.semester_id)
                        invalid_scope = (snapshot.values.get("semester_id") != scope
                            or not _context_has_semester(snapshot.values.get("context", {}), scope)
                            or snapshot.values.get("thread_id") != str(thread.id)
                            or snapshot.values.get("user_id") != str(thread.user_id))
                if invalid_scope:
                    self.supersede_legacy(run_id, lease_owner)
                    return True
                if snapshot.values and not snapshot.next:
                    result = snapshot.values
                elif snapshot.values:
                    result = graph.invoke(None, config, durability="sync")
                else:
                    result = graph.invoke(self.load_state(run_id), config, durability="sync")
                self.finish(run_id, lease_owner, result)
        except ModelError as error:
            self.finish(run_id, lease_owner, error=error.message)
        except HTTPException as error:
            self.finish(run_id, lease_owner, error=error.detail if isinstance(error.detail, str) else "任务无法处理，可重试。")
        except Exception as error:
            log_failure(logger, "Schedule worker failed run_id=%s", error, run_id)
            self.finish(run_id, lease_owner, error="处理暂时中断，已保留输入和有效识别结果，可稍后重试。")
        return True


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.setup:
        setup_checkpoints(settings.database_url)
        print("Schedule checkpoint schema ready.")
        return
    worker = Worker(settings)
    try:
        if args.once:
            worker.run_once()
        else:
            while True:
                try:
                    worked = worker.run_once()
                except Exception as error:
                    log_failure(logger, "Schedule worker unavailable; retrying.", error)
                    worked = False
                if not worked:
                    time.sleep(2)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
