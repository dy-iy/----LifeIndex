"""Executable specification for class-v1.0 (T01), not a ledger or API.

These pure functions accept validated evidence from future server workflows.
They never assess answer quality, trust client scores, or persist a settlement.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext

RULE_VERSION = "class-v1.0"
INITIAL_PRICE = Decimal("1000.00")
MIN_PRICE = Decimal("1.00")
LESSON_LIMIT = Decimal("0.03")
DAY_LIMIT = Decimal("0.10")
MAKEUP_DURATION_CAP = Decimal("0.8")
DEFAULT_TIMEZONE = "Asia/Shanghai"
INCOMPLETE_AFTER_HOURS = 48
CORRECTION_WINDOW_DAYS = 7
CENT = Decimal("0.01")


def _decimal(value: Decimal | int | str, name: str) -> Decimal:
    # Floats are deliberately rejected at the numerical settlement boundary.
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be Decimal, int or a decimal string")
    try:
        result = Decimal(value)
    except (ValueError, TypeError, ArithmeticError):
        raise ValueError(f"{name} is not a valid decimal") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _price(value: Decimal | int | str, name: str) -> Decimal:
    result = _decimal(value, name)
    if result < MIN_PRICE or result != result.quantize(CENT):
        raise ValueError(f"{name} must be at least 1.00 and use whole cents")
    return result


@dataclass(frozen=True)
class Participation:
    duration_ratio: Decimal
    quality: Decimal
    score: Decimal
    raw_rate: Decimal
    rule_version: str = RULE_VERSION


def attendance_result(
    effective_seconds: Decimal | int | str,
    planned_seconds: Decimal | int | str,
    quality: Decimal | int | str,
    *,
    makeup: bool = False,
) -> Participation:
    """Duration evidence must already be intersected with the lesson window.

    Quality is a stored, validated evaluation, never a request-body score.
    Missing review / unknown attendance has no result and must not call this.
    """
    effective = _decimal(effective_seconds, "effective_seconds")
    planned = _decimal(planned_seconds, "planned_seconds")
    quality = _decimal(quality, "quality")
    if planned <= 0 or effective < 0 or not 0 <= quality <= 100:
        raise ValueError("Duration or quality outside the accepted range")
    if not isinstance(makeup, bool):
        raise ValueError("makeup must be a boolean")
    with localcontext() as context:
        context.prec = 40
        ratio = min(effective / planned, MAKEUP_DURATION_CAP if makeup else Decimal(1))
        score = 40 * ratio + Decimal("0.6") * quality
        rate = max(-LESSON_LIMIT, min(LESSON_LIMIT, (score - 60) / 40 * LESSON_LIMIT))
        return Participation(ratio, quality, score, rate)


@dataclass(frozen=True)
class PricePreview:
    before: Decimal
    after: Decimal
    raw_points: Decimal
    actual_points: Decimal
    clipped_points: Decimal
    rounding_points: Decimal
    actual_rate: Decimal
    lower: Decimal
    upper: Decimal
    rule_version: str = RULE_VERSION


def daily_bounds(previous_close: Decimal | int | str) -> tuple[Decimal, Decimal]:
    close = _price(previous_close, "previous_close")
    with localcontext() as context:
        context.prec = 40
        lower = max(MIN_PRICE, close * (1 - DAY_LIMIT)).quantize(CENT, rounding=ROUND_CEILING)
        upper = (close * (1 + DAY_LIMIT)).quantize(CENT, rounding=ROUND_FLOOR)
        return lower, upper


def _apply_points(current: Decimal | int | str, previous_close: Decimal | int | str,
                  points: Decimal) -> PricePreview:
    current = _price(current, "current")
    lower, upper = daily_bounds(previous_close)
    if not lower <= current <= upper:
        raise ValueError("Current price is outside today's legal range")
    with localcontext() as context:
        context.prec = 40
        raw_after = current + points
        rounded = raw_after.quantize(CENT, rounding=ROUND_HALF_UP)
        after = max(lower, min(upper, rounded))
        actual = after - current
        # Signed discarded points are distinct from sub-cent rounding.
        return PricePreview(current, after, points, actual, rounded - after,
                            rounded - raw_after, actual / current, lower, upper)


def price_preview(current: Decimal | int | str, previous_close: Decimal | int | str,
                  raw_rate: Decimal | int | str) -> PricePreview:
    """Preview one lesson. Confirmed absence uses raw_rate=-LESSON_LIMIT."""
    current = _price(current, "current")
    rate = _decimal(raw_rate, "raw_rate")
    if abs(rate) > LESSON_LIMIT:
        raise ValueError("Lesson rate exceeds the versioned limit")
    with localcontext() as context:
        context.prec = 40
        return _apply_points(current, previous_close, current * rate)


def correction_preview(
    current: Decimal | int | str,
    previous_close: Decimal | int | str,
    latest_target_points: Decimal | int | str,
    replacement_target_points: Decimal | int | str,
) -> PricePreview:
    """Apply the difference between two successive revision targets today.

    Each target is the actual points of a re-evaluation at the ORIGINAL event's
    before-price, previous close and rule version. Never use today's price to
    value that lesson. The original event is revision zero.

    Advance the target even when today's adjustment is clipped. This discards
    clipping permanently: submitting the same target tomorrow cannot recover
    a previously discarded adjustment. The future ledger must also enforce
    unique input versions/idempotency keys under an account lock.
    """
    latest = _decimal(latest_target_points, "latest_target_points")
    replacement = _decimal(replacement_target_points, "replacement_target_points")
    if latest != latest.quantize(CENT) or replacement != replacement.quantize(CENT):
        raise ValueError("Revision targets must use whole cents")
    return _apply_points(current, previous_close, replacement - latest)
