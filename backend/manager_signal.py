"""Versioned manager sentiment -> bounded lesson message, using Decimal."""
from decimal import Decimal

VERSION = "class-signal-v1"
SNAPSHOT = {"version": VERSION, "manager_weights": {"long": "0.5", "short": "0.5"},
    "strength_maximum": 100, "lesson_limit": "0.03", "neutral_strength": 0,
    "duration_weight": "0", "quality_score_used": False,
    "formula": "0.03 * (signed_long + signed_short) / 200"}

SINGLE_VERSION = "class-signal-v2"
SINGLE_SNAPSHOT = {"version": SINGLE_VERSION, "manager_weights": {"long_short": "1"},
    "strength_maximum": 100, "lesson_limit": "0.03", "neutral_strength": 0,
    "duration_weight": "0", "quality_score_used": False,
    "formula": "0.03 * signed_long_short / 100"}
SNAPSHOTS = {VERSION: SNAPSHOT, SINGLE_VERSION: SINGLE_SNAPSHOT}


def combine(reports):
    if not isinstance(reports, dict) or set(reports) not in ({"long", "short"}, {"long_short"}):
        raise ValueError("A long/short report or a complete legacy pair is required")
    single = "long_short" in reports
    signs = {"bullish": 1, "bearish": -1, "neutral": 0}
    signed = {}
    for role, report in reports.items():
        if not isinstance(report, dict):
            raise ValueError("Invalid manager report")
        direction, strength = report.get("direction"), report.get("strength")
        if direction not in signs or type(strength) is not int or not 0 <= strength <= 100:
            raise ValueError("Invalid manager direction or strength")
        if (direction == "neutral") != (strength == 0):
            raise ValueError("Inconsistent neutral signal")
        signed[role] = signs[direction] * strength
    net = Decimal(signed["long_short"]) if single else Decimal(signed["long"] + signed["short"]) / 2
    rate = Decimal("0.03") * net / 100
    return {"version": SINGLE_VERSION if single else VERSION, "signed_strengths": signed, "net_strength": str(net),
        "direction": "bullish" if net > 0 else "bearish" if net < 0 else "neutral",
        "raw_rate": str(rate)}
