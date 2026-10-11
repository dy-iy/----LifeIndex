"""Neutral review summaries and independent manager signals; no model prices."""
from typing import Annotated, Literal
import re
from pydantic import Field, TypeAdapter, ValidationError, model_validator
from evaluation_model import StrictModel, ModelError, has_unsupported_telemetry
from review_v5 import Outcome, Invalid, model_record, text_step, INVALID_MESSAGE

QUALITY_VERSION = "class-summary-v6"
WORKFLOW_VERSION = "class-agents-v4"
FIELDS = ("activity", "recall", "understanding", "focus")
EVIDENCE_IDS = ["summary:" + name for name in FIELDS]


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


class Reports(StrictModel):
    long: ManagerReport
    short: ManagerReport


class Assessment(Summary):
    managers: Reports


class JoinReports(StrictModel):
    long_report: ManagerReport
    short_report: ManagerReport


QUALITY = TypeAdapter(Annotated[Summary | Outcome | Invalid, Field(discriminator="kind")])
ASSESSMENT = TypeAdapter(Annotated[Assessment | Outcome | Invalid, Field(discriminator="kind")])


def validate(value, *, assembled=False):
    try:
        return (ASSESSMENT if assembled else QUALITY).validate_python(value).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ModelError("复盘总结或经理判断格式无效，已保留回答，请重试。") from None


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
