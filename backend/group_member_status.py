"""Bounded, read-only member presence and personal index summaries for groups."""
import json
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import load_only

from auth import CurrentUser, Db, utcnow
from class_index import market_context_from_rows, money, quote_summary
from index_models import ClassIndexAccount, ClassMarketState
from models import Lesson, StudySession, User
from study_group_models import StudyGroup, StudyGroupMember
from study_groups import GroupMemberResponse, group_not_found, group_query
from study_sessions import TIMING_GRACE, session_result
from timetable_actions import lesson_result

router = APIRouter()
COURSE_FIELDS = ("name", "timezone", "starts_at", "ends_at", "start_time", "end_time")


class MemberLessonResponse(BaseModel):
    id: str
    name: str
    timezone: str
    starts_at: datetime
    ends_at: datetime
    start_time: str
    end_time: str
    status: Literal["scheduled"] = "scheduled"
    is_deleted: bool = False


class MemberQuoteResponse(BaseModel):
    price: str
    previous_close: str
    change_points: str
    change_percent: str
    business_date: str
    timezone: str
    quote_at: datetime
    catching_up: bool


class MemberPointResponse(BaseModel):
    time: datetime
    price: str


class MemberStatusResponse(GroupMemberResponse):
    class_status: Literal["in_class", "idle"]
    timer_status: Literal["timing", "not_timing"]
    timer_phase: Literal["running", "paused", "idle"]
    lesson: MemberLessonResponse | None
    quote: MemberQuoteResponse
    points: list[MemberPointResponse]


class GroupMemberStatusResponse(BaseModel):
    group_id: uuid.UUID
    group_version: int
    server_now: datetime
    members: list[MemberStatusResponse]


# Select opening/closing observations and real extrema in sixteen time buckets.
# Each member therefore returns at most 64 source points, regardless of the
# five-second tick count. Legacy settlements without a market point still draw.
MEMBER_POINTS_SQL = """
    WITH requested AS (
        SELECT * FROM jsonb_to_recordset(CAST(:members AS jsonb))
        AS r(user_id uuid, business_date date, timezone text, quote_at timestamptz)
    ), samples AS (
        SELECT p.user_id, p.occurred_at AS at, p.price, p.sequence, 1 AS priority,
               greatest(0, least(15, floor(extract(epoch FROM
                     ((p.occurred_at AT TIME ZONE r.timezone)
                     - r.business_date::timestamp)) / 5400)::integer)) AS bucket
        FROM requested r JOIN class_market_points p
          ON p.user_id = r.user_id AND p.business_date = r.business_date
        WHERE p.occurred_at <= r.quote_at
        UNION ALL
        SELECT e.user_id, e.created_at, e.price_after, e.sequence, 0,
               greatest(0, least(15, floor(extract(epoch FROM
                     ((e.created_at AT TIME ZONE r.timezone)
                     - r.business_date::timestamp)) / 5400)::integer))
        FROM requested r JOIN class_index_events e
          ON e.user_id = r.user_id AND e.business_date = r.business_date
        WHERE e.created_at <= r.quote_at AND NOT EXISTS (
            SELECT 1 FROM class_market_points p WHERE p.user_id = e.user_id
              AND p.business_date = e.business_date AND p.event_id = e.id
        )
    ), ranked AS (
        SELECT *,
          row_number() OVER (PARTITION BY user_id, bucket
              ORDER BY at, priority, sequence) AS first,
          row_number() OVER (PARTITION BY user_id, bucket
              ORDER BY at DESC, priority DESC, sequence DESC) AS last,
          row_number() OVER (PARTITION BY user_id, bucket
              ORDER BY price, at, priority, sequence) AS low,
          row_number() OVER (PARTITION BY user_id, bucket
              ORDER BY price DESC, at, priority, sequence) AS high
        FROM samples
    )
    SELECT user_id, at, price FROM ranked
    WHERE first = 1 OR last = 1 OR low = 1 OR high = 1
    ORDER BY user_id, at, priority, sequence
"""


def select_current_lesson(lessons, now):
    """Match timer.currentLesson: ongoing class, distance, latest start, id."""
    return min(lessons, key=lambda lesson: (
        not (lesson.starts_at <= now < lesson.ends_at),
        max(lesson.starts_at - now, now - lesson.ends_at, timedelta(0)),
        -lesson.starts_at.timestamp(), str(lesson.id),
    ), default=None)


def member_lesson(lessons, active, now):
    # The personal timer displays a running/paused session's saved occurrence
    # during its grace window, even when another class's window is adjacent.
    if active is not None:
        rendered = session_result(active, [], now, include_segments=False)
        return {"id": rendered["lesson_id"], **{field: rendered[field] for field in COURSE_FIELDS}}
    current = select_current_lesson(lessons, now)
    if current is None:
        return None
    rendered = lesson_result(current)
    return {"id": rendered["id"], **{field: rendered[field] for field in COURSE_FIELDS}}


@router.get("/api/groups/{group_id}/members/status", response_model=GroupMemberStatusResponse)
def get_member_status(group_id: uuid.UUID, user: CurrentUser, db: Db):
    # Group GETs already use one repeatable-read, read-only snapshot. Expired
    # timers are filtered with the same grace cutoff as timer reconciliation,
    # without changing another member's learning records while viewing a group.
    row = db.execute(group_query(user.id).where(StudyGroup.id == group_id)).one_or_none()
    if row is None:
        raise group_not_found()
    group = row[0]
    now = utcnow()
    rows = db.execute(select(User, StudyGroupMember, ClassIndexAccount, ClassMarketState)
        .options(load_only(User.id, User.display_name, User.avatar_id, User.timezone),
                 load_only(ClassMarketState.cursor_at, ClassMarketState.sequence,
                           ClassMarketState.timezone, ClassMarketState.rule_version,
                           ClassMarketState.running, ClassMarketState.starts_at, ClassMarketState.ends_at))
        .join(StudyGroupMember, StudyGroupMember.user_id == User.id)
        .outerjoin(ClassIndexAccount, ClassIndexAccount.user_id == User.id)
        .outerjoin(ClassMarketState, ClassMarketState.user_id == User.id)
        .where(StudyGroupMember.group_id == group.id, StudyGroupMember.status == "active")
        .order_by((User.id == group.owner_id).desc(), StudyGroupMember.joined_at, User.id)).all()
    member_ids = [member.id for member, *_ in rows]
    lessons = {member_id: [] for member_id in member_ids}
    for lesson in db.scalars(select(Lesson).where(
        Lesson.user_id.in_(member_ids), Lesson.is_deleted.is_(False), Lesson.status == "scheduled",
        Lesson.starts_at <= now + TIMING_GRACE, Lesson.ends_at > now - TIMING_GRACE,
    )):
        lessons[lesson.user_id].append(lesson)
    active = {item.user_id: item for item in db.scalars(select(StudySession).where(
        StudySession.user_id.in_(member_ids), StudySession.phase.in_(["running", "paused"]),
        StudySession.voided.is_(False), StudySession.starts_at <= now + TIMING_GRACE,
        StudySession.ends_at > now - TIMING_GRACE,
    ))}
    quotes = {member.id: quote_summary(member, now, market_context_from_rows(member, now, account, state))
              for member, membership, account, state in rows}
    points = {member_id: [] for member_id in member_ids}
    requested = [{"user_id": str(member_id), "business_date": quote["business_date"],
                  "timezone": quote["timezone"], "quote_at": quote["quote_at"]}
                 for member_id, quote in quotes.items()]
    for point in db.execute(text(MEMBER_POINTS_SQL), {"members": json.dumps(requested)}).mappings():
        points[point["user_id"]].append({"time": point["at"], "price": money(point["price"])})

    members = []
    for member, membership, account, state in rows:
        timer = active.get(member.id)
        quote = quotes[member.id]
        member_points = points[member.id]
        quote_at = datetime.fromisoformat(quote["quote_at"])
        latest = {"time": quote_at, "price": quote["price"]}
        if not member_points or member_points[-1] != latest:
            member_points.append(latest)
        members.append(MemberStatusResponse(
            id=member.id, display_name=member.display_name, nickname=membership.nickname,
            effective_name=membership.nickname or member.display_name or "群成员", avatar_url=member.avatar_url,
            is_owner=member.id == group.owner_id, is_self=member.id == user.id,
            class_status="in_class" if any(lesson.starts_at <= now < lesson.ends_at
                                          for lesson in lessons[member.id]) else "idle",
            timer_status="timing" if timer is not None and timer.phase == "running" else "not_timing",
            timer_phase=timer.phase if timer is not None else "idle",
            lesson=member_lesson(lessons[member.id], timer, now), quote=quote, points=member_points,
        ))
    return GroupMemberStatusResponse(group_id=group.id, group_version=group.version,
                                     server_now=now, members=members)
