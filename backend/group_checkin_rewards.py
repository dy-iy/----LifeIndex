"""Immutable first-full-check-in awards; all writes hold the group lock."""
from decimal import Decimal, ROUND_HALF_UP, localcontext

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from study_group_models import StudyGroupCheckinReward, StudyGroupEvent

CHECKIN_REWARD_RATE = Decimal("0.03")
CHECKIN_REWARD_RULE = "group-full-checkin-previous-close-3pct-v1"
POINT_UNIT = Decimal("0.00000001")


def reward_result(reward):
    """The award snapshot stays stable even if today's active roster changes."""
    if reward is None:
        return None
    return dict(awarded_at=reward.awarded_at,
                points=format(reward.reward_points, ".8f"),
                previous_close=format(reward.previous_close, ".8f"),
                rate=format(reward.rate, ".2f"),
                member_count=reward.member_count)


def award_full_checkin(db, group, member_ids, local_date, now):
    """Called only after a new check-in makes the locked current roster full."""
    if not member_ids or db.get(StudyGroupCheckinReward, (group.id, local_date)) is not None:
        return
    # Import inside the write path: group_index imports group list helpers,
    # while this award module is also read by the check-in API.
    from group_index import ZONE, history_catching_up, membership_intervals, midnight, previous_close

    events = db.scalars(select(StudyGroupEvent).where(StudyGroupEvent.group_id == group.id)
                        .order_by(StudyGroupEvent.version)).all()
    earliest, intervals = membership_intervals(events)
    earliest = earliest or group.created_at
    if local_date > earliest.astimezone(ZONE).date():
        cutoff = midnight(local_date)
        prior_intervals = [interval for interval in intervals if interval.start < cutoff]
        # The market worker can briefly lag midnight (including its partial
        # closing tick). A frozen award must use a fully closed previous day.
        # Raising here rolls back the new check-in and its receipt atomically;
        # never lock other members' users while already holding this group.
        if history_catching_up(db, prior_intervals, cutoff):
            raise HTTPException(409, "昨日行情正在同步，请稍后再打卡。")
    with localcontext() as context:
        context.prec = 48
        previous = Decimal(previous_close(db, intervals, earliest, local_date,
                                          group.initial_price, group_id=group.id))
        previous = previous.quantize(POINT_UNIT, rounding=ROUND_HALF_UP)
        points = (previous * CHECKIN_REWARD_RATE).quantize(POINT_UNIT, rounding=ROUND_HALF_UP)
    db.execute(insert(StudyGroupCheckinReward).values(
        group_id=group.id, local_date=local_date, awarded_at=now,
        previous_close=previous, reward_points=points, rate=CHECKIN_REWARD_RATE,
        member_ids=[str(member_id) for member_id in member_ids], member_count=len(member_ids),
        rule_version=CHECKIN_REWARD_RULE)
        .on_conflict_do_nothing(index_elements=["group_id", "local_date"]))
