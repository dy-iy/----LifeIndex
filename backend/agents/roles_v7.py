"""Summary-only manager context; raw answers and history stop at the reviewer."""
import hashlib
import json
from pathlib import Path
from agents.context import CONTEXT_VERSION, select_sources, encode, byte_size, digest
from agents.prompts import load_prompt
from evaluation_model import ModelError
from review_v7 import QUALITY, QUALITY_VERSION, ManagerReport, EVIDENCE_IDS, model_record, summary_view

CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PROMPTS = ("review_summary_v7.txt", "long_short_manager_v1.txt", "context_v1.txt")


def prepare_messages(role, snapshot, clarification, retrieval, quality=None):
    if role not in {"quality", "long_short"}:
        raise ValueError("Unknown role")
    filename = PROMPTS[0] if role == "quality" else PROMPTS[1]
    schema = QUALITY.json_schema() if role == "quality" else ManagerReport.model_json_schema()
    system = load_prompt(filename).rsplit("Schema:", 1)[0] + load_prompt("context_v1.txt") + "\nSchema:\n" + json.dumps(schema, ensure_ascii=False)
    sources = select_sources(retrieval, "learning_v7", include_history=False)
    if role == "quality":
        record = {key: value for key, value in model_record(snapshot).items() if key in {
            "course", "question_version", "answers", "source",
            "review_context", "current_step", "current_question", "participation_label", "focus_label", "untimed_reason_label"}}
        payload = {"record": record, "retrieval": sources}
    else:
        payload = {"summary": summary_view(quality, snapshot), "available_evidence_ids": EVIDENCE_IDS,
                   "retrieval": sources}
    # Only current required facts and product rules enter v7; there is no
    # historical context to trim and no legacy evidence list to overwrite.
    system_bytes, payload_bytes = len(system.encode("utf-8")), byte_size(payload)
    total = system_bytes + payload_bytes + 256
    if total > 48000:
        raise ModelError("当前必要资料超过处理范围，请缩小本次任务；原始输入已保留。", status_code=422)
    manifest = {"version": CONTEXT_VERSION, "code_sha256": CODE_SHA256, "profile": role, "limit_bytes": 48000,
        "system_bytes": system_bytes, "payload_bytes": payload_bytes, "total_text_bytes": total,
        "initial_text_bytes": total, "output_reserve_tokens": 4000 if role == "quality" else 1200,
        "omitted": {}, "input_source_count": len(retrieval.get("sources", [])),
        "source_count": len(sources["sources"]),
        "sources": [{key: item[key] for key in ("id", "version", "authority", "sha256")} for item in sources["sources"]],
        "context_sha256": digest({"system": system, "payload": payload}),
        "scoring_version": QUALITY_VERSION, "scoring_context_sha256": CODE_SHA256}
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": encode(payload)}],
        "manifest": manifest, "retrieval": sources,
        "evidence_ids": EVIDENCE_IDS, "scoring_version": QUALITY_VERSION}
