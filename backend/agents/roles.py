"""Independent quality and fund-manager contracts; no role can write a price."""
import json
from typing import Annotated
from pydantic import Field, ValidationError
from agents.model_client import invoke_json
from agents.prompts import load_prompt
from agents.context import CONTEXT_VERSION, LEGACY_CONTEXT, ContextError, evaluation_context, encode, byte_size, digest
from evaluation_model import (StrictModel, FortyDimension, FocusDimension, ThirtyDimension, validate_assessment, ModelError)


class Quality(StrictModel):
    recall: FortyDimension
    understanding: FortyDimension
    focus: FocusDimension
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    missing_information: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=5)
    question: str | None = Field(default=None, min_length=1, max_length=400)


class ManagerReport(StrictModel):
    argument: str = Field(min_length=1, max_length=600)
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(min_length=1, max_length=8)


class QualityV4(Quality):
    recall: ThirtyDimension
    focus: ThirtyDimension


def validate_quality(value, *, scoring_version=None):
    if scoring_version == "class-summary-v7":
        from review_v7 import validate
        return validate(value)
    if scoring_version == "class-summary-v6":
        from review_v6 import validate
        return validate(value)
    if scoring_version == "class-quality-v5":
        from review_v5 import validate
        return validate(value)
    try:
        schema = QualityV4 if scoring_version == "class-quality-v4" else Quality
        quality = schema.model_validate(value).model_dump(mode="json")
        # Reuse the established evidence, bounds and fake-telemetry safeguards.
        validate_assessment({**quality, "managers": {"long": {"argument": "等待独立观点。"},
                                                    "short": {"argument": "等待独立观点。"}}}, scoring_version=scoring_version)
        return quality
    except (ValidationError, ValueError, TypeError):
        raise ModelError("质量评估格式或证据不完整，已保留复盘，请重试。") from None


def current_evidence_ids(answer_count=3):
    return [f"assessment:{key}" for key in ("recall", "understanding", "focus")] + [
        f"record:answer:{i}" for i in range(1, answer_count + 1)]


def validate_manager(value, quality, retrieval, *, evidence_ids=None, scoring_version=None):
    if scoring_version in {"class-summary-v6", "class-summary-v7"}:
        from review_v6 import validate_manager as validate_signal
        return validate_signal(value)
    try:
        result = ManagerReport.model_validate(value).model_dump(mode="json")
        current = set(current_evidence_ids() if evidence_ids is None else evidence_ids)
        allowed = current | {item["id"] for item in retrieval.get("sources", [])}
        refs = set(result["evidence_refs"])
        if not refs <= allowed or not refs & current:
            raise ValueError("Invalid or non-current evidence citation")
        validate_assessment({**quality, "managers": {"long": {"argument": result["argument"]},
                                                    "short": {"argument": result["argument"]}}}, scoring_version=scoring_version)
        return result
    except (ValidationError, ValueError, TypeError):
        raise ModelError("经理观点缺少有效证据引用，已保留评分，请重试。") from None


def prepare_messages(role, snapshot, clarification, retrieval, quality=None, *, context_policy=LEGACY_CONTEXT, scoring_version=None):
    if scoring_version == "class-summary-v7":
        from agents.roles_v7 import prepare_messages as prepare_v7
        return prepare_v7(role, snapshot, clarification, retrieval, quality)
    if role not in {"quality", "long", "short"}:
        raise ValueError("Unknown role")
    if scoring_version == "class-summary-v6":
        from agents.roles_v6 import prepare_messages as prepare_v6
        return prepare_v6(role, snapshot, clarification, retrieval, quality)
    if scoring_version == "class-quality-v5":
        from agents.roles_v5 import prepare_messages as prepare_v5
        return prepare_v5(role, snapshot, clarification, retrieval, quality)
    filename = "quality_v2.txt" if role == "quality" else f"{role}_manager_v2.txt"
    if role == "quality" and snapshot.get("question_version") == "class-review-v2":
        filename = "quality_v3.txt"
    if role == "quality" and scoring_version == "class-quality-v4":
        filename = "quality_v4.txt"
    from review_flow import model_snapshot
    snapshot = model_snapshot(snapshot)
    evidence_ids = current_evidence_ids(len(snapshot["answers"]) if snapshot.get("question_version") == "class-review-v2" else 3)
    schema = (QualityV4 if scoring_version == "class-quality-v4" else Quality) if role == "quality" else ManagerReport
    system = load_prompt(filename) + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    if context_policy == CONTEXT_VERSION:
        system = load_prompt(filename).rsplit("Schema:", 1)[0] + load_prompt("context_v1.txt") + "\nSchema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        try:
            compiler = evaluation_context
            if scoring_version == "class-quality-v4":
                from agents.quality_context import evaluation_context_v4
                compiler = evaluation_context_v4
            compiled = compiler(role, snapshot, clarification, retrieval, quality, system=system)
        except ContextError as error:
            raise ModelError(str(error), status_code=422) from None
        if quality is not None and snapshot.get("question_version") == "class-review-v2":
            # Keep the frozen legacy compiler unchanged for resumable v1 jobs.
            # New records expose only answer slots present in their projection.
            compiled.payload["available_evidence_ids"] = evidence_ids + [
                item["id"] for item in compiled.payload["retrieval"]["sources"]]
            size = byte_size(compiled.payload)
            delta = size - compiled.manifest["payload_bytes"]
            compiled.manifest.update(payload_bytes=size,
                total_text_bytes=compiled.manifest["total_text_bytes"] + delta,
                initial_text_bytes=compiled.manifest["initial_text_bytes"] + delta,
                context_sha256=digest({"system": system, "payload": compiled.payload}))
        return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": encode(compiled.payload)}],
                "manifest": compiled.manifest, "retrieval": compiled.payload["retrieval"], "evidence_ids": evidence_ids, "scoring_version": scoring_version}
    if context_policy != LEGACY_CONTEXT:
        raise ModelError("未知上下文版本，已保留任务，请联系管理员。")
    payload = {"record": snapshot, "clarification": clarification, "retrieval": retrieval}
    if quality is not None:
        payload.update(assessment=quality, available_evidence_ids=evidence_ids +
                       [item["id"] for item in retrieval.get("sources", [])])
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text.encode()) > 60000:
        raise ModelError("评估资料过长，请缩短回答后重试。", status_code=422)
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "manifest": {"version": LEGACY_CONTEXT}, "retrieval": retrieval, "evidence_ids": evidence_ids, "scoring_version": scoring_version}


def messages(role, snapshot, clarification, retrieval, quality=None, *, context_policy=LEGACY_CONTEXT):
    return prepare_messages(role, snapshot, clarification, retrieval, quality, context_policy=context_policy)["messages"]


async def invoke_quality(messages, *, api_key):
    result = await invoke_json(messages, api_key=api_key, max_tokens=4000)
    return {"assessment": result["value"], "usage": result["usage"]}


async def invoke_manager(messages, *, api_key):
    result = await invoke_json(messages, api_key=api_key, max_tokens=1200)
    return {"manager": result["value"], "usage": result["usage"]}
