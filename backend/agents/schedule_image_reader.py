"""Read card text once; bind weekdays and periods to independently measured geometry."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
from typing import Annotated, Literal, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from agents.prompts import load_prompt
from agents.model_client import invoke_json
from schedule_model import ModelError
from schedule_plan import SchedulePlan, Operation

IMAGE_READER_VERSION = "schedule-image-v1"
Clock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(Strict):
    number: int = Field(strict=True, ge=1, le=32)
    start_time: Clock
    end_time: Clock


class Card(Strict):
    card_id: str = Field(min_length=1, max_length=64)
    name: str = Field(max_length=100)
    location: str = Field(default="", max_length=200)


class Reading(Strict):
    weekday_headers: list[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(min_length=7, max_length=7)
    displayed_week: int | None = Field(default=None, strict=True, ge=1, le=54)
    term_label: str = Field(default="", max_length=100)
    all_cards_accounted_for: bool = Field(strict=True)
    periods: list[Period] = Field(min_length=1, max_length=32)
    cards: list[Card] = Field(min_length=1, max_length=100)
    uncertainties: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=10)


def image_cache_key(image_id):
    # Upload IDs are immutable and caller checks ownership/thread before lookup.
    from agents.schedule_vision import LAYOUT_VERSION
    value = [IMAGE_READER_VERSION, LAYOUT_VERSION, str(image_id), load_prompt("schedule_image_v1.txt")]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def prepare_image_messages(layout, original=None):
    metadata = layout.metadata()
    system = load_prompt("schedule_image_v1.txt") + json.dumps(Reading.model_json_schema(), ensure_ascii=False)
    content = [{"type": "text", "text": json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))}]
    if original is not None:
        content.append({"type": "text", "text": "原图仅供核对是否遗漏课程卡及表头周次，课程文字按后续放大卡片逐项读取。"})
        content.append({"type": "image_url", "image_url": {"url": f"data:{original['mime_type']};base64," + base64.b64encode(original['data']).decode()}})
    for view in layout.images:
        content.append({"type": "text", "text": view.caption})
        content.append({"type": "image_url", "image_url": {"url": f"data:{view.mime_type};base64," + base64.b64encode(view.data).decode()}})
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def bind_reading(value, layout, image_id):
    """Model transcribes labels, never chooses a course's row or weekday."""
    try:
        reading = Reading.model_validate(value).model_dump(mode="json")
        if reading["weekday_headers"] != list(range(1, 8)) or not reading["all_cards_accounted_for"]:
            raise ValueError("Unsupported column order")
        metadata = layout.metadata() if hasattr(layout, "metadata") else layout
        slots = metadata["cards"]
        by_id = {item["card_id"]: item for item in reading["cards"]}
        if len(by_id) != len(reading["cards"]) or set(by_id) != {item["card_id"] for item in slots}:
            raise ValueError("Missing, duplicate or invented card")
        periods = {item["number"]: item for item in reading["periods"]}
        if (len(periods) != len(reading["periods"]) or set(periods) != set(range(1, len(periods) + 1))
                or len(periods) < max(item["period_end"] for item in slots)
                or isinstance(metadata.get("periods"), list) and len(periods) > len(metadata["periods"])):
            raise ValueError("Incomplete period axis")
        previous_end = "00:00"
        for number in sorted(periods):
            row = periods[number]
            if not previous_end <= row["start_time"] < row["end_time"]:
                raise ValueError("Invalid or overlapping periods")
            previous_end = row["end_time"]
        if reading["uncertainties"] or any(not item["name"].strip() for item in by_id.values()):
            raise ValueError("Uncertain image labels")
        courses = [{"source_card_id": slot["card_id"], "weekday": slot["weekday"],
                    "period_start": slot["period_start"], "period_end": slot["period_end"],
                    "name": by_id[slot["card_id"]]["name"].strip(),
                    "location": by_id[slot["card_id"]]["location"],
                    "start_time": periods[slot["period_start"]]["start_time"],
                    "end_time": periods[slot["period_end"]]["end_time"]} for slot in slots]
        return {"version": IMAGE_READER_VERSION, "image_id": str(image_id),
                "layout_version": metadata["version"], "displayed_week": reading["displayed_week"],
                "term_label": reading["term_label"], "periods": reading["periods"], "courses": courses}
    except (ValidationError, ValueError, KeyError, TypeError):
        raise ModelError("课表图片的课程卡或节次尚未完整核对，已保留原图和要求；请重试识图或补充更清晰的课程区域。", status_code=422) from None


def invoke_image_reader(messages, *, api_key):
    result = asyncio.run(invoke_json(messages, api_key=api_key, max_tokens=8000))
    return {"reading": result["value"], "usage": result["usage"]}


class ImportImageCourses(Strict):
    kind: Literal["import_image_courses"]
    image_id: uuid.UUID
    start_week: int = Field(strict=True, ge=1, le=54)
    end_week: int = Field(strict=True, ge=1, le=54)
    week_pattern: Literal["all", "odd", "even"] = "all"
    card_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=100)


class SetImagePeriods(Strict):
    kind: Literal["set_image_periods"]
    image_id: uuid.UUID


class ImagePlanningPlan(SchedulePlan):
    operations: list[Union[Operation, ImportImageCourses, SetImagePeriods]] = Field(default_factory=list, max_length=100)


def validate_image_planning_plan(value):
    try:
        result = ImagePlanningPlan.model_validate(value).model_dump(mode="json", exclude_unset=True)
        result.setdefault("questions", [])
        result.setdefault("operations", [])
        return result
    except (ValidationError, ValueError, TypeError):
        raise ModelError("课表方案的图片引用或操作格式不完整，已保留任务，请重试。") from None


def compile_image_imports(plan, observations, progress=None, *, persist_periods=False, semester_timezone="Asia/Shanghai"):
    return compile_image_imports_with_progress(plan, observations, progress, persist_periods=persist_periods,
                                              semester_timezone=semester_timezone)[0]


def compile_image_imports_with_progress(plan, observations, progress=None, *, persist_periods=False, semester_timezone="Asia/Shanghai"):
    """Resolve an import directive using saved image facts, without another LLM.

    The planner chooses user-requested weeks and selection, never rewrites card
    weekdays/times. Existing delete/holiday actions remain normal operations.
    """
    from fastapi import HTTPException
    from schedule_model import validate_plan
    from agents.schedule_memory import IMAGE_IMPORT_PROGRESS_VERSION, TaskMemoryError, validate_import_progress
    try:
        previous = validate_import_progress(progress)
    except TaskMemoryError as error:
        raise HTTPException(422, str(error)) from None
    progress_by_image = {item["image_id"]: deepcopy(item) for item in (previous or {}).get("images", [])}
    images = {item["image_id"]: item for item in observations}
    if len(images) != len(observations):
        raise HTTPException(422, "图片识别来源重复，已保留当前导入任务。")
    for image_id, image in images.items():
        available = sorted(item["source_card_id"] for item in image["courses"])
        signature = hashlib.sha256(json.dumps(sorted(image["courses"], key=lambda item: item["source_card_id"]),
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prior = progress_by_image.get(image_id)
        if prior is not None and (prior["reading_sha256"] != signature or sorted(prior["available_card_ids"]) != available):
            raise HTTPException(422, "这张图片的识别资料已发生变化，不能沿用旧导入进度；已导入课程保持不变，请重新核对图片。")
        progress_by_image.setdefault(image_id, {"image_id": image_id, "reading_sha256": signature,
            "available_card_ids": available, "imported_card_ids": [], "remaining_card_ids": available})
    result, imported, selected_periods = [], set(), None
    explicit_axis = any(op["kind"] in {"set_image_periods", "set_semester_periods"} for op in plan["operations"])

    def select_periods(periods):
        nonlocal selected_periods
        periods = sorted(deepcopy(periods), key=lambda item: item["number"])
        if selected_periods is not None and selected_periods != periods:
            raise HTTPException(422, "这几张课表的节次时间不同，请指定本学期使用哪张节次表。")
        selected_periods = periods

    for op in plan["operations"]:
        if persist_periods and op["kind"] == "set_semester_periods":
            if images and not any(sorted(op["periods"], key=lambda item: item["number"]) ==
                                  sorted(image["periods"], key=lambda item: item["number"]) for image in images.values()):
                raise HTTPException(422, "节次时间与图片不一致，请引用图片中的节次表，不要猜测整点时间。")
            select_periods(op["periods"])
            continue
        if op["kind"] == "set_image_periods":
            image = images.get(op["image_id"])
            if not persist_periods or image is None:
                raise HTTPException(422, "节次表引用的图片不可用，请重新读取图片。")
            select_periods(image["periods"])
            continue
        if op["kind"] != "import_image_courses":
            if images and op["kind"] == "create_course":
                raise HTTPException(422, "图片课程必须引用已识别的课程卡导入，不能重新猜测星期或时段。")
            # Older v4 checkpoints retain their frozen Shanghai policy; new
            # tasks explicitly supply the authenticated account's saved zone.
            result.append({**op, "timezone": semester_timezone} if persist_periods and op["kind"] == "create_semester" else op)
            continue
        image = images.get(op["image_id"])
        if image is None:
            raise HTTPException(422, "导入引用的图片不在当前任务的已核对资料中。")
        if persist_periods and not explicit_axis:
            select_periods(image["periods"])
        available = {item["source_card_id"] for item in image["courses"]}
        already = set(progress_by_image[op["image_id"]]["imported_card_ids"])
        selected = op.get("card_ids") or sorted(available - already)
        if len(selected) != len(set(selected)) or not set(selected) <= available:
            raise HTTPException(422, "导入引用了不存在或重复的课程卡。")
        if set(selected) & already:
            raise HTTPException(422, "所选课程卡已经在本任务中导入，请只选择尚未导入的课程卡；card_ids留空会继续剩余课程。")
        for item in image["courses"]:
            if item["source_card_id"] not in selected:
                continue
            key = (op["image_id"], item["source_card_id"])
            if key in imported:
                raise HTTPException(422, "同一张课程卡不能重复导入，请合并导入范围。")
            imported.add(key)
            result.append({"kind": "create_course", **{field: item[field] for field in
                ("name", "location", "weekday", "start_time", "end_time")},
                "start_week": op["start_week"], "end_week": op["end_week"], "week_pattern": op.get("week_pattern", "all")})
    if selected_periods is not None:
        # Append to preserve course/lesson IDs derived from operation indices.
        # New semesters get the same axis directly in their creation preview.
        create = next((op for op in result if op["kind"] == "create_semester"), None)
        if create is not None:
            result = [{**op, "periods": selected_periods} if op is create else op for op in result]
        else:
            result.append({"kind": "set_semester_periods", "periods": selected_periods})
    compiled = validate_plan({**plan, "operations": result})
    for image_id, card_id in imported:
        progress_by_image[image_id]["imported_card_ids"].append(card_id)
    for item in progress_by_image.values():
        item["imported_card_ids"] = sorted(set(item["imported_card_ids"]))
        item["remaining_card_ids"] = sorted(set(item["available_card_ids"]) - set(item["imported_card_ids"]))
    updated = None
    if progress_by_image:
        updated = {"version": IMAGE_IMPORT_PROGRESS_VERSION,
            "images": [progress_by_image[key] for key in sorted(progress_by_image)],
            "complete": all(not item["remaining_card_ids"] for item in progress_by_image.values())}
        try:
            updated = validate_import_progress(updated)
        except TaskMemoryError as error:
            raise HTTPException(422, str(error)) from None
    return compiled, updated
