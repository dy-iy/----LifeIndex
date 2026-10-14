"""One bounded multimodal request for timetable extraction and editing."""
import base64
import json
from typing import Any

import httpx

from agents.prompts import load_prompt, prompt_manifest
from agents.schedule_image_reader import normalize_understanding

MODEL_NAME = "doubao-seed-2-1-pro-260915"
MODEL_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
PROMPT_VERSION = "schedule_v6.txt"
MAX_OUTPUT_TOKENS = 12000
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_HISTORY_BYTES = 512 * 1024
MAX_INPUT_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGES = 3
MAX_IMAGE_TOKENS = 5120  # Ark's upper bound for detail="xhigh".


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


def prepare_messages(state: dict, images: list[dict]) -> dict:
    """Use every current attachment once; checkpoints never contain image bytes."""
    from agents.schedule_image_reader import UNDERSTANDING_SCHEMA, SCHEMA_VERSION
    prompt_name = state.get("schedule_prompt", PROMPT_VERSION)
    if prompt_name != PROMPT_VERSION:
        raise ModelError("旧版课表任务已被替代，请发送新的课表说明。", status_code=409)
    context = _bounded_json(state.get("context", {}), MAX_CONTEXT_BYTES, "当前课表数据过多，任务输入已保留。")
    history = _bounded_json(state.get("history", []), MAX_HISTORY_BYTES, "当前对话数据过多，任务输入已保留。")
    text = state.get("input_text", "")
    try:
        valid_text = isinstance(text, str) and len(text.encode("utf-8")) <= MAX_INPUT_BYTES
    except UnicodeError:
        valid_text = False
    if not valid_text:
        raise ModelError("本次描述超过长度限制，任务输入已保留。", status_code=422)
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ModelError("每条消息最多处理三张课表图片。", status_code=422)
    system = load_prompt(prompt_name) + json.dumps(UNDERSTANDING_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    payload = ('{"current_timetable":' + context + ',"historical_user_context":' + history
               + ',"current_input":' + json.dumps(text, ensure_ascii=False)
               + ',"current_image_ids":' + json.dumps(state.get("image_ids", [])) + '}')
    content: list[dict] = [{"type": "text", "text": payload}]
    image_bytes = 0
    for item in images:
        if not isinstance(item, dict):
            raise ModelError("附件数据不可用，任务输入已保留。", status_code=422)
        mime, data = item.get("mime_type"), item.get("data")
        if mime not in {"image/jpeg", "image/png", "image/webp"} or not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
            raise ModelError("附件格式或大小不符合要求，任务输入已保留。", status_code=422)
        image_bytes += len(data)
        if image_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ModelError("本条消息的图片超过处理大小限制，任务输入已保留。", status_code=422)
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"), "detail": "xhigh"}})
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "manifest": {"version": SCHEMA_VERSION, "model": MODEL_NAME, "prompt": prompt_name,
                         "prompt_hash": prompt_manifest([prompt_name])[prompt_name],
                         "image_count": len(images), "image_bytes": image_bytes}}


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
                raise ModelError("课表助手暂时繁忙，输入已保留，可稍后重试。", status_code=429)
            if response.status_code != 200:
                raise ModelError("课表助手服务暂时不可用，输入已保留，可稍后重试。")
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ModelError("课表助手响应超过大小限制，输入已保留。")
        envelope = json.loads(chunks, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict):
            raise ValueError("invalid envelope")
        usage = _usage(envelope.get("usage"))
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ModelError("课表助手响应中断，输入已保留，可重试。", usage=usage)
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        decoded = json.loads(content, object_pairs_hook=_unique_object)
        try:
            understanding = normalize_understanding(decoded)
        except ModelError as error:
            raise ModelError(error.message, usage=usage) from None
        return {"understanding": understanding, "usage": usage}
    except httpx.TimeoutException:
        raise ModelError("课表助手处理超时，输入已保留，可稍后重试。", status_code=504) from None
    except httpx.HTTPError:
        raise ModelError("课表助手暂时无法连接，输入已保留，可稍后重试。") from None
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        raise ModelError("课表助手返回的数据无法解析，输入已保留，可重试。", usage=usage) from None


def invoke_planner(messages: list[dict], *, api_key: str, client: httpx.Client | None = None) -> dict:
    """Exactly one HTTP call. Only an explicit retry can initiate another call."""
    if not api_key or not api_key.strip():
        raise ModelError("课表助手尚未配置，输入已保留。", status_code=503)
    if client is not None:
        return _invoke(client, messages, api_key)
    with httpx.Client(follow_redirects=False, trust_env=False) as owned_client:
        return _invoke(owned_client, messages, api_key)
