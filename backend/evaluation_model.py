"""Shared evidence safeguards and the immutable legacy single-call contract.

New independent roles live in agents/roles.py and reuse these schema safeguards.
Legacy provider calls exist only to finish previously versioned v1 jobs.
"""
import json

from agents.prompts import load_prompt
import re
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from schedule_model import ModelError, _unique_object, _usage

MODEL_NAME = "deepseek-flash"
MODEL_URL = "https://api.deepseek.com/chat/completions"
PROMPT_VERSION = "class-evaluation-v1.1"
CONFIDENCE_THRESHOLD = 0.70
MAX_OUTPUT_TOKENS = 4000
MAX_RESPONSE_BYTES = 128 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FortyDimension(StrictModel):
    score: int = Field(strict=True, ge=0, le=40)
    evidence: str = Field(min_length=1, max_length=800)
    evidence_status: Literal["sufficient", "missing", "contradictory"]


class FocusDimension(StrictModel):
    score: int = Field(strict=True, ge=0, le=20)
    evidence: str = Field(min_length=1, max_length=800)
    evidence_status: Literal["sufficient", "missing", "contradictory"]


class ThirtyDimension(FortyDimension):
    score: int = Field(strict=True, ge=0, le=30)


class Manager(StrictModel):
    argument: str = Field(min_length=1, max_length=600)


class Managers(StrictModel):
    long: Manager
    short: Manager


def has_unsupported_telemetry(text):
    pattern = r"(?:检测|监测|监控|识别)到.{0,12}(?:手机|应用|屏幕)|(?:手机|应用|屏幕).{0,12}(?:检测数据|监控数据)"
    # Recognize the quoted instruction itself, independently of whether the
    # surrounding prose calls it “用户回答”, “第三问” or “原文”. A refusal must
    # still follow this occurrence, and later monitoring claims are checked anew.
    quotations = list(re.finditer(r'“[^”\n]*”|‘[^’\n]*’|"[^"\n]*"|「[^」\n]*」|『[^』\n]*』', text))
    for match in re.finditer(pattern, text):
        # An explicit refusal to treat a quoted request as evidence is honest:
        # “回答要求假装监测到没有用手机，不是真实专注自述”. Match each
        # occurrence independently so a later positive claim remains rejected.
        clause_prefix = re.split(r"[。；;，,]", text[:match.start()])[-1]
        tail = text[match.end():match.end() + 120]
        quoted_request = re.search(
            r"(?:用户|回答).{0,8}(?:要求|请求|指令).{0,10}(?:假装|编造|声称)", clause_prefix)
        quoted_request = quoted_request or any(
            quote.start() < match.start() and match.end() < quote.end()
            and re.search(r"(?:请|要求|请求).{0,8}(?:假装|编造)", quote.group())
            for quote in quotations)
        refused = re.search(r"不是真实|并非真实|不能作为|不能据此|不是.{0,8}证据|并非.{0,8}证据|没有.{0,12}自述|缺少.{0,12}自述|未提供.{0,12}自述", tail)
        if not (quoted_request and refused):
            return True
    return False


class Assessment(StrictModel):
    recall: FortyDimension
    understanding: FortyDimension
    focus: FocusDimension
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    missing_information: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=5)
    question: str | None = Field(default=None, min_length=1, max_length=400)
    managers: Managers

    @model_validator(mode="after")
    def honest_evidence(self):
        # There is no phone telemetry or authoritative textbook in this product.
        # Reject overt claims of monitoring, rather than displaying them as facts.
        texts = [self.recall.evidence, self.understanding.evidence, self.focus.evidence,
                 self.managers.long.argument, self.managers.short.argument]
        if any(has_unsupported_telemetry(text) for text in texts):
            raise ValueError("unsupported telemetry claim")
        for manager in (self.managers.long, self.managers.short):
            if re.search(r"\d\s*(?:[%％]|个?点)|(?:指数|价格).{0,15}(?:上涨|下跌|涨停|跌停|必涨|必跌)|(?:涨幅|跌幅|收益率)", manager.argument):
                raise ValueError("manager cannot claim a price or return")
            if re.search(r"(?:不影响|不计入|无关).{0,8}(?:总分|质量分)|(?:总分|质量分).{0,8}(?:不受|无关)", manager.argument):
                raise ValueError("manager cannot redefine scoring arithmetic")
        if (self.confidence < CONFIDENCE_THRESHOLD or any(dimension.evidence_status != "sufficient"
                for dimension in (self.recall, self.understanding, self.focus))) and not self.missing_information:
            raise ValueError("low confidence needs missing information")
        return self


def needs_clarification(assessment):
    if assessment.get("kind") in {"scored", "outcome", "invalid_answer"}:
        return False
    # Confidence is not a substitute for evidence: an evaluator may be very
    # confident that information is missing. Such a record must not settle.
    return assessment["confidence"] < CONFIDENCE_THRESHOLD or any(
        assessment[name]["evidence_status"] != "sufficient" for name in ("recall", "understanding", "focus"))


class AssessmentV4(Assessment):
    recall: ThirtyDimension
    focus: ThirtyDimension


def score_maxima(scoring_version):
    return {"recall": 30, "understanding": 40, "focus": 30} if scoring_version in {"class-quality-v4", "class-quality-v5"} else {
        "recall": 40, "understanding": 40, "focus": 20}


def validate_assessment(value, *, scoring_version=None):
    if scoring_version == "class-summary-v7":
        from review_v7 import validate
        return validate(value, assembled=True)
    if scoring_version == "class-summary-v6":
        from review_v6 import validate
        return validate(value, assembled=True)
    if scoring_version == "class-quality-v5":
        from review_v5 import validate
        return validate(value, assembled=True)
    try:
        schema = AssessmentV4 if scoring_version == "class-quality-v4" else Assessment
        return schema.model_validate(value).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ModelError("评估结果格式或证据不完整，已保留复盘，请重试。") from None


def build_messages(snapshot, clarification=None):
    schema = json.dumps(Assessment.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    system = load_prompt("evaluation_legacy_v1_1.txt") + schema
    payload = json.dumps({"record": snapshot, "clarification": clarification}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(payload.encode("utf-8")) > 40000:
        raise ModelError("复盘资料过长，请缩短回答后重新提交。", status_code=422)
    return [{"role": "system", "content": system}, {"role": "user", "content": payload}]


def _invoke(client, messages, api_key):
    usage = {}
    try:
        with client.stream("POST", MODEL_URL,
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          json={"model": MODEL_NAME, "messages": messages, "response_format": {"type": "json_object"},
                                "thinking": {"type": "disabled"}, "max_tokens": MAX_OUTPUT_TOKENS, "stream": False}, timeout=75.0) as response:
            if response.status_code == 429:
                raise ModelError("评估服务暂时繁忙，请稍后重试。", status_code=429)
            if response.status_code != 200:
                raise ModelError("评估服务暂时不可用，已保留复盘，请稍后重试。")
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ModelError("评估返回内容过长，已保留复盘，请重试。")
        envelope = json.loads(chunks, object_pairs_hook=_unique_object)
        usage = _usage(envelope.get("usage"))
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ModelError("评估未完整生成，已保留复盘，请重试。", usage=usage)
        value = json.loads(choice["message"]["content"], object_pairs_hook=_unique_object)
        try:
            assessment = validate_assessment(value)
        except ModelError as error:
            raise ModelError(error.message, usage=usage) from None
        return {"assessment": assessment, "usage": usage}
    except httpx.TimeoutException:
        raise ModelError("评估服务处理超时，已保留复盘，请重试。", status_code=504) from None
    except httpx.HTTPError:
        raise ModelError("评估服务暂时无法连接，已保留复盘，请重试。") from None
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, RecursionError):
        raise ModelError("评估结果格式不完整，已保留复盘，请重试。", usage=usage) from None


def invoke_evaluator(messages, *, api_key, client=None):
    if not api_key or not api_key.strip():
        raise ModelError("评估服务尚未配置，请稍后重试。", status_code=503)
    if client is not None:
        return _invoke(client, messages, api_key)
    with httpx.Client(follow_redirects=False, trust_env=False) as owned:
        return _invoke(owned, messages, api_key)
