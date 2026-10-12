"""Validated plans and deterministic, atomic timetable proposal execution."""
import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator
from sqlalchemy import select

from models import CourseTemplate, Lesson, Semester, User
from timetable_actions import (
    create_course_record, create_semester_record, delete_course_record, delete_lesson_record, recorded_lessons,
    update_course_record, update_lesson_record,
)
import timetable_actions as tt


class CreateSemesterOp(tt.SemesterCreate):
    kind: Literal["create_semester"]


class SetSemesterPeriodsOp(tt.Input):
    """Only the bound semester's display axis; baseline_hash guards its version."""
    kind: Literal["set_semester_periods"]
    periods: list[tt.Period] = Field(min_length=1, max_length=30)

    _periods = field_validator("periods")(tt.validate_periods)


class CreateCourseOp(tt.Input):
    kind: Literal["create_course"]
    name: str = Field(min_length=1, max_length=100)
    weekday: int = Field(strict=True, ge=1, le=7)
    start_week: int = Field(strict=True, ge=1, le=54)
    end_week: int = Field(strict=True, ge=1, le=54)
    week_pattern: tt.WeekPattern = "all"
    start_time: tt.ClockTime
    end_time: tt.ClockTime
    location: str = Field(default="", max_length=200)


class UpdateCourseOp(tt.CoursePatch):
    kind: Literal["update_course"]
    course_id: uuid.UUID

    @model_validator(mode="after")
    def actual_change(self):
        if not self.model_fields_set - {"kind", "course_id", "version"}:
            raise ValueError("请提供课程要修改的字段。")
        return self


class DeleteCourseOp(tt.Input):
    kind: Literal["delete_course"]
    course_id: uuid.UUID
    version: tt.Version


class PatchLessonOp(tt.LessonPatch):
    kind: Literal["patch_lesson"]
    lesson_id: uuid.UUID

    @model_validator(mode="after")
    def actual_change(self):
        if not self.model_fields_set - {"kind", "lesson_id", "version"}:
            raise ValueError("请提供课次要修改的字段。")
        return self


class DeleteLessonOp(tt.Input):
    kind: Literal["delete_lesson"]
    lesson_id: uuid.UUID
    version: tt.Version


class CancelRangeOp(tt.Input):
    kind: Literal["cancel_range"]
    start_date: date
    end_date: date
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("停课区间的结束日期不能早于开始日期。")
        return self


class MoveDayOp(tt.Input):
    kind: Literal["move_day"]
    source_date: date
    target_date: date
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def different_dates(self):
        if self.source_date == self.target_date:
            raise ValueError("调休的来源日期和目标日期不能相同。")
        return self


Operation = Annotated[Union[
    CreateSemesterOp, CreateCourseOp, UpdateCourseOp, DeleteCourseOp,
    PatchLessonOp, DeleteLessonOp, CancelRangeOp, MoveDayOp, SetSemesterPeriodsOp,
], Field(discriminator="kind")]


class SchedulePlan(tt.Input):
    reply: str = Field(min_length=1, max_length=8000)
    questions: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=2)
    operations: list[Operation] = Field(default_factory=list, max_length=100)


def _uuid(value):
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _records(db, user_id, semester_id=None, *, all_semesters=False):
    """Stable canonical data including membership, soft deletions and versions."""
    user_id = _uuid(user_id)
    semesters = db.scalars(select(Semester).where(Semester.user_id == user_id).order_by(Semester.id)
                          .execution_options(populate_existing=True)).all()
    courses = db.scalars(select(CourseTemplate).where(CourseTemplate.user_id == user_id,
        True if all_semesters else CourseTemplate.semester_id == semester_id).order_by(CourseTemplate.id)
                        .execution_options(populate_existing=True)).all()
    lessons = db.scalars(select(Lesson).where(Lesson.user_id == user_id,
        True if all_semesters else Lesson.semester_id == semester_id).order_by(Lesson.id)
                        .execution_options(populate_existing=True)).all()
    used = set(db.scalars(select(CourseTemplate.semester_id).where(
        CourseTemplate.user_id == user_id).distinct()))
    semester_values = {}
    for row in semesters:
        values = tt.semester_result(row, row.id in used)
        # A midnight boundary alone is not a concurrent timetable edit.
        values.pop("today")
        semester_values[str(row.id)] = values
    return {
        "semesters": semester_values,
        "courses": {str(row.id): tt.course_result(row) for row in courses},
        "lessons": {str(row.id): tt.lesson_result(row) for row in lessons},
    }


def context_snapshot(db, user_id, semester_id=None):
    user_id = _uuid(user_id)
    user = db.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(404, "未找到该账户。")
    semester = tt.owned(db, Semester, user_id, _uuid(semester_id)) if semester_id is not None else None
    records = _records(db, user_id, semester_id)
    semesters = list(records["semesters"].values())
    for item in semesters:
        item["today"] = tt.utcnow().astimezone(ZoneInfo(item["timezone"])).date().isoformat()
    target = str(semester.id) if semester is not None else None
    recorded = {str(item) for item in recorded_lessons(db, user_id)}
    weeks = []
    if semester:
        for number in range(1, tt.total_weeks(semester) + 1):
            start = semester.first_week_monday + timedelta(weeks=number - 1)
            weeks.append({"number": number, "start_date": max(start, semester.start_date).isoformat(),
                          "end_date": min(start + timedelta(days=6), semester.end_date).isoformat()})
    return {
        "today": tt.utcnow().astimezone(ZoneInfo(user.timezone)).date().isoformat(),
        "timezone": user.timezone,
        "semesters": semesters,
        "semester": next((item for item in semesters if item["id"] == target), None),
        "week_dates": weeks,
        "courses": [item for item in records["courses"].values()
                    if item["semester_id"] == target and not item["is_deleted"]],
        "lessons": [{**item, "has_learning_record": item["id"] in recorded}
                    for item in records["lessons"].values() if item["semester_id"] == target and not item["is_deleted"]],
    }


def snapshot_hash(db, user_id, semester_id=None, *, records=None, legacy=False):
    user_id = _uuid(user_id)
    if semester_id is not None:
        tt.owned(db, Semester, user_id, _uuid(semester_id))
    user = db.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(404, "未找到该账户。")
    payload = {"timezone": user.timezone, **(records if records is not None else
        _records(db, user_id, semester_id, all_semesters=legacy))}
    if semester_id is not None and not legacy:
        key = str(semester_id)
        payload["semesters"] = {key: payload["semesters"][key]}
    return ("" if legacy else "semester-v1:") + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _body(op, cls, *exclude):
    return cls.model_validate(op.model_dump(exclude_unset=True, exclude={"kind", *exclude}))


def _require_semester_record(db, user_id, model, item_id, semester_id):
    item = tt.owned(db, model, user_id, item_id)
    if item.semester_id != semester_id:
        raise HTTPException(404, "该记录不属于当前对话的学期。")
    return item


def _date_in_semester(day, semester):
    if not semester.start_date <= day <= semester.end_date:
        raise HTTPException(422, "放假或调课日期必须在当前学期内。")


def _summarize(before, after, semester_id, baseline_hash):
    changes = []
    counts = dict(created_semesters=0, created_courses=0, updated_courses=0,
                  deleted_courses=0, created_lessons=0, updated_lessons=0, deleted_lessons=0)
    for category in ("semesters", "courses", "lessons"):
        for record_id, value in after[category].items():
            old = before[category].get(record_id)
            if value == old:
                continue
            if category == "semesters":
                if old is not None:
                    # has_courses is derived; only actual axis edits count.
                    if old["periods"] == value["periods"]:
                        continue
                    kind, count = "set_semester_periods", "updated_semesters"
                    counts.setdefault(count, 0)
                else:
                    kind, count = "create_semester", "created_semesters"
            elif category == "courses":
                if old is None:
                    kind, count = "create_course", "created_courses"
                elif not old["is_deleted"] and value["is_deleted"]:
                    kind, count = "delete_course", "deleted_courses"
                else:
                    kind, count = "update_course", "updated_courses"
            else:
                if old is not None and not old["is_deleted"] and value["is_deleted"]:
                    kind, count = "delete_lesson", "deleted_lessons"
                else:
                    kind, count = ("create_lesson", "created_lessons") if old is None else ("update_lesson", "updated_lessons")
            counts[count] += 1
            title = value.get("name", "课表变更")
            if category == "lessons":
                title = f'{value["local_date"]} {value["start_time"]} {title}'
            changes.append({"kind": kind, "title": title, "before": old, "after": value})
        if category in {"courses", "lessons"}:
            for record_id in before[category].keys() - after[category].keys():
                old = before[category][record_id]
                if old.get("semester_id", str(semester_id)) != str(semester_id):
                    # Frozen pre-v5 snapshots may contain other semesters.
                    continue
                kind, count = ("delete_course", "deleted_courses") if category == "courses" else ("delete_lesson", "deleted_lessons")
                counts[count] += 1
                title = old.get("name", "课表变更")
                if category == "lessons":
                    title = f'{old["local_date"]} {old["start_time"]} {title}'
                changes.append({"kind": kind, "title": title, "before": old, "after": None})
    return {"semester_id": str(semester_id), "baseline_hash": baseline_hash,
            "changes": changes, "counts": counts}


def _preflight_timing(db, user, semester_id, plan, now):
    """Check a batch before its first business write, under the timer-start lock."""
    from timetable_lifecycle import assert_not_timing
    tt.lock_reader(db, user.id)
    if semester_id is None:
        return
    rows = db.scalars(select(Lesson).where(Lesson.user_id == user.id, Lesson.semester_id == semester_id,
                                          Lesson.is_deleted.is_(False))).all()
    deleting, modifying, lesson_versions = [], [], []
    for op in plan.operations:
        if op.kind == "delete_course":
            course = _require_semester_record(db, user.id, CourseTemplate, op.course_id, semester_id)
            tt.check_version(course, op.version)
            deleting.extend(db.scalars(select(Lesson).where(Lesson.user_id == user.id, Lesson.course_id == course.id)).all())
        elif op.kind == "update_course":
            _require_semester_record(db, user.id, CourseTemplate, op.course_id, semester_id)
            *_, affected = tt._prepare_course_update(db, user, op.course_id, _body(op, tt.CoursePatch, "course_id"))
            modifying.extend(affected)
        elif op.kind in {"patch_lesson", "delete_lesson"}:
            lesson = _require_semester_record(db, user.id, Lesson, op.lesson_id, semester_id)
            lesson_versions.append((lesson, op.version))
            (deleting if op.kind == "delete_lesson" else modifying).append(lesson)
        elif op.kind == "cancel_range":
            modifying.extend(row for row in rows
                if op.start_date.isoformat() <= tt.lesson_result(row)["local_date"] <= op.end_date.isoformat()
                and not (row.status == "cancelled" and row.note == op.reason and row.is_override))
        elif op.kind == "move_day":
            modifying.extend(row for row in rows if tt.lesson_result(row)["local_date"] == op.source_date.isoformat())
    assert_not_timing(db, user.id, deleting, operation="delete", now=now)
    assert_not_timing(db, user.id, modifying, operation="modify", now=now)
    for lesson, version in lesson_versions:
        tt.check_version(lesson, version)


def _execute(db, user, semester_id, plan, proposal_id, baseline_hash, before):
    if plan.questions:
        raise HTTPException(422, "请先回答课表助理的澄清问题，再确认完整方案。")
    if not plan.operations:
        raise HTTPException(422, "当前没有可以应用的课表变更。")
    proposal_id = _uuid(proposal_id)
    semester_id = _uuid(semester_id) if semester_id is not None else None
    initial_creates = [op for op in plan.operations if op.kind == "create_semester"]
    if (semester_id is None and len(initial_creates) != 1) or (semester_id is not None and initial_creates):
        raise HTTPException(422, "新对话需创建一个学期；已绑定学期的对话不能新建其他学期。")
    now = tt.utcnow()
    _preflight_timing(db, user, semester_id, plan, now)
    if semester_id is not None:
        semester = tt.owned(db, Semester, user.id, semester_id)
    else:
        op = initial_creates[0]
        index = plan.operations.index(op)
        semester = create_semester_record(db, user, _body(op, tt.SemesterCreate),
                                          record_id=uuid.uuid5(proposal_id, f"semester:{index}"))
        semester_id = semester.id

    # Build the weekly timetable first. Date exceptions then operate on a frozen
    # source-day snapshot, so holiday declarations and day swaps cannot eat their
    # own source lessons or duplicate classes already moved by another operation.
    for index, op in enumerate(plan.operations):
        lesson_factory = lambda week, index=index: uuid.uuid5(proposal_id, f"lesson:{index}:{week}")
        if op.kind == "set_semester_periods":
            periods = [period.model_dump() for period in op.periods]
            if semester.periods != periods:
                semester.periods = periods
                semester.version += 1
        elif op.kind == "create_course":
            body = tt.CourseCreate(semester_id=semester_id, **op.model_dump(exclude={"kind"}))
            create_course_record(db, user, body, record_id=uuid.uuid5(proposal_id, f"course:{index}"),
                                 lesson_id_factory=lesson_factory)
        elif op.kind == "update_course":
            _require_semester_record(db, user.id, CourseTemplate, op.course_id, semester_id)
            update_course_record(db, user, op.course_id, _body(op, tt.CoursePatch, "course_id"),
                                 now=now, lesson_id_factory=lesson_factory)
        elif op.kind == "delete_course":
            _require_semester_record(db, user.id, CourseTemplate, op.course_id, semester_id)
            delete_course_record(db, user, op.course_id, op.version, now=now)
        elif op.kind == "patch_lesson":
            _require_semester_record(db, user.id, Lesson, op.lesson_id, semester_id)
            update_lesson_record(db, user, op.lesson_id, _body(op, tt.LessonPatch, "lesson_id"), now=now)
        elif op.kind == "delete_lesson":
            _require_semester_record(db, user.id, Lesson, op.lesson_id, semester_id)
            delete_lesson_record(db, user, op.lesson_id, op.version, now=now)
    db.flush()
    new_lesson_ids = {row.id for row in db.scalars(select(Lesson).where(
        Lesson.user_id == user.id, Lesson.semester_id == semester_id,
    )).all() if str(row.id) not in before["lessons"]}
    source_rows = db.scalars(select(Lesson).where(
        Lesson.user_id == user.id, Lesson.semester_id == semester_id, Lesson.is_deleted.is_(False),
    ).order_by(Lesson.starts_at, Lesson.id)).all()
    snapshots = {row.id: tt.lesson_result(row) for row in source_rows}
    move_sources = {}
    moved_ids = set()
    for index, op in enumerate(plan.operations):
        if op.kind == "cancel_range":
            _date_in_semester(op.start_date, semester)
            _date_in_semester(op.end_date, semester)
        if op.kind != "move_day":
            continue
        _date_in_semester(op.source_date, semester)
        _date_in_semester(op.target_date, semester)
        selected = [row for row in source_rows if snapshots[row.id]["local_date"] == op.source_date.isoformat()]
        if not selected:
            raise HTTPException(422, f"{op.source_date.isoformat()} 没有可调动的课程，请核对来源日期和周次。")
        if any(row.id in moved_ids for row in selected):
            raise HTTPException(422, "同一节课不能在一个方案中调动两次；额外复制课程需另行说明。")
        move_sources[index] = selected
        moved_ids.update(row.id for row in selected)

    # Cancellation follows the original snapshot dates. A subsequent makeup day
    # deliberately restores those selected lessons while keeping target classes.
    for op in plan.operations:
        if op.kind != "cancel_range":
            continue
        for row in source_rows:
            original = snapshots[row.id]
            if not op.start_date.isoformat() <= original["local_date"] <= op.end_date.isoformat():
                continue
            if row.status == "cancelled" and row.note == op.reason and row.is_override:
                continue
            body = tt.LessonPatch(version=row.version, status="cancelled", note=op.reason)
            update_lesson_record(db, user, row.id, body, now=now, allow_history=row.id in new_lesson_ids)
    for index, selected in move_sources.items():
        op = plan.operations[index]
        for row in selected:
            original = snapshots[row.id]
            body = tt.LessonPatch(
                version=row.version, date=op.target_date,
                start_time=original["start_time"], end_time=original["end_time"],
                status="leave" if original["status"] == "leave" else "scheduled", note=op.reason,
            )
            update_lesson_record(db, user, row.id, body, now=now, allow_history=row.id in new_lesson_ids)
    tt.assert_no_overlap(db, user.id, before_lessons=before["lessons"], semester_id=semester_id)
    return _summarize(before, _records(db, user.id, semester_id), semester_id, baseline_hash)


def preview_plan(db, user, semester_id, plan, proposal_id):
    """Run the exact executor inside a rollback-only savepoint; never commit."""
    plan = plan if isinstance(plan, SchedulePlan) else SchedulePlan.model_validate(plan)
    before = _records(db, user.id, semester_id)
    baseline = snapshot_hash(db, user.id, semester_id, records=before)
    transaction = db.begin_nested()
    try:
        return _execute(db, user, semester_id, plan, proposal_id, baseline, before)
    finally:
        transaction.rollback()


def apply_plan(db, user, semester_id, plan, proposal_id, baseline_hash):
    """Caller holds User FOR UPDATE and owns proposal idempotency and commit."""
    plan = plan if isinstance(plan, SchedulePlan) else SchedulePlan.model_validate(plan)
    legacy = not baseline_hash.startswith("semester-v1:")
    before = _records(db, user.id, semester_id, all_semesters=legacy)
    if snapshot_hash(db, user.id, semester_id, records=before, legacy=legacy) != baseline_hash:
        raise HTTPException(409, "课表已在其他操作中变化，请重新生成方案后再确认。")
    # A savepoint also guarantees that an executor error leaves no partial changes
    # if the API catches it to record a proposal failure in its outer transaction.
    with db.begin_nested():
        return _execute(db, user, semester_id, plan, proposal_id, baseline_hash, before)
