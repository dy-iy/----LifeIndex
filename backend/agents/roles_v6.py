"""Summary-only manager context; raw answers and history stop at the reviewer."""
import hashlib
import json
from pathlib import Path
from agents.context import RECORD_FIELDS, compile_context, select_sources, encode, ContextError, byte_size, digest
from agents.prompts import load_prompt
from evaluation_model import ModelError
from review_v6 import QUALITY, QUALITY_VERSION, ManagerReport, EVIDENCE_IDS, model_record, summary_view

CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PROMPTS = ("review_summary_v6.txt", "long_manager_v4.txt", "short_manager_v4.txt", "context_v1.txt")


def prepare_messages(role, snapshot, clarification, retrieval, quality=None):
    filename = PROMPTS[0] if role == "quality" else f"{role}_manager_v4.txt"
    schema = QUALITY.json_schema() if role == "quality" else ManagerReport.model_json_schema()
    system = load_prompt(filename).rsplit("Schema:", 1)[0] + load_prompt("context_v1.txt") + "\nSchema:\n" + json.dumps(schema, ensure_ascii=False)
    sources = select_sources(retrieval, "learning_v6", include_history=False)
    if role == "quality":
        record = {key: value for key, value in model_record(snapshot).items() if key in {
            "course", "question_version", "answers", "source",
            "review_context", "current_step", "current_question", "participation_label", "focus_label", "untimed_reason_label"}}
        payload = {"record": record, "retrieval": sources}
    else:
        payload = {"summary": summary_view(quality, snapshot), "available_evidence_ids": EVIDENCE_IDS,
                   "retrieval": sources}
    try:
        compiled = compile_context(role, payload, system=system, input_source_count=len(retrieval.get("sources", [])))
    except ContextError as error:
        raise ModelError(str(error), status_code=422) from None
    if role != "quality":
        # The frozen legacy compiler installs legacy score/answer references.
        # Only actual summary fields are legal for the new manager contract.
        compiled.payload["available_evidence_ids"] = list(EVIDENCE_IDS)
        size = byte_size(compiled.payload)
        delta = size - compiled.manifest["payload_bytes"]
        compiled.manifest.update(payload_bytes=size, total_text_bytes=compiled.manifest["total_text_bytes"] + delta,
            initial_text_bytes=compiled.manifest["initial_text_bytes"] + delta,
            context_sha256=digest({"system": system, "payload": compiled.payload}))
    compiled.manifest.update(scoring_version=QUALITY_VERSION, scoring_context_sha256=CODE_SHA256)
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": encode(compiled.payload)}],
        "manifest": compiled.manifest, "retrieval": compiled.payload["retrieval"],
        "evidence_ids": EVIDENCE_IDS, "scoring_version": QUALITY_VERSION}
