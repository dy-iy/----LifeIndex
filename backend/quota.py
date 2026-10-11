from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import ChatDailyUsage


def reserve_chat_request(db: Session, user_id, limit: int):
    """Charge an attempted provider request atomically, including failed requests.

    UTC dates are intentional: changing a user's display timezone must not reset
    an operational quota. This is a request count, not a currency cost estimate.
    """
    day = datetime.now(timezone.utc).date()
    statement = insert(ChatDailyUsage).values(user_id=user_id, usage_date=day, request_count=1)
    statement = statement.on_conflict_do_update(
        index_elements=[ChatDailyUsage.user_id, ChatDailyUsage.usage_date],
        set_={"request_count": ChatDailyUsage.request_count + 1},
        where=ChatDailyUsage.request_count < limit,
    ).returning(ChatDailyUsage.request_count)
    count = db.scalar(statement)
    db.commit()
    if count is None:
        raise HTTPException(429, "今日 AI 调用次数已达上限，请明日再试。")
    return count
