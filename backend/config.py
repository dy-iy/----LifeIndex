"""Environment configuration. Secrets are never included in API responses."""
import os
from dataclasses import dataclass


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    session_ttl_seconds: int = 30 * 24 * 60 * 60
    ai_daily_request_limit: int = 20  # Evaluation and legacy chat only.
    schedule_input_rate: int = 2
    schedule_output_rate: int = 8

    evaluation_daily_budget_micro: int = 1_000_000
    evaluation_input_rate: int = 2
    evaluation_output_rate: int = 8

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            wechat_app_id=os.getenv("WECHAT_APP_ID", "").strip(),
            wechat_app_secret=os.getenv("WECHAT_APP_SECRET", "").strip(),
            session_ttl_seconds=positive_int("SESSION_TTL_SECONDS", 30 * 24 * 60 * 60),
            ai_daily_request_limit=positive_int("AI_DAILY_REQUEST_LIMIT", 20),
            schedule_input_rate=positive_int("SCHEDULE_INPUT_CNY_PER_MILLION", 2),
            schedule_output_rate=positive_int("SCHEDULE_OUTPUT_CNY_PER_MILLION", 8),
            evaluation_daily_budget_micro=positive_int("EVALUATION_DAILY_BUDGET_MICRO_CNY", 1_000_000),
            evaluation_input_rate=positive_int("EVALUATION_INPUT_CNY_PER_MILLION", 2),
            evaluation_output_rate=positive_int("EVALUATION_OUTPUT_CNY_PER_MILLION", 8),
        )
