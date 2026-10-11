"""Direct review judgments. Legacy evidence-gated contracts remain unchanged."""
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from evaluation_model import StrictModel, Managers, ModelError, has_unsupported_telemetry
from review_flow import FOCUS_LABELS, UNTIMED_REASON_LABELS, LABELS

QUALITY_VERSION = "class-quality-v5"
WORKFLOW_VERSION = "class-agents-v3"
INVALID_MESSAGE = "回答无效，请重新回答"


class Dimension(StrictModel):
    score: int = Field(strict=True, ge=0, le=40)
    evidence: str = Field(min_length=1, max_length=800)
    basis: Literal["self_report", "inferred"]


class ThirtyDimension(Dimension):
    score: int = Field(strict=True, ge=0, le=30)


class Scored(StrictModel):
    kind: Literal["scored"]
    learning: Literal["learning", "none"]
    recall: ThirtyDimension
    understanding: Dimension
    focus: ThirtyDimension

    @model_validator(mode="after")
    def consistent_activity(self):
        if self.learning == "none" and any(item.score for item in (self.recall, self.understanding, self.focus)):
            raise ValueError("No learning activity must have zero learning quality")
        if any(has_unsupported_telemetry(item.evidence) for item in (self.recall, self.understanding, self.focus)):
            raise ValueError("Unsupported telemetry")
        return self


class Outcome(StrictModel):
    kind: Literal["outcome"]
    decision: Literal["absent", "leave", "cancelled"]
    reason: str = Field(min_length=1, max_length=1000)


class Invalid(StrictModel):
    kind: Literal["invalid_answer"]
    reason: str = Field(min_length=1, max_length=500)


QUALITY = TypeAdapter(Annotated[Scored | Outcome | Invalid, Field(discriminator="kind")])


class ScoredAssessment(Scored):
    managers: Managers

    @model_validator(mode="after")
    def safe_explanations(self):
        import re
        for manager in (self.managers.long, self.managers.short):
            if has_unsupported_telemetry(manager.argument) or re.search(
                r"\d\s*(?:[%％]|个?点)|(?:指数|价格).{0,15}(?:上涨|下跌|涨停|跌停|必涨|必跌)|(?:涨幅|跌幅|收益率)", manager.argument
            ):
                raise ValueError("Manager cannot invent telemetry or returns")
            if re.search(r"(?:不影响|不计入|无关).{0,8}(?:总分|质量分)|(?:总分|质量分).{0,8}(?:不受|无关)", manager.argument):
                raise ValueError("Manager cannot redefine scoring arithmetic")
        return self


ASSESSMENT = TypeAdapter(Annotated[ScoredAssessment | Outcome | Invalid, Field(discriminator="kind")])


def validate(value, *, assembled=False):
    try:
        return (ASSESSMENT if assembled else QUALITY).validate_python(value).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ModelError("评估结果格式无效，已保留回答，请重试。") from None


def text_step(snapshot):
    context = snapshot.get("review_context") or {}
    if context.get("participation") == "not_attended" or context.get("untimed_reason") == "not_attended":
        return "absence"
    if context.get("participation") in {"not_learning", "low_content"}:
        return "activity"
    return "learning"


def validate_branch(value, snapshot):
    """A model cannot turn an unrelated learning answer into an attendance write."""
    if value["kind"] != "invalid_answer" and (value["kind"] == "outcome") != (text_step(snapshot) == "absence"):
        raise ModelError("评估结果与当前问题不匹配，已保留回答，请重试。")
    return value


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
