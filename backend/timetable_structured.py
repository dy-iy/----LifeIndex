"""JSONB timetable source of truth and deterministic legacy lesson projection.

This module never commits: callers own the transaction and account write lock.
Extraction is tolerant; explicit edits are sparse and preserve JSON null.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
import uuid
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from db import check_version, owned
from models import CourseTemplate, Lesson, ScheduleThread, Semester, TimetableCourse, TimetableDocument

SCHEMA_VERSION = "structured-timetable-v1"
SCHEDULE_FIELDS = ("weekday", "period_table_id", "period_numbers", "start_time", "end_time", "weeks", "teacher", "location")
CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _now():
    return datetime.now(timezone.utc)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _text(value, limit):
    return value.strip()[:limit] or None if isinstance(value, str) else None


def _numbers(value, maximum):
    if not isinstance(value, list):
        return None
    result = sorted({item for item in value if isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= maximum})
    return result if result or not value else None


def normalize_schedule(raw, *, schedule_id=None, sparse=False):
    raw = raw if isinstance(raw, dict) else {}
    result = {} if sparse else {field: None for field in SCHEDULE_FIELDS}
    for field in SCHEDULE_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if field == "weekday":
            value = value if type(value) is int and 1 <= value <= 7 else None
        elif field in ("weeks", "period_numbers"):
            value = _numbers(value, 54 if field == "weeks" else 30)
        elif field in ("start_time", "end_time"):
            value = value if isinstance(value, str) and CLOCK.fullmatch(value) else None
        elif field == "period_table_id":
            value = _text(value, 100)
        else:
            value = _text(value, 200 if field == "location" else 100)
        if sparse and raw[field] is not None and value is None:
            # Only an explicit JSON null can clear an existing value.
            continue
        result[field] = value
    if not sparse:
        result["id"] = str(schedule_id or _uuid(raw.get("id")) or uuid.uuid4())
    return result


def _normalize_periods(raw):
    items = []
    seen = set()
    for period in raw if isinstance(raw, list) else []:
        if not isinstance(period, dict) or type(period.get("number")) is not int or not 1 <= period["number"] <= 30 or period["number"] in seen:
            continue
        seen.add(period["number"])
        items.append({"number": period["number"], **{
            key: value if isinstance(value := period.get(key), str) and CLOCK.fullmatch(value) else None
            for key in ("start_time", "end_time")
        }})
    return sorted(items, key=lambda item: item["number"])


def _scope(user_id, semester_id, thread_id):
    if (semester_id is None) == (thread_id is None):
        raise HTTPException(422, "semester_id 和 thread_id 必须且只能提供一个。")
    return {"user_id": user_id, "semester_id": semester_id, "thread_id": thread_id}


def get_document(db, user_id, semester_id=None, thread_id=None, create=False):
    scope = _scope(user_id, semester_id, thread_id)
    if semester_id is not None:
        semester = owned(db, Semester, user_id, semester_id)
    else:
        thread = owned(db, ScheduleThread, user_id, thread_id)
        if thread.semester_id is not None:
            return get_document(db, user_id, semester_id=thread.semester_id, create=create)
    document = db.scalar(select(TimetableDocument).filter_by(**scope))
    if document is None and create:
        data = {"schema_version": SCHEMA_VERSION, "period_tables": [], "sources": []}
        if semester_id is not None and semester.periods:
            data["period_tables"] = [{"id": str(uuid.uuid4()), "weeks": None, "periods": _normalize_periods(semester.periods)}]
            data["display_period_table_id"] = data["period_tables"][0]["id"]
        document = TimetableDocument(**scope, data=data, version=1)
        db.add(document)
        db.flush()
    return document


def course_result(course):
    return {"id": str(course.id), "document_id": str(course.document_id), "version": course.version,
            "data": deepcopy(course.data), "is_deleted": course.is_deleted,
            "updated_at": course.updated_at.isoformat() if course.updated_at else None}


def document_snapshot(db, document):
    if document is None:
        return {"document": None, "document_id": None, "version": None, "period_tables": [], "courses": []}
    data = {key: deepcopy(value) for key, value in document.data.items() if key != "sources"}
    courses = db.scalars(select(TimetableCourse).where(
        TimetableCourse.user_id == document.user_id, TimetableCourse.document_id == document.id,
        TimetableCourse.is_deleted.is_(False),
    ).order_by(TimetableCourse.created_at, TimetableCourse.id)).all()
    return {"document": {"id": str(document.id), "version": document.version,
                          "semester_id": str(document.semester_id) if document.semester_id else None,
                          "thread_id": str(document.thread_id) if document.thread_id else None, "data": data},
            "document_id": str(document.id), "version": document.version,
            "semester_id": str(document.semester_id) if document.semester_id else None,
            "thread_id": str(document.thread_id) if document.thread_id else None,
            "period_tables": data.get("period_tables", []), "courses": [course_result(item) for item in courses]}


def structured_snapshot(db, user_id, semester_id=None, thread_id=None):
    if isinstance(semester_id, TimetableDocument):
        if semester_id.user_id != user_id:
            raise HTTPException(404, "未找到该课表。")
        return document_snapshot(db, semester_id)
    return document_snapshot(db, get_document(db, user_id, semester_id=semester_id, thread_id=thread_id))


def _touch(document):
    document.version += 1
    document.updated_at = _now()


def _course_touch(course):
    course.version += 1
    course.updated_at = _now()


def _current_period_table(data):
    """Old tables remain stored; one table supplies the current semester's clocks."""
    tables = data.get("period_tables", [])
    return (next((item for item in tables if item["id"] == data.get("display_period_table_id")), None)
            or next((item for item in tables if item.get("source") == "manual"), None)
            or next(iter(tables), None))


def _merge_period_clocks(table, periods, *, preserve_known=False):
    stored = {item["number"]: deepcopy(item) for item in table.get("periods", [])}
    for period in periods:
        current = stored.setdefault(period["number"], deepcopy(period))
        for field in ("start_time", "end_time"):
            if period[field] is not None and (not preserve_known or current.get(field) is None):
                current[field] = period[field]
    table["periods"] = sorted(stored.values(), key=lambda item: item["number"])


def _merge_period_tables(document, tables):
    data = deepcopy(document.data)
    stored = data.setdefault("period_tables", [])
    aliases = {}
    for raw in tables if isinstance(tables, list) else []:
        if not isinstance(raw, dict):
            continue
        periods = _normalize_periods(raw.get("periods"))
        if not periods:
            continue
        matching = _current_period_table(data)
        if matching is None:
            matching = {"id": str(uuid.uuid4()), "weeks": None, "periods": periods}
            stored.append(matching)
        else:
            _merge_period_clocks(matching, periods, preserve_known=True)
        data["display_period_table_id"] = matching["id"]
        if raw.get("id") is not None:
            aliases[str(raw["id"])] = matching["id"]
    document.data = data
    return aliases


def sync_period_axis(db, user, document, *, previous_tables=None, preferred_id=None):
    """Reuse one current table, preserving configured semester clocks on old data."""
    if document.semester_id is None:
        return
    semester = owned(db, Semester, user.id, document.semester_id)
    data = deepcopy(document.data)
    tables = data.setdefault("period_tables", [])
    by_id = {table["id"]: table for table in tables}
    selected = preferred_id or data.get("display_period_table_id")
    if selected not in by_id:
        configured = _normalize_periods(semester.periods)
        old_tables = previous_tables if previous_tables is not None else tables
        matching = next((table for table in old_tables if table["id"] in by_id
                         and _normalize_periods(table.get("periods")) == configured), None)
        table = by_id.get(matching["id"]) if matching else _current_period_table(data)
        if table is None and configured and previous_tables is None:
            table = {"id": str(uuid.uuid4()), "weeks": None, "source": "manual", "periods": configured}
            tables.append(table)
            by_id[table["id"]] = table
        elif table is not None and configured and matching is None and previous_tables is None:
            # Binding an older document must not replace the user's semester settings.
            table["periods"] = configured
            table["source"] = "manual"
        selected = table["id"] if table else None
    periods = _normalize_periods(by_id[selected].get("periods")) if selected else []
    for period in periods:
        if period["start_time"] and period["end_time"] and period["start_time"] >= period["end_time"]:
            period["start_time"] = period["end_time"] = None
    data["display_period_table_id"] = selected
    document.data = data
    if semester.periods != periods:
        semester.periods = periods
        semester.version += 1
    db.flush()


def _apply_period_table_changes(db, user, document, operations, aliases, unapplied):
    """Apply each explicit table operation with its course references atomically."""
    applied, updated, diagnostics = [], [], []
    for raw in operations if isinstance(operations, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            with db.begin_nested():
                action = raw.get("action")
                if action not in {"create", "update", "delete", "clear"}:
                    raise HTTPException(422, "无法识别节次表操作。")
                data = deepcopy(document.data)
                before_tables = deepcopy(data.get("period_tables", []))
                tables = data.setdefault("period_tables", [])
                raw_id = raw.get("id")
                if "id" in raw and (not isinstance(raw_id, str) or not raw_id.strip()):
                    raise HTTPException(422, "节次表目标 ID 无效。")
                identifier = aliases.get(str(raw_id), raw_id)
                target = next((table for table in tables if table["id"] == identifier), None)
                if action in {"update", "delete"}:
                    if identifier is None:
                        target = _current_period_table(data)
                    if target is None:
                        reason = "存在多套节次表，无法唯一确定操作目标。" if identifier is None and len(tables) > 1 else "未找到属于当前课表的节次表。"
                        raise HTTPException(422, reason)
                    if action == "update":
                        target = _current_period_table(data) or target
                    identifier = target["id"]
                if action == "clear" and identifier is not None:
                    raise HTTPException(422, "清空全部节次表不接受单个目标，请使用删除操作。")
                removed_ids = set()
                if action == "create":
                    target = _current_period_table(data)
                    if target is None:
                        target = {"id": str(uuid.uuid4()), "weeks": None, "periods": []}
                        tables.append(target)
                    identifier = target["id"]
                    _merge_period_clocks(target, _normalize_periods(raw.get("periods", [])), preserve_known=True)
                elif action == "update":
                    if "weeks" in raw:
                        weeks = _numbers(raw["weeks"], 54)
                        if raw["weeks"] is None or weeks is not None:
                            target["weeks"] = weeks
                    periods = {period["number"]: deepcopy(period) for period in target.get("periods", [])}
                    removals = _numbers(raw.get("period_removals", []), 30) or []
                    if set(removals) - set(periods):
                        raise HTTPException(422, "未找到待删除的节次。")
                    for number in removals:
                        periods.pop(number)
                    for patch in raw.get("periods", []) if isinstance(raw.get("periods"), list) else []:
                        if not isinstance(patch, dict) or type(patch.get("number")) is not int or not 1 <= patch["number"] <= 30:
                            continue
                        if patch["number"] in removals:
                            raise HTTPException(422, "同一操作不能同时修改和删除相同节次。")
                        period = periods.setdefault(patch["number"], {"number": patch["number"], "start_time": None, "end_time": None})
                        for field in ("start_time", "end_time"):
                            if field in patch and (patch[field] is None or isinstance(patch[field], str) and CLOCK.fullmatch(patch[field])):
                                period[field] = patch[field]
                    target["periods"] = sorted(periods.values(), key=lambda period: period["number"])
                elif action == "delete":
                    removed_ids.add(identifier)
                    data["period_tables"] = [table for table in tables if table["id"] != identifier]
                else:
                    removed_ids.update(table["id"] for table in tables)
                    data["period_tables"] = []
                if target is not None and action in {"create", "update"}:
                    if any(period.get("start_time") and period.get("end_time") and period["start_time"] >= period["end_time"] for period in target["periods"]):
                        raise HTTPException(422, "节次结束时间须晚于开始时间。")
                    data["display_period_table_id"] = identifier
                document.data = data
                sync_period_axis(db, user, document, previous_tables=before_tables)
                changed_courses = []
                courses = db.scalars(select(TimetableCourse).where(TimetableCourse.user_id == user.id,
                    TimetableCourse.document_id == document.id, TimetableCourse.is_deleted.is_(False))).all()
                for course in courses:
                    course_data = deepcopy(course.data)
                    for schedule in course_data.get("schedules", []):
                        if schedule.get("period_table_id") in removed_ids:
                            schedule["period_table_id"] = None
                        schedule.update(_effective_schedule(schedule, document.data.get("period_tables", []),
                                                            document.data.get("display_period_table_id")))
                    if course_data != course.data:
                        _guard_metadata_change(db, user, course, course.data, course_data)
                        course.data = course_data
                        _course_touch(course)
                        changed_courses.append(course.id)
                db.flush()
                operation_diagnostics = sync_document(db, user, document, course_ids=changed_courses)
                effect = {"action": action, "id": identifier if action != "clear" else None,
                          "affected_table_ids": sorted(removed_ids) if removed_ids else [identifier]}
                if action in {"create", "update"}:
                    effect["table"] = deepcopy(target)
            if action == "create" and raw_id is not None:
                aliases[str(raw_id)] = identifier
            applied.append(effect)
            updated.extend(str(course_id) for course_id in changed_courses)
            diagnostics.extend(operation_diagnostics)
        except HTTPException as error:
            tables = document.data.get("period_tables", [])
            unapplied.append({"reason": error.detail, "period_table_change": deepcopy(raw),
                              "candidate_period_table_ids": [table["id"] for table in tables] if raw.get("id") is None else []})
    return applied, updated, diagnostics


def _remap_period_ids(value, aliases):
    if isinstance(value, list):
        return [_remap_period_ids(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: aliases.get(str(item), item) if key == "period_table_id" else _remap_period_ids(item, aliases)
                for key, item in value.items()}
    return value


def _exception_fields(raw):
    result = normalize_schedule({key: raw[key] for key in ("start_time", "end_time") if key in raw}, sparse=True)
    date_key = "local_date" if "local_date" in raw else "date"
    if date_key in raw:
        value = raw[date_key]
        if value is None:
            result["date"] = None
        elif isinstance(value, str):
            try:
                result["date"] = date.fromisoformat(value).isoformat()
            except ValueError:
                pass
    if raw.get("status") in ("scheduled", "leave", "cancelled"):
        result["status"] = raw["status"]
    if "note" in raw and (raw["note"] is None or isinstance(raw["note"], str)):
        result["note"] = _text(raw["note"], 500)
    if type(raw.get("is_deleted")) is bool:
        result["is_deleted"] = raw["is_deleted"]
    return result


def _same_schedule(left, right):
    if left.get("id") == right.get("id"):
        return True
    if all(left.get(field) == right.get(field) for field in SCHEDULE_FIELDS):
        return True
    # A weekday plus either exact clock times or periods identifies an occurrence.
    # Different explicit weeks remain separate semester segments.
    if left.get("weekday") is not None and right.get("weekday") is not None and left["weekday"] != right["weekday"]:
        return False
    if left.get("weeks") is not None and right.get("weeks") is not None and left["weeks"] != right["weeks"]:
        return False
    for field in ("start_time", "end_time", "period_numbers", "period_table_id", "teacher", "location"):
        if left.get(field) is not None and right.get(field) is not None and left[field] != right[field]:
            return False
    anchors = sum(bool(left.get(field) and left.get(field) == right.get(field))
                  for field in ("period_numbers", "start_time", "weeks"))
    return anchors >= (1 if left.get("weekday") is not None and right.get("weekday") is not None else 2)


def _merge_course(course, raw, aliases, tables=None, default_table_id=None):
    data = deepcopy(course.data)
    name = _text(raw.get("name"), 100)
    if data.get("name") is None and name is not None:
        data["name"] = name
    schedules = [_effective_schedule(schedule, tables or [], default_table_id)
                 for schedule in data.setdefault("schedules", [])]
    data["schedules"] = schedules
    schedule_aliases = {}
    for item in raw.get("schedules", []) if isinstance(raw.get("schedules"), list) else []:
        if not isinstance(item, dict) or (item.get("id") is None and not any(item.get(field) is not None for field in SCHEDULE_FIELDS)):
            continue
        normalized = normalize_schedule(item)
        if normalized.get("period_table_id") in aliases:
            normalized["period_table_id"] = aliases[normalized["period_table_id"]]
        if normalized.get("period_table_id") not in {table["id"] for table in tables or []}:
            normalized["period_table_id"] = None
        normalized = _effective_schedule(normalized, tables or [], default_table_id)
        current = next((schedule for schedule in schedules if _same_schedule(schedule, normalized)), None)
        if current is None:
            # Never trust model-created UUIDs for new records.
            normalized = _effective_schedule(normalized, tables or [], default_table_id)
            normalized["id"] = str(uuid.uuid4())
            schedules.append(normalized)
        else:
            for field in SCHEDULE_FIELDS:
                if normalized.get(field) is not None:
                    if field in current.get("_cleared_fields", []) and item.get(field) is None:
                        continue
                    current[field] = normalized[field]
            current.update(_effective_schedule(current, tables or [], default_table_id))
        if item.get("id") is not None:
            schedule_aliases[str(item["id"])] = current["id"] if current is not None else normalized["id"]
    data.setdefault("exceptions", [])
    # Extracted single-occurrence facts can supplement existing facts. Explicit
    # user replacement/clears are handled only by the sparse changes interface.
    for raw_exception in raw.get("exceptions", []) if isinstance(raw.get("exceptions"), list) else []:
        if not isinstance(raw_exception, dict):
            continue
        exception = {"schedule_id": schedule_aliases.get(str(raw_exception.get("schedule_id")), raw_exception.get("schedule_id")),
                     "original_week": raw_exception.get("original_week"), **_exception_fields(raw_exception)}
        if exception.get("schedule_id") not in {item["id"] for item in schedules} or type(exception.get("original_week")) is not int or not 1 <= exception["original_week"] <= 54:
            continue
        current = next((item for item in data["exceptions"] if item.get("schedule_id") == exception["schedule_id"]
                        and item.get("original_week") == exception["original_week"]), None)
        if current is None:
            data["exceptions"].append(exception)
        else:
            for field, value in exception.items():
                if current.get(field) is None and value is not None:
                    current[field] = value
    data["schedules"] = [_effective_schedule(schedule, tables or [], default_table_id) for schedule in schedules]
    changed = data != course.data
    course.data = data
    if changed:
        _course_touch(course)
    return changed


def _patch_data(course, patch, tables=None, default_table_id=None):
    data = deepcopy(course.data)
    if "name" in patch and (patch["name"] is None or isinstance(patch["name"], str)):
        data["name"] = _text(patch["name"], 100)
    schedules = data.setdefault("schedules", [])
    split_aliases, addition_aliases = {}, {}
    updates = patch.get("schedule_updates", patch.get("schedules", []))
    if patch.get("schedule_id") is not None:
        updates = [*updates, {"id": patch["schedule_id"], "fields": patch.get("fields", {}), "weeks": patch.get("weeks")}]
    for update in updates:
        schedule = next((item for item in schedules if item["id"] == str(update.get("id"))), None)
        if schedule is None:
            raise HTTPException(404, "未找到指定的课程安排。")
        fields = update.get("fields", {key: value for key, value in update.items() if key not in ("id", "target_weeks")})
        normalized_fields = normalize_schedule(fields, sparse=True)
        if normalized_fields.get("period_table_id") is not None and normalized_fields["period_table_id"] not in {table["id"] for table in tables or []}:
            raise HTTPException(404, "未找到属于当前课表的节次表。")
        if not normalized_fields:
            continue
        target_weeks = update.get("target_weeks", update.get("weeks") if "fields" in update else None)
        if target_weeks is not None:
            target_weeks = _numbers(target_weeks, 54)
            if not target_weeks or schedule.get("weeks") is None or not set(target_weeks) <= set(schedule["weeks"]):
                raise HTTPException(422, "局部周次修改必须属于原课程的明确周次。")
            remaining = sorted(set(schedule["weeks"]) - set(target_weeks))
            if remaining:
                split = deepcopy(schedule)
                split["id"] = str(uuid.uuid4())
                split["_split_from"] = schedule["id"]
                split["weeks"] = target_weeks
                schedule["weeks"] = remaining
                schedules.append(split)
                split_aliases.update({(schedule["id"], week): split["id"] for week in target_weeks})
                # Exceptions follow the original week, regardless of schedule split.
                for exception in data.get("exceptions", []):
                    if exception.get("schedule_id") == schedule["id"] and exception.get("original_week") in target_weeks:
                        exception["schedule_id"] = split["id"]
                schedule = split
        schedule.update(normalized_fields)
        cleared = set(schedule.get("_cleared_fields", []))
        for field in ("start_time", "end_time"):
            if field in normalized_fields:
                if normalized_fields[field] is None:
                    cleared.add(field)
                else:
                    cleared.discard(field)
        if cleared:
            schedule["_cleared_fields"] = sorted(cleared)
        else:
            schedule.pop("_cleared_fields", None)
    removals = {str(item) for item in patch.get("schedule_removals", patch.get("remove_schedule_ids", []))}
    if removals - {item["id"] for item in schedules}:
        raise HTTPException(404, "未找到待移除的课程安排。")
    data["schedules"] = [item for item in schedules if item["id"] not in removals]
    for raw in patch.get("schedule_additions", patch.get("add_schedules", [])):
        addition = normalize_schedule(raw, schedule_id=uuid.uuid4())
        if addition.get("period_table_id") is not None and addition["period_table_id"] not in {table["id"] for table in tables or []}:
            raise HTTPException(404, "未找到属于当前课表的节次表。")
        addition = _effective_schedule(addition, tables or [], default_table_id)
        data["schedules"].append(addition)
        if isinstance(raw, dict) and raw.get("id") is not None:
            addition_aliases[str(raw["id"])] = addition["id"]
    if "exceptions" in patch:
        exceptions = data.setdefault("exceptions", [])
        for original in patch["exceptions"]:
            raw = deepcopy(original)
            if isinstance(raw, dict):
                schedule_id = addition_aliases.get(str(raw.get("schedule_id")), raw.get("schedule_id"))
                raw["schedule_id"] = split_aliases.get((schedule_id, raw.get("original_week")), schedule_id)
            if not isinstance(raw, dict) or raw.get("schedule_id") not in {item["id"] for item in data["schedules"]} or type(raw.get("original_week")) is not int or not 1 <= raw["original_week"] <= 54:
                raise HTTPException(422, "单次安排需要有效的排课 ID 和原始周次。")
            current = next((item for item in exceptions if item.get("schedule_id") == raw["schedule_id"] and item.get("original_week") == raw["original_week"]), None)
            if current is None:
                current = {"schedule_id": raw["schedule_id"], "original_week": raw["original_week"]}
                exceptions.append(current)
            current.update(_exception_fields(raw))
    data["schedules"] = [_effective_schedule(schedule, tables or [], default_table_id)
                         for schedule in data["schedules"]]
    return data


def _guard_metadata_change(db, user, course, before, after):
    """Teacher/period metadata belongs to a live occurrence too."""
    from timetable_lifecycle import assert_not_timing
    previous = {item["id"]: item for item in before.get("schedules", [])}
    affected = {}
    for schedule in after.get("schedules", []):
        old = previous.get(schedule["id"])
        if old is None or not any(old.get(field) != schedule.get(field) for field in ("teacher", "period_table_id", "period_numbers")):
            continue
        query = select(Lesson).join(CourseTemplate, Lesson.course_id == CourseTemplate.id).where(
            Lesson.user_id == user.id, CourseTemplate.user_id == user.id,
            CourseTemplate.source_course_id == course.id,
            CourseTemplate.source_schedule_id == _uuid(schedule["id"]),
        )
        if old.get("weeks") is not None and schedule.get("weeks") is not None:
            query = query.where(Lesson.week_number.in_(set(old["weeks"]) & set(schedule["weeks"])))
        affected.update({item.id: item for item in db.scalars(query).all()})
    if affected:
        assert_not_timing(db, user.id, list(affected.values()), operation="modify")


def create_structured_course(db, user, document, data):
    if not isinstance(document, TimetableDocument):
        document = owned(db, TimetableDocument, user.id, document)
    if document.user_id != user.id:
        raise HTTPException(404, "未找到该课表。")
    sync_period_axis(db, user, document)
    course = TimetableCourse(user_id=user.id, document_id=document.id,
                             data={"name": _text(data.get("name"), 100), "schedules": [], "exceptions": []}, version=1)
    db.add(course)
    _merge_course(course, data, {}, document.data.get("period_tables", []), document.data.get("display_period_table_id"))
    course.version = 1
    _touch(document)
    db.flush()
    sync_document(db, user, document)
    return course_result(course)


def patch_structured_course(db, user, course_id, patch):
    patch = patch.model_dump(mode="json", exclude_unset=True) if hasattr(patch, "model_dump") else patch
    course = owned(db, TimetableCourse, user.id, course_id)
    if course.is_deleted:
        raise HTTPException(404, "未找到该课程。")
    check_version(course, patch.get("version"))
    document = owned(db, TimetableDocument, user.id, course.document_id)
    sync_period_axis(db, user, document)
    data = _patch_data(course, patch, document.data.get("period_tables", []), document.data.get("display_period_table_id"))
    _guard_metadata_change(db, user, course, course.data, data)
    course.data = data
    audit = deepcopy(document.data)
    audit.setdefault("sources", []).append({"type": "manual_course_patch", "course_id": str(course.id),
                                            "recorded_at": _now().isoformat(), "patch": deepcopy(patch)})
    document.data = audit
    _course_touch(course)
    _touch(document)
    db.flush()
    diagnostics = sync_document(db, user, document)
    return {**course_result(course), "diagnostics": diagnostics}


def associate_document(db, user, document, semester_id):
    semester = owned(db, Semester, user.id, semester_id)
    if document.user_id != user.id:
        raise HTTPException(404, "未找到该课表。")
    if document.semester_id is not None and document.semester_id != semester.id:
        raise HTTPException(409, "该课表已关联其他学期。")
    original_thread_id = document.thread_id
    target = get_document(db, user.id, semester_id=semester.id)
    if target is None:
        document.semester_id, document.thread_id = semester.id, None
        target = document
        if semester.periods:
            data = deepcopy(target.data)
            table = _current_period_table(data)
            if table is None:
                table = {"id": str(uuid.uuid4()), "weeks": None}
                data.setdefault("period_tables", []).append(table)
            table.update(source="manual", periods=_normalize_periods(semester.periods))
            data["display_period_table_id"] = table["id"]
            target.data = data
        _touch(target)
    elif target.id != document.id:
        data = deepcopy(target.data)
        data["sources"] = [*data.get("sources", []), *document.data.get("sources", [])]
        target.data = data
        # Retain old tables internally; projection binds schedules to the current clocks.
        known = {item["id"] for item in data.get("period_tables", [])}
        data["period_tables"].extend(item for item in document.data.get("period_tables", []) if item["id"] not in known)
        target.data = deepcopy(data)
        for course in db.scalars(select(TimetableCourse).where(TimetableCourse.user_id == user.id, TimetableCourse.document_id == document.id)).all():
            course.document_id = target.id
        _touch(target)
        db.flush()
        db.delete(document)
    if original_thread_id is not None:
        thread = owned(db, ScheduleThread, user.id, original_thread_id)
        thread.semester_id = semester.id
        thread.version += 1
        thread.updated_at = _now()
    db.flush()
    sync_period_axis(db, user, target)
    sync_document(db, user, target)
    return target


def _apply_semester(db, user, document, raw):
    if not isinstance(raw, dict):
        return document
    data = deepcopy(document.data)
    data["semester"] = raw
    document.data = data
    if raw.get("id") is not None:
        semester_id = _uuid(raw["id"])
        if semester_id is None:
            return document
        try:
            with db.begin_nested():
                owned(db, Semester, user.id, semester_id)
                return associate_document(db, user, document, semester_id)
        except HTTPException:
            return document
    if document.semester_id is not None or not all(raw.get(key) for key in ("name", "start_date", "end_date")):
        return document
    from timetable_actions import SemesterCreate, create_semester_record
    try:
        start = date.fromisoformat(raw["start_date"])
        first = raw.get("first_week_monday") or (start - timedelta(days=start.weekday())).isoformat()
        body = SemesterCreate(name=raw["name"], start_date=start, end_date=raw["end_date"],
                              first_week_monday=first, timezone=raw.get("timezone"))
        # Validation happens before insertion; invalid/incomplete source stays in JSONB.
        semester = Semester(user_id=user.id, **{**body.model_dump(), "timezone": body.timezone or user.timezone})
        from timetable_actions import validate_semester
        validate_semester(semester)
    except (ValueError, TypeError, HTTPException):
        return document
    semester = create_semester_record(db, user, body)
    return associate_document(db, user, document, semester.id)


def apply_understanding(db, user, document, understanding, run_id=None):
    if document.user_id != user.id:
        raise HTTPException(404, "未找到该课表。")
    raw = understanding if isinstance(understanding, dict) else {}
    if raw.get("intent") == "read":
        return {"saved": False, "document_id": str(document.id), "semester_id": str(document.semester_id) if document.semester_id else None,
                "created_courses": [], "updated_courses": [], "unapplied_changes": raw.get("unapplied_changes", []),
                "period_table_changes": [], "diagnostics": [], "structured": document_snapshot(db, document)}
    sources = document.data.get("sources", [])
    if run_id and any(item.get("run_id") == str(run_id) for item in sources if isinstance(item, dict)):
        return {"saved": True, "document_id": str(document.id), "semester_id": str(document.semester_id) if document.semester_id else None,
                "created_courses": [], "updated_courses": [], "unapplied_changes": [], "period_table_changes": [], "diagnostics": [], "structured": document_snapshot(db, document)}
    created, updated, touched, diagnostics = [], [], set(), []
    period_changes = []
    retained_changes = set()
    unapplied = deepcopy(raw.get("unapplied_changes", [])) if isinstance(raw.get("unapplied_changes"), list) else []
    expected = raw.get("document_version", raw.get("expected_document_version"))
    expected_document_id = raw.get("expected_document_id")
    if ((expected is not None and expected != document.version)
            or (expected_document_id is not None and str(expected_document_id) != str(document.id))):
        unapplied.append({"reason": "课表已被其他操作更新，识别结果已保留但未覆盖新版本。", "understanding": deepcopy(raw)})
    else:
        document = _apply_semester(db, user, document, raw.get("semester"))
        requested_semester = raw.get("semester", {})
        if isinstance(requested_semester, dict) and requested_semester.get("id") is not None and str(document.semester_id) != str(requested_semester["id"]):
            unapplied.append({"reason": "未找到可关联的学期，原课表和关联请求已保留。", "semester": requested_semester})
        sync_period_axis(db, user, document)
        before_tables = deepcopy(document.data.get("period_tables", []))
        aliases = _merge_period_tables(document, raw.get("period_tables", []))
        sync_period_axis(db, user, document, previous_tables=before_tables)
        rows = db.scalars(select(TimetableCourse).where(TimetableCourse.user_id == user.id, TimetableCourse.document_id == document.id,
                                                        TimetableCourse.is_deleted.is_(False))).all()
        by_id = {str(item.id): item for item in rows}
        base_versions = {str(item.id): item.version for item in rows}
        period_changes, period_updated, period_diagnostics = _apply_period_table_changes(
            db, user, document, raw.get("period_table_changes", []), aliases, unapplied)
        updated.extend(period_updated)
        diagnostics.extend(period_diagnostics)
        if raw.get("intent") != "read":
            for item in raw.get("courses", []) if isinstance(raw.get("courses"), list) else []:
                if not isinstance(item, dict):
                    continue
                incoming_schedules = item.get("schedules", []) if isinstance(item.get("schedules"), list) else []
                known_schedule = any(isinstance(schedule, dict) and any(schedule.get(field) is not None for field in SCHEDULE_FIELDS)
                                     for schedule in incoming_schedules)
                if _text(item.get("name"), 100) is None and not known_schedule and item.get("id") is None:
                    continue
                course = by_id.get(str(item.get("id")))
                if item.get("id") is not None and course is None:
                    unapplied.append({"reason": "未找到属于当前课表的课程。", "change": item})
                    continue
                if course is None:
                    candidates = [row for row in rows if row.data.get("name") == _text(item.get("name"), 100) and row.data.get("name") is not None]
                    course = candidates[0] if len(candidates) == 1 else None
                if course is not None and item.get("version") is not None and item["version"] != base_versions.get(str(course.id), course.version):
                    unapplied.append({"reason": "课程版本已更新。", "change": item})
                    continue
                new_course = course is None
                try:
                    with db.begin_nested():
                        if new_course:
                            course = TimetableCourse(user_id=user.id, document_id=document.id,
                                                     data={"name": None, "schedules": [], "exceptions": []}, version=1)
                            db.add(course)
                        before = deepcopy(course.data)
                        changed = _merge_course(course, item, aliases, document.data.get("period_tables", []),
                                                document.data.get("display_period_table_id"))
                        if not new_course:
                            _guard_metadata_change(db, user, course, before, course.data)
                        if new_course:
                            course.version = 1
                        db.flush()
                        course_diagnostics = sync_document(db, user, document, course_ids=[course.id])
                    diagnostics.extend(course_diagnostics)
                    if new_course:
                        rows.append(course)
                        by_id[str(course.id)] = course
                        created.append(str(course.id))
                    elif changed:
                        updated.append(str(course.id))
                    touched.add(str(course.id))
                except HTTPException as error:
                    unapplied.append({"reason": error.detail, "change": item})
            for change in raw.get("changes", []) if isinstance(raw.get("changes"), list) else []:
                if not isinstance(change, dict):
                    continue
                course = by_id.get(str(change.get("course_id")))
                if course is None or (change.get("version") is not None and base_versions.get(str(course.id), course.version) != change["version"]):
                    unapplied.append({"reason": "课程目标不唯一、不可访问或版本已更新。", "change": change})
                    continue
                try:
                    with db.begin_nested():
                        if change.get("action") == "delete":
                            course.is_deleted = True
                        else:
                            data = _patch_data(course, _remap_period_ids(change, aliases), document.data.get("period_tables", []),
                                               document.data.get("display_period_table_id"))
                            _guard_metadata_change(db, user, course, course.data, data)
                            course.data = data
                        _course_touch(course)
                        db.flush()
                        course_diagnostics = sync_document(db, user, document, course_ids=[course.id])
                    diagnostics.extend(course_diagnostics)
                    updated.append(str(course.id))
                    retained_changes.add(str(course.id))
                    if change.get("action") == "delete":
                        by_id.pop(str(course.id), None)
                except (HTTPException, TypeError, ValueError) as error:
                    unapplied.append({"reason": getattr(error, "detail", "修改内容无法应用。"), "change": change})
            if raw.get("mode") == "replace":
                if unapplied:
                    unapplied.append({"reason": "部分课程或修改目标无法应用，本次整体替换未执行；已识别的新信息已合并保存。", "mode": "replace"})
                else:
                    retained = touched | set(created) | retained_changes | {str(item.get("id")) for item in raw.get("courses", []) if isinstance(item, dict)}
                    removed = [course for course in rows if str(course.id) not in retained and not course.is_deleted]
                    try:
                        with db.begin_nested():
                            for course in removed:
                                course.is_deleted = True
                                _course_touch(course)
                            db.flush()
                            replacement_diagnostics = sync_document(db, user, document, course_ids=[course.id for course in removed])
                        diagnostics.extend(replacement_diagnostics)
                        updated.extend(str(course.id) for course in removed)
                    except HTTPException as error:
                        unapplied.append({"reason": error.detail, "mode": "replace"})
            # Period-based courses always follow the current semester clocks,
            # including old known times and clears from earlier editor versions.
            if raw.get("period_tables"):
                for course in rows:
                    if course.is_deleted:
                        continue
                    data = deepcopy(course.data)
                    data["schedules"] = [_effective_schedule(schedule, document.data.get("period_tables", []),
                                                             document.data.get("display_period_table_id"))
                                         for schedule in data.get("schedules", [])]
                    if data != course.data:
                        try:
                            with db.begin_nested():
                                _guard_metadata_change(db, user, course, course.data, data)
                                course.data = data
                                _course_touch(course)
                                db.flush()
                                course_diagnostics = sync_document(db, user, document, course_ids=[course.id])
                            diagnostics.extend(course_diagnostics)
                            updated.append(str(course.id))
                        except HTTPException as error:
                            unapplied.append({"reason": error.detail, "course_id": str(course.id)})
    data = deepcopy(document.data)
    data.setdefault("sources", []).append({"run_id": str(run_id) if run_id else None, "recorded_at": _now().isoformat(),
                                            "understanding": deepcopy(raw), "unapplied_changes": deepcopy(unapplied)})
    document.data = data
    _touch(document)
    db.flush()
    return {"saved": True, "document_id": str(document.id), "semester_id": str(document.semester_id) if document.semester_id else None,
            "created_courses": list(dict.fromkeys(created)), "updated_courses": list(dict.fromkeys(updated)),
            "unapplied_changes": unapplied, "period_table_changes": period_changes,
            "diagnostics": diagnostics, "structured": document_snapshot(db, document)}


def _effective_schedule(schedule, tables, default_table_id=None):
    """Periods own the full clock interval; direct-clock legacy schedules survive."""
    result = deepcopy(schedule)
    if not schedule.get("period_numbers"):
        return result
    table = _current_period_table({"period_tables": tables, "display_period_table_id": default_table_id})
    result["period_table_id"] = table["id"] if table is not None else None
    result["start_time"] = result["end_time"] = None
    cleared = [field for field in result.get("_cleared_fields", []) if field not in ("start_time", "end_time")]
    if cleared:
        result["_cleared_fields"] = cleared
    else:
        result.pop("_cleared_fields", None)
    if table is not None:
        periods = {item["number"]: item for item in _normalize_periods(table.get("periods", []))}
        numbers = _numbers(schedule.get("period_numbers"), 30) or []
        selected = [periods.get(number) for number in numbers]
        if selected and all(selected):
            start, end = selected[0]["start_time"], selected[-1]["end_time"]
            if start and end and start < end:
                result.update(start_time=start, end_time=end)
    return result


def sync_document(db, user, document, *, now=None, course_ids=None):
    """Project all dates; only an unfinished timer protects a live occurrence.

    Build and guard the complete projection before writing any lesson/template.
    Finished learning records are independent snapshots maintained by lifecycle.
    """
    if document.semester_id is None:
        query = select(TimetableCourse).where(TimetableCourse.user_id == user.id,
            TimetableCourse.document_id == document.id, TimetableCourse.is_deleted.is_(True))
        if course_ids is not None:
            query = query.where(TimetableCourse.id.in_(course_ids))
        for course in db.scalars(query).all():
            db.delete(course)
        db.flush()
        return []
    from timetable_actions import interval, new_lesson, assert_no_overlap
    from timetable_lifecycle import assert_not_timing, remove_lessons
    now = now or _now()
    semester = owned(db, Semester, user.id, document.semester_id)
    query = select(TimetableCourse).where(TimetableCourse.user_id == user.id, TimetableCourse.document_id == document.id)
    if course_ids is not None:
        query = query.where(TimetableCourse.id.in_(course_ids))
    courses = db.scalars(query).all()
    templates = db.scalars(select(CourseTemplate).where(
        CourseTemplate.user_id == user.id, CourseTemplate.semester_id == semester.id,
        CourseTemplate.source_course_id.in_([item.id for item in courses]),
    )).all() if courses else []
    by_source = {(str(item.source_course_id), str(item.source_schedule_id)): item for item in templates}
    existing = {item.id: db.scalars(select(Lesson).where(Lesson.user_id == user.id, Lesson.course_id == item.id)).all()
                for item in templates}
    diagnostics, specs, canonical_data = [], [], {}
    transfer_ids, active = set(), set()
    for course in courses:
        if course.is_deleted:
            continue
        canonical = deepcopy(course.data)
        canonical["schedules"] = [_effective_schedule(schedule, document.data.get("period_tables", []),
                                                        document.data.get("display_period_table_id"))
                                   for schedule in canonical.get("schedules", [])]
        if canonical != course.data:
            _guard_metadata_change(db, user, course, course.data, canonical)
        canonical_data[course.id] = canonical
        for schedule in canonical.get("schedules", []):
            key = (str(course.id), schedule["id"])
            ready = bool(canonical.get("name") and schedule.get("weekday") and schedule.get("weeks")
                         and schedule.get("start_time") and schedule.get("end_time")
                         and schedule["start_time"] < schedule["end_time"])
            if not ready:
                diagnostics.append({"course_id": str(course.id), "schedule_id": schedule["id"],
                                    "type": "incomplete", "message": "已保存，信息补全后进入周课表。"})
                continue
            generated = {}
            for week in schedule["weeks"]:
                day = semester.first_week_monday + timedelta(days=(week - 1) * 7 + schedule["weekday"] - 1)
                if not semester.start_date <= day <= semester.end_date:
                    continue
                try:
                    generated[week] = interval(day, schedule["start_time"], schedule["end_time"], semester.timezone)
                except HTTPException as error:
                    diagnostics.append({"course_id": str(course.id), "schedule_id": schedule["id"], "week": week, "message": error.detail})
            template = by_source.get(key)
            lessons = list(existing.get(template.id, [])) if template is not None else []
            transfers = []
            if template is None and schedule.get("_split_from"):
                previous = by_source.get((str(course.id), schedule["_split_from"]))
                if previous is not None:
                    transfers = [item for item in existing.get(previous.id, []) if item.week_number in schedule["weeks"]]
                    lessons.extend(transfers)
                    transfer_ids.update(item.id for item in transfers)
            active.add(key)
            specs.append({"course": course, "canonical": canonical, "schedule": schedule, "template": template,
                          "generated": generated, "lessons": {item.week_number: item for item in lessons},
                          "transfers": transfers, "updates": []})
    deletions, modifications = {}, {}
    for spec in specs:
        course, schedule, generated = spec["course"], spec["schedule"], spec["generated"]
        exceptions = {item["original_week"]: item for item in spec["canonical"].get("exceptions", [])
                      if item.get("schedule_id") == schedule["id"]}
        lessons = spec["lessons"]
        weeks = set(generated) | set(lessons)
        for week in sorted(weeks):
            lesson = lessons.get(week)
            if lesson is not None and lesson.id in transfer_ids and lesson not in spec["transfers"]:
                continue
            exception = exceptions.get(week)
            if exception is not None and exception.get("is_deleted") is True:
                if lesson is not None:
                    deletions[lesson.id] = lesson
                continue
            if week not in generated and exception is None:
                if lesson is not None:
                    deletions[lesson.id] = lesson
                continue
            if lesson is None and week not in generated:
                continue
            starts_at, ends_at = generated.get(week, (lesson.starts_at, lesson.ends_at) if lesson is not None else (None, None))
            values = {"starts_at": starts_at, "ends_at": ends_at, "name": spec["canonical"]["name"],
                      "location": schedule.get("location") or "", "timezone": semester.timezone,
                      "status": "scheduled", "is_deleted": False}
            if lesson is not None and lesson.is_override and exception is None:
                # Retain an old manual occurrence even if it predates JSONB capture.
                local_start = lesson.starts_at.astimezone(ZoneInfo(lesson.timezone))
                local_end = lesson.ends_at.astimezone(ZoneInfo(lesson.timezone))
                exception = {"schedule_id": schedule["id"], "original_week": week,
                             "date": local_start.date().isoformat(), "start_time": local_start.strftime("%H:%M"),
                             "end_time": local_end.strftime("%H:%M"), "status": lesson.status,
                             "note": lesson.note, "is_deleted": lesson.is_deleted}
                spec["canonical"].setdefault("exceptions", []).append(exception)
                exceptions[week] = exception
                if lesson.is_deleted:
                    deletions[lesson.id] = lesson
                    continue
            if exception:
                tz = ZoneInfo(semester.timezone)
                generated_start, generated_end = starts_at.astimezone(tz), ends_at.astimezone(tz)
                previous_start = (lesson.starts_at if lesson is not None and lesson.is_override else starts_at).astimezone(tz)
                previous_end = (lesson.ends_at if lesson is not None and lesson.is_override else ends_at).astimezone(tz)
                try:
                    actual_day = (date.fromisoformat(exception["date"]) if exception.get("date") else
                                  generated_start.date() if "date" in exception else previous_start.date())
                    actual_start = exception.get("start_time") or (generated_start if "start_time" in exception else previous_start).strftime("%H:%M")
                    actual_end = exception.get("end_time") or (generated_end if "end_time" in exception else previous_end).strftime("%H:%M")
                    if not semester.start_date <= actual_day <= semester.end_date:
                        raise ValueError
                    values["starts_at"], values["ends_at"] = interval(actual_day, actual_start, actual_end, semester.timezone)
                except (ValueError, TypeError, HTTPException):
                    diagnostics.append({"course_id": str(course.id), "schedule_id": schedule["id"], "week": week,
                                        "message": "单次调课日期或时间无效，原安排已保留。"})
                    continue
                exception.setdefault("date", actual_day.isoformat())
                exception.setdefault("start_time", actual_start)
                exception.setdefault("end_time", actual_end)
                if lesson is not None and lesson.is_override:
                    values.update(status=lesson.status, note=lesson.note)
                if exception.get("status") in ("scheduled", "leave", "cancelled"):
                    values["status"] = exception["status"]
                if "note" in exception:
                    values["note"] = _text(exception["note"], 500) or ""
                values["is_override"] = True
            if lesson is not None and (lesson in spec["transfers"] or any(getattr(lesson, key) != value for key, value in values.items())):
                modifications[lesson.id] = lesson
            spec["updates"].append((week, lesson, starts_at, ends_at, values))
    inactive = [item for item in templates if (str(item.source_course_id), str(item.source_schedule_id)) not in active]
    for template in inactive:
        for lesson in existing[template.id]:
            if lesson.id not in transfer_ids:
                deletions[lesson.id] = lesson
    assert_not_timing(db, user.id, list(deletions.values()), operation="delete", now=now)
    assert_not_timing(db, user.id, [item for key, item in modifications.items() if key not in deletions], operation="modify", now=now)
    for spec in specs:
        course, schedule = spec["course"], spec["schedule"]
        template = spec["template"]
        values = {"name": spec["canonical"]["name"], "weekday": schedule["weekday"], "weeks": schedule["weeks"],
                  "start_week": min(schedule["weeks"]), "end_week": max(schedule["weeks"]), "week_pattern": "all",
                  "start_time": schedule["start_time"], "end_time": schedule["end_time"],
                  "location": schedule.get("location") or "", "is_deleted": False}
        if template is None:
            template = CourseTemplate(user_id=user.id, semester_id=semester.id, source_course_id=course.id,
                                      source_schedule_id=uuid.UUID(schedule["id"]), version=1, **values)
            db.add(template)
            spec["template"] = template
        else:
            if any(getattr(template, key) != value for key, value in values.items()):
                template.version += 1
            for key, value in values.items():
                setattr(template, key, value)
        db.flush()
        for lesson in spec["transfers"]:
            lesson.course_id = template.id
    db.flush()
    remove_lessons(db, user.id, list(deletions.values()), now=now)
    for template in inactive:
        db.delete(template)
    for spec in specs:
        template = spec["template"]
        for week, lesson, starts_at, ends_at, values in spec["updates"]:
            if lesson is not None and lesson.id in deletions:
                continue
            if lesson is None:
                lesson = new_lesson(template, semester, week, starts_at, ends_at)
                db.add(lesson)
            elif lesson.id in modifications:
                lesson.version += 1
            for key, value in values.items():
                setattr(lesson, key, value)
    for course in courses:
        if course.is_deleted:
            db.delete(course)
        elif canonical_data.get(course.id) != course.data:
            course.data = canonical_data[course.id]
            _course_touch(course)
    db.flush()
    for conflict in assert_no_overlap(db, user.id):
        left, right = conflict["left"], conflict["right"]
        if str(semester.id) not in {left["semester_id"], right["semester_id"]}:
            continue
        diagnostics.append({"type": "overlap", "message":
            f'「{left["name"]}」与「{right["name"]}」存在时间重叠，共 {conflict["occurrence_count"]} 次，已全部保留。',
            "conflict": conflict})
    return diagnostics


def _legacy_weeks(course):
    if course.weeks is not None:
        return deepcopy(course.weeks)
    return [week for week in range(course.start_week, course.end_week + 1)
            if course.week_pattern == "all" or (week % 2 == 1 if course.week_pattern == "odd" else week % 2 == 0)]


def capture_legacy_course(db, user, course):
    """Capture an already-applied manual edit; never reproject here."""
    if course.user_id != user.id:
        raise HTTPException(404, "未找到该课程。")
    document = get_document(db, user.id, semester_id=course.semester_id, create=True)
    structured = db.scalar(select(TimetableCourse).where(TimetableCourse.user_id == user.id, TimetableCourse.id == course.source_course_id)) if course.source_course_id else None
    if structured is None:
        structured = TimetableCourse(user_id=user.id, document_id=document.id, data={"name": course.name, "schedules": [], "exceptions": []}, version=1)
        db.add(structured)
        db.flush()
        course.source_course_id = structured.id
    schedule_id = str(course.source_schedule_id or uuid.uuid4())
    course.source_schedule_id = uuid.UUID(schedule_id)
    data = deepcopy(structured.data)
    data["name"] = course.name
    existing = next((item for item in data["schedules"] if item["id"] == schedule_id), None)
    raw = {"weekday": course.weekday, "weeks": _legacy_weeks(course), "start_time": course.start_time,
           "end_time": course.end_time, "location": course.location or None}
    course.weeks = raw["weeks"]
    if course.is_deleted:
        data["schedules"] = [item for item in data["schedules"] if item["id"] != schedule_id]
        if not data["schedules"]:
            structured.is_deleted = True
    elif existing is None:
        data["schedules"].append(normalize_schedule(raw, schedule_id=schedule_id))
    else:
        existing.update(raw)
    structured.data = data
    _course_touch(structured)
    _touch(document)
    db.flush()
    return structured


def capture_legacy_lesson(db, user, lesson):
    """Persist the original-week identity of a manual single-occurrence edit."""
    template = db.scalar(select(CourseTemplate).where(CourseTemplate.user_id == user.id, CourseTemplate.id == lesson.course_id))
    if template is None or lesson.user_id != user.id:
        raise HTTPException(404, "未找到该课次。")
    structured = db.scalar(select(TimetableCourse).where(TimetableCourse.user_id == user.id, TimetableCourse.id == template.source_course_id)) if template.source_course_id else None
    if structured is None:
        structured = capture_legacy_course(db, user, template)
    data = deepcopy(structured.data)
    exceptions = data.setdefault("exceptions", [])
    current = next((item for item in exceptions if item.get("schedule_id") == str(template.source_schedule_id) and item.get("original_week") == lesson.week_number), None)
    if current is None:
        current = {"schedule_id": str(template.source_schedule_id), "original_week": lesson.week_number}
        exceptions.append(current)
    local_start = lesson.starts_at.astimezone(ZoneInfo(lesson.timezone))
    local_end = lesson.ends_at.astimezone(ZoneInfo(lesson.timezone))
    current.update(date=local_start.date().isoformat(), start_time=local_start.strftime("%H:%M"), end_time=local_end.strftime("%H:%M"),
                   status=lesson.status, note=lesson.note, is_deleted=lesson.is_deleted)
    structured.data = data
    _course_touch(structured)
    document = owned(db, TimetableDocument, user.id, structured.document_id)
    _touch(document)
    db.flush()
    return structured
