"""Read-only group quotes and exact charts from membership and price ledgers.

The group is the arithmetic mean of the members who belonged at each instant,
plus the permanently earned full-check-in rewards at that instant.
Personal extrema cannot be averaged: simultaneous observations are applied as
one timestamp before taking any group high or low. No derived prices are stored.
"""
import json
import hashlib
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Annotated, Literal
from threading import Lock
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import load_only

from auth import CurrentUser, Db
from class_index import money as personal_money, next_period, period_start
from index_models import ClassIndexAccount, ClassMarketState
from market_math import STEP
from rules import INITIAL_PRICE
from study_group_models import StudyGroup, StudyGroupCheckinReward, StudyGroupEvent
from study_groups import group_not_found, group_query

router = APIRouter()
GROUP_TIMEZONE = "Asia/Shanghai"
ZONE = ZoneInfo(GROUP_TIMEZONE)
GROUP_RULE_VERSION = "group-member-mean-full-checkin-three-percent-v3"


class ClosedDayCache:
    """Bounded derived closed bars; every request still checks database access.

    Publication uses the database snapshot timestamp. An older in-flight read
    cannot replace a newer membership/source generation or invalidate its bars.
    The lock covers only small dictionary operations, never database work.
    """
    def __init__(self, groups=32, days=744):
        self.groups, self.days = groups, days
        self.entries = OrderedDict()
        self.lock = Lock()

    def read(self, group_id, signature, snapshot_at):
        with self.lock:
            entry = self.entries.get(group_id)
            if entry is None or entry["signature"] != signature or snapshot_at < entry["snapshot_at"]:
                return {}
            self.entries.move_to_end(group_id)
            return {day: dict(row) for day, row in entry["days"].items()}

    def invalidate(self, group_id, snapshot_at):
        with self.lock:
            entry = self.entries.get(group_id)
            if entry is not None and snapshot_at >= entry["snapshot_at"]:
                self.entries.pop(group_id)

    def publish(self, group_id, signature, snapshot_at, rows):
        with self.lock:
            entry = self.entries.get(group_id)
            if entry is not None and snapshot_at < entry["snapshot_at"]:
                return
            previous = entry["days"] if entry and entry["signature"] == signature else {}
            days = {**previous, **{row["day"]: dict(row) for row in rows}}
            if len(days) > self.days:
                days = {day: days[day] for day in sorted(days)[-self.days:]}
            self.entries[group_id] = {"signature": signature, "snapshot_at": snapshot_at, "days": days}
            self.entries.move_to_end(group_id)
            while len(self.entries) > self.groups:
                self.entries.popitem(last=False)


closed_days = ClosedDayCache()


def money(value, *, signed=False):
    with localcontext() as context:
        context.prec = 48
        return personal_money(value, signed=signed)


def percent(points, previous):
    with localcontext() as context:
        context.prec = 48
        return money(Decimal(points) / Decimal(previous) * 100, signed=True)


@dataclass(frozen=True)
class MembershipInterval:
    user_id: uuid.UUID
    start: datetime
    end: datetime | None = None


def membership_intervals(events):
    """Migration starts an explicit baseline, never guesses pre-migration exits.

    Old events used transaction-start timestamps. Preserve version order and
    clamp only their read-time effective timestamps so history cannot go back.
    """
    active = {}
    intervals = []
    earliest = None
    previous_at = None
    for event in events:
        recorded_at = event.created_at.astimezone(timezone.utc)
        at = max(previous_at, recorded_at) if previous_at else recorded_at
        previous_at = at
        payload = event.payload or {}
        if event.event_type in ("created", "group.migrated"):
            if earliest is not None:
                continue
            earliest = at
            ids = ([entry["user_id"] for entry in payload.get("members", [])]
                   if event.event_type == "group.migrated" else
                   [payload.get("owner_id") or event.actor_user_id])
            for user_id in ids:
                if user_id is not None:
                    active[uuid.UUID(str(user_id))] = at
        elif earliest is None:
            continue
        elif event.event_type == "joined":
            actor_id = payload.get("user_id") or event.actor_user_id
            if actor_id is not None:
                active.setdefault(uuid.UUID(str(actor_id)), at)
        else:
            leaving = []
            if event.event_type == "left":
                leaving = [payload.get("user_id") or event.actor_user_id]
            elif event.event_type == "members_removed":
                leaving = payload.get("user_ids", [])
            elif event.event_type == "owner_transferred" and payload.get("left"):
                leaving = [payload.get("previous_owner_id") or event.actor_user_id]
            elif event.event_type == "dissolved":
                leaving = list(active)
            for user_id in leaving:
                if user_id is None:
                    continue
                user_id = uuid.UUID(str(user_id))
                start = active.pop(user_id, None)
                if start is not None and start < at:
                    intervals.append(MembershipInterval(user_id, start, at))
    intervals.extend(MembershipInterval(user_id, start) for user_id, start in active.items())
    return earliest, intervals


# Coarse date bounds use the existing owner/business_date/sequence index; the
# final predicate always uses actual UTC time, independent of personal zones.
# Seeds seek the newest source by its immutable per-owner sequence/date. Account
# heads are deliberately not backdated into history.
SEEDS_SQL = """
    WITH intervals AS (
        SELECT * FROM jsonb_to_recordset(CAST(:intervals AS jsonb))
        AS i(ordinal integer, user_id uuid, starts_at timestamptz, ends_at timestamptz, carried boolean)
    )
    SELECT i.ordinal, i.user_id, i.starts_at, i.ends_at, i.carried,
           COALESCE(CASE WHEN p.occurred_at IS NOT NULL AND
                              (e.created_at IS NULL OR p.occurred_at >= e.created_at)
                         THEN p.price ELSE e.price_after END, 1000) AS price
    FROM intervals i
    LEFT JOIN LATERAL (
        SELECT price, occurred_at FROM class_market_points
        WHERE user_id = i.user_id AND (occurred_at < i.starts_at OR
              (:include_boundary_tick AND occurred_at = i.starts_at AND kind = 'tick'))
          AND business_date <= (i.starts_at AT TIME ZONE 'UTC')::date + 2
        ORDER BY business_date DESC, sequence DESC LIMIT 1
    ) p ON true
    LEFT JOIN LATERAL (
        SELECT price_after, created_at FROM class_index_events
        WHERE user_id = i.user_id AND created_at < i.starts_at
          AND business_date <= (i.starts_at AT TIME ZONE 'UTC')::date + 2
        ORDER BY business_date DESC, sequence DESC LIMIT 1
    ) e ON true
    ORDER BY i.ordinal
"""


SWEEP_SQL = """
    WITH intervals AS (
        SELECT * FROM jsonb_to_recordset(CAST(:intervals AS jsonb))
        AS i(ordinal integer, user_id uuid, starts_at timestamptz,
             ends_at timestamptz, carried boolean, seed numeric)
    ), market_samples AS MATERIALIZED (
        SELECT i.ordinal, p.occurred_at AS at, p.price, p.sequence,
               p.event_id, p.kind,
               CASE WHEN p.kind = 'tick' AND
                   (p.occurred_at AT TIME ZONE :zone)::time = time '00:00:00' AND
                   (i.starts_at < p.occurred_at OR i.carried) THEN 0 ELSE 1 END AS phase
        FROM intervals i JOIN class_market_points p ON p.user_id = i.user_id
        WHERE p.business_date BETWEEN :lower_date AND :upper_date
          AND p.occurred_at >= i.starts_at AND (p.occurred_at < i.ends_at OR
              (p.occurred_at = i.ends_at AND p.kind = 'tick' AND
               (p.occurred_at AT TIME ZONE :zone)::time = time '00:00:00'))
    ), samples AS (
        SELECT ordinal, starts_at AS at, seed AS price, 0::bigint AS sequence,
               0 AS priority, true AS membership, false AS event,
               CASE WHEN carried AND (starts_at AT TIME ZONE :zone)::time = time '00:00:00'
                    THEN 0 ELSE 1 END AS phase FROM intervals
        UNION ALL
        SELECT ordinal, at, price, sequence, 2, false, event_id IS NOT NULL, phase
        FROM market_samples
        UNION ALL
        SELECT i.ordinal, e.created_at, e.price_after, e.sequence, 1, false, true, 1
        FROM intervals i JOIN class_index_events e ON e.user_id = i.user_id
        WHERE e.business_date BETWEEN :lower_date AND :upper_date
          AND e.created_at >= i.starts_at AND e.created_at < i.ends_at
          AND NOT EXISTS (SELECT 1 FROM market_samples p
                          WHERE p.ordinal = i.ordinal AND p.event_id = e.id)
    ), ranked AS (
        SELECT *, price - COALESCE(lag(price) OVER (
            PARTITION BY ordinal ORDER BY at, phase, priority, sequence), 0) AS delta
        FROM samples
    ), reward_events AS MATERIALIZED (
        SELECT awarded_at, reward_points FROM study_group_checkin_rewards
        WHERE group_id = :group_id AND awarded_at >= :starts_at AND awarded_at < :ends_at
    ), changes AS (
        SELECT at, delta, CASE WHEN membership THEN 1 ELSE 0 END AS member_delta,
               membership, event, phase, 0::numeric AS reward_delta FROM ranked
        UNION ALL
        SELECT i.ends_at, -sum(r.delta), -1, true, false, 1, 0::numeric
        FROM intervals i JOIN ranked r ON r.ordinal = i.ordinal
        WHERE i.ends_at < :ends_at
        GROUP BY i.ordinal, i.ends_at
        UNION ALL
        -- The terminal midnight tick closes the old day; a zero-change opening
        -- observation then carries that value into the new group trading day.
        SELECT DISTINCT at, 0::numeric, 0, false, false, 1, 0::numeric FROM ranked
        WHERE phase = 0 AND (at AT TIME ZONE :zone)::time = time '00:00:00'
        UNION ALL
        -- Rewards are additive group events, independent of the member count.
        -- Midnight personal ticks retain phase 0, while a new day's reward is
        -- phase 1 and therefore cannot enter the previous day's close.
        SELECT awarded_at, 0::numeric, 0, false, true, 1, reward_points FROM reward_events
        UNION ALL
        -- Keep the instant before the award as well. This preserves the jump
        -- and the pre-award low even on a day without any personal ticks.
        SELECT awarded_at, 0::numeric, 0, false, false, 0, 0::numeric FROM reward_events
    ), batches AS (
        SELECT at, phase, sum(delta) AS delta, sum(member_delta) AS member_delta,
               bool_or(membership) AS membership, bool_or(event) AS event,
               sum(reward_delta) AS reward_delta
        FROM changes GROUP BY at, phase
    ), totals AS (
        SELECT at, phase, sum(delta) OVER (ORDER BY at, phase) AS total,
               sum(member_delta) OVER (ORDER BY at, phase) AS members, membership, event,
               :reward_seed + sum(reward_delta) OVER (ORDER BY at, phase) AS rewards
        FROM batches
    ), prices AS (
        SELECT at, phase, total / members + rewards AS price, membership, event,
               CASE WHEN phase = 0 AND (at AT TIME ZONE :zone)::time = time '00:00:00'
                    THEN at - interval '1 microsecond' ELSE at END AS effective_at
        FROM totals WHERE members > 0
    ), selected_prices AS (
        SELECT * FROM prices WHERE effective_at >= :starts_at AND effective_at < :ends_at
    )
"""

INTRADAY_SQL = SWEEP_SQL + """
    , ranked_prices AS (
        SELECT *, row_number() OVER (
                   PARTITION BY date_trunc('minute', effective_at) ORDER BY at, phase) AS first,
                  row_number() OVER (
                   PARTITION BY date_trunc('minute', effective_at) ORDER BY at DESC, phase DESC) AS last,
                  row_number() OVER (
                   PARTITION BY date_trunc('minute', effective_at) ORDER BY price, at, phase) AS low,
                  row_number() OVER (
                   PARTITION BY date_trunc('minute', effective_at) ORDER BY price DESC, at, phase) AS high
        FROM selected_prices
    )
    SELECT at, phase, price, membership, event,
           EXISTS (SELECT 1 FROM reward_events r WHERE r.awarded_at = ranked_prices.at) AS reward
    FROM ranked_prices
    -- Reward-before observations must survive minute compression even when
    -- other ticks establish that minute's extrema. Phase 0 also retains the
    -- existing terminal midnight close, while the reward-after is an event.
    WHERE first = 1 OR last = 1 OR low = 1 OR high = 1 OR membership OR event OR phase = 0
    ORDER BY at, phase
"""

DAILY_SQL = SWEEP_SQL + """
    , day_prices AS MATERIALIZED (
        SELECT *, (effective_at AT TIME ZONE :zone)::date AS day,
               row_number() OVER (PARTITION BY (effective_at AT TIME ZONE :zone)::date
                                  ORDER BY at, phase) AS first,
               row_number() OVER (PARTITION BY (effective_at AT TIME ZONE :zone)::date
                                  ORDER BY at DESC, phase DESC) AS last
        FROM selected_prices
    ), bounds AS (
        SELECT day, max(price) AS high, min(price) AS low,
               count(*) FILTER (WHERE event OR membership) AS event_count,
               count(*) AS sample_count
        FROM day_prices GROUP BY day
    )
    SELECT b.day, opening.price AS open, b.high, b.low, closing.price AS close,
           b.event_count, b.sample_count
    FROM bounds b JOIN day_prices opening ON opening.day = b.day AND opening.first = 1
                  JOIN day_prices closing ON closing.day = b.day AND closing.last = 1
    ORDER BY b.day
"""


def interval_records(intervals, floor, end):
    return [{"ordinal": ordinal, "user_id": str(interval.user_id),
             "starts_at": max(interval.start, floor).isoformat(),
             "ends_at": min(interval.end or end, end).isoformat(), "carried": interval.start < floor}
            for ordinal, interval in enumerate(intervals)
            if interval.start < end and (interval.end is None or interval.end > floor)]


def seed_records(db, records, *, include_boundary_tick=False):
    if not records:
        return []
    rows = []
    for source in db.execute(text(SEEDS_SQL), {"intervals": json.dumps(records),
                                              "include_boundary_tick": include_boundary_tick}).mappings():
        row = dict(source)
        row["seed"] = str(row.pop("price"))
        rows.append(row)
    return rows


def reward_points_before(db, group_id, at):
    """Strict boundary keeps a midnight award in its own business day."""
    if group_id is None:
        return Decimal(0)
    return db.scalar(select(func.coalesce(func.sum(StudyGroupCheckinReward.reward_points), 0))
                     .where(StudyGroupCheckinReward.group_id == group_id,
                            StudyGroupCheckinReward.awarded_at < at))


def reward_sources(db, group_id, now):
    return tuple((row.local_date.isoformat(), row.awarded_at.isoformat(), str(row.reward_points))
                 for row in db.execute(select(StudyGroupCheckinReward.local_date,
                                              StudyGroupCheckinReward.awarded_at,
                                              StudyGroupCheckinReward.reward_points)
                                       .where(StudyGroupCheckinReward.group_id == group_id,
                                              StudyGroupCheckinReward.awarded_at <= now)
                                       .order_by(StudyGroupCheckinReward.local_date)))


def aggregate_rows(db, intervals, floor, end, *, intraday=False, group_id=None):
    records = seed_records(db, interval_records(intervals, floor, end))
    if not records:
        return []
    # JSON has only our UUIDs, timestamps and Decimal seeds; it is always bound,
    # never interpolated into SQL. Dates cover every IANA offset around UTC.
    serializable = [{**row, "user_id": str(row["user_id"]),
                     "starts_at": row["starts_at"].isoformat(),
                     "ends_at": row["ends_at"].isoformat()} for row in records]
    parameters = {"intervals": json.dumps(serializable), "starts_at": floor, "ends_at": end,
                  "lower_date": floor.date() - timedelta(days=2),
                  "upper_date": end.date() + timedelta(days=2), "zone": GROUP_TIMEZONE,
                  "group_id": group_id, "reward_seed": reward_points_before(db, group_id, floor)}
    return list(db.execute(text(INTRADAY_SQL if intraday else DAILY_SQL), parameters).mappings())


def historical_price(db, intervals, at, *, include_boundary_tick=False, group_id=None):
    # At the boundary, use the instant immediately before it, including users
    # who leave exactly at midnight and excluding users who join at midnight.
    active = [interval for interval in intervals if interval.start < at and
              (interval.end is None or interval.end >= at)]
    if not active:
        return None
    records = [{"ordinal": index, "user_id": str(interval.user_id),
                "starts_at": at.isoformat(), "ends_at": at.isoformat(), "carried": True}
               for index, interval in enumerate(active)]
    seeds = seed_records(db, records, include_boundary_tick=include_boundary_tick)
    with localcontext() as context:
        context.prec = 48
        return (sum((Decimal(row["seed"]) for row in seeds), Decimal(0)) / len(seeds)
                + reward_points_before(db, group_id, at))


def midnight(day):
    return datetime.combine(day, time.min, ZONE).astimezone(timezone.utc)


def previous_close(db, intervals, earliest, day, initial_price, *, group_id=None):
    if day <= earliest.astimezone(ZONE).date():
        return Decimal(money(initial_price))
    prior = historical_price(db, intervals, midnight(day), include_boundary_tick=True, group_id=group_id)
    if prior is None:
        prior = initial_price
    return Decimal(money(prior))


def quote_result(group, current_price, now, previous, daily):
    price = Decimal(money(current_price))
    row = daily[-1] if daily else None
    high = max(previous, row["high"] if row else price, price)
    low = min(previous, row["low"] if row else price, price)
    return {"code": "group", "price": money(price), "previous_close": money(previous),
            "change_points": money(price - previous, signed=True),
            "change_percent": percent(price - previous, previous),
            "business_date": now.astimezone(ZONE).date().isoformat(), "timezone": GROUP_TIMEZONE,
            "open": money(previous), "high": money(high), "low": money(low),
            "server_now": now.isoformat(), "quote_at": now.isoformat(),
            "activated": True, "continuous": True, "market_state": "group",
            "group_version": group.version, "initial_price": money(group.initial_price),
            "rule_version": GROUP_RULE_VERSION}


def history_sources(db, intervals, now):
    cutoffs = {}
    for interval in intervals:
        cutoff = min(interval.end or now, now)
        cutoffs[interval.user_id] = max(cutoffs.get(interval.user_id, cutoff), cutoff)
    if not cutoffs:
        return False, ()
    states = db.scalars(select(ClassMarketState).options(load_only(
        ClassMarketState.user_id, ClassMarketState.activated_at, ClassMarketState.cursor_at,
        ClassMarketState.timezone, ClassMarketState.running,
        ClassMarketState.starts_at, ClassMarketState.ends_at))
        .where(ClassMarketState.user_id.in_(cutoffs))).all()
    state_by_user = {state.user_id: state for state in states}
    accounts = dict(db.execute(select(ClassIndexAccount.user_id, ClassIndexAccount.sequence)
                               .where(ClassIndexAccount.user_id.in_(cutoffs))).all())
    source_signature = tuple(sorted(
        (str(user_id), "continuous", state_by_user[user_id].activated_at.isoformat())
        if user_id in state_by_user else (str(user_id), "legacy", accounts.get(user_id))
        for user_id in cutoffs))
    catching_up = False
    for state in states:
        cutoff = cutoffs[state.user_id]
        if cutoff <= state.activated_at:
            continue
        target = state.activated_at + ((cutoff - state.activated_at) // STEP) * STEP
        # The personal engine also closes partial steps at its own midnight and
        # running timer boundaries. A sub-five-second delayed midnight tick can
        # still change a group candle which the client had already retained.
        zone = ZoneInfo(state.timezone)
        boundary = datetime.combine(cutoff.astimezone(zone).date(), time.min, zone).astimezone(timezone.utc)
        if state.activated_at <= boundary <= cutoff:
            target = max(target, boundary)
        if state.running:
            for boundary in (state.starts_at, state.ends_at):
                if boundary is not None and state.activated_at <= boundary <= cutoff:
                    target = max(target, boundary)
        if state.cursor_at < target:
            catching_up = True
    return catching_up, source_signature


def history_catching_up(db, intervals, now):
    return history_sources(db, intervals, now)[0]


def membership_signature(earliest, intervals, source_signature, initial_price, reward_signature=()):
    facts = sorted((str(interval.user_id), interval.start.isoformat(),
                    interval.end.isoformat() if interval.end else "") for interval in intervals)
    return hashlib.sha256(json.dumps([GROUP_RULE_VERSION, str(initial_price),
                                    earliest.isoformat(), facts, source_signature, reward_signature],
                                    separators=(",", ":")).encode()).hexdigest()


def cached_daily_rows(db, group_id, intervals, earliest, floor, end, now,
                      catching_up, signature, snapshot_at):
    """Reuse complete past days, recomputing the current day from actual ticks."""
    if catching_up:
        closed_days.invalidate(group_id, snapshot_at)
        return aggregate_rows(db, intervals, floor, end, group_id=group_id)
    today = now.astimezone(ZONE).date()
    current_floor = midnight(today)
    last_closed_end = min(end, current_floor)
    requested = []
    day = floor.astimezone(ZONE).date()
    while midnight(day) < last_closed_end:
        requested.append(day)
        day += timedelta(days=1)
    cached = closed_days.read(group_id, signature, snapshot_at)
    missing = [day for day in requested if day not in cached]
    fresh = []
    # Group adjacent gaps into bounded scans, rather than one query per day.
    while missing:
        start = missing.pop(0)
        finish = start
        while missing and missing[0] == finish + timedelta(days=1):
            finish = missing.pop(0)
        scan_floor = max(midnight(start), earliest)
        scan_end = midnight(finish + timedelta(days=1))
        rows = {row["day"]: dict(row) for row in aggregate_rows(db, intervals, scan_floor, scan_end,
                                                             group_id=group_id)}
        carry = historical_price(db, intervals, midnight(start), include_boundary_tick=True,
                                 group_id=group_id)
        if carry is None:
            carry = historical_price(db, intervals, earliest + timedelta(microseconds=1),
                                     group_id=group_id) or INITIAL_PRICE
        day = start
        while day <= finish:
            row = rows.get(day) or {"day": day, "open": carry, "high": carry, "low": carry,
                                    "close": carry, "event_count": 0, "sample_count": 0}
            carry = row["close"]
            fresh.append(row)
            cached[day] = row
            day += timedelta(days=1)
    if fresh:
        closed_days.publish(group_id, signature, snapshot_at, fresh)
    result = [cached[day] for day in requested]
    if end > current_floor:
        result.extend(aggregate_rows(db, intervals, max(floor, current_floor), end, group_id=group_id))
    return result


@router.get("/api/groups/{group_id}/index")
def get_group_index(group_id: uuid.UUID, user: CurrentUser, db: Db,
                    period: Literal["intraday", "day", "week", "month"] = "intraday",
                    limit: Annotated[int, Query(ge=1, le=120)] = 120,
                    before: date | None = None, since: date | None = None,
                    selected_date: Annotated[date | None, Query(alias="date")] = None):
    row = db.execute(group_query(user.id).where(StudyGroup.id == group_id)).one_or_none()
    if row is None:
        raise group_not_found()
    group, _, current_price = row
    events = db.scalars(select(StudyGroupEvent).where(StudyGroupEvent.group_id == group.id)
                        .order_by(StudyGroupEvent.version)).all()
    earliest, intervals = membership_intervals(events)
    if earliest is None or not intervals:
        raise HTTPException(409, "群指数历史尚未准备好，请刷新后重试。")
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZONE).date()
    if before is not None and since is not None:
        raise HTTPException(422, "历史翻页与当前周期刷新不能同时使用。")
    if before == date.min:
        raise HTTPException(422, "历史翻页日期无效。")
    if (since and since > today) or (selected_date and selected_date > today):
        raise HTTPException(422, "不能查询未来群指数。")
    previous = previous_close(db, intervals, earliest, today, group.initial_price, group_id=group.id)
    today_floor = max(midnight(today), earliest)
    live_end = now + timedelta(microseconds=1)
    catching_up, source_signature = history_sources(db, intervals, now)
    signature = membership_signature(earliest, intervals, source_signature, group.initial_price,
                                     reward_sources(db, group.id, now))
    snapshot_at = db.scalar(select(func.transaction_timestamp()))
    if catching_up:
        closed_days.invalidate(group.id, snapshot_at)

    def current_quote(daily=None):
        if daily is None:
            daily = aggregate_rows(db, intervals, today_floor, live_end, group_id=group.id)
        quote = quote_result(group, current_price, now, previous, daily)
        quote.update(history_start=earliest.isoformat(),
                     history_catching_up=catching_up)
        return quote

    if period == "intraday":
        if before is not None or since is not None:
            raise HTTPException(422, "分时日期请使用 date 参数。")
        day = selected_date or today
        day_floor = max(midnight(day), earliest)
        end = live_end if day == today else midnight(day + timedelta(days=1))
        points = aggregate_rows(db, intervals, day_floor, end, intraday=True,
                                group_id=group.id) if day_floor < end else []
        # Minute selection retains actual extrema after group aggregation, so
        # today needs one sweep for both the line and quote high/low.
        extrema = [{"high": max(point["price"] for point in points),
                    "low": min(point["price"] for point in points)}] if points else []
        quote = current_quote(extrema if day == today else None)
        reference = previous if day == today else previous_close(db, intervals, earliest, day,
                                                               group.initial_price, group_id=group.id)
        rendered = [{"id": f"g-{int(point['at'].timestamp() * 1000000)}-{point['phase']}", "time": point["at"].astimezone(ZONE).isoformat(),
                     "price": money(point["price"]),
                     "kind": ("reward_before" if point["phase"] == 0 else "reward") if point["reward"]
                             else "membership" if point["membership"] else "tick"}
                    for point in points]
        if day == today:
            # Account heads and chart are from the same repeatable-read snapshot.
            # The final observation also covers dormant accounts without ticks.
            rendered.append({"id": "g-current", "time": now.astimezone(ZONE).isoformat(),
                             "price": quote["price"], "kind": "tick"})
        return {"period": period, "date": day.isoformat(), "timezone": GROUP_TIMEZONE,
                "previous_close": money(reference), "points": rendered, "ongoing": day == today,
                "continuous": True, "quote_at": quote["quote_at"], "quote": quote,
                "history_start": earliest.isoformat()}
    if selected_date is not None:
        raise HTTPException(422, "K 线日期请使用 before 或 since 参数。")
    earliest_day = earliest.astimezone(ZONE).date()
    earliest_start = period_start(earliest_day, period)
    if before is not None and before <= earliest_start:
        return {"period": period, "items": [], "next_before": None, "quote": current_quote(),
                "history_start": earliest.isoformat()}
    end_day = min(today, before - timedelta(days=1)) if before else today
    start = period_start(end_day, period)
    if before and start >= before:
        start = period_start(start - timedelta(days=1), period)
    floor_start = max(earliest_start, period_start(since, period)) if since else earliest_start
    starts = []
    while start >= floor_start and len(starts) < limit:
        starts.append(start)
        start = period_start(start - timedelta(days=1), period)
    starts.reverse()
    if not starts:
        return {"period": period, "items": [], "next_before": None, "quote": current_quote(),
                "history_start": earliest.isoformat()}
    floor = max(midnight(starts[0]), earliest)
    ceiling_day = min(next_period(starts[-1], period) - timedelta(days=1), today)
    end = live_end if ceiling_day == today else midnight(ceiling_day + timedelta(days=1))
    days = cached_daily_rows(db, group.id, intervals, earliest, floor, end, now,
                            catching_up, signature, snapshot_at)
    by_day = {item["day"]: item for item in days}
    quote = current_quote([by_day[today]] if today in by_day else [] if ceiling_day == today else None)
    carry = previous_close(db, intervals, earliest, starts[0], group.initial_price, group_id=group.id)
    items = []
    for start in starts:
        finish = min(next_period(start, period) - timedelta(days=1), today)
        opening = carry
        high, low, event_count = carry, carry, 0
        day = max(start, earliest_day)
        while day <= finish:
            if day in by_day:
                item = by_day[day]
                high, low = max(high, item["high"]), min(low, item["low"])
                carry = item["close"]
                event_count += item["event_count"]
            if day == today:
                carry = Decimal(quote["price"])
                high, low = max(high, carry), min(low, carry)
            day += timedelta(days=1)
        items.append({"date": start.isoformat(), "end_date": finish.isoformat(),
                      "open": money(opening), "high": money(high), "low": money(low),
                      "close": money(carry), "event_count": event_count,
                      "ongoing": next_period(start, period) - timedelta(days=1) >= today})
    return {"period": period, "items": items,
            "next_before": starts[0].isoformat() if starts[0] > earliest_start else None,
            "quote": quote, "history_start": earliest.isoformat()}
