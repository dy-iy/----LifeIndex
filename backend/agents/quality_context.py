"""New scoring context; the original context compiler stays frozen for old jobs."""
import hashlib
from pathlib import Path

from agents.context import RECORD_FIELDS, STUDY_FIELDS, compile_context, select_sources

CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def evaluation_context_v4(role, snapshot, clarification, retrieval, quality, *, system):
    record = {key: value for key, value in snapshot.items() if key in RECORD_FIELDS}
    if isinstance(snapshot.get("study"), dict):
        record["study"] = {key: value for key, value in snapshot["study"].items() if key in STUDY_FIELDS}
    payload = {"record": record, "clarification": clarification,
               "retrieval": select_sources(retrieval, "learning_v4", include_history=role != "quality")}
    if quality is not None:
        evidence = {key: {field: quality[key][field] for field in ("evidence", "evidence_status")}
                    for key in ("recall", "understanding", "focus")}
        evidence.update({key: quality[key] for key in ("confidence", "missing_information", "question") if key in quality})
        payload.update(assessment=evidence, available_evidence_ids=[])
    result = compile_context(role, payload, system=system, input_source_count=len(retrieval.get("sources", [])))
    result.manifest.update(scoring_version="class-quality-v4", scoring_context_sha256=CODE_SHA256)
    if role == "quality":
        result.manifest["excluded_historical_sources"] = sum(item["authority"] == "user_history" for item in retrieval.get("sources", []))
    return result
