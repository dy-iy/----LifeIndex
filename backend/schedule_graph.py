"""Durable proposal graph; database writes live in the idempotent apply callback.

Each proposal uses its run ID as LangGraph's configurable thread_id. The chat
thread ID in state is only business context and must not be used as that cursor.
Callbacks must not put clients, database sessions or image bytes into the state.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from schedule_model import validate_plan


class ScheduleState(TypedDict, total=False):
    run_id: str
    user_id: str
    thread_id: str
    semester_id: str | None
    input_text: str
    image_ids: list[str]
    new_image_ids: list[str]
    context: dict[str, Any]
    history: list[dict[str, Any]]
    plan: dict[str, Any]
    preview: dict[str, Any]
    reply: str
    status: str
    result: dict[str, Any] | None
    route: str
    retrieval: dict[str, Any]
    conversation_memory: dict[str, Any]
    context_policy: str
    context_code_sha256: str
    schedule_prompt: str
    image_observations: list[dict[str, Any]]
    repair_count: int
    validation_feedback: dict[str, Any]
    repair_needed: bool
    image_import_progress: dict[str, Any] | None


Callback = Callable[[ScheduleState], dict[str, Any]]


@dataclass(frozen=True)
class PlanningResult:
    """Internal server metadata is separate from the public operation schema."""
    plan: dict[str, Any]
    image_import_progress: dict[str, Any] | None = None


InterpretCallback = Callable[[ScheduleState], dict[str, Any] | PlanningResult]


def _input_error(error: HTTPException) -> dict[str, Any]:
    # These errors originate in our deterministic timetable validation, never
    # in provider responses. Do not serialize exception objects into checkpoints.
    detail = error.detail
    reply = detail if isinstance(detail, str) and len(detail) <= 1000 else "课表方案未通过校验，请补充或更正安排后重试。"
    return {"status": "needs_input", "reply": reply, "preview": {}, "result": None,
            "validation_feedback": {"message": reply, "conflicts": getattr(error, "conflicts", [])}}


def _check_references(state: ScheduleState, plan: dict[str, Any]) -> None:
    context = state.get("context", {})
    courses = {item.get("id"): item for item in context.get("courses", [])}
    lessons = {item.get("id"): item for item in context.get("lessons", [])}
    for operation in plan["operations"]:
        for field, items in (("course_id", courses), ("lesson_id", lessons)):
            if field not in operation:
                continue
            item = items.get(operation[field])
            if item is None or item.get("version") != operation.get("version"):
                raise HTTPException(422, "方案引用的课程不在当前课表中，或版本已变化，请重新描述需要修改的课程。")


def build_graph(checkpointer, interpret: InterpretCallback, preview: Callback, apply: Callback,
                *, retrieve: Callback | None = None, memory: Callback | None = None,
                extract: Callback | None = None, legacy: bool = False):
    """Compile a synchronous graph with a durable human-confirmation boundary.

The apply callback must be atomic and idempotent by run_id: checkpoint writes
and application transactions cannot commit atomically together, so a recovered
worker may call apply again after the application transaction already committed.
"""
    def route_node(state: ScheduleState):
        view_only = {"查看课表", "看看课表", "查看我的课表", "看看我的课表", "今天有什么课", "本周有什么课"}
        route = "read" if not state.get("new_image_ids", state.get("image_ids")) and state.get("input_text", "").strip(" 。？！!?") in view_only else "plan"
        return {"route": route}

    def retrieval_node(state):
        return {"retrieval": retrieve(state) if retrieve else {}}

    def memory_node(state):
        return {"conversation_memory": memory(state) if memory else {}}

    def image_node(state):
        return {"image_observations": extract(state) if extract else []}

    def can_repair(state):
        return state.get("schedule_prompt") in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"} and state.get("repair_count", 0) < 2

    def repair_node(state):
        return {"repair_count": state.get("repair_count", 0) + 1, "repair_needed": False}

    def interpret_node(state: ScheduleState):
        try:
            value = interpret(state)
            progress = value.image_import_progress if isinstance(value, PlanningResult) else None
            plan = validate_plan(value.plan if isinstance(value, PlanningResult) else value)
        except HTTPException as error:
            if error.status_code not in {409, 422}:
                # Quota/auth/service errors need explicit retry, not another
                # automatic planning attempt or a misleading information prompt.
                raise
            value = _input_error(error)
            value.update(plan=getattr(error, "plan", state.get("plan")), repair_needed=can_repair(state))
            return value
        if state.get("route") == "read" and plan["operations"]:
            return _input_error(HTTPException(422, "本次仅查询课表，不会修改课程。请明确说明需要怎样调整。"))
        questions = plan["questions"]
        reply = plan["reply"]
        missing_questions = [question for question in questions if question not in reply]
        if missing_questions:
            reply += "\n" + "\n".join(missing_questions)
        if (state.get("schedule_prompt") in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"} and state.get("route") != "read"
                and not questions and not plan["operations"]
                and ((state.get("conversation_memory") or {}).get("pending_proposal") or state.get("image_ids"))):
            # Prose claiming a preserved/confirmable proposal is not a real preview.
            # Continue the active task, with a finite repair budget, rather than
            # sending the user into an impossible confirmation loop.
            requested_cancel = state.get("input_text", "").strip(" 。！!") in {"取消", "取消导入", "取消这次导入", "取消本次任务", "不用了"}
            if not requested_cancel:
                value = _input_error(HTTPException(422, "当前导入方案尚未通过核对，原图和先前要求已保留，暂时没有可确认的预览。"))
                value.update(plan=plan, repair_needed=can_repair(state))
                value["validation_feedback"]["kind"] = "empty_active_proposal"
                return value
            return {"plan": plan, "reply": "已取消这次课表修改。", "status": "cancelled", "preview": {}, "result": None}
        return {
            "plan": plan,
            "reply": reply,
            "status": "needs_input" if questions else ("planning" if plan["operations"] else "completed"),
            "preview": {},
            "result": None,
            "repair_needed": False,
            "image_import_progress": progress,
        }

    def after_interpret(state: ScheduleState):
        if state.get("repair_needed"):
            return "repair"
        return "preview" if state["status"] == "planning" else END

    def preview_node(state: ScheduleState):
        try:
            _check_references(state, state["plan"])
            value = preview(state)
        except HTTPException as error:
            value = _input_error(error)
            value["repair_needed"] = can_repair(state)
            return value
        # Persist the status *before* entering the interrupting node. Updates
        # returned by a node that interrupts would otherwise never be committed.
        return {"preview": value, "status": "awaiting_confirmation", "repair_needed": False, "validation_feedback": {}}

    def after_preview(state: ScheduleState):
        if state.get("repair_needed"):
            return "repair"
        return "confirm" if state["status"] == "awaiting_confirmation" else END

    def confirm_node(state: ScheduleState) -> Command[Literal["apply", "confirm", "__end__"]]:
        response = interrupt({"kind": "confirm", "preview": state["preview"]})
        decision = response.get("decision") if isinstance(response, dict) else None
        if decision == "apply":
            return Command(goto="apply", update={"status": "applying"})
        if decision == "cancel":
            return Command(goto=END, update={"status": "cancelled", "reply": "已取消这次课表修改。", "result": None})
        # Unexpected resume payloads cannot authorize writes. Re-entering the
        # node creates a fresh interrupt without replaying a loop of side effects.
        return Command(goto="confirm", update={"reply": "请确认应用或取消本次方案。"})

    def apply_node(state: ScheduleState):
        try:
            result = apply(state)
        except HTTPException as error:
            return _input_error(error)
        return {"result": result, "status": "applied", "reply": "已按确认的方案更新课表。"}

    builder = StateGraph(ScheduleState)
    builder.add_node("interpret", interpret_node)
    builder.add_node("preview", preview_node)
    builder.add_node("confirm", confirm_node)
    builder.add_node("apply", apply_node)
    builder.add_node("route_request", route_node)
    builder.add_node("retrieve_knowledge", retrieval_node)
    builder.add_node("recall_memory", memory_node)
    builder.add_node("extract_images", image_node)
    builder.add_node("repair", repair_node)
    builder.add_edge(START, "route_request")
    builder.add_edge("route_request", "retrieve_knowledge")
    builder.add_edge("route_request", "recall_memory")
    if legacy:
        # Old checkpoints already scheduled a two-branch barrier. Its channel
        # identity must remain identical until that invocation finishes.
        builder.add_edge(["retrieve_knowledge", "recall_memory"], "interpret")
    else:
        builder.add_edge("route_request", "extract_images")
        builder.add_edge(["retrieve_knowledge", "recall_memory", "extract_images"], "interpret")
    builder.add_conditional_edges("interpret", after_interpret, ["preview", "repair", END])
    builder.add_conditional_edges("preview", after_preview, ["confirm", "repair", END])
    builder.add_edge("repair", "interpret")
    builder.add_edge("apply", END)
    return builder.compile(checkpointer=checkpointer)
