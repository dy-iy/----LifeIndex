"""Conservative CNY usage accounting, persisted before any model request."""
import json
import uuid

from sqlalchemy import select

from auth import utcnow
from models import ScheduleDailyUsage, ScheduleModelCall, User


def token_upper_bound(messages):
    # UTF-8 bytes bound text token counts conservatively. Images are charged by
    # provider image tokens, never by the length of their base64 transport.
    texts, images = [], 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        else:
            for block in content:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    images += 1
    return len(json.dumps(texts, ensure_ascii=False).encode()) + 8192 + images * 2048


def reserve(factory, user_id, run_id, messages, settings, *, input_hash=None):
    user_id, run_id = uuid.UUID(str(user_id)), uuid.UUID(str(run_id))
    estimated = token_upper_bound(messages) * settings.schedule_input_rate + 12000 * settings.schedule_output_rate
    with factory() as db:
        db.scalar(select(User).where(User.id == user_id).with_for_update())
        day = utcnow().date()
        usage = db.get(ScheduleDailyUsage, (user_id, day))
        if usage is None:
            usage = ScheduleDailyUsage(user_id=user_id, usage_date=day, reserved_micro=0, charged_micro=0, request_count=0)
            db.add(usage)
        # Calls and costs are recorded without caps. Keep reservations atomic
        # so in-flight requests and unknown provider usage remain accounted for.
        usage.reserved_micro += estimated
        usage.request_count += 1
        call = ScheduleModelCall(user_id=user_id, run_id=run_id, usage_date=day, status="reserved",
                                 input_hash=input_hash,
                                 reserved_micro=estimated, input_rate=settings.schedule_input_rate,
                                 output_rate=settings.schedule_output_rate)
        db.add(call)
        db.flush()
        call_id = call.id
        db.commit()
        return call_id


def settle(factory, call_id, tokens, response=None):
    with factory() as db:
        call = db.get(ScheduleModelCall, call_id)
        if call is None:
            return
        db.scalar(select(User).where(User.id == call.user_id).with_for_update())
        db.refresh(call)
        if call.status != "reserved":
            return
        usage = db.get(ScheduleDailyUsage, (call.user_id, call.usage_date))
        prompt, completion = tokens.get("prompt_tokens"), tokens.get("completion_tokens")
        known = (type(prompt) is int and type(completion) is int and prompt >= 0 and 0 <= completion <= 12000)
        cost = prompt * call.input_rate + completion * call.output_rate if known else call.reserved_micro
        usage.reserved_micro -= call.reserved_micro
        usage.charged_micro += cost
        call.charged_micro = cost
        call.response = response
        call.status = "estimated" if known else "unknown"
        if known:
            call.input_tokens, call.output_tokens = prompt, completion
        db.commit()
