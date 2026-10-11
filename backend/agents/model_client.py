"""Bounded asynchronous JSON transport shared by independent model roles."""
import json
import httpx
from schedule_model import MODEL_URL, MODEL_NAME, ModelError, _unique_object, _usage


async def invoke_json(messages, *, api_key, max_tokens, client=None):
    if not api_key or not api_key.strip():
        raise ModelError("模型服务尚未配置，请联系管理员。", status_code=503)
    if client is None:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as owned:
            return await invoke_json(messages, api_key=api_key, max_tokens=max_tokens, client=owned)
    usage = {}
    try:
        async with client.stream("POST", MODEL_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": MODEL_NAME, "messages": messages, "response_format": {"type": "json_object"},
                  "thinking": {"type": "disabled"}, "max_tokens": max_tokens, "stream": False},
            timeout=75.0) as response:
            if response.status_code == 429:
                raise ModelError("模型服务暂时繁忙，已保留当前结果，请稍后重试。", status_code=429)
            if response.status_code != 200:
                raise ModelError("模型服务暂时不可用，已保留当前结果，请重试。")
            raw = bytearray()
            async for block in response.aiter_bytes():
                raw.extend(block)
                if len(raw) > 128 * 1024:
                    raise ModelError("模型结果过长，已保留输入，请重试。")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        usage = _usage(value.get("usage"))
        choice = value["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ModelError("模型结果未完整生成，已保留输入，请重试。", usage=usage)
        return {"value": json.loads(choice["message"]["content"], object_pairs_hook=_unique_object), "usage": usage}
    except httpx.TimeoutException:
        raise ModelError("模型服务处理超时，已保存已有结果，请重试。", status_code=504, usage=usage) from None
    except httpx.HTTPError:
        raise ModelError("暂时无法连接模型服务，请重试。", usage=usage) from None
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, RecursionError):
        raise ModelError("模型结果格式不完整，已保留输入，请重试。", usage=usage) from None
