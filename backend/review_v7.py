"""Current summary and single-manager contract. Historical versions stay frozen."""
from typing import Annotated, Literal
import re
from pydantic import Field, TypeAdapter, ValidationError, model_validator
from evaluation_model import StrictModel, ModelError, has_unsupported_telemetry
from review_flow import FOCUS_LABELS, UNTIMED_REASON_LABELS, LABELS

QUALITY_VERSION = "class-summary-v7"
WORKFLOW_VERSION = "class-agents-v5"
FIELDS = ("activity", "recall", "understanding", "focus")
EVIDENCE_IDS = ["summary:" + name for name in FIELDS]

class Outcome(StrictModel):
    kind: Literal["outcome"]
    decision: Literal["absent", "leave", "cancelled"]
    reason: str = Field(min_length=1, max_length=1000)


class Invalid(StrictModel):
    kind: Literal["invalid_answer"]
    reason: str = Field(min_length=1, max_length=500)


def text_step(snapshot):
    context = snapshot.get("review_context") or {}
    if context.get("participation") == "not_attended" or context.get("untimed_reason") == "not_attended":
        return "absence"
    if context.get("participation") in {"not_learning", "low_content"}:
        return "activity"
    return "learning"


def model_record(snapshot):
    context = snapshot.get("review_context") or {}
    step = text_step(snapshot)
    participation = context.get("participation", "unspecified")
    return {**snapshot, "review_context": context, "current_step": step,
        "current_question": {"learning": "简述这节课学到了什么", "absence": "未到课原因",
            "activity": "没听课的话，这节课在做什么呢？" if participation == "not_learning" else "这节课在做什么"}[step],
        "participation_label": {"not_learning": "到了，但基本没听", "not_attended": "未到课"}.get(participation, LABELS.get(participation, "未选择")),
        "focus_label": FOCUS_LABELS.get(context.get("focus")),
        "untimed_reason_label": UNTIMED_REASON_LABELS.get(context.get("untimed_reason"))}


class Observation(StrictModel):
    text: str = Field(min_length=1, max_length=700)
    basis: Literal["self_report", "inferred", "not_reported"]


class Summary(StrictModel):
    kind: Literal["summary"]
    learning: Literal["learning", "none", "unclear"]
    activity: Observation
    recall: Observation
    understanding: Observation
    focus: Observation

    @model_validator(mode="after")
    def no_fabricated_measurements(self):
        if any(has_unsupported_telemetry(getattr(self, name).text) for name in FIELDS):
            raise ValueError("Unsupported telemetry")
        return self


class ManagerReport(StrictModel):
    direction: Literal["bullish", "bearish", "neutral"]
    strength: int = Field(strict=True, ge=0, le=100)
    argument: str = Field(min_length=1, max_length=600)
    evidence_refs: list[Literal["summary:activity", "summary:recall", "summary:understanding", "summary:focus"]] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def valid_signal(self):
        if (self.direction == "neutral") != (self.strength == 0):
            raise ValueError("Neutral must be zero; directional signals must have positive strength")
        if has_unsupported_telemetry(self.argument) or re.search(r"\d\s*[%％]|(?:指数|价格).{0,15}(?:必涨|必跌|涨停|跌停)", self.argument):
            raise ValueError("Manager cannot promise a return or fabricate telemetry")
        return self


def validate_manager(value):
    try:
        return ManagerReport.model_validate(value).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        raise ModelError("经理方向、强度或总结引用无效，已保留总结，请重试。") from None


def validate_branch(value, snapshot):
    if value["kind"] != "invalid_answer" and (value["kind"] == "outcome") != (text_step(snapshot) == "absence"):
        raise ModelError("复盘总结与当前问题不匹配，已保留回答，请重试。")
    return value


def summary_view(value, snapshot):
    """Authoritative context is attached by the server, never rewritten by a model."""
    record = model_record(snapshot)
    return {"course": record["course"], "current_question": record["current_question"],
        "participation": record["participation_label"], "focus_choice": record["focus_label"],
        "untimed_reason": record["untimed_reason_label"], "learning": value["learning"],
        **{name: value[name] for name in FIELDS}}


QUALITY = TypeAdapter(Annotated[Summary | Outcome | Invalid, Field(discriminator="kind")])


class Reports(StrictModel):
    long_short: ManagerReport


class Assessment(Summary):
    managers: Reports


class JoinReports(StrictModel):
    long_short_report: ManagerReport


ASSESSMENT = TypeAdapter(Annotated[Assessment | Outcome | Invalid, Field(discriminator="kind")])


def validate(value, *, assembled=False):
    try:
        return (ASSESSMENT if assembled else QUALITY).validate_python(value).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ModelError("复盘总结或多空判断格式无效，已保留回答，请重试。") from None
