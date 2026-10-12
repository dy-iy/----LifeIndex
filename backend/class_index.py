"""Deterministic, owned class settlements and real-event market read models.

Callers decide whether evidence is eligible and hold their transaction open. This
module additionally locks User, never commits, and never accepts model prices.
"""
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import aliased, load_only

from auth import CurrentUser, Db
from index_models import (ClassIndexAccount, ClassIndexEvent, ClassIndexReceipt,
                          ClassMarketState, ClassMarketDay, ClassMarketPoint)
import market_math
import manager_signal
from market_engine import advance, record_point, market_mode
from models import Lesson, User
from rules import (CENT, DEFAULT_TIMEZONE, INITIAL_PRICE, RULE_VERSION, attendance_result,
                   correction_preview, daily_bounds, price_preview)

router = APIRouter()
ZERO = Decimal(0)
RULE_SNAPSHOT = {
    "version": RULE_VERSION, "initial_price": "1000.00", "minimum_price": "1.00",
    "lesson_limit": "0.03", "daily_limit": "0.10", "makeup_duration_cap": "0.8",
    "duration_weight": "0.4", "quality_weight": "0.6", "neutral_score": "60",
    "price_rounding": "ROUND_HALF_UP", "daily_lower_rounding": "ROUND_CEILING",
    "daily_upper_rounding": "ROUND_FLOOR", "clipped_corrections_carry_forward": False,
}


def utcnow():
    return datetime.now(timezone.utc)


def money(value, *, signed=False):
    value = Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
    if not value:
        value = abs(value)
    return format(value, "+.2f" if signed else ".2f")


def decimal_text(value, places=12):
    if value is None:
        return None
    value = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(value, "f").rstrip("0").rstrip(".") if value else "0"


def percent(points, before):
    return money(Decimal(points) / Decimal(before) * 100, signed=True)


def json_default(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError("Unsupported settlement evidence type")


def event_result(event, *, detail=False):
    change = money(event.actual_points, signed=True)
    label = "上涨" if event.actual_points > 0 else "下跌" if event.actual_points < 0 else "持平"
    action = "历史课程纠错" if event.revision else "本节课程结算"
    summary = f"{action}后指数{label}，实际变化 {change} 点，现价 {money(event.price_after)}。"
    if event.clipped_points:
        summary += f"当日涨跌停限制截断 {money(event.clipped_points, signed=True)} 点，该部分不结转。"
    if event.revision:
        summary += f"原目标 {money(event.previous_target_points, signed=True)} 点，替代目标 {money(event.target_points, signed=True)} 点；历史行情保持原记录。"
    result = {
        "id": str(event.id), "lesson_id": str(event.lesson_id), "sequence": event.sequence,
        "revision": event.revision, "kind": event.kind, "is_correction": event.revision > 0,
        "business_date": event.business_date.isoformat(), "timezone": event.timezone,
        "created_at": event.created_at.astimezone(timezone.utc).isoformat(),
        "lesson_name": event.lesson_name, "price_before": money(event.price_before),
        "price_after": money(event.price_after), "previous_close": money(event.previous_close),
        "change_points": change, "change_percent": percent(event.actual_points, event.price_before),
        "raw_points": decimal_text(event.raw_points), "clipped_points": money(event.clipped_points, signed=True),
        "rounding_points": decimal_text(event.rounding_points), "raw_rate": decimal_text(event.raw_rate, 18),
        "quality": decimal_text(event.quality, 4), "duration_ratio": decimal_text(event.duration_ratio, 18),
        "participation_score": decimal_text(event.participation_score, 18), "makeup": event.makeup,
        "rule_version": event.rule_version,
        "message_rule_version": event.rule_snapshot.get("message_rule", {}).get("version", event.rule_version),
        "original_event_id": str(event.original_event_id) if event.original_event_id else None,
        "previous_event_id": str(event.previous_event_id) if event.previous_event_id else None,
        "target_points": money(event.target_points, signed=True),
        "previous_target_points": money(event.previous_target_points, signed=True),
        "manager_summary": summary,
    }
    result.update({
            "source_key": event.source_key, "effective_seconds": event.effective_seconds,
            "planned_seconds": event.planned_seconds,
            "lesson_starts_at": event.lesson_starts_at.isoformat(), "lesson_ends_at": event.lesson_ends_at.isoformat(),
            "original_price_before": money(event.original_price_before),
            "original_previous_close": money(event.original_previous_close),
            "cumulative_actual_points": money(event.cumulative_actual_points, signed=True),
            "cumulative_discarded_points": money(event.cumulative_discarded_points, signed=True),
        })
    if detail:
        result.update(evidence=event.evidence, rule_snapshot=event.rule_snapshot)
    return result


def settle_lesson(db, user_id, lesson_id, source_key, kind, effective_seconds=0,
                  planned_seconds=0, quality=None, makeup=False, evidence=None, now=None, manager_reports=None):
    """Append once under the user mutex; corrections always value original terms.

    Eligibility/time limits and trusted evaluation provenance belong to the
    calling workflow. Arbitrary client scores must never reach this function.
    None denotes an excluded course that has never affected the index.
    """
    if kind not in ("attendance", "absence", "excluded"):
        raise ValueError("Unknown settlement kind")
    if not isinstance(source_key, str) or not source_key.strip() or len(source_key) > 200:
        raise ValueError("Invalid settlement source key")
    if not isinstance(makeup, bool) or any(type(value) is not int or not 0 <= value <= 2147483647 for value in (effective_seconds, planned_seconds)):
        raise ValueError("Settlement durations must be nonnegative integer seconds")
    now = now or utcnow()
    if now.tzinfo is None:
        raise ValueError("Settlement time must be timezone-aware")
    signal = manager_signal.combine(manager_reports) if manager_reports is not None else None
    if signal and (kind != "attendance" or quality is not None):
        raise ValueError("Manager signals replace learning scores only")
    participation = attendance_result(effective_seconds, planned_seconds, quality, makeup=makeup) if kind == "attendance" and signal is None else None
    if participation and participation.quality != participation.quality.quantize(Decimal("0.0001")):
        raise ValueError("Quality must use at most four decimal places")
    rate = Decimal(signal["raw_rate"]) if signal else participation.raw_rate if participation else Decimal("-0.03") if kind == "absence" else ZERO
    payload = {"lesson_id": str(lesson_id), "kind": kind, "effective_seconds": effective_seconds,
               "planned_seconds": planned_seconds, "quality": decimal_text(participation.quality, 4) if participation else None,
               "makeup": makeup, "evidence": evidence or {}}
    if signal:
        payload["manager_signal"] = signal
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=json_default)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(404, "账户不存在。")
    receipt = db.get(ClassIndexReceipt, (user_id, source_key), populate_existing=True)
    if receipt:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, "结算来源已用于不同的评估版本。")
        return event_result(db.get(ClassIndexEvent, receipt.event_id)) if receipt.event_id else None
    from timetable_lifecycle import learning_lesson
    lesson = learning_lesson(db, user_id, lesson_id)
    previous = db.scalar(select(ClassIndexEvent).where(ClassIndexEvent.user_id == user_id, ClassIndexEvent.lesson_id == lesson_id)
                         .order_by(ClassIndexEvent.revision.desc()).limit(1))
    if kind == "excluded" and previous is None:
        db.add(ClassIndexReceipt(user_id=user_id, source_key=source_key, fingerprint=fingerprint, event_id=None))
        db.flush()
        return None
    zone = user.timezone or DEFAULT_TIMEZONE
    local_date = now.astimezone(ZoneInfo(zone)).date()
    account = db.get(ClassIndexAccount, user_id, populate_existing=True)
    state = db.get(ClassMarketState, user_id)
    if state:
        advance(db, state, account, now, exact=True, max_steps=None)
        now, zone, local_date = state.cursor_at, state.timezone, account.business_date
    if account is None:
        account = ClassIndexAccount(user_id=user_id, price=INITIAL_PRICE, previous_close=INITIAL_PRICE,
                                    business_date=local_date, timezone=zone, sequence=0)
        db.add(account)
    # Timezone changes do not move existing events backwards to a closed day.
    business_date = max(local_date, account.business_date)
    if business_date > account.business_date:
        account.previous_close = market_math.display(account.price)
        account.business_date = business_date
    account.timezone = zone
    original = db.get(ClassIndexEvent, previous.original_event_id) if previous and previous.original_event_id else previous
    if original and original.rule_version not in (RULE_VERSION, market_math.VERSION):
        raise HTTPException(409, "该历史规则版本暂不支持自动纠错。")
    version = original.rule_version if original else market_math.VERSION if state else RULE_VERSION
    if previous:
        original_preview = market_math.jump if version == market_math.VERSION else price_preview
        target = ZERO if kind == "excluded" else original_preview(original.price_before, original.previous_close, rate).actual_points
        preview = (market_math.apply_points(account.price, account.previous_close, target-previous.target_points)
                   if state else correction_preview(account.price, account.previous_close, previous.target_points, target))
    else:
        preview = (market_math.jump if state else price_preview)(account.price, account.previous_close, rate)
        target = preview.actual_points
    displayed_points = market_math.display(preview.after)-market_math.display(preview.before)
    if state:
        record_point(db, state, account, now, "message_before")
        before_sequence = state.sequence
    event = ClassIndexEvent(
        id=uuid.uuid4(), user_id=user_id, lesson_id=lesson_id, source_key=source_key,
        sequence=account.sequence + 1, revision=previous.revision + 1 if previous else 0,
        kind=kind, business_date=business_date, timezone=zone, created_at=now.astimezone(timezone.utc),
        lesson_name=lesson.name, lesson_starts_at=lesson.starts_at, lesson_ends_at=lesson.ends_at,
        price_before=preview.before, price_after=preview.after, previous_close=account.previous_close,
        raw_points=preview.raw_points, actual_points=displayed_points, clipped_points=preview.clipped_points,
        rounding_points=preview.rounding_points, raw_rate=rate,
        quality=participation.quality if participation else None,
        duration_ratio=participation.duration_ratio if participation else None,
        participation_score=participation.score if participation else None,
        effective_seconds=effective_seconds, planned_seconds=planned_seconds, makeup=makeup,
        rule_version=version,
        rule_snapshot=({**(original.rule_snapshot if original else market_math.SNAPSHOT if state else RULE_SNAPSHOT),
                        "message_rule": manager_signal.SNAPSHOTS[signal["version"]]} if signal else
                       {key: value for key, value in (original.rule_snapshot if original else market_math.SNAPSHOT if state else RULE_SNAPSHOT).items()
                        if key != "message_rule"}),
        evidence=json.loads(encoded)["evidence"],
        original_event_id=original.id if original else None, previous_event_id=previous.id if previous else None,
        target_points=target, previous_target_points=previous.target_points if previous else ZERO,
        original_price_before=original.price_before if original else preview.before,
        original_previous_close=original.previous_close if original else account.previous_close,
        cumulative_actual_points=(previous.cumulative_actual_points if previous else ZERO) + displayed_points,
        cumulative_discarded_points=(previous.cumulative_discarded_points + preview.clipped_points) if previous else ZERO,
    )
    account.price = preview.after
    account.sequence = event.sequence
    db.add(event)
    db.flush()
    if state:
        db.execute(update(ClassMarketPoint).where(ClassMarketPoint.user_id == user_id,
                   ClassMarketPoint.sequence == before_sequence).values(event_id=event.id))
        record_point(db, state, account, now, "message_after", event_id=event.id)
    db.add(ClassIndexReceipt(user_id=user_id, source_key=source_key, fingerprint=fingerprint, event_id=event.id))
    db.flush()
    db.refresh(event)  # Response-loss retries return precisely the stored decimals.
    return event_result(event)


def market_context(db, user, now):
    state = db.get(ClassMarketState, user.id)
    account = db.get(ClassIndexAccount, user.id)
    if state:
        return account, state, account.business_date, account.price, account.previous_close
    today = now.astimezone(ZoneInfo(user.timezone)).date()
    if account is None:
        return None, None, today, INITIAL_PRICE, INITIAL_PRICE
    today = max(today, account.business_date)
    previous_close = account.previous_close if today == account.business_date else account.price
    return account, None, today, account.price, previous_close


@router.get("/api/indices/class")
def get_index(user: CurrentUser, db: Db):
    return quote_result(db, user, utcnow())


def quote_result(db, user, now, context=None):
    account, state, today, price, previous_close = context or market_context(db, user, now)
    lower, upper = daily_bounds(previous_close)
    quote_at = state.cursor_at if state else now
    result = {"code": "class", "name": "上课指数", "price": money(price), "previous_close": money(previous_close),
            "change_points": money(market_math.display(price) - previous_close, signed=True), "change_percent": percent(market_math.display(price) - previous_close, previous_close),
            "lower_limit": money(lower), "upper_limit": money(upper), "business_date": today.isoformat(),
            "timezone": state.timezone if state else user.timezone, "event_count": account.sequence if account else 0,
            "rule_version": state.rule_version if state else RULE_VERSION,
            "server_now": now.isoformat(), "quote_at": quote_at.isoformat(),
            "activated": state is not None, "market_state": market_mode(state, max(now, quote_at)) if state else "dormant",
            "market_sequence": state.sequence if state else 0, "catching_up": bool(state and now-quote_at >= market_math.STEP)}
    if state:
        day = db.get(ClassMarketDay, (user.id, today))
        result.update({name: money(getattr(day, name)) for name in ("open", "high", "low")})
    else:
        high, low = db.execute(select(func.max(ClassIndexEvent.price_after), func.min(ClassIndexEvent.price_after)).where(
            ClassIndexEvent.user_id == user.id, ClassIndexEvent.business_date == today)).one()
        result.update(open=money(previous_close), high=money(max(previous_close, high) if high is not None else previous_close),
                      low=money(min(previous_close, low) if low is not None else previous_close))
    return result


@router.get("/api/indices/class/events")
def list_events(user: CurrentUser, db: Db, limit: Annotated[int, Query(ge=1, le=50)] = 20,
                cursor: Annotated[str | None, Query(max_length=30)] = None):
    query = select(ClassIndexEvent).options(load_only(*[
        getattr(ClassIndexEvent, column.key) for column in ClassIndexEvent.__mapper__.column_attrs
        if column.key not in {"evidence"}
    ])).where(ClassIndexEvent.user_id == user.id)
    if cursor is not None:
        if not cursor.isascii() or not cursor.isdecimal() or int(cursor) <= 0:
            raise HTTPException(422, "行情游标无效，请刷新。")
        query = query.where(ClassIndexEvent.sequence < int(cursor))
    rows = db.scalars(query.order_by(ClassIndexEvent.sequence.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    return {"items": [event_result(event) for event in page],
            "next_cursor": str(page[-1].sequence) if len(rows) > limit else None}


@router.get("/api/indices/class/events/{event_id}")
def get_event(event_id: uuid.UUID, user: CurrentUser, db: Db):
    event = db.scalar(select(ClassIndexEvent).where(ClassIndexEvent.user_id == user.id, ClassIndexEvent.id == event_id))
    if event is None:
        raise HTTPException(404, "结算记录不存在。")
    return event_result(event, detail=True)


def period_start(day, period):
    return day - timedelta(days=day.weekday()) if period == "week" else day.replace(day=1) if period == "month" else day


def next_period(day, period):
    if period == "month":
        return day.replace(year=day.year + 1, month=1) if day.month == 12 else day.replace(month=day.month + 1)
    return day + timedelta(days=7 if period == "week" else 1)


@router.get("/api/indices/class/candles")
def get_candles(user: CurrentUser, db: Db, period: Literal["day", "week", "month"] = "day",
                limit: Annotated[int, Query(ge=1, le=366)] = 120, before: date | None = None,
                since: date | None = None):
    if before is not None and since is not None:
        raise HTTPException(422, "历史翻页与当前周期刷新不能同时使用。")
    now = utcnow()
    context = market_context(db, user, now)
    if since is not None and since > context[2]:
        raise HTTPException(422, "不能查询未来 K 线。")
    return {**candles_result(db, user, period, limit, before, context=context, since=since),
            "quote": quote_result(db, user, now, context)}


def candles_result(db, user, period, limit, before, *, context=None, since=None):
    _, _, today, _, _ = context or market_context(db, user, utcnow())
    first = db.scalar(select(ClassIndexEvent).options(load_only(ClassIndexEvent.business_date, ClassIndexEvent.sequence, ClassIndexEvent.price_after)).where(ClassIndexEvent.user_id == user.id).order_by(ClassIndexEvent.sequence).limit(1))
    first_day = db.scalar(select(ClassMarketDay).where(ClassMarketDay.user_id == user.id)
                          .order_by(ClassMarketDay.business_date).limit(1))
    if first is None and first_day is None:
        return {"period": period, "items": [], "next_before": None}
    earliest = period_start(min(item.business_date for item in (first, first_day) if item), period)
    if before is not None and before <= earliest:
        return {"period": period, "items": [], "next_before": None}
    # before is an exclusive candle start date; never fabricate pre-history bars.
    end = min(today, before - timedelta(days=1)) if before else today
    end_start = period_start(end, period)
    if before and end_start >= before:
        end_start = period_start(end_start - timedelta(days=1), period)
    starts = []
    current = end_start
    floor_date = max(earliest, period_start(since, period)) if since else earliest
    while current >= floor_date and len(starts) < limit:
        starts.append(current)
        current = period_start(current - timedelta(days=1), period)
    if not starts:
        return {"period": period, "items": [], "next_before": None}
    starts.reverse()
    floor, ceiling = starts[0], min(next_period(starts[-1], period) - timedelta(days=1), today)
    prior = db.scalar(select(ClassIndexEvent).options(load_only(ClassIndexEvent.business_date, ClassIndexEvent.sequence, ClassIndexEvent.price_after)).where(ClassIndexEvent.user_id == user.id, ClassIndexEvent.business_date < floor)
                      .order_by(ClassIndexEvent.sequence.desc()).limit(1))
    close = prior.price_after if prior else INITIAL_PRICE
    prior_day = db.scalar(select(ClassMarketDay).where(ClassMarketDay.user_id == user.id,
                        ClassMarketDay.business_date < floor).order_by(ClassMarketDay.business_date.desc()).limit(1))
    if prior_day and (not prior or prior_day.business_date >= prior.business_date):
        close = prior_day.close
    rows = db.scalars(select(ClassIndexEvent).options(load_only(ClassIndexEvent.business_date, ClassIndexEvent.sequence, ClassIndexEvent.price_after)).where(ClassIndexEvent.user_id == user.id,
                      ClassIndexEvent.business_date >= floor, ClassIndexEvent.business_date <= ceiling)
                      .order_by(ClassIndexEvent.sequence)).all()
    by_period = {}
    for event in rows:
        by_period.setdefault(period_start(event.business_date, period), []).append(event)
    market_days = db.scalars(select(ClassMarketDay).where(ClassMarketDay.user_id == user.id,
                             ClassMarketDay.business_date >= floor, ClassMarketDay.business_date <= ceiling)
                             .order_by(ClassMarketDay.business_date)).all()
    days_by_period = {}
    for day in market_days:
        days_by_period.setdefault(period_start(day.business_date, period), {})[day.business_date] = day
    items = []
    for start in starts:
        events = by_period.get(start, [])
        opening = close
        prices = [opening]
        continuous = days_by_period.get(start, {})
        dates = sorted(set(continuous) | {event.business_date for event in events})
        for day in dates:
            if day in continuous:
                bar = continuous[day]
                prices.extend((bar.open, bar.high, bar.low, bar.close))
                close = bar.close
            else:
                values = [event.price_after for event in events if event.business_date == day]
                prices.extend(values)
                close = values[-1]
        end_date = next_period(start, period) - timedelta(days=1)
        items.append({"date": start.isoformat(), "end_date": min(end_date, today).isoformat(),
                      "open": money(opening), "high": money(max(prices)), "low": money(min(prices)),
                      "close": money(close), "event_count": len(events), "ongoing": end_date >= today})
    return {"period": period, "items": items,
            "next_before": starts[0].isoformat() if starts[0] > earliest else None}


@router.get("/api/indices/class/intraday")
def get_intraday(user: CurrentUser, db: Db, selected_date: Annotated[date | None, Query(alias="date")] = None,
                 cursor: Annotated[str | None, Query(max_length=40)] = None):
    return intraday_result(db, user, selected_date, cursor=cursor)


def intraday_result(db, user, selected_date, *, cursor=None):
    now = utcnow()
    context = market_context(db, user, now)
    _, _, today, _, _ = context
    day = selected_date or today
    if day > today:
        raise HTTPException(422, "不能查询未来分时。")
    replace_from = None
    floor_sequence = 0
    if cursor:
        try:
            stamp, sequence = cursor.split(":", 1)
            cursor_day = date.fromisoformat(stamp)
            if not sequence.isascii() or not sequence.isdecimal() or not 0 < int(sequence) <= 9223372036854775807:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "分时游标无效，请重新读取行情。") from None
        if cursor_day == day:
            point = db.get(ClassMarketPoint, (user.id, int(sequence)))
            if point is None or point.business_date != day:
                raise HTTPException(422, "分时游标无效，请重新读取行情。")
            # The last minute is mutable: replace it, including old extrema
            # which may no longer be selected after another tick arrives.
            replace_from = point.occurred_at.replace(second=0, microsecond=0)
            preceding = db.scalar(select(ClassMarketPoint.sequence).where(
                ClassMarketPoint.user_id == user.id, ClassMarketPoint.business_date == day,
                ClassMarketPoint.occurred_at < replace_from,
            ).order_by(ClassMarketPoint.sequence.desc()).limit(1))
            floor_sequence = (preceding or 0) + 1
    bar = db.get(ClassMarketDay, (user.id, day))
    previous = None if bar else db.scalar(select(ClassIndexEvent.price_after).where(
        ClassIndexEvent.user_id == user.id, ClassIndexEvent.business_date < day,
    ).order_by(ClassIndexEvent.sequence.desc()).limit(1))
    minute = func.date_trunc("minute", ClassMarketPoint.occurred_at)
    sequence = ClassMarketPoint.sequence
    ranked = select(ClassMarketPoint,
        func.row_number().over(partition_by=minute, order_by=sequence).label("first"),
        func.row_number().over(partition_by=minute, order_by=sequence.desc()).label("last"),
        func.row_number().over(partition_by=minute, order_by=(ClassMarketPoint.price, sequence)).label("low"),
        func.row_number().over(partition_by=minute, order_by=(ClassMarketPoint.price.desc(), sequence)).label("high"),
    ).where(ClassMarketPoint.user_id == user.id, ClassMarketPoint.business_date == day,
            sequence >= floor_sequence).subquery()
    sample = aliased(ClassMarketPoint, ranked)
    rows = db.scalars(select(sample).where(or_(ranked.c.first == 1, ranked.c.last == 1,
        ranked.c.low == 1, ranked.c.high == 1, ranked.c.kind != "tick")).order_by(ranked.c.sequence)).all()
    query = select(ClassIndexEvent).options(load_only(ClassIndexEvent.id, ClassIndexEvent.created_at,
        ClassIndexEvent.timezone, ClassIndexEvent.price_after, ClassIndexEvent.lesson_name,
        ClassIndexEvent.actual_points, ClassIndexEvent.kind)).where(
        ClassIndexEvent.user_id == user.id, ClassIndexEvent.business_date == day)
    if replace_from is not None:
        query = query.where(ClassIndexEvent.created_at >= replace_from)
    events = db.scalars(query.order_by(ClassIndexEvent.sequence)).all()
    event_map = {event.id: event for event in events}
    linked = {row.event_id for row in rows if row.event_id}
    zone = bar.timezone if bar else events[0].timezone if events else user.timezone
    points = [{"id": str(event.id), "event_id": str(event.id),
               "time": event.created_at.astimezone(ZoneInfo(event.timezone)).isoformat(),
               "price": money(event.price_after), "lesson_name": event.lesson_name,
               "change_points": money(event.actual_points, signed=True), "kind": event.kind}
              for event in events if event.id not in linked]
    for row in rows:
        event = event_map.get(row.event_id)
        points.append({"id": f"sample-{row.sequence}", "sequence": row.sequence,
                       "event_id": str(row.event_id) if row.event_id else None,
                       "time": row.occurred_at.astimezone(ZoneInfo(zone)).isoformat(), "price": money(row.price),
                       "kind": row.kind, "lesson_name": event.lesson_name if event else "",
                       "change_points": money(event.actual_points, signed=True) if event else None})
    points.sort(key=lambda row: (datetime.fromisoformat(row["time"]), row.get("sequence", 0)))
    quote = quote_result(db, user, now, context)
    result = {"date": day.isoformat(), "timezone": zone,
              "previous_close": money(bar.previous_close if bar else previous if previous is not None else INITIAL_PRICE),
              "points": points, "ongoing": day == today,
              "sample_count": bar.sample_count if bar else 0,
              "continuous": bool(bar), "quote_at": quote["quote_at"] if quote["activated"] else None,
              "cursor": f"{day.isoformat()}:{rows[-1].sequence}" if rows else None,
              "replace_from": replace_from.isoformat() if replace_from is not None else None,
              "quote": quote}
    if bar:
        result.update({name: money(getattr(bar, name)) for name in ("open", "high", "low", "close")})
    return result
