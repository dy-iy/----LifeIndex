"""Source-backed context for an unfinished timetable task, without LLM summaries.

The caller supplies owned runs newest first. User words and the questions they
answered remain together until the task is cancelled or an import is applied.
Assistant prose is deliberately absent: a failed preview is not a user rule.
"""
from copy import deepcopy
import json


TASK_MEMORY_VERSION = "schedule-task-v1"
IMAGE_IMPORT_PROGRESS_VERSION = "schedule-image-progress-v1"
MAX_TASK_TURNS = 200
MAX_TASK_BYTES = 64 * 1024


class TaskMemoryError(ValueError):
    pass


def validate_import_progress(value):
    """Validate server-written progress before using it as a task boundary."""
    if value is None:
        return None
    try:
        if value["version"] != IMAGE_IMPORT_PROGRESS_VERSION or type(value["complete"]) is not bool:
            raise ValueError
        images = value["images"]
        if not isinstance(images, list) or not 1 <= len(images) <= 6:
            raise ValueError
        seen = set()
        for item in images:
            identifier = item["image_id"]
            checksum = item["reading_sha256"]
            if not isinstance(identifier, str) or identifier in seen or not isinstance(checksum, str) or len(checksum) != 64:
                raise ValueError
            seen.add(identifier)
            for field in ("available_card_ids", "imported_card_ids", "remaining_card_ids"):
                cards = item[field]
                if not isinstance(cards, list) or len(cards) > 100 or any(not isinstance(card, str) or not card for card in cards) or len(cards) != len(set(cards)):
                    raise ValueError
            available, imported, remaining = (set(item[field]) for field in
                ("available_card_ids", "imported_card_ids", "remaining_card_ids"))
            if not available or not imported <= available or remaining != available - imported:
                raise ValueError
        if value["complete"] != all(not item["remaining_card_ids"] for item in images):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise TaskMemoryError("图片导入进度的版本或内容不完整，已保留课程和原始要求，请重试或联系管理员。") from None
    return deepcopy(value)


def completes_import(plan, result=None):
    if isinstance(result, dict) and "image_import_progress" in result:
        progress = validate_import_progress(result["image_import_progress"])
        if progress is not None:
            return progress["complete"]
    # Old runs/checkpoints did not carry image progress; retain their boundary.
    return any(item.get("kind") == "create_course" for item in (plan or {}).get("operations", []))


def active_task_rows(rows):
    """Return oldest-first rows after a cancellation or completed import.

    Fetch MAX_TASK_TURNS + 1 rows so an older live requirement cannot silently
    fall off the query limit. An applied deletion is not an import boundary.
    """
    active = []
    for row in rows:
        if row.status == "cancelled" or row.status == "applied" and completes_import(row.plan, getattr(row, "result", None)):
            break
        active.append(row)
        if len(active) > MAX_TASK_TURNS:
            raise TaskMemoryError("这项未完成的课表任务超过了对话轮数上限，原始要求已保留；请新建对话并整理本次安排。")
    return list(reversed(active))


def _questions(row):
    values = (row.plan or {}).get("questions", [])
    return [value for value in values if isinstance(value, str) and value.strip()]


def build_pending_proposal(rows, *, current=None):
    """Keep the last proposal and all source statements from its active task.

    Questions-only runs are unfinished tasks too. A later empty model response
    cannot erase the task. The capsule preserves questions as question context,
    never as facts or fresh requirements; later user replies remain verbatim.
    """
    active = active_task_rows(rows)
    pending = next((row for row in reversed(active)
        if row.status in {"needs_input", "superseded"}
        and ((row.plan or {}).get("operations") or _questions(row))), None)
    # An image can still be the source of an unfinished import after a partial
    # deletion or an erroneous empty answer. Retain it even without a draft.
    if pending is None:
        pending = next((row for row in reversed(active) if row.image_ids), None)
    if pending is None:
        return None

    capsule = {"version": TASK_MEMORY_VERSION, "turns": [], "image_sources": []}
    previous_questions = None
    for row in active + ([current] if current is not None else []):
        source = str(row.id)
        turn = {"source": source, "status": row.status, "input": row.input_text}
        if row.confirmation_text:
            turn["confirmation"] = row.confirmation_text
        if previous_questions is not None:
            # Association only: the next utterance may answer, correct or reject
            # the questions. Do not assert that all questions are still open.
            turn["in_reply_to"] = previous_questions
        questions = _questions(row)
        if questions:
            turn["asked_questions"] = questions
        previous_questions = {"source": source, "questions": questions} if questions else None
        capsule["turns"].append(turn)
        if row.image_ids:
            capsule["image_sources"].append({"source": source, "image_ids": list(dict.fromkeys(row.image_ids))})

    proposal = {"source": str(pending.id),
                "plan": deepcopy(pending.plan) if pending.status in {"needs_input", "superseded"} else None,
                "task_context": capsule}
    for row in reversed(active):
        if row.status == "applied" and isinstance(getattr(row, "result", None), dict) and "image_import_progress" in row.result:
            proposal["image_import_progress"] = validate_import_progress(row.result["image_import_progress"])
            break
    size = len(json.dumps(proposal, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    if size > MAX_TASK_BYTES:
        raise TaskMemoryError("这项未完成的课表任务内容超过处理范围，原始要求已保留；请新建对话并缩小本次安排。")
    return proposal
