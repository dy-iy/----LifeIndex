"""Durable one-pass timetable graph: understand, then save without interruption.

The graph cursor is a run ID. The application callback must be atomic and
idempotent by that ID because application commits and graph checkpoints are
separate transactions.
"""
from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class ScheduleState(TypedDict, total=False):
    run_id: str
    user_id: str
    thread_id: str
    semester_id: str | None
    input_text: str
    image_ids: list[str]
    period_time_setup_pending: bool
    context: dict[str, Any]
    history: list[dict[str, Any]]
    plan: dict[str, Any]
    reply: str
    status: str
    result: dict[str, Any] | None
    schedule_prompt: str


Callback = Callable[[ScheduleState], dict[str, Any]]


def build_graph(checkpointer, interpret: Callback, apply: Callback):
    def interpret_node(state):
        understanding = interpret(state)
        return {"plan": understanding, "status": "saving"}

    def apply_node(state):
        result = apply(state)
        return {"result": result, "status": "completed" if state["plan"].get("intent") == "read" else "applied",
                "reply": result.get("reply", "已保存识别到的课表信息。")}

    builder = StateGraph(ScheduleState)
    builder.add_node("interpret", interpret_node)
    builder.add_node("apply", apply_node)
    builder.add_edge(START, "interpret")
    builder.add_edge("interpret", "apply")
    builder.add_edge("apply", END)
    return builder.compile(checkpointer=checkpointer)
