"""Schema and tolerant normalization for a single multimodal timetable response.

Image coordinates and course-card bindings are deliberately absent: all visual
interpretation belongs to the LLM, together with the user's original words.
"""
from copy import deepcopy
import re

SCHEMA_VERSION = "timetable-json-v2"
SCHEDULE_FIELDS = ("weekday", "period_table_id", "period_numbers", "start_time", "end_time", "weeks", "teacher", "location")

_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INTS = {"type": ["array", "null"], "items": {"type": "integer"}}
_SCHEDULE_PROPERTIES = {
    "id": {"type": "string", "description": "Copy an existing schedule ID only for that occurrence. For a new schedule, omit ID or use a request-local S1/S2 alias when exceptions refer to it. The server always assigns the stable ID."},
    "weekday": {"type": ["integer", "null"], "minimum": 1, "maximum": 7},
    "period_table_id": _NULLABLE_STRING,
    "period_numbers": _NULLABLE_INTS,
    "start_time": _NULLABLE_STRING,
    "end_time": _NULLABLE_STRING,
    "weeks": _NULLABLE_INTS,
    "teacher": _NULLABLE_STRING,
    "location": _NULLABLE_STRING,
}
UNDERSTANDING_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"enum": ["write", "read"]},
        "mode": {"enum": ["merge", "replace"], "description": "replace only when explicitly requested by the current user"},
        "period_tables": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "weeks": _NULLABLE_INTS,
            "periods": {"type": "array", "items": {"type": "object", "properties": {
                "number": {"type": ["integer", "null"]},
                "start_time": _NULLABLE_STRING, "end_time": _NULLABLE_STRING},
                "required": ["number", "start_time", "end_time"]}}}, "required": ["periods"]}},
        "period_table_changes": {"type": "array", "description": "Explicit current-period-table CRUD. Omitted fields preserve saved values; null explicitly clears a value. Period-based course clocks always follow the current table from the first selected period's start to the last selected period's end. Missing periods or boundary clocks clear both course clocks. Never implicitly delete courses or their weeks; preserve direct-clock schedules without periods.",
            "items": {"type": "object", "properties": {
                "action": {"enum": ["create", "update", "delete", "clear"]},
                "id": {"type": "string", "description": "Stable period-table ID for update/delete; a request-local P1/P2 alias is allowed for create. clear needs no ID."},
                "weeks": _NULLABLE_INTS,
                "periods": {"type": "array", "description": "Sparse upserts by period number. Unmentioned periods and unprovided clock fields remain unchanged.",
                    "items": {"type": "object", "properties": {
                        "number": {"type": "integer", "minimum": 1, "maximum": 30},
                        "start_time": _NULLABLE_STRING, "end_time": _NULLABLE_STRING}, "required": ["number"]}},
                "period_removals": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 30},
                    "description": "Only for update: remove these period numbers, retaining every unmentioned period."},
                "source_text": {"type": "string"},
            }, "required": ["action"]}},
        "courses": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "version": {"type": "integer"}, "name": _NULLABLE_STRING,
            "schedules": {"type": "array", "items": {"type": "object", "properties": _SCHEDULE_PROPERTIES}},
            "exceptions": {"type": "array", "items": {"type": "object"}},
        }, "required": ["name", "schedules"]}},
        "changes": {"type": "array", "items": {"type": "object", "properties": {
            "course_id": {"type": ["string", "null"]}, "version": {"type": "integer"},
            "action": {"enum": ["update", "delete"]}, "name": _NULLABLE_STRING,
            "schedules": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "weeks": _NULLABLE_INTS,
                "fields": {"type": "object", "properties": _SCHEDULE_PROPERTIES}}, "required": ["id", "fields"]}},
            "add_schedules": {"type": "array", "items": {"type": "object", "properties": _SCHEDULE_PROPERTIES}},
            "remove_schedule_ids": {"type": "array", "items": {"type": "string"}},
            "exceptions": {"type": "array", "items": {"type": "object", "properties": {
                "schedule_id": {"type": "string"}, "original_week": {"type": "integer"},
                "local_date": _NULLABLE_STRING, "start_time": _NULLABLE_STRING, "end_time": _NULLABLE_STRING,
                "status": {"enum": ["scheduled", "leave", "cancelled"]}, "note": _NULLABLE_STRING,
                "is_deleted": {"type": "boolean"}}, "required": ["schedule_id", "original_week"]}},
            "candidate_course_ids": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"}, "source_text": {"type": "string"},
        }, "required": ["action"]}},
        "semester": {"type": ["object", "null"], "properties": {
            "id": _NULLABLE_STRING, "name": _NULLABLE_STRING, "start_date": _NULLABLE_STRING, "first_week_monday": _NULLABLE_STRING,
            "end_date": _NULLABLE_STRING, "timezone": _NULLABLE_STRING}},
        "sources": {"type": "array", "items": {"type": "object"}},
        "unapplied_changes": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["intent", "mode", "period_tables", "period_table_changes", "courses", "changes", "sources", "unapplied_changes"],
}


def _text(value, limit=500):
    return value.strip()[:limit] or None if isinstance(value, str) else None


def _positive(value, maximum=366):
    if type(value) is int and 1 <= value <= maximum:
        return value
    return None


def _numbers(value, maximum=54):
    if not isinstance(value, list):
        return None
    values = sorted({_positive(item, maximum) for item in value} - {None})
    return values or None


def _clock(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{1,2})\s*", value)
    if not match:
        return None
    hour, minute = map(int, match.groups())
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def normalize_fields(value, *, partial=False):
    """Omitted patches stay omitted; explicit null is never converted to unset."""
    value = value if isinstance(value, dict) else {}
    result = {}
    for field in SCHEDULE_FIELDS:
        if partial and field not in value:
            continue
        raw = value.get(field)
        if field == "weekday":
            result[field] = _positive(raw, 7)
        elif field in {"weeks", "period_numbers"}:
            result[field] = _numbers(raw, 30 if field == "period_numbers" else 54)
        elif field in {"start_time", "end_time"}:
            result[field] = _clock(raw)
        else:
            result[field] = _text(raw)
        if partial and raw is not None and result[field] is None:
            # An invalid inferred value cannot silently clear saved information.
            del result[field]
    if not partial and value.get("id"):
        result["id"] = _text(value["id"], 100)
    return result


def normalize_period_table_change(value):
    """Keep explicit clears distinct from omitted or invalid inferred values."""
    if not isinstance(value, dict) or value.get("action") not in {"create", "update", "delete", "clear"}:
        return None
    action = value["action"]
    result = {"action": action}
    if _text(value.get("source_text")):
        result["source_text"] = _text(value["source_text"])
    if "id" in value:
        table_id = _text(value["id"], 100)
        # A supplied but invalid target must not become an omitted target:
        # omission may select the sole table or permit a global clear.
        result["id"] = table_id if table_id is not None else deepcopy(value["id"])
    if action == "clear":
        return result
    if action == "delete":
        return result
    if "weeks" in value:
        weeks = _numbers(value["weeks"])
        if value["weeks"] is None or weeks is not None:
            result["weeks"] = weeks
    elif action == "create":
        result["weeks"] = None
    if isinstance(value.get("periods"), list) or action == "create":
        result["periods"] = []
        for period in value.get("periods", []) if isinstance(value.get("periods"), list) else []:
            if not isinstance(period, dict) or _positive(period.get("number"), 30) is None:
                continue
            item = {"number": period["number"]}
            for field in ("start_time", "end_time"):
                if action == "update" and field not in period:
                    continue
                raw = period.get(field)
                clock = _clock(raw)
                if action == "create" or raw is None or clock is not None:
                    item[field] = clock
            result["periods"].append(item)
    if action == "update" and isinstance(value.get("period_removals"), list):
        result["period_removals"] = _numbers(value["period_removals"], 30) or []
    return result


def normalize_understanding(value):
    """Normalize business fields independently, preserving partial useful data.

Only a completely unusable response is a technical error. Bad fields become
null and the original response is retained as provenance, never repaired by a
second LLM call.
"""
    from schedule_model import ModelError
    if not isinstance(value, dict) or not any(key in value for key in ("courses", "changes", "period_tables", "period_table_changes", "intent")):
        raise ModelError("课表助手返回的数据无法解析，已保留本次输入，请重试。")
    result = {"intent": "read" if value.get("intent") == "read" else "write",
              "mode": "replace" if value.get("mode") == "replace" else "merge",
              "period_tables": [], "period_table_changes": [], "courses": [], "changes": [],
              "sources": deepcopy(value.get("sources")) if isinstance(value.get("sources"), list) else [],
              "unapplied_changes": deepcopy(value.get("unapplied_changes")) if isinstance(value.get("unapplied_changes"), list) else []}
    if isinstance(value.get("semester"), dict):
        result["semester"] = {key: _text(value["semester"].get(key), 100)
                              for key in ("id", "name", "start_date", "first_week_monday", "end_date", "timezone")}
    for table in value.get("period_tables", []) if isinstance(value.get("period_tables"), list) else []:
        if not isinstance(table, dict):
            continue
        periods = []
        for period in table.get("periods", []) if isinstance(table.get("periods"), list) else []:
            if isinstance(period, dict):
                periods.append({"number": _positive(period.get("number"), 30),
                                "start_time": _clock(period.get("start_time")), "end_time": _clock(period.get("end_time"))})
        result["period_tables"].append({"id": _text(table.get("id"), 100), "weeks": _numbers(table.get("weeks")), "periods": periods})
    for change in value.get("period_table_changes", []) if isinstance(value.get("period_table_changes"), list) else []:
        normalized = normalize_period_table_change(change)
        if normalized is not None:
            result["period_table_changes"].append(normalized)
        else:
            result["unapplied_changes"].append({"period_table_change": deepcopy(change),
                                                "reason": "节次表修改类型无效，已保留原始内容。"})
    for course in value.get("courses", []) if isinstance(value.get("courses"), list) else []:
        if not isinstance(course, dict):
            continue
        data = {"name": _text(course.get("name"), 200), "schedules": [],
                "exceptions": deepcopy(course.get("exceptions")) if isinstance(course.get("exceptions"), list) else []}
        if course.get("id"):
            data["id"] = _text(course["id"], 100)
        if type(course.get("version")) is int:
            data["version"] = course["version"]
        for schedule in course.get("schedules", []) if isinstance(course.get("schedules"), list) else []:
            if isinstance(schedule, dict):
                data["schedules"].append(normalize_fields(schedule))
        # Preserve an unnamed course only when some actual course fact exists.
        if data["name"] or any(any(item.get(key) is not None for key in SCHEDULE_FIELDS) for item in data["schedules"]):
            result["courses"].append(data)
    for change in value.get("changes", []) if isinstance(value.get("changes"), list) else []:
        if not isinstance(change, dict):
            continue
        item = deepcopy(change)
        item["action"] = "delete" if change.get("action") == "delete" else "update"
        if "name" in change:
            name = _text(change["name"], 200)
            if change["name"] is None or name is not None:
                item["name"] = name
            else:
                item.pop("name", None)
        if "fields" in change:
            item["fields"] = normalize_fields(change["fields"], partial=True)
        if "weeks" in change:
            item["weeks"] = _numbers(change["weeks"])
        if isinstance(change.get("schedules"), list):
            item["schedules"] = [{**part, "fields": normalize_fields(part.get("fields"), partial=True),
                                  **({"weeks": _numbers(part["weeks"])} if "weeks" in part else {})}
                                 for part in change["schedules"] if isinstance(part, dict)]
        if isinstance(change.get("add_schedules"), list):
            item["add_schedules"] = [normalize_fields(part) for part in change["add_schedules"] if isinstance(part, dict)]
        result["changes"].append(item)
    # Raw data also preserves unconvertible week descriptions and invalid values.
    if not any(isinstance(source, dict) and source.get("kind") == "llm_extraction"
               and source.get("schema_version") == SCHEMA_VERSION for source in result["sources"]):
        result["sources"].append({"kind": "llm_extraction", "schema_version": SCHEMA_VERSION, "raw": deepcopy(value)})
    if result["intent"] == "read":
        result.update(mode="merge", courses=[], changes=[], period_tables=[], period_table_changes=[])
        result.pop("semester", None)
    return result
