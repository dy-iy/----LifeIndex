"""Daily self check-ins and calendar history for active group members."""
import calendar
import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert

from auth import CurrentUser, Db, utcnow
from db import IdempotencyKey, Input, mutate
from group_checkin_rewards import award_full_checkin, reward_result
from group_messages import lock_current_auth, require_membership
from group_index import membership_intervals
from models import User
from study_group_models import StudyGroup, StudyGroupCheckin, StudyGroupCheckinReward, StudyGroupEvent, StudyGroupMember
from study_groups import lock_group

router = APIRouter()
CHECKIN_TIMEZONE = "Asia/Shanghai"
CHECKIN_ZONE = ZoneInfo(CHECKIN_TIMEZONE)


class GroupCheckinCreate(Input):
    local_date: date


class GroupCheckinMemberResponse(BaseModel):
    id: uuid.UUID
    effective_name: str
    avatar_url: str | None
    is_self: bool
    checked_in: bool
    checked_at: datetime | None


class GroupCheckinRewardResponse(BaseModel):
    awarded_at: datetime
    points: str
    previous_close: str
    rate: str
    member_count: int


class GroupCheckinResponse(BaseModel):
    group_id: uuid.UUID
    group_version: int
    local_date: date
    created_date: date
    today_date: date
    is_today: bool
    history_complete: bool
    timezone: str
    server_now: datetime
    member_count: int
    completed_count: int
    percent: int
    is_full: bool
    full_achieved: bool
    reward: GroupCheckinRewardResponse | None
    my_checked_in: bool
    my_checked_at: datetime | None
    checked_member_ids: list[uuid.UUID]
    members: list[GroupCheckinMemberResponse]


class GroupCheckinCalendarDay(BaseModel):
    local_date: date
    member_count: int
    completed_count: int
    percent: int
    is_full: bool
    full_achieved: bool
    reward: GroupCheckinRewardResponse | None
    history_complete: bool


class GroupCheckinCalendarResponse(BaseModel):
    group_id: uuid.UUID
    group_version: int
    month: str
    created_date: date
    today_date: date
    timezone: str
    server_now: datetime
    days: list[GroupCheckinCalendarDay]


def today(now):
    return now.astimezone(CHECKIN_ZONE).date()


def checkin_date_bounds(group, now):
    return group.created_at.astimezone(CHECKIN_ZONE).date(), today(now)


def require_checkin_date(group, local_date, now):
    created_date, today_date = checkin_date_bounds(group, now)
    if not created_date <= local_date <= today_date:
        raise HTTPException(422, "只能查看群聊创建日至今天的打卡记录。")


def current_members(db, group):
    return db.execute(select(User, StudyGroupMember.nickname)
        .select_from(StudyGroupMember)
        .join(User, User.id == StudyGroupMember.user_id)
        .where(StudyGroupMember.group_id == group.id, StudyGroupMember.status == "active")
        .order_by(StudyGroupMember.joined_at, StudyGroupMember.user_id)).all()


def membership_history(db, group):
    events = db.scalars(select(StudyGroupEvent).where(StudyGroupEvent.group_id == group.id)
                        .order_by(StudyGroupEvent.version)).all()
    baseline = next((event for event in events
                     if event.event_type in ("created", "group.migrated")), None)
    complete = bool(baseline is not None and baseline.version == 1
                    and len(events) == group.version
                    and all(event.version == index for index, event in enumerate(events, 1)))
    for event in events:
        payload = event.payload or {}
        if event.event_type == "created" and not (payload.get("owner_id") or event.actor_user_id):
            complete = False
        elif event.event_type in ("joined", "left") and not (payload.get("user_id") or event.actor_user_id):
            complete = False
        elif event.event_type == "owner_transferred" and payload.get("left") and not (
                payload.get("previous_owner_id") or event.actor_user_id):
            complete = False
        elif event.event_type == "members_removed" and not payload.get("user_ids"):
            complete = False
    try:
        earliest, intervals = membership_intervals(events)
    except (KeyError, TypeError, ValueError):
        # Partial legacy payloads cannot establish a reliable denominator.
        earliest, intervals, complete = None, [], False
    return earliest, intervals, baseline, complete


def historical_member_ids(local_date, history):
    """Use the closed day's member set; later joins/exits cannot rewrite it."""
    cutoff = datetime.combine(local_date + timedelta(days=1), time.min, CHECKIN_ZONE)
    earliest, intervals, baseline, complete = history
    if earliest is not None and earliest < cutoff:
        applicable = [interval for interval in intervals if interval.start < cutoff
                      and (interval.end is None or interval.end >= cutoff)]
        applicable.sort(key=lambda interval: (interval.start, str(interval.user_id)))
        return list(dict.fromkeys(interval.user_id for interval in applicable)), complete

    # A migrated group has no complete pre-migration exit ledger. Its immutable
    # baseline can identify some earlier members, but cannot establish full
    # attendance. Never use today's mutable joined_at values to fill this gap.
    known = []
    if baseline is not None and baseline.event_type == "group.migrated":
        for member in (baseline.payload or {}).get("members") or []:
            try:
                joined_at = datetime.fromisoformat(member["joined_at"])
                if joined_at.tzinfo is not None and joined_at < cutoff:
                    known.append((joined_at, uuid.UUID(member["user_id"])))
            except (KeyError, TypeError, ValueError):
                continue
    known.sort(key=lambda member: (member[0], str(member[1])))
    return list(dict.fromkeys(member[1] for member in known)), False


def progress_fields(total, count, history_complete=True):
    return dict(member_count=total, completed_count=count,
                percent=(count * 200 + total) // (total * 2) if total else 0,
                is_full=history_complete and total > 0 and count == total,
                history_complete=history_complete)


def checkin_result(db, group, user_id, now, local_date=None):
    created_date, today_date = checkin_date_bounds(group, now)
    local_date = local_date or today_date
    require_checkin_date(group, local_date, now)
    is_today = local_date == today_date
    if is_today:
        rows = current_members(db, group)
        member_ids, history_complete = [member.id for member, _ in rows], True
        profiles = {member.id: (member, nickname) for member, nickname in rows}
    else:
        member_ids, history_complete = historical_member_ids(local_date, membership_history(db, group))
        profiles = {member.id: (member, nickname) for member, nickname in db.execute(
            select(User, StudyGroupMember.nickname).outerjoin(StudyGroupMember, and_(
                StudyGroupMember.user_id == User.id, StudyGroupMember.group_id == group.id))
            .where(User.id.in_(member_ids))).all()} if member_ids else {}
    checked_at_by_member = dict(db.execute(select(StudyGroupCheckin.user_id, StudyGroupCheckin.checked_at)
        .where(StudyGroupCheckin.group_id == group.id, StudyGroupCheckin.local_date == local_date,
               StudyGroupCheckin.user_id.in_(member_ids))).all()) if member_ids else {}
    members = []
    for member_id in member_ids:
        member, nickname = profiles.get(member_id, (None, None))
        checked_at = checked_at_by_member.get(member_id)
        members.append(GroupCheckinMemberResponse(
            id=member_id, effective_name=nickname or (member.display_name if member else None) or "群成员",
            avatar_url=member.avatar_url if member else None, is_self=member_id == user_id,
            checked_in=checked_at is not None, checked_at=checked_at))
    checked = [member.id for member in members if member.checked_in]
    my_checked_at = next((member.checked_at for member in members if member.is_self), None)
    reward = db.get(StudyGroupCheckinReward, (group.id, local_date))
    return GroupCheckinResponse(
        group_id=group.id, group_version=group.version, local_date=local_date,
        created_date=created_date, today_date=today_date, is_today=is_today,
        timezone=CHECKIN_TIMEZONE, server_now=now,
        **progress_fields(len(member_ids), len(checked), history_complete),
        full_achieved=reward is not None, reward=reward_result(reward),
        my_checked_in=my_checked_at is not None, my_checked_at=my_checked_at,
        checked_member_ids=checked, members=members,
    ).model_dump(mode="json")


def checkin_calendar_result(db, group, now, month):
    created_date, today_date = checkin_date_bounds(group, now)
    year, month_number = map(int, month.split("-"))
    try:
        month_start = date(year, month_number, 1)
    except ValueError:
        raise HTTPException(422, "请选择有效月份。") from None
    month_end = date(year, month_number, calendar.monthrange(year, month_number)[1])
    first, last = max(created_date, month_start), min(today_date, month_end)
    days = []
    if first <= last:
        history = membership_history(db, group) if first < today_date else None
        active_ids = [member.id for member, _ in current_members(db, group)] if last == today_date else []
        receipts = {}
        for day, member_id in db.execute(select(StudyGroupCheckin.local_date, StudyGroupCheckin.user_id)
                .where(StudyGroupCheckin.group_id == group.id,
                       StudyGroupCheckin.local_date >= first, StudyGroupCheckin.local_date <= last)).all():
            receipts.setdefault(day, set()).add(member_id)
        rewards = {reward.local_date: reward for reward in db.scalars(select(StudyGroupCheckinReward)
            .where(StudyGroupCheckinReward.group_id == group.id,
                   StudyGroupCheckinReward.local_date >= first,
                   StudyGroupCheckinReward.local_date <= last)).all()}
        local_date = first
        while local_date <= last:
            member_ids, history_complete = ((active_ids, True) if local_date == today_date
                                           else historical_member_ids(local_date, history))
            count = len(set(member_ids) & receipts.get(local_date, set()))
            reward = rewards.get(local_date)
            days.append(GroupCheckinCalendarDay(local_date=local_date,
                        full_achieved=reward is not None, reward=reward_result(reward),
                        **progress_fields(len(member_ids), count, history_complete)))
            local_date += timedelta(days=1)
    return GroupCheckinCalendarResponse(
        group_id=group.id, group_version=group.version, month=month,
        created_date=created_date, today_date=today_date,
        timezone=CHECKIN_TIMEZONE, server_now=now, days=days).model_dump(mode="json")


def require_today(local_date, now):
    if local_date != today(now):
        raise HTTPException(409, "打卡日期已更新，请刷新今日进度后重试。")


@router.get("/api/groups/{group_id}/checkins/today", response_model=GroupCheckinResponse)
def get_group_checkins(group_id: uuid.UUID, user: CurrentUser, db: Db):
    # get_db starts a repeatable-read snapshot before authentication for group
    # GETs, so permissions and member counts describe the same committed state.
    require_membership(db, group_id, user.id)
    group = db.get(StudyGroup, group_id)
    return checkin_result(db, group, user.id, utcnow())


@router.get("/api/groups/{group_id}/checkins/calendar", response_model=GroupCheckinCalendarResponse)
def get_group_checkin_calendar(group_id: uuid.UUID, user: CurrentUser, db: Db,
                              month: Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]):
    require_membership(db, group_id, user.id)
    group = db.get(StudyGroup, group_id)
    return checkin_calendar_result(db, group, utcnow(), month)


@router.get("/api/groups/{group_id}/checkins", response_model=GroupCheckinResponse)
def get_group_checkins_for_date(group_id: uuid.UUID, user: CurrentUser, db: Db,
                               local_date: Annotated[date, Query(alias="date")]):
    require_membership(db, group_id, user.id)
    group = db.get(StudyGroup, group_id)
    return checkin_result(db, group, user.id, utcnow(), local_date)


@router.post("/api/groups/{group_id}/checkins/today", response_model=GroupCheckinResponse)
def create_group_checkin(group_id: uuid.UUID, body: GroupCheckinCreate, request: Request,
                         user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        # All membership mutations use this same group lock; authorization and
        # the active member set therefore remain valid through the commit.
        group, _ = lock_group(db, group_id, user.id, request)
        lock_current_auth(db, user.id, request)
        now = utcnow()
        require_today(body.local_date, now)
        inserted = db.execute(insert(StudyGroupCheckin).values(
            group_id=group.id, local_date=body.local_date, user_id=user.id, checked_at=now)
            .on_conflict_do_nothing(index_elements=["group_id", "local_date", "user_id"])
            .returning(StudyGroupCheckin.user_id)).scalar_one_or_none()
        if inserted is not None:
            member_ids = [member.id for member, _ in current_members(db, group)]
            checked_ids = set(db.scalars(select(StudyGroupCheckin.user_id)
                .where(StudyGroupCheckin.group_id == group.id,
                       StudyGroupCheckin.local_date == body.local_date,
                       StudyGroupCheckin.user_id.in_(member_ids))).all())
            if member_ids and set(member_ids) <= checked_ids:
                award_full_checkin(db, group, member_ids, body.local_date, now)
        return {"group_id": str(group.id), "local_date": body.local_date.isoformat()}

    def render(receipt, replayed):
        # Reusing a request key must never restore access after leave/removal,
        # or silently turn yesterday's retry into a new day's check-in.
        group, _ = lock_group(db, group_id, user.id, request)
        lock_current_auth(db, user.id, request)
        now = utcnow()
        require_today(date.fromisoformat(receipt["local_date"]), now)
        return checkin_result(db, group, user.id, now)

    # Like chat sends, daily check-in is independent of the management toggle.
    return mutate(db, user, request, key, body, 200, action,
                  no_key_update=True, render_response=render)
