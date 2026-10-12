"""Persistent continuous market. Callers hold the User lock and own the transaction.

Maintenance closes only canonical five-second ticks. Accepted business mutations may
split a tick. Historical timer evidence never changes these immutable actions.
"""
from datetime import datetime, time, timedelta, timezone
import logging
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.exc import SQLAlchemyError

from index_models import (ClassIndexAccount, ClassIndexEvent, ClassMarketAction,
                          ClassMarketDay, ClassMarketPoint, ClassMarketState)
from diagnostics import log_failure
from market_math import (MOOD_UNIT, SNAPSHOT, STEP, VERSION, display, normals, step)
from rules import INITIAL_PRICE
from models import StudySession

DEFAULT_BATCH_STEPS = 4096
logger = logging.getLogger(__name__)


def market_mode(state, at):
    return "timer" if state.running and state.starts_at <= at < state.ends_at else "idle"


def ensure_account(db, user, now):
    account = db.get(ClassIndexAccount, user.id)
    if account is None:
        account = ClassIndexAccount(user_id=user.id, price=INITIAL_PRICE, previous_close=INITIAL_PRICE,
                                    business_date=now.astimezone(ZoneInfo(user.timezone)).date(),
                                    timezone=user.timezone, sequence=0)
        db.add(account)
        db.flush()
    return account


def day_record(db, state, account):
    day = db.get(ClassMarketDay, (state.user_id, account.business_date))
    if day is None:
        # On the activation day include any pre-activation course settlements.
        legacy = db.scalars(select(ClassIndexEvent).where(
            ClassIndexEvent.user_id == state.user_id,
            ClassIndexEvent.business_date == account.business_date).order_by(ClassIndexEvent.sequence)).all()
        opening = account.previous_close if legacy else account.price
        values = [opening, account.price] + [event.price_after for event in legacy]
        day = ClassMarketDay(user_id=state.user_id, business_date=account.business_date,
                             timezone=state.timezone, previous_close=account.previous_close,
                             open=opening, high=max(values), low=min(values), close=account.price,
                             sample_count=0, event_count=len(legacy))
        db.add(day)
        db.flush()
    return day


def record_point(db, state, account, at, kind, *, event_id=None, day=None, buffer=None):
    day = day if day is not None else day_record(db, state, account)
    state.sequence += 1
    row = dict(user_id=state.user_id, sequence=state.sequence, occurred_at=at,
               business_date=account.business_date, price=account.price, kind=kind, event_id=event_id)
    if buffer is not None:
        buffer.append(row)
    else:
        db.execute(insert(ClassMarketPoint), [row])
    day.high, day.low, day.close = max(day.high, account.price), min(day.low, account.price), account.price
    day.sample_count += 1
    if kind == "message_after":
        day.event_count += 1


def advance(db, state, account, now, *, exact=False, max_steps=DEFAULT_BATCH_STEPS):
    """Advance a bounded prefix; exact mutations use max_steps=None.

No query-time partial tick: this is what makes poll frequency irrelevant.
"""
    if state.rule_version != VERSION or state.rule_snapshot != SNAPSHOT:
        raise ValueError("Unsupported continuous market rule snapshot")
    now = max(now, state.cursor_at)
    target = now if exact else max(state.cursor_at, state.activated_at + ((now-state.activated_at)//STEP)*STEP)
    day = day_record(db, state, account)
    rows, count = [], 0
    zone = ZoneInfo(state.timezone)
    while state.cursor_at < target and (max_steps is None or count < max_steps):
        start = state.cursor_at
        grid_end = state.activated_at + (((start-state.activated_at)//STEP)+1)*STEP
        midnight = datetime.combine(account.business_date+timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        end = min(target, grid_end, midnight)
        if state.running:
            for boundary in (state.starts_at, state.ends_at):
                if start < boundary < end:
                    end = boundary
        change, state.mood = step(account.price, account.previous_close, state.mood, state.seed,
                                  start, end, market_mode(state, start) == "timer")
        account.price, state.cursor_at = change.after, end
        record_point(db, state, account, end, "tick", day=day, buffer=rows)
        count += 1
        if end == midnight:
            account.previous_close = display(account.price)
            account.business_date += timedelta(days=1)
            # Flush the old day's closing candle before creating the next one.
            db.execute(insert(ClassMarketPoint), rows)
            rows.clear()
            db.flush()
            day = day_record(db, state, account)
            record_point(db, state, account, end, "open", day=day, buffer=rows)
        if len(rows) >= 1000:
            db.execute(insert(ClassMarketPoint), rows)
            rows.clear()
    if rows:
        db.execute(insert(ClassMarketPoint), rows)
    account.timezone = state.timezone
    db.flush()
    return state.cursor_at < target


def timer_action(db, user, item, now, operation):
    """Called only after a validated timer mutation, inside its idempotent write."""
    state = db.get(ClassMarketState, user.id)
    if state is None and operation not in ("start", "resume", "reconcile"):
        return  # Existing timers do not silently gain backdated activation.
    if state is not None and operation == "finish" and state.session_id != item.id:
        # Confirming an already-finished record (including a timed-out or
        # offline-corrected one) cannot stop a different course's live market.
        return
    source = f"timer:{item.id}:{item.version}:{operation}"
    if state is not None and db.scalar(select(ClassMarketAction.sequence).where(
            ClassMarketAction.user_id == user.id, ClassMarketAction.source_key == source)) is not None:
        return
    account = ensure_account(db, user, now)
    if state is None:
        day = max(account.business_date, now.astimezone(ZoneInfo(user.timezone)).date())
        if day > account.business_date:
            account.previous_close = display(account.price)
            account.business_date = day
        seed = secrets.token_hex(32)
        state = ClassMarketState(user_id=user.id, activated_at=now, cursor_at=now, sequence=0,
                                 seed=seed, mood=normals(seed, "initial")[0].quantize(MOOD_UNIT),
                                 rule_version=VERSION, rule_snapshot=SNAPSHOT, timezone=user.timezone,
                                 running=False)
        db.add(state)
        db.flush()
        account.timezone = state.timezone
        record_point(db, state, account, now, "activation")
    else:
        advance(db, state, account, now, exact=True, max_steps=None)
    now = state.cursor_at
    state.session_id = item.id
    state.running = item.phase == "running"
    state.starts_at, state.ends_at = item.starts_at, item.ends_at
    record_point(db, state, account, now, "state")
    db.add(ClassMarketAction(user_id=user.id, sequence=state.sequence, source_key=source,
                            occurred_at=now, kind=operation, snapshot={
                                "session_id": str(item.id), "running": state.running,
                                "starts_at": item.starts_at.isoformat(), "ends_at": item.ends_at.isoformat(),
                                "price": str(account.price), "previous_close": str(account.previous_close),
                                "business_date": account.business_date.isoformat(),
                                "mood": str(state.mood), "rule_version": state.rule_version,
                            }))
    db.flush()


def reconcile_active_timer(db, user, now):
    """Adopt a pre-upgrade active timer from now, never replay its old history.

    The caller holds the same user lock as timer mutations. Paused timers have
    already started too; they activate in idle mode. Finished/makeup records do
    not activate an otherwise dormant account.
    """
    state = db.get(ClassMarketState, user.id)
    if state is None:
        item = db.scalar(select(StudySession).where(
            StudySession.user_id == user.id, StudySession.phase.in_(["running", "paused"]),
            StudySession.is_makeup.is_(False), StudySession.voided.is_(False),
        ))
        if item is not None:
            timer_action(db, user, item, max(now, item.started_at), "reconcile")
            state = db.get(ClassMarketState, user.id)
    return state


def maintain_markets(factory, now=None, limit=100):
    """Fair bounded batches. Stale checkpoints sort first; locked owners are skipped."""
    from models import User
    now = now or datetime.now(timezone.utc)
    with factory() as db:
        pending = db.scalars(select(StudySession.user_id).where(
            StudySession.phase.in_(["running", "paused"]), StudySession.is_makeup.is_(False),
            StudySession.voided.is_(False),
            ~select(ClassMarketState.user_id).where(ClassMarketState.user_id == StudySession.user_id).exists(),
        ).order_by(StudySession.started_at).limit(limit)).all()
        owners = db.scalars(select(ClassMarketState.user_id).where(ClassMarketState.cursor_at <= now-STEP)
                            .order_by(ClassMarketState.cursor_at).limit(max(0, limit-len(pending)))).all()
    processed = 0
    for user_id in dict.fromkeys(pending + owners):
        try:
            with factory.begin() as db:
                owner = db.scalar(select(User).where(User.id == user_id).with_for_update(skip_locked=True))
                if owner is not None:
                    state = reconcile_active_timer(db, owner, now)
                    if state is not None:
                        account = db.get(ClassIndexAccount, user_id)
                        advance(db, state, account, now, max_steps=720)
                        processed += 1
        except (SQLAlchemyError, ValueError) as error:
            # An unavailable/corrupt account must not stall everybody's quotes.
            log_failure(logger, "Market maintenance transaction failed for user_id=%s; retrying next round.", error, user_id)
    return processed
