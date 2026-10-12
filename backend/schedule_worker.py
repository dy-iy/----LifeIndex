"""Durable LangGraph worker. Run schema setup explicitly before starting it."""
import argparse
from copy import deepcopy
import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import time
import uuid

from dotenv import load_dotenv
from fastapi import HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
import psycopg
from psycopg import sql
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import sessionmaker

from auth import utcnow
from config import Settings
from db import make_engine, checkpoint_location
from diagnostics import log_failure
from models import ScheduleImage, ScheduleRun, ScheduleThread, ScheduleModelCall, User
from agents.memory import remember_schedule
from agents.context import CONTEXT_VERSION, CONTEXT_CODE_SHA256
from agents.schedule_memory import MAX_TASK_TURNS, TaskMemoryError, active_task_rows, completes_import, validate_import_progress
from agents.tools.registry import Scope, record_trace
from agents.tools.schedule import ScheduleTools
from schedule_api import latest_run, touch
from schedule_budget import reserve, settle
from schedule_graph import PlanningResult, build_graph
from schedule_model import ModelError, prepare_messages, invoke_planner, validate_plan
from schedule_plan import apply_plan, context_snapshot, preview_plan

logger = logging.getLogger(__name__)


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
        # A fencing lock protects an entire graph invocation, including slow
        # provider work, even if another process reclaims an expired job lease.
        lock_id = int.from_bytes(uuid.UUID(str(run_id)).bytes[:8], "big", signed=True)
        acquired = saver.conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_id,)).fetchone()["acquired"]
        if not acquired:
            yield None
            return
        try:
            yield saver
        finally:
            saver.conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))


def compact_context(context):
    fields = {"id", "course_id", "version", "name", "local_date", "start_time", "end_time", "status", "is_override", "week_number", "has_learning_record"}
    return {**context, "lessons": [{key: value for key, value in item.items() if key in fields}
                                  for item in context.get("lessons", [])]}


def compact_history(history):
    """Keep user answers and the latest unfinished proposal, not repeated plans."""
    rows = history[-12:]
    last_plan = next((index for index in range(len(rows) - 1, -1, -1)
                      if (rows[index].get("unapplied_plan") or {}).get("operations")), None)
    return [{**row, "reply": row["reply"][:1200 if index == len(rows) - 1 else 500],
             "unapplied_plan": row.get("unapplied_plan") if index == last_plan else None}
            for index, row in enumerate(rows)]


class Worker:
    def __init__(self, settings, planner=invoke_planner, image_reader=None):
        self.settings, self.planner = settings, planner
        self.image_reader = image_reader
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
            run.attempts += 1
            run.updated_at = now
            # Every visible transition invalidates known_version polling,
            # including the terminal failure after repeated lease expiry.
            db.execute(update(ScheduleThread).where(ScheduleThread.id == run.thread_id).values(
                version=ScheduleThread.version + 1, updated_at=now))
            if run.attempts > 3:
                run.status, run.error = "failed", "任务多次中断，已保留对话和方案，请手动重试。"
                run.lease_owner, run.lease_until = None, None
                db.commit()
                return None
            lease_owner = uuid.uuid4()
            run.status = "confirming" if run.decision else "running"
            run.lease_owner, run.lease_until = lease_owner, now + timedelta(minutes=5)
            db.commit()
            return run.id, lease_owner

    def load_state(self, run_id):
        with self.factory() as db:
            run = db.get(ScheduleRun, run_id)
            thread = db.get(ScheduleThread, run.thread_id)
            history = []
            image_ids = []
            historical_images = []
            rows = db.scalars(select(ScheduleRun).where(ScheduleRun.user_id == run.user_id,
                ScheduleRun.thread_id == thread.id, ScheduleRun.created_at <= run.created_at)
                .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc()).limit(MAX_TASK_TURNS + 1)).all()
            try:
                rows = active_task_rows(rows)
            except TaskMemoryError as error:
                raise ModelError(str(error), status_code=422) from None
            for item in rows:
                if item.status == "applied":
                    if completes_import(item.plan, item.result):
                        historical_images = []
                    else:
                        historical_images.extend(image_ids + item.image_ids)
                    image_ids = []
                elif item.status == "cancelled":
                    historical_images, image_ids = [], []
                if item.id != run.id:
                    history.append({"input": item.input_text, "confirmation": item.confirmation_text,
                                    "reply": item.reply, "status": item.status,
                                    "unapplied_plan": item.plan if item.status in {"needs_input", "superseded"} else None})
                if item.status not in {"applied", "cancelled"}:
                    image_ids.extend(item.image_ids)
                if item.id == run.id:
                    break
            def usable_image(image_id):
                image = db.get(ScheduleImage, uuid.UUID(image_id))
                if image is None or image.user_id != run.user_id or image.thread_id != thread.id:
                    return False
                if image.expires_at > utcnow() and image.data is not None:
                    return True
                from agents.schedule_image_reader import image_cache_key
                cached = db.scalar(select(ScheduleModelCall.response).join(ScheduleRun,
                    ScheduleRun.id == ScheduleModelCall.run_id).where(
                    ScheduleModelCall.user_id == run.user_id, ScheduleRun.thread_id == thread.id,
                    ScheduleModelCall.input_hash == image_cache_key(image_id),
                    ScheduleModelCall.response.is_not(None)).order_by(ScheduleModelCall.created_at).limit(1))
                return isinstance(cached, dict) and cached.get("kind") == "image_reading"

            if run.image_ids:
                # A replacement upload can recover an expired earlier image;
                # otherwise report the expiry and ask the user to reattach it.
                image_ids = [image_id for image_id in image_ids if image_id in run.image_ids or usable_image(image_id)]
            # A small applied edit (e.g. deleting an old course) must not erase
            # the screenshot needed to finish the same conversation's import.
            historical_images = [image_id for image_id in dict.fromkeys(historical_images) if usable_image(image_id)]
            return {"run_id": str(run.id), "thread_id": str(thread.id), "user_id": str(run.user_id),
                    "context_policy": CONTEXT_VERSION,
                    "schedule_prompt": "schedule_v5.txt",
                    "context_code_sha256": CONTEXT_CODE_SHA256,
                    "semester_id": str(thread.semester_id) if thread.semester_id else None,
                    "new_image_ids": list(run.image_ids),
                    "input_text": run.input_text, "image_ids": list(dict.fromkeys(historical_images + image_ids)),
                    "context": compact_context(context_snapshot(db, run.user_id, thread.semester_id)),
                    "history": compact_history(history)}

    def extract_images(self, state):
        """Cache image facts per owned immutable upload, independently of chat turns."""
        if state.get("schedule_prompt") not in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"} or state.get("route") == "read":
            return []
        identifiers = state.get("image_ids", [])
        if len(identifiers) > 6 or len(identifiers) != len(set(identifiers)):
            raise ModelError("一段待处理的安排最多使用六张不同课表图片，请先完成当前导入。", status_code=422)
        from agents.schedule_vision import analyze_schedule_image
        from agents.schedule_image_reader import image_cache_key, prepare_image_messages, bind_reading, invoke_image_reader
        facts = []
        for image_id in state.get("image_ids", []):
            digest = image_cache_key(image_id)
            with self.factory() as db:
                item = db.get(ScheduleImage, uuid.UUID(image_id))
                if item is None or str(item.user_id) != state["user_id"] or str(item.thread_id) != state["thread_id"]:
                    raise ModelError("图片不可用，请重新选择上传。", status_code=422)
                cached = db.scalar(select(ScheduleModelCall.response).join(ScheduleRun, ScheduleRun.id == ScheduleModelCall.run_id)
                    .where(ScheduleModelCall.user_id == item.user_id, ScheduleRun.thread_id == item.thread_id,
                           ScheduleModelCall.input_hash == digest, ScheduleModelCall.response.is_not(None))
                    .order_by(ScheduleModelCall.created_at).limit(1))
                if cached and cached.get("kind") == "image_reading":
                    facts.append(bind_reading(cached["reading"], cached["layout"], image_id))
                    continue
                if item.expires_at <= utcnow() or not item.data:
                    raise ModelError("之前的课表图片已过期，请重新上传并继续说明。", status_code=422)
                layout = analyze_schedule_image(item.data, item.mime_type)
                if layout is None:
                    # Other layouts retain the existing bounded vision path;
                    # uncertain geometry must never supply invented coordinates.
                    continue
                pending = db.scalars(select(ScheduleModelCall.id).where(ScheduleModelCall.user_id == item.user_id,
                    ScheduleModelCall.run_id == uuid.UUID(state["run_id"]), ScheduleModelCall.input_hash == digest,
                    ScheduleModelCall.status == "reserved")).all()
            for call_id in pending:
                settle(self.factory, call_id, {})
            messages = prepare_image_messages(layout, {"data": item.data, "mime_type": item.mime_type})
            call_id = reserve(self.factory, state["user_id"], state["run_id"], messages, self.settings, input_hash=digest)
            answer = None
            try:
                reader = self.image_reader or invoke_image_reader
                answer = reader(messages, api_key=os.getenv("DEEPSEEK_API_KEY", "").strip())
                observation = bind_reading(answer["reading"], layout, image_id)
            except ModelError as error:
                settle(self.factory, call_id, answer.get("usage", {}) if answer else error.usage)
                raise
            except Exception as error:
                log_failure(logger, "Schedule image reading failed run_id=%s image_id=%s", error,
                            state["run_id"], image_id)
                settle(self.factory, call_id, {})
                raise ModelError("课表图像读取暂未完成，原图和要求已保留，请重试。") from None
            settle(self.factory, call_id, answer.get("usage", {}),
                {"kind": "image_reading", "reading": answer["reading"], "layout": layout.metadata()})
            facts.append(observation)
        if facts and len(facts) != len(identifiers):
            # A progress manifest covers checked cards only. Calling a mixed
            # batch complete would abandon the unrecognised uploads.
            raise ModelError("不同格式的课表图片暂不能同批导入，请分别上传到新对话；原图与要求已保留。", status_code=422)
        return facts

    def interpret(self, state, *, with_progress=False):
        # Keep the shared evaluation compiler stable. The task capsule is its
        # existing protected slot for required schedule facts and repair feedback.
        state = deepcopy(state)
        if state.get("schedule_prompt") in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"}:
            memory = state.setdefault("conversation_memory", {})
            pending = memory.get("pending_proposal") or {"source": state["run_id"], "plan": None}
            if state.get("image_observations") or state.get("validation_feedback"):
                memory["pending_proposal"] = {**pending, "image_observations": state.get("image_observations", []),
                    "validation_feedback": state.get("validation_feedback", {}),
                    "candidate_plan": state.get("plan") if state.get("validation_feedback") else None}
        images = []
        observed = {item["image_id"] for item in state.get("image_observations", [])}
        with self.factory() as db:
            for image_id in ([] if state.get("route") == "read" else state.get("image_ids", [])):
                if image_id in observed:
                    continue
                item = db.get(ScheduleImage, uuid.UUID(image_id))
                if item is None or str(item.user_id) != state["user_id"] or str(item.thread_id) != state["thread_id"]:
                    raise ModelError("图片不可用，请重新选择上传。", status_code=422)
                if item.expires_at <= utcnow() or item.data is None:
                    raise ModelError("之前的课表图片已过期，请重新上传并继续说明。", status_code=422)
                images.append({"mime_type": item.mime_type, "data": item.data})
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ModelError("课表助手尚未配置模型服务，请联系管理员。", status_code=503)
        prepared = prepare_messages(state, images)
        messages = prepared["messages"]
        digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.factory() as db:
            cached = db.scalar(select(ScheduleModelCall.response).where(
                ScheduleModelCall.user_id == uuid.UUID(state["user_id"]), ScheduleModelCall.run_id == uuid.UUID(state["run_id"]),
                ScheduleModelCall.input_hash == digest, ScheduleModelCall.response.is_not(None)).limit(1))
            pending = db.scalars(select(ScheduleModelCall.id).where(ScheduleModelCall.run_id == uuid.UUID(state["run_id"]),
                ScheduleModelCall.user_id == uuid.UUID(state["user_id"]), ScheduleModelCall.input_hash == digest,
                ScheduleModelCall.status == "reserved")).all()
        for call in pending:
            settle(self.factory, call, {})
        record_trace(self.factory, Scope(uuid.UUID(state["user_id"]), uuid.UUID(state["run_id"]), "schedule-v2", "context"),
                     "context_schedule", "succeeded", time.monotonic(), {**prepared["manifest"], "cache_hit": cached is not None})
        if cached is not None:
            return self._compiled_plan(cached, state, with_progress=with_progress)
        call_id = reserve(self.factory, state["user_id"], state["run_id"], messages, self.settings, input_hash=digest)
        result = None
        try:
            result = self.planner(messages, api_key=key)
            if state.get("schedule_prompt") in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"}:
                from agents.schedule_image_reader import validate_image_planning_plan
                result["plan"] = validate_image_planning_plan(result["plan"])
            else:
                result["plan"] = validate_plan(result["plan"])
        except ModelError as error:
            settle(self.factory, call_id, result.get("usage", {}) if result else error.usage)
            raise
        except Exception as error:
            log_failure(logger, "Schedule planning failed run_id=%s", error, state["run_id"])
            settle(self.factory, call_id, {})
            raise ModelError("课表助手暂时无法处理，请稍后重试。") from None
        settle(self.factory, call_id, result.get("usage", {}), result["plan"])
        return self._compiled_plan(result["plan"], state, with_progress=with_progress)

    @staticmethod
    def _compiled_plan(plan, state, *, with_progress=False):
        if state.get("schedule_prompt") not in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"}:
            return validate_plan(plan)
        from agents.schedule_image_reader import compile_image_imports_with_progress
        try:
            pending = (state.get("conversation_memory") or {}).get("pending_proposal") or {}
            compiled, progress = compile_image_imports_with_progress(plan, state.get("image_observations", []),
                pending.get("image_import_progress"),
                persist_periods=state.get("schedule_prompt") in {"schedule_v4.txt", "schedule_v5.txt"},
                semester_timezone=state["context"]["timezone"] if state.get("schedule_prompt") == "schedule_v5.txt" else "Asia/Shanghai")
            return PlanningResult(compiled, progress) if with_progress else compiled
        except HTTPException as error:
            error.plan = plan
            raise

    def memory(self, state):
        return ScheduleTools(self, state).memory()

    def retrieve(self, state):
        return ScheduleTools(self, state).retrieve()

    @staticmethod
    def execution_plan(state):
        # Pre-v5 checkpoints may omit a create-semester zone. Preserve the
        # already previewed default when resuming those frozen proposals.
        plan = deepcopy(state["plan"])
        for operation in plan.get("operations", []):
            if operation["kind"] == "create_semester" and not operation.get("timezone"):
                operation["timezone"] = (state["context"]["timezone"]
                    if state.get("schedule_prompt") == "schedule_v5.txt" else "Asia/Shanghai")
        return plan

    def preview(self, state):
        with self.factory() as db:
            user = db.scalar(select(User).where(User.id == uuid.UUID(state["user_id"])).with_for_update())
            # The executor rolls back its own savepoint; no business records are saved.
            return preview_plan(db, user, state["semester_id"], self.execution_plan(state), state["run_id"])

    def apply(self, state, lease_owner):
        with self.factory() as db:
            user = db.scalar(select(User).where(User.id == uuid.UUID(state["user_id"])).with_for_update())
            run = db.scalar(select(ScheduleRun).where(ScheduleRun.id == uuid.UUID(state["run_id"])).with_for_update())
            if run is None or run.user_id != user.id or str(run.thread_id) != state["thread_id"]:
                raise HTTPException(404, "未找到当前课表任务。")
            if run.status == "applied":
                return run.result
            if run.lease_owner != lease_owner or run.decision != "apply" or run.status != "confirming":
                raise HTTPException(409, "方案状态已经变化，请读取最新对话。")
            thread = db.get(ScheduleThread, run.thread_id)
            current = latest_run(db, thread.id)
            if current.id != run.id:
                raise HTTPException(409, "方案已被后续对话替代，请读取最新对话。")
            result = apply_plan(db, user, thread.semester_id, self.execution_plan(state), run.id, state["preview"]["baseline_hash"])
            if state.get("image_import_progress") is not None:
                # Derived only from owned source rows and the compiled proposal.
                # Save it in the same transaction as course writes/idempotency.
                result["image_import_progress"] = validate_import_progress(state["image_import_progress"])
            thread.semester_id = uuid.UUID(result["semester_id"])
            touch(thread)
            run.status, run.result, run.reply = "applied", result, "已按确认的方案更新课表。"
            run.lease_owner, run.lease_until, run.error = None, None, ""
            run.updated_at = utcnow()
            remember_schedule(db, run, thread)
            if completes_import(state["plan"], result):
                db.execute(update(ScheduleImage).where(ScheduleImage.user_id == user.id,
                    ScheduleImage.thread_id == thread.id,
                    ScheduleImage.id.in_([uuid.UUID(item) for item in state.get("image_ids", [])])).values(data=None))
            db.commit()
            return result

    def finish(self, run_id, lease_owner, state=None, error=None):
        with self.factory() as db:
            initial = db.get(ScheduleRun, run_id)
            db.scalar(select(User).where(User.id == initial.user_id).with_for_update())
            run = db.scalar(select(ScheduleRun).where(ScheduleRun.id == run_id).with_for_update().execution_options(populate_existing=True))
            if run.lease_owner != lease_owner or run.status == "applied":
                return
            thread = db.get(ScheduleThread, run.thread_id)
            if error is not None:
                run.status, run.error = "failed", str(error)[:1000]
            else:
                run.status = state.get("status", "failed")
                run.reply = state.get("reply", "")[:12000]
                run.plan = state.get("plan")
                run.preview = state.get("preview") or None
                run.error = ""
                if run.status == "needs_input" and run.decision:
                    run.status = "stale"
                    run.error = run.reply
                if run.status == "cancelled":
                    db.execute(update(ScheduleImage).where(ScheduleImage.user_id == run.user_id,
                        ScheduleImage.thread_id == thread.id,
                        ScheduleImage.id.in_([uuid.UUID(item) for item in state.get("image_ids", [])])).values(data=None))
            run.lease_owner, run.lease_until = None, None
            run.updated_at = utcnow()
            touch(thread)
            if error is None:
                remember_schedule(db, run, thread)
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
                graph = build_graph(saver, lambda state: self.interpret(state, with_progress=True), self.preview, lambda state: self.apply(state, lease_owner),
                                    retrieve=self.retrieve, memory=self.memory, extract=self.extract_images)
                config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 20}
                snapshot = graph.get_state(config)
                if snapshot.values and snapshot.values.get("schedule_prompt") not in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"}:
                    graph = build_graph(saver, lambda state: self.interpret(state, with_progress=True), self.preview, lambda state: self.apply(state, lease_owner),
                                        retrieve=self.retrieve, memory=self.memory, legacy=True)
                    snapshot = graph.get_state(config)
                with self.factory() as db:
                    run = db.get(ScheduleRun, run_id)
                    decision = run.decision
                    # A previous owner may have finished while this worker waited.
                    if run.lease_owner != lease_owner or run.status == "applied":
                        return True
                if snapshot.values and not snapshot.next:
                    result = snapshot.values
                elif decision and any(task.interrupts for task in snapshot.tasks):
                    result = graph.invoke(Command(resume={"decision": decision}), config, durability="sync")
                elif snapshot.values:
                    result = graph.invoke(None, config, durability="sync")
                elif decision:
                    raise HTTPException(409, "方案恢复信息不可用，请重新发送说明生成方案。")
                else:
                    result = graph.invoke(self.load_state(run_id), config, durability="sync")
                self.finish(run_id, lease_owner, result)
        except ModelError as error:
            self.finish(run_id, lease_owner, error=error.message)
        except HTTPException as error:
            self.finish(run_id, lease_owner, error=error.detail if isinstance(error.detail, str) else "任务无法处理，请重试。")
        except Exception as error:
            log_failure(logger, "Schedule worker failed run_id=%s", error, run_id)
            self.finish(run_id, lease_owner, error="处理暂时中断，已保留对话。请稍后重试。")
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
