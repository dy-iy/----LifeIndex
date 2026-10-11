"""Versioned, deterministic model-context projections and priority budgets.

Runtime capabilities never enter model context. Required facts are preserved
whole; optional historical material is removed with an auditable manifest.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

CONTEXT_VERSION = "context-v1"
CONTEXT_CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LEGACY_CONTEXT = "legacy-context"
TEXT_LIMITS = {"quality": 48000, "long": 48000, "short": 48000, "schedule": 256 * 1024}
RETRIEVAL_LIMIT = 16000
RECORD_FIELDS = frozenset({"course", "question_version", "answers", "review_version", "session_version",
    "planned_seconds", "effective_seconds", "elapsed_seconds", "is_makeup", "source", "submitted_at", "starts_at", "ends_at"})
STUDY_FIELDS = frozenset({"name", "local_date", "start_time", "end_time", "reported_seconds"})


class ContextError(ValueError):
    pass


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ContextError("上下文格式不完整，已保留输入，请重试。") from None


def byte_size(value):
    try:
        return len(encode(value).encode("utf-8"))
    except UnicodeError:
        raise ContextError("上下文包含无效字符，请检查输入。") from None


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompiledContext:
    payload: dict
    manifest: dict


def select_sources(retrieval, domain, *, include_history):
    from agents.retrieval import rules
    if not isinstance(retrieval, dict):
        raise ContextError("检索上下文格式不正确，已保留任务。")
    official = {item["id"]: item for item in rules(domain)}
    sources = retrieval.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 64:
        raise ContextError("检索来源数量或格式不正确，已保留任务。")
    seen, history = {}, []
    for source in sources:
        if not isinstance(source, dict) or any(not isinstance(source.get(key), str)
                for key in ("id", "version", "authority", "title", "text", "sha256")):
            raise ContextError("检索来源缺少版本或内容校验信息，已保留任务。")
        if len(source["id"]) > 160 or len(source["title"]) > 300 or len(source["text"]) > 2400:
            raise ContextError("检索来源超过允许范围，已保留任务。")
        checksum = hashlib.sha256(source["text"].encode()).hexdigest()
        if source["sha256"] != checksum:
            raise ContextError("检索来源内容校验失败，已保留任务。")
        identity = (source["version"], checksum, source["authority"])
        if source["id"] in seen:
            if seen[source["id"]] != identity:
                raise ContextError("同一检索来源出现冲突版本，已保留任务。")
            continue
        seen[source["id"]] = identity
        if source["authority"] == "product_rule":
            expected = official.get(source["id"])
            if expected is None or any(source[key] != expected[key] for key in expected):
                raise ContextError("产品规则来源或版本不匹配，已保留任务。")
        elif source["authority"] == "user_history":
            if source["id"].startswith("rule:"):
                raise ContextError("个人历史不能使用产品规则标识。")
            if include_history:
                history.append({key: source[key] for key in ("id", "version", "authority", "title", "text", "sha256")})
        else:
            raise ContextError("未知检索来源类型，已保留任务。")
    selected = list(official.values())
    for item in history[:3]:
        if byte_size(selected + [item]) <= RETRIEVAL_LIMIT:
            selected.append(item)
    return {"sources": selected, "empty": False, "retriever_version": retrieval.get("retriever_version", "cjk-bm25-v1")}


def compile_context(profile, payload, *, system, input_source_count=0, limit=None):
    """Fit text by dropping optional material, without clipping current facts."""
    payload = deepcopy(payload)
    cap = TEXT_LIMITS[profile] if limit is None else min(limit, TEXT_LIMITS[profile])
    system_bytes = len(system.encode("utf-8"))
    dropped = {}

    def measure():
        if "available_evidence_ids" in payload:
            payload["available_evidence_ids"] = [f"assessment:{key}" for key in ("recall", "understanding", "focus")] + [
                f"record:answer:{i}" for i in (1, 2, 3)] + [item["id"] for item in payload["retrieval"]["sources"]]
        # Include the message-envelope overhead in the conservative byte budget.
        return byte_size(payload) + system_bytes + 256

    initial = measure()

    def removed(section, amount=1):
        dropped[section] = dropped.get(section, 0) + amount

    def over():
        return measure() > cap

    # Old assistant prose has the lowest priority. The user's latest text,
    # authoritative timetable, pending plan and all current grading evidence
    # are never truncated or summarized by another model.
    if profile == "schedule":
        for row in payload["history"]:
            if not over():
                break
            if row.get("reply"):
                row["reply"] = ""
                removed("assistant_replies")
    sources = payload["retrieval"]["sources"]
    while over() and any(item["authority"] == "user_history" for item in sources):
        index = max(i for i, item in enumerate(sources) if item["authority"] == "user_history")
        sources.pop(index)
        removed("retrieved_history")
    if profile == "schedule":
        memory = payload["conversation_memory"]
        # These arrays are reverse-chronological and chronological respectively.
        for rows, index, label, minimum in [
            (memory["confirmed_history"], -1, "older_memory", 0),
            (payload["history"], 0, "older_turns", 3),
            (memory["recent_user_statements"], 0, "older_statements", 3),
        ]:
            while over() and len(rows) > minimum:
                rows.pop(index)
                removed(label)
    total = measure()
    if total > cap:
        raise ContextError("当前必要资料超过处理范围，请缩小本次任务；原始输入已保留。")
    manifest = {"version": CONTEXT_VERSION, "code_sha256": CONTEXT_CODE_SHA256, "profile": profile, "limit_bytes": cap,
        "system_bytes": system_bytes, "payload_bytes": byte_size(payload), "total_text_bytes": total,
        "initial_text_bytes": initial, "output_reserve_tokens": 12000 if profile == "schedule" else (4000 if profile == "quality" else 1200),
        "omitted": dropped, "input_source_count": input_source_count,
        "source_count": len(sources), "sources": [{key: item[key] for key in ("id", "version", "authority", "sha256")} for item in sources],
        "context_sha256": digest({"system": system, "payload": payload})}
    return CompiledContext(payload, manifest)


def evaluation_context(role, snapshot, clarification, retrieval, quality, *, system, limit=None):
    record = {key: value for key, value in snapshot.items() if key in RECORD_FIELDS}
    if isinstance(snapshot.get("study"), dict):
        record["study"] = {key: value for key, value in snapshot["study"].items() if key in STUDY_FIELDS}
    payload = {"record": record, "clarification": clarification,
        "retrieval": select_sources(retrieval, "learning", include_history=role != "quality")}
    if quality is not None:
        # Managers explain evidence. Numeric grading belongs to the assessor
        # and deterministic settlement, and is unnecessary in their context.
        evidence = {key: {field: quality[key][field] for field in ("evidence", "evidence_status")}
                    for key in ("recall", "understanding", "focus")}
        evidence.update({key: quality[key] for key in ("confidence", "missing_information", "question") if key in quality})
        payload.update(assessment=evidence, available_evidence_ids=[])
    result = compile_context(role, payload, system=system, input_source_count=len(retrieval.get("sources", [])), limit=limit)
    if role == "quality":
        result.manifest["excluded_historical_sources"] = sum(item["authority"] == "user_history" for item in retrieval.get("sources", []))
    return result


def schedule_context(state, *, system, limit=None):
    raw_memory = state.get("conversation_memory", {})
    if not isinstance(raw_memory, dict) or any(not isinstance(raw_memory.get(key, []), list)
            for key in ("confirmed_history", "recent_user_statements")):
        raise ContextError("对话记忆格式不正确，已保留任务。")
    history = deepcopy(state.get("history", []))
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ContextError("历史对话格式不正确，已保留任务。")
    pending = raw_memory.get("pending_proposal")
    if "pending_proposal" not in raw_memory:
        latest = next((row for row in reversed(history) if row.get("unapplied_plan")), None)
        pending = {"plan": latest["unapplied_plan"]} if latest else None
    # Preserve pending operations once, not repeated copies in every turn.
    history = [{key: value for key, value in row.items() if key in {"input", "confirmation", "reply", "status"}}
               for row in history]
    memory = {"pending_proposal": deepcopy(pending), "confirmed_history": deepcopy(raw_memory.get("confirmed_history", [])),
              "recent_user_statements": deepcopy(raw_memory.get("recent_user_statements", []))}
    current = {key: deepcopy(value) for key, value in state.get("context", {}).items()
               if key in {"today", "timezone", "semester", "semesters", "week_dates", "courses", "lessons"}}
    payload = {"context": current, "history": history, "conversation_memory": memory,
        "retrieval": select_sources(state.get("retrieval", {}), "schedule", include_history=True),
        "route": state.get("route", "plan"), "input_text": state.get("input_text", "")}
    return compile_context("schedule", payload, system=system,
                           input_source_count=len(state.get("retrieval", {}).get("sources", [])), limit=limit)
