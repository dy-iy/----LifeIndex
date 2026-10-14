"""Environment configuration. Secrets are never included in API responses."""
import os
from dataclasses import dataclass, field


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None


def boolean_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError(f"{name} must be true, false, 1 or 0")
    return value in ("true", "1")


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    session_ttl_seconds: int = 30 * 24 * 60 * 60
    ai_daily_request_limit: int = 20  # Evaluation only.
    schedule_input_rate: int = 6
    schedule_output_rate: int = 30

    evaluation_daily_budget_micro: int = 1_000_000
    evaluation_input_rate: int = 2
    evaluation_output_rate: int = 8
    # Keep reads available while migrations and the client rollout are verified.
    group_management_enabled: bool = False
    group_invite_key_id: str = "v1"
    group_invite_signing_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            wechat_app_id=os.getenv("WECHAT_APP_ID", "").strip(),
            wechat_app_secret=os.getenv("WECHAT_APP_SECRET", "").strip(),
            session_ttl_seconds=positive_int("SESSION_TTL_SECONDS", 30 * 24 * 60 * 60),
            ai_daily_request_limit=positive_int("AI_DAILY_REQUEST_LIMIT", 20),
            schedule_input_rate=positive_int("SCHEDULE_INPUT_CNY_PER_MILLION", 6),
            schedule_output_rate=positive_int("SCHEDULE_OUTPUT_CNY_PER_MILLION", 30),
            evaluation_daily_budget_micro=positive_int("EVALUATION_DAILY_BUDGET_MICRO_CNY", 1_000_000),
            evaluation_input_rate=positive_int("EVALUATION_INPUT_CNY_PER_MILLION", 2),
            evaluation_output_rate=positive_int("EVALUATION_OUTPUT_CNY_PER_MILLION", 8),
            group_management_enabled=boolean_env("GROUP_MANAGEMENT_ENABLED"),
            group_invite_key_id=os.getenv("GROUP_INVITE_KEY_ID", "v1").strip(),
            group_invite_signing_key=os.getenv("GROUP_INVITE_SIGNING_KEY", ""),
        )
