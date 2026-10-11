"""New context compiler; never changes the bytes used by a saved v4 task."""
import hashlib
import json
from pathlib import Path

from agents.context import (RECORD_FIELDS, STUDY_FIELDS, compile_context, select_sources,
                            encode, ContextError, digest, byte_size)
from agents.prompts import load_prompt
from review_v5 import QUALITY, QUALITY_VERSION, model_record
from evaluation_model import ModelError

CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def prepare_messages(role, snapshot, clarification, retrieval, quality=None):
    from agents.roles import ManagerReport, current_evidence_ids
    filename = "quality_v5.txt" if role == "quality" else f"{role}_manager_v3.txt"
    schema = QUALITY.json_schema() if role == "quality" else ManagerReport.model_json_schema()
    system = load_prompt(filename).rsplit("Schema:", 1)[0] + load_prompt("context_v1.txt") + "\nSchema:\n" + json.dumps(schema, ensure_ascii=False)
    record = {key: value for key, value in model_record(snapshot).items() if key in RECORD_FIELDS or key in {
        "review_context", "current_step", "current_question", "participation_label", "focus_label", "untimed_reason_label"}}
    if isinstance(snapshot.get("study"), dict):
        record["study"] = {key: value for key, value in snapshot["study"].items() if key in STUDY_FIELDS}
    payload = {"record": record, "clarification": clarification,
        "retrieval": select_sources(retrieval, "learning_v5", include_history=role != "quality")}
    evidence_ids = current_evidence_ids(len(snapshot["answers"]))
    if quality is not None:
        payload["assessment"] = {name: {key: quality[name][key] for key in ("evidence", "basis")}
                                 for name in ("recall", "understanding", "focus")}
        payload["assessment"].update(kind=quality["kind"], learning=quality["learning"])
        payload["available_evidence_ids"] = evidence_ids
    try:
        compiled = compile_context(role, payload, system=system, input_source_count=len(retrieval.get("sources", [])))
    except ContextError as error:
        raise ModelError(str(error), status_code=422) from None
    if quality is not None:
        compiled.payload["available_evidence_ids"] = evidence_ids + [item["id"] for item in compiled.payload["retrieval"]["sources"]]
        size = byte_size(compiled.payload)
        delta = size - compiled.manifest["payload_bytes"]
        compiled.manifest.update(payload_bytes=size, total_text_bytes=compiled.manifest["total_text_bytes"] + delta,
            initial_text_bytes=compiled.manifest["initial_text_bytes"] + delta,
            context_sha256=digest({"system": system, "payload": compiled.payload}))
    compiled.manifest.update(scoring_version=QUALITY_VERSION, scoring_context_sha256=CODE_SHA256)
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": encode(compiled.payload)}],
        "manifest": compiled.manifest, "retrieval": compiled.payload["retrieval"],
        "evidence_ids": evidence_ids, "scoring_version": QUALITY_VERSION}
