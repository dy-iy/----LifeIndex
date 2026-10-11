"""Versioned class-v2.0 mathematics. Randomness is keyed by canonical intervals.

Only Box–Muller's trigonometry uses binary64; all price arithmetic uses Decimal.
The algorithm identifier pins that choice. Quotes never choose new intervals.
"""
import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext

from rules import CENT, daily_bounds

VERSION = "class-v2.0"
ALGORITHM = "hmac-sha256-box-muller-binary64-decimal40-v1"
UNIT = Decimal("0.00000001")
MOOD_UNIT = Decimal("0.000000000000000000000001")
STEP = timedelta(seconds=5)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SNAPSHOT = {
    "version": VERSION, "algorithm": ALGORITHM, "step_seconds": 5,
    "timer_mu": "0.002", "idle_mu": "-0.0002", "timer_sigma": "0.003",
    "idle_sigma": "0.001", "sentiment_a": "0.001", "sentiment_tau_seconds": 60,
    "internal_places": 8, "mood_places": 24, "decimal_precision": 40,
    "lesson_limit": "0.03", "daily_limit": "0.10", "minimum_price": "1.00",
    "initial_price": "1000.00", "duration_weight": "0.4", "quality_weight": "0.6",
    "neutral_score": "60", "makeup_duration_cap": "0.8", "price_rounding": "ROUND_HALF_UP",
    "clipped_corrections_carry_forward": False,
}


def micros(at):
    delta = at - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def display(value):
    with localcontext() as context:
        context.prec = 40
        return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def normals(seed, key):
    digest = hmac.new(bytes.fromhex(seed), (ALGORITHM + ":" + key).encode(), hashlib.sha256).digest()
    # Exactly representable binary64 uniforms; never round to zero or one.
    u = ((int.from_bytes(digest[:8], "big") >> 12) + 0.5) / 2**52
    v = ((int.from_bytes(digest[8:16], "big") >> 12) + 0.5) / 2**52
    radius, angle = math.sqrt(-2 * math.log(u)), 2 * math.pi * v
    return Decimal(str(radius * math.cos(angle))), Decimal(str(radius * math.sin(angle)))


@dataclass(frozen=True)
class Change:
    before: Decimal
    after: Decimal
    raw_points: Decimal
    actual_points: Decimal
    clipped_points: Decimal
    rounding_points: Decimal


def apply_points(price, previous_close, points):
    with localcontext() as context:
        context.prec = 40
        lower, upper = daily_bounds(previous_close)
        if not lower <= price <= upper or not points.is_finite():
            raise ValueError("Invalid continuous market price or adjustment")
        raw = price + points
        rounded = raw.quantize(UNIT, rounding=ROUND_HALF_UP)
        after = min(upper, max(lower, rounded))
        return Change(price, after, points, after-price, rounded-after, rounded-raw)


def jump(price, previous_close, rate):
    if not rate.is_finite() or abs(rate) > Decimal("0.03"):
        raise ValueError("Lesson message exceeds its versioned limit")
    with localcontext() as context:
        context.prec = 40
        return apply_points(price, previous_close, price * rate)


def step(price, previous_close, mood, seed, start, end, timer):
    if not start < end or end-start > STEP:
        raise ValueError("Market steps must be positive and at most five seconds")
    z, noise = normals(seed, f"{micros(start)}:{micros(end)}")
    with localcontext() as context:
        context.prec = 40
        seconds = Decimal(micros(end)-micros(start)) / 1000000
        dt = seconds / 3600
        rho = (-seconds/60).exp()
        mood = (rho*mood + (1-rho*rho).sqrt()*z).quantize(MOOD_UNIT, rounding=ROUND_HALF_UP)
        mu, sigma = (Decimal("0.002"), Decimal("0.003")) if timer else (Decimal("-0.0002"), Decimal("0.001"))
        rate = mu*dt + Decimal("0.001")*mood*dt + sigma*dt.sqrt()*noise
        change = apply_points(price, previous_close, price * (rate.exp()-1))
        return change, mood
