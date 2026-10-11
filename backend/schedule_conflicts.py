"""Deterministic overlap diagnostics shared by manual edits and agent planning.

The detector only describes conflicts; neither a model reply nor a diagnostic
can grant permission to ignore one. Inputs are already owner-scoped snapshots.
"""
from collections.abc import Iterable, Mapping
from datetime import datetime

from fastapi import HTTPException


MAX_CONFLICTS = 6
MAX_SAMPLE_DATES = 8
_SCHEDULE_FIELDS = ("starts_at", "ends_at", "status", "is_deleted")


def _instant(value):
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Overlap diagnostics require timezone-aware instants")
    return instant


def _side(item, before_lessons, recorded):
    item_id = str(item["id"])
    old = before_lessons.get(item_id) if before_lessons is not None else item
    changed = old is None or any(item.get(field) != old.get(field) for field in _SCHEDULE_FIELDS)
    return {
        "id": item_id,
        "course_id": str(item["course_id"]),
        "semester_id": str(item["semester_id"]),
        "name": item["name"],
        "local_date": str(item["local_date"]),
        "start_time": item["start_time"],
        "end_time": item["end_time"],
        "starts_at": _instant(item["starts_at"]).isoformat(),
        "ends_at": _instant(item["ends_at"]).isoformat(),
        "timezone": item["timezone"],
        "week_number": item["week_number"],
        "version": item["version"],
        "has_learning_record": item_id in recorded or item.get("has_learning_record") is True,
        "source": "proposed" if changed else "existing",
    }


def find_conflicts(lessons: Iterable[Mapping], *, before_lessons: Mapping | None = None,
                   recorded_lesson_ids=(), limit: int = MAX_CONFLICTS) -> list[dict]:
    """Find all overlapping pairs, grouping repeated weeks by course and source.

    ``before_lessons`` must cover all owned semesters, as does the database
    check. Omitting it marks rows as existing (for ordinary manual routes).
    Unchanged pre-existing rows must never be labelled as newly imported.
    Up to six distinct course pairs and eight example dates per pair are kept;
    occurrence counts include later repeats. Adjacent intervals do not overlap.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CONFLICTS:
        raise ValueError(f"Conflict limit must be between 1 and {MAX_CONFLICTS}")
    recorded = {str(value) for value in recorded_lesson_ids}
    rows = sorted(
        ((_instant(item["starts_at"]), _instant(item["ends_at"]), item)
         for item in lessons if item.get("status") == "scheduled" and not item.get("is_deleted", False)),
        key=lambda row: (row[0], str(row[2]["id"])),
    )
    groups = {}
    active = []
    for start, end, item in rows:
        active = [row for row in active if row[1] > start]
        right = _side(item, before_lessons, recorded)
        for _, _, previous in active:
            left = _side(previous, before_lessons, recorded)
            # Course identity (not name) distinguishes separate recurring slots
            # of the same subject. Order-independent keys combine later weeks.
            pair = tuple(sorted(((left["course_id"], left["source"]),
                                 (right["course_id"], right["source"]))))
            if pair not in groups:
                if len(groups) == limit:
                    continue
                groups[pair] = {"left": left, "right": right, "occurrence_count": 0,
                                "protected_occurrence_count": 0, "dates": []}
            group = groups[pair]
            group["occurrence_count"] += 1
            if left["has_learning_record"] or right["has_learning_record"]:
                # A later recorded week must not disappear behind an earlier
                # unrecorded example of the same recurring collision.
                if not group["protected_occurrence_count"]:
                    group["left"], group["right"] = left, right
                group["protected_occurrence_count"] += 1
            for day in (left["local_date"], right["local_date"]):
                if day not in group["dates"] and len(group["dates"]) < MAX_SAMPLE_DATES:
                    group["dates"].append(day)
        active.append((start, end, item))
    return list(groups.values())


def format_conflicts(conflicts: list[dict]) -> str:
    """Return a bounded Chinese message usable by clients expecting a string."""
    def label(side):
        source = "拟导入/调整" if side["source"] == "proposed" else "现有"
        protected = "（已有学习记录）" if side["has_learning_record"] else ""
        name = side["name"]
        name = name if len(name) <= 28 else name[:27] + "…"
        return (f'{source}「{name}」{side["local_date"]} '
                f'{side["start_time"]}–{side["end_time"]}{protected}')

    lines = ["课表方案存在课程时间重叠（最多列出 6 组，同一对课程跨周合并）："]
    for item in conflicts[:MAX_CONFLICTS]:
        repeat = f'，共 {item["occurrence_count"]} 次' if item["occurrence_count"] > 1 else ""
        lines.append(f'{label(item["left"])} 与 {label(item["right"])} 冲突{repeat}。')
    lines.append("方案尚未应用；需要核对识别的星期、节次及现有安排后修正，不能直接忽略冲突。")
    return "\n".join(lines)


class ScheduleConflictError(HTTPException):
    """Compatible HTTP detail plus machine-readable, owner-scoped diagnostics."""
    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(409, format_conflicts(conflicts))
