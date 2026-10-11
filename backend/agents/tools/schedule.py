"""Scoped timetable read, recall, retrieval, preview and confirmed-apply tools."""
import uuid
from fastapi import HTTPException
from sqlalchemy import select
from agents.memory import recall_schedule
from agents.retrieval import schedule_knowledge
from agents.schedule_memory import MAX_TASK_TURNS, TaskMemoryError, active_task_rows, build_pending_proposal
from models import ScheduleRun, ScheduleThread


class ScheduleTools:
    def __init__(self, worker, state):
        self.worker, self.state = worker, state
        self.user_id, self.run_id = uuid.UUID(state["user_id"]), uuid.UUID(state["run_id"])

    def records(self, db):
        run = db.get(ScheduleRun, self.run_id)
        if run is None or run.user_id != self.user_id or str(run.thread_id) != self.state["thread_id"]:
            raise HTTPException(404, "未找到当前课表任务。")
        thread = db.get(ScheduleThread, run.thread_id)
        if thread.user_id != self.user_id:
            raise HTTPException(404, "未找到当前课表对话。")
        return run, thread

    def memory(self):
        with self.worker.factory() as db:
            run, thread = self.records(db)
            remembered = recall_schedule(db, run.user_id, thread.id, thread.semester_id)
            rows = db.scalars(select(ScheduleRun).where(ScheduleRun.user_id == run.user_id,
                ScheduleRun.thread_id == thread.id, ScheduleRun.created_at < run.created_at)
                .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc()).limit(MAX_TASK_TURNS + 1)).all()
            try:
                pending = build_pending_proposal(rows, current=run)
                active = active_task_rows(rows)
            except TaskMemoryError as error:
                raise HTTPException(422, str(error)) from None
            active_sources = {str(row.id) for row in active}
            return {"pending_proposal": pending,
                    "recent_user_statements": [row.input_text for row in active[-12:]],
                    "confirmed_history": [{"source": item["source_id"], "status": item["status"],
                        "input": item["input"][:600]} for item in remembered
                        if item["status"] == "applied" or item["source_id"] in active_sources][:8]}

    def retrieve(self):
        with self.worker.factory() as db:
            run, thread = self.records(db)
            query = " ".join([item.get("input", "") for item in self.state.get("history", [])[-3:]] + [run.input_text])
            return schedule_knowledge(db, run.user_id, thread.id, thread.semester_id, query[:6000])
