"""Freeze the creator's personal opening quote once, inside the user lock.

The stored baseline is independent of the changing group member mean. A delayed
personal midnight close is completed at that canonical boundary only; creating a
group never introduces a poll-dependent partial tick at the creation instant.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from class_index import market_context
from index_models import ClassIndexAccount, ClassMarketDay, ClassMarketState
from market_engine import advance
from market_math import display


@dataclass(frozen=True)
class GroupInitialOpen:
    price: Decimal
    calendar_date: date
    timezone: str
    source: str

    def event_payload(self):
        return {"initial_price": str(self.price), "initial_price_source": self.source,
                "initial_price_date": self.calendar_date.isoformat(),
                "initial_price_timezone": self.timezone}


def complete_creation_midnight(db, user, created_at):
    """Caller owns the same user row lock as personal market maintenance."""
    state = db.get(ClassMarketState, user.id)
    if state is None:
        return
    day = created_at.astimezone(ZoneInfo(state.timezone)).date()
    if db.get(ClassMarketDay, (user.id, day)) is not None:
        return
    boundary = datetime.combine(day, time.min, ZoneInfo(state.timezone)).astimezone(timezone.utc)
    account = db.get(ClassIndexAccount, user.id)
    if account is None or state.cursor_at >= boundary:
        raise HTTPException(409, "个人指数开盘数据尚未准备好，请稍后重试。")
    # Midnight is an existing engine boundary, including its terminal old-day
    # tick followed by the new-day opening record. Never advance to created_at.
    advance(db, state, account, boundary, exact=True, max_steps=None)


def resolve_group_initial_open(db, user, created_at):
    """Read the exact opening value shown by this creator's personal quote."""
    account, state, day, _, previous = market_context(db, user, created_at)
    zone = state.timezone if state is not None else user.timezone
    if state is not None:
        opening = db.get(ClassMarketDay, (user.id, day))
        if opening is None:
            raise HTTPException(409, "个人指数开盘数据尚未准备好，请稍后重试。")
        value, source = opening.open, "personal_market_day_open"
    else:
        # Dormant quotes render their previous close as the day's opening price,
        # including the initial 1000 when there has never been an index account.
        value = display(previous)
        source = "personal_quote_open" if account is not None else "personal_default_open"
    return GroupInitialOpen(Decimal(value), day, zone, source)
