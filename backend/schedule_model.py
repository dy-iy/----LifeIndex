"""Bounded multimodal JSON planning. Model output never authorizes a write."""
import base64
import json

from agents.prompts import load_prompt
from agents.context import CONTEXT_VERSION, LEGACY_CONTEXT, CONTEXT_CODE_SHA256, ContextError, schedule_context, encode
from typing import Any

import httpx
from pydantic import ValidationError

from schedule_plan import SchedulePlan


MODEL_NAME = "deepseek-flash"
MODEL_URL = "https://api.deepseek.com/chat/completions"
MAX_OUTPUT_TOKENS = 12000
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 200 * 1024
MAX_HISTORY_BYTES = 48 * 1024
MAX_INPUT_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGES = 6


class ModelError(Exception):
    """Only safe user-facing text and sanitized usage may leave this module."""
    def __init__(self, message: str, *, status_code: int = 502, usage: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.usage = usage or {}


def _bounded_json(value: Any, limit: int, message: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ModelError(message, status_code=422) from None
    if size > limit:
        raise ModelError(message, status_code=422)
    return encoded


def validate_plan(value: Any) -> dict:
    try:
        # exclude_unset preserves patch semantics: omitted fields must not turn
        # into explicit nulls or reset defaults when the batch is revalidated.
        result = SchedulePlan.model_validate(value).model_dump(mode="json", exclude_unset=True)
        result.setdefault("questions", [])
        result.setdefault("operations", [])
        return result
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ModelError("课表助手返回的方案格式不完整，请补充描述后重试。") from None


def prepare_messages(state: dict, images: list[dict]) -> dict:
    """Build temporary API messages; callers persist image IDs, never these data URLs."""
    context = _bounded_json(state.get("context", {}), MAX_CONTEXT_BYTES, "当前课表内容过多，请缩小需要处理的学期或课程范围。")
    text = state.get("input_text", "")
    try:
        valid_text = isinstance(text, str) and len(text.encode("utf-8")) <= MAX_INPUT_BYTES
    except UnicodeError:
        valid_text = False
    if not valid_text:
        raise ModelError("本次描述过长，请缩短后重试。", status_code=422)
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ModelError("一段待处理的安排最多使用六张课表图片，请先完成当前导入。", status_code=422)
    prompt_name = state.get("schedule_prompt", "schedule_v2.txt")
    if prompt_name not in {"schedule_v2.txt", "schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"}:
        raise ModelError("未知课表提示版本，已保留当前任务。")
    schema_model = SchedulePlan
    if prompt_name in {"schedule_v3.txt", "schedule_v4.txt", "schedule_v5.txt"}:
        from agents.schedule_image_reader import ImagePlanningPlan
        schema_model = ImagePlanningPlan
    schema = json.dumps(schema_model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    system = load_prompt(prompt_name) + schema
    policy = state.get("context_policy", LEGACY_CONTEXT)
    if policy == CONTEXT_VERSION:
        if state.get("context_code_sha256", CONTEXT_CODE_SHA256) != CONTEXT_CODE_SHA256:
            raise ModelError("上下文构建版本已经改变，请按原版本恢复课表任务。")
        system = load_prompt(prompt_name).rsplit("JSON Schema:", 1)[0] + load_prompt("context_v1.txt") + "\nJSON Schema:\n" + schema
        try:
            compiled = schedule_context(state, system=system)
        except ContextError as error:
            raise ModelError(str(error), status_code=422) from None
        payload, manifest = encode(compiled.payload), compiled.manifest
    elif policy == LEGACY_CONTEXT:
        history = _bounded_json(state.get("history", []), MAX_HISTORY_BYTES, "这段对话内容过多，请新建对话并概括需要修改的安排。")
        memory = _bounded_json(state.get("conversation_memory", {}), MAX_HISTORY_BYTES, "当前待办信息较多，请缩小这次修改范围。")
        retrieval = _bounded_json(state.get("retrieval", {}), 18000, "检索内容过多，请缩小范围。")
        payload = ('{"context":' + context + ',"history":' + history + ',"conversation_memory":' + memory
                   + ',"retrieval":' + retrieval + ',"route":' + json.dumps(state.get("route", "plan"))
                   + ',"input_text":' + json.dumps(text, ensure_ascii=False) + '}')
        manifest = {"version": LEGACY_CONTEXT}
    else:
        raise ModelError("未知上下文版本，已保留当前课表任务。")
    content: list[dict] = [{"type": "text", "text": payload}]
    image_bytes = 0
    for item in images:
        if not isinstance(item, dict):
            raise ModelError("图片数据不可用，请重新上传。", status_code=422)
        mime = item.get("mime_type")
        data = item.get("data")
        if mime not in {"image/jpeg", "image/png", "image/webp"} or not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
            raise ModelError("图片格式或大小不符合要求，请重新上传。", status_code=422)
        image_bytes += len(data)
        if image_bytes > MAX_TOTAL_IMAGE_BYTES:
            # Base64 expansion must remain below the provider's 48 MiB request
            # limit, including the bounded context and JSON/schema overhead.
            raise ModelError("这组图片处理后过大，请裁剪课表区域或分批导入。", status_code=422)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}})
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "manifest": {**manifest, "image_count": len(images), "image_bytes": image_bytes}}


def build_messages(state: dict, images: list[dict]) -> list[dict]:
    return prepare_messages(state, images)["messages"]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items()
            if key in {"prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "total_tokens"}
            and type(count) is int and 0 <= count <= 2_000_000}


def _invoke(client: httpx.Client, messages: list[dict], api_key: str) -> dict:
    usage = {}
    try:
        with client.stream("POST", MODEL_URL,
                           headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                           json={"model": MODEL_NAME, "messages": messages,
                                 "response_format": {"type": "json_object"},
                                 "thinking": {"type": "disabled"},
                                 "max_tokens": MAX_OUTPUT_TOKENS, "stream": False},
                           timeout=75.0) as response:
            if response.status_code == 429:
                raise ModelError("课表助手暂时繁忙，请稍后重试。", status_code=429)
            if response.status_code != 200:
                raise ModelError("课表助手暂时无法处理，请稍后重试。")
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ModelError("课表助手返回内容过长，请缩小本次修改范围。")
        envelope = json.loads(chunks, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict):
            raise ValueError("invalid envelope")
        usage = _usage(envelope.get("usage"))
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ModelError("课表助手未能完整生成方案，请缩小本次修改范围后重试。", usage=usage)
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        decoded = json.loads(content, object_pairs_hook=_unique_object)
        try:
            if isinstance(decoded, dict) and any(isinstance(op, dict) and op.get("kind") in {"import_image_courses", "set_image_periods"}
                    for op in decoded.get("operations", []) if isinstance(decoded.get("operations"), list)):
                from agents.schedule_image_reader import validate_image_planning_plan
                plan = validate_image_planning_plan(decoded)
            else:
                plan = validate_plan(decoded)
        except ModelError as error:
            raise ModelError(error.message, usage=usage) from None
        return {"plan": plan, "usage": usage}
    except httpx.TimeoutException:
        raise ModelError("课表助手处理超时，请稍后重试。", status_code=504) from None
    except httpx.HTTPError:
        raise ModelError("课表助手暂时无法连接，请稍后重试。") from None
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        raise ModelError("课表助手返回的方案格式不完整，请补充描述后重试。", usage=usage) from None


def invoke_planner(messages: list[dict], *, api_key: str, client: httpx.Client | None = None) -> dict:
    """Make one bounded request; retry/quotas belong to the job layer, not HTTP."""
    if not api_key or not api_key.strip():
        raise ModelError("课表助手尚未配置，请稍后重试。", status_code=503)
    if client is not None:
        return _invoke(client, messages, api_key)
    with httpx.Client(follow_redirects=False, trust_env=False) as owned_client:
        return _invoke(owned_client, messages, api_key)
