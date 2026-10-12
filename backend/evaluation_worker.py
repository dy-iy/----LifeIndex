"""Leased assessment -> manager arguments -> deterministic settlement workflow.

Successful model results are committed before settlement and reused after a
restart. A per-job advisory lock fences provider calls; input and lease versions
are checked again under the User lock before any ledger mutation.
"""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import os
import logging
from pathlib import Path
import time
import uuid

from dotenv import load_dotenv
from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.orm import sessionmaker

from config import Settings
from db import make_engine
from diagnostics import log_failure
from evaluation_api import current_input, deadline, enqueue_evaluation, fingerprint, utcnow
from evaluation_model import MODEL_NAME, PROMPT_VERSION, MAX_OUTPUT_TOKENS, ModelError, build_messages, invoke_evaluator, needs_clarification, validate_assessment, score_maxima
from evaluation_models import EvaluationCall, EvaluationDailyUsage, EvaluationJob
from models import Review, StudySession, User
from schedule_budget import token_upper_bound
import review_flow
import review_v5
import review_v6
import review_v7

logger = logging.getLogger("lifefund.review.worker")


class Worker:
    def __init__(self, settings, evaluator=None, manager=None):
        from agents.roles import invoke_quality, invoke_manager
        from agents.evaluation_runtime import EvaluationRuntime
        self.settings = settings
        self.evaluator, self.manager = evaluator or invoke_quality, manager or invoke_manager
        self.legacy_evaluator = evaluator or invoke_evaluator
        self.runtime = EvaluationRuntime(self)
        from agents.evaluation_runtime_v5 import DirectEvaluationRuntime
        self.direct_runtime = DirectEvaluationRuntime(self)
        from agents.evaluation_runtime_v6 import SummaryEvaluationRuntime
        self.summary_runtime = SummaryEvaluationRuntime(self)
        from agents.evaluation_runtime_v7 import LongShortEvaluationRuntime
        self.long_short_runtime = LongShortEvaluationRuntime(self)
        self.engine = make_engine(settings.database_url)
        if self.engine is None:
            raise ValueError("DATABASE_URL is required for the evaluation worker")
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def close(self):
        self.engine.dispose()

    def backfill_pending(self, limit=100):
        """Upgrade legacy pending reviews without scoring records submitted late."""
        with self.factory() as db:
            ids = db.scalars(select(Review.session_id).join(StudySession, StudySession.id == Review.session_id).where(
                Review.status == "pending_evaluation", StudySession.voided.is_(False), StudySession.phase == "finished",
                ~exists(select(EvaluationJob.id).where(EvaluationJob.session_id == Review.session_id,
                    EvaluationJob.review_version == Review.version, EvaluationJob.session_version == StudySession.version)),
            ).order_by(Review.submitted_at, Review.session_id).limit(limit)).all()
        count = 0
        for session_id in ids:
            with self.factory() as db:
                study = db.get(StudySession, session_id)
                db.scalar(select(User).where(User.id == study.user_id).with_for_update())
                db.refresh(study)
                review = db.get(Review, session_id)
                if review and not study.voided and review.status == "pending_evaluation":
                    enqueue_evaluation(db, study.user_id, review, study, utcnow(), legacy=True)
                    count += 1
                db.commit()
        return count

    def claim(self):
        with self.factory() as db:
            now = utcnow()
            job = db.scalar(select(EvaluationJob).where(or_(EvaluationJob.status == "queued", and_(
                EvaluationJob.status == "running", EvaluationJob.lease_until < now,
            ))).order_by(EvaluationJob.created_at, EvaluationJob.id).with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            if not current_input(db, job):
                job.status, job.error = "superseded", "复盘或课后记录已更新，本次评估已停止。"
                job.version += 1
                job.lease_owner = job.lease_until = None
                job.updated_at = now
                db.commit()
                return None
            logging.getLogger("lifefund.review.performance").info("evaluation_%s_wait_ms=%.2f", job.status,
                max(0, (now - job.updated_at).total_seconds() * 1000))
            job.attempts += 1
            job.version += 1
            job.updated_at = now
            if job.attempts > 3:
                job.status, job.error = "failed", "任务多次中断，已保存复盘与评估，可稍后重试。"
                if job.prompt_version in {review_v5.QUALITY_VERSION, review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION}:
                    job.error = "评估失败，请重试。" + job.error
                job.lease_owner = job.lease_until = None
                db.commit()
                return None
            job.status, job.error = "running", ""
            job.lease_owner, job.lease_until = uuid.uuid4(), now + timedelta(minutes=5)
            db.commit()
            return job.id, job.lease_owner

    @contextmanager
    def fence(self, job_id):
        lock = int.from_bytes(job_id.bytes[:8], "big", signed=True)
        with self.engine.connect() as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": lock})
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:lock)"), {"lock": lock})

    def locked_job(self, db, job_id, owner):
        user_id = db.scalar(select(EvaluationJob.user_id).where(EvaluationJob.id == job_id))
        if user_id is None:
            raise ModelError("评估记录已不存在。")
        db.scalar(select(User).where(User.id == user_id).with_for_update())
        job = db.scalar(select(EvaluationJob).where(EvaluationJob.id == job_id).execution_options(populate_existing=True))
        if job.status != "running" or job.lease_owner != owner:
            raise ModelError("评估状态已变化，本次处理已停止。")
        if not current_input(db, job):
            raise ModelError("复盘或课后记录已更新，本次评估已停止。")
        return job

    def settle_call(self, call_id, tokens, response=None):
        with self.factory() as db:
            call = db.get(EvaluationCall, call_id)
            if call is None:
                return
            db.scalar(select(User).where(User.id == call.user_id).with_for_update())
            db.refresh(call)
            if call.status != "reserved":
                return
            usage = db.get(EvaluationDailyUsage, (call.user_id, call.usage_date))
            prompt, completion = tokens.get("prompt_tokens"), tokens.get("completion_tokens")
            known = (type(prompt) is int and type(completion) is int and 0 <= prompt <= 2_000_000
                     and 0 <= completion <= MAX_OUTPUT_TOKENS)
            cost = prompt * call.input_rate + completion * call.output_rate if known else call.reserved_micro
            usage.reserved_micro -= call.reserved_micro
            usage.charged_micro += cost
            call.charged_micro, call.status = cost, "completed" if known or response is not None else "unknown"
            call.response = response
            if known:
                call.input_tokens, call.output_tokens = prompt, completion
            db.commit()

    def reserve(self, job_id, owner, request_hash, messages, *, stage="legacy", max_output_tokens=MAX_OUTPUT_TOKENS):
        with self.factory() as db:
            job = self.locked_job(db, job_id, owner)
            now = utcnow()
            usage = db.get(EvaluationDailyUsage, (job.user_id, now.date()))
            if usage is None:
                usage = EvaluationDailyUsage(user_id=job.user_id, usage_date=now.date(), reserved_micro=0,
                                             charged_micro=0, request_count=0)
                db.add(usage)
            estimated = token_upper_bound(messages) * self.settings.evaluation_input_rate + max_output_tokens * self.settings.evaluation_output_rate
            if usage.request_count >= self.settings.ai_daily_request_limit:
                raise ModelError("今天的评估调用次数已用完，复盘已保存，可明天重试。", status_code=429)
            if usage.reserved_micro + usage.charged_micro + estimated > self.settings.evaluation_daily_budget_micro:
                raise ModelError("今天的评估预算不足，复盘已保存，可明天重试。", status_code=429)
            usage.reserved_micro += estimated
            usage.request_count += 1
            call = EvaluationCall(user_id=job.user_id, job_id=job.id, input_hash=request_hash, stage=stage, usage_date=now.date(),
                                  reserved_micro=estimated, input_rate=self.settings.evaluation_input_rate,
                                  output_rate=self.settings.evaluation_output_rate)
            db.add(call)
            db.flush()
            identifier = call.id
            db.commit()
            return identifier

    def assess(self, state):
        job_id, owner = state["job_id"], state["lease_owner"]
        with self.factory() as db:
            version = db.get(EvaluationJob, job_id).workflow_version
        if version == review_v7.WORKFLOW_VERSION:
            return asyncio.run(self.long_short_runtime.assess(job_id, owner))
        if version == review_v6.WORKFLOW_VERSION:
            return asyncio.run(self.summary_runtime.assess(job_id, owner))
        if version == review_v5.WORKFLOW_VERSION:
            return asyncio.run(self.direct_runtime.assess(job_id, owner))
        if version == "class-agents-v2":
            return asyncio.run(self.runtime.assess(job_id, owner))
        # A crash leaves an unknown provider charge. Conservatively close it,
        # once, before allocating another request under the same account budget.
        with self.factory() as db:
            job = self.locked_job(db, job_id, owner)
            pending = db.scalars(select(EvaluationCall.id).where(EvaluationCall.job_id == job_id,
                                  EvaluationCall.status == "reserved")).all()
            existing = job.assessment
            if existing is None and (job.model_version != MODEL_NAME or job.prompt_version != PROMPT_VERSION):
                raise ModelError("这份评估使用旧版模型规则，请联系管理员恢复对应版本，避免静默改分。")
            snapshot, clarification = job.input_snapshot, job.clarification
            request_hash = fingerprint({"input_hash": job.input_hash, "clarification": clarification,
                                        "model": job.model_version, "prompt": job.prompt_version})
        for call_id in pending:
            self.settle_call(call_id, {})
        if existing is not None:
            return {"assessment": validate_assessment(existing)}
        with self.factory() as db:
            cached = db.scalar(select(EvaluationCall).where(EvaluationCall.job_id == job_id,
                EvaluationCall.input_hash == request_hash, EvaluationCall.response.is_not(None))
                .order_by(EvaluationCall.created_at, EvaluationCall.id).limit(1))
            assessment = cached.response if cached else None
        if assessment is None:
            api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
            if not api_key:
                raise ModelError("评估服务尚未配置，请联系管理员。", status_code=503)
            messages = build_messages(snapshot, clarification)
            call_id = self.reserve(job_id, owner, request_hash, messages)
            try:
                answer = self.legacy_evaluator(messages, api_key=api_key)
                assessment = validate_assessment(answer["assessment"])
            except ModelError as error:
                self.settle_call(call_id, error.usage)
                raise
            except Exception as error:
                log_failure(logger, "Legacy evaluation provider failed job_id=%s", error, job_id)
                self.settle_call(call_id, {})
                raise ModelError("评估服务暂时不可用，已保留复盘，请重试。") from None
            self.settle_call(call_id, answer.get("usage", {}), assessment)
        with self.factory() as db:
            job = self.locked_job(db, job_id, owner)
            job.assessment = assessment
            job.version += 1
            job.updated_at = utcnow()
            db.commit()
        return {"assessment": assessment}

    def complete(self, state):
        with self.factory() as db:
            job = self.locked_job(db, state["job_id"], state["lease_owner"])
            direct = job.prompt_version in {review_v5.QUALITY_VERSION, review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION}
            assessment, now = validate_assessment(job.assessment or (job.quality_result if direct else None), scoring_version=job.prompt_version), utcnow()
            study = db.get(StudySession, job.session_id)
            if job.prompt_version in {review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION}:
                review_v6.validate_branch(assessment, job.input_snapshot)
                job.assessment = assessment
                if assessment["kind"] != "summary":
                    return self.complete_direct_result(db, job, study, assessment, now)
                return self.complete_summary(db, job, study, assessment, now)
            if direct:
                review_v5.validate_branch(assessment, job.input_snapshot)
                job.assessment = assessment
                if assessment["kind"] != "scored":
                    return self.complete_direct_result(db, job, study, assessment, now)
            if needs_clarification(assessment):
                if job.input_snapshot.get("question_version") == review_flow.QUESTION_VERSION:
                    flow = review_flow.plan(job.input_snapshot, job.dialogue, assessment)
                    job.status = "needs_input" if flow["action"] == "ask" and now <= deadline(study) else "hold"
                    job.error = flow["reason"]
                else:
                    job.status = "needs_input" if job.clarification is None and now <= deadline(study) else "hold"
                    job.error = ("现有信息暂时无法评分，本次记录已保存，可以结束。" if job.status == "hold" else "")
            else:
                from class_index import settle_lesson
                snapshot = job.input_snapshot
                quality = sum(assessment[name]["score"] for name in ("recall", "understanding", "focus"))
                job.settlement = settle_lesson(db, job.user_id, study.lesson_id, f"evaluation:{job.id}", "attendance",
                    effective_seconds=snapshot["effective_seconds"], planned_seconds=snapshot["planned_seconds"],
                    quality=quality, makeup=snapshot["is_makeup"], evidence={
                        "evaluation_id": str(job.id), "review_version": job.review_version,
                        "session_version": job.session_version, "input_hash": job.input_hash,
                        "input_snapshot": snapshot, "clarification": job.clarification, "dialogue": job.dialogue,
                        "assessment": assessment, "model_version": job.model_version,
                        "prompt_version": job.prompt_version, "agent_context": job.agent_context,
                        "workflow_version": job.workflow_version,
                        "lesson_name": snapshot["course"], "study": snapshot["study"],
                        "review": {"version": job.review_version, "answers": snapshot["answers"],
                                   "question_version": snapshot["question_version"], "submitted_at": snapshot["submitted_at"]},
                        "evaluation": {"id": str(job.id), "prompt_version": job.prompt_version,
                            "score_maxima": score_maxima(job.prompt_version), "scores": {
                            **{name: assessment[name]["score"] for name in ("recall", "understanding", "focus")}, "total": quality},
                            "evidence": {name: assessment[name]["evidence"] for name in ("recall", "understanding", "focus")},
                            "evidence_status": {name: assessment[name].get("evidence_status", assessment[name].get("basis")) for name in ("recall", "understanding", "focus")},
                            "confidence": assessment.get("confidence"), "missing_information": assessment.get("missing_information", [])},
                        "managers": assessment["managers"],
                    }, now=now)
                job.status, job.error = "succeeded", ""
            job.version += 1
            job.updated_at = now
            job.lease_owner = job.lease_until = None
            from agents.memory import remember_evaluation
            if job.prompt_version != review_v7.QUALITY_VERSION:
                remember_evaluation(db, job)
            db.commit()
        return {}

    def complete_summary(self, db, job, study, assessment, now):
        from class_index import settle_lesson
        from manager_signal import combine
        snapshot = job.input_snapshot
        signal = combine(assessment["managers"])
        evidence = {"evaluation_id": str(job.id), "review_version": job.review_version,
                "session_version": job.session_version, "input_hash": job.input_hash, "input_snapshot": snapshot,
                "assessment": assessment, "model_version": job.model_version, "prompt_version": job.prompt_version,
                "workflow_version": job.workflow_version, "agent_context": job.agent_context,
                "signal": signal}
        if job.prompt_version != review_v7.QUALITY_VERSION:
            evidence.update(lesson_name=snapshot["course"], study=snapshot["study"],
                review={"version": job.review_version, "answers": snapshot["answers"],
                    "question_version": snapshot["question_version"], "submitted_at": snapshot["submitted_at"]},
                summary=review_v6.summary_view(assessment, snapshot), managers=assessment["managers"])
        job.settlement = settle_lesson(db, job.user_id, study.lesson_id, f"evaluation:{job.id}", "attendance",
            effective_seconds=snapshot["effective_seconds"], planned_seconds=snapshot["planned_seconds"],
            makeup=snapshot["is_makeup"], manager_reports=assessment["managers"], now=now,
            evidence=evidence)
        job.status, job.error = "succeeded", ""
        job.version += 1
        job.updated_at = now
        job.lease_owner = job.lease_until = None
        from agents.memory import remember_evaluation
        if job.prompt_version != review_v7.QUALITY_VERSION:
            remember_evaluation(db, job)
        db.commit()
        return {}

    def complete_direct_result(self, db, job, study, assessment, now):
        if assessment["kind"] == "invalid_answer":
            job.status, job.error = "invalid_answer", review_v5.INVALID_MESSAGE
        else:
            from lesson_outcomes import apply_decision, eligible_lesson
            from models import LessonOutcome
            from timetable_lifecycle import learning_lesson
            lesson, outcome = learning_lesson(db, job.user_id, study.lesson_id), db.get(LessonOutcome, study.lesson_id)
            eligible_lesson(lesson, outcome, study, now)
            from evaluation_api import require_window
            # Timely submissions may finish after the editing deadline, just as
            # learning scores do. Never backdate the actual settlement itself.
            require_window(study, datetime.fromisoformat(job.input_snapshot["submitted_at"]))
            value = apply_decision(db, db.get(User, job.user_id), lesson, outcome, study, now,
                assessment["decision"], assessment["reason"], automatic_job=job)
            job.settlement = value["settlement"]
            job.status, job.error = "succeeded", ""
        job.version += 1
        job.updated_at = now
        job.lease_owner = job.lease_until = None
        from agents.memory import remember_evaluation
        if job.prompt_version != review_v7.QUALITY_VERSION:
            remember_evaluation(db, job)
        db.commit()
        return {}

    def prepare_branch(self, job_id, owner):
        """Resolve explicit branches before any paid model call; fence as usual."""
        with self.factory() as db:
            job = self.locked_job(db, job_id, owner)
            if job.prompt_version in {review_v5.QUALITY_VERSION, review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION}:
                return False
            if job.input_snapshot.get("question_version") != review_flow.QUESTION_VERSION:
                return False
            flow = review_flow.plan(job.input_snapshot, job.dialogue)
            if flow["action"] == "assess":
                return False
            if flow["action"] == "not_learning":
                job.assessment = validate_assessment(review_flow.no_learning_assessment(), scoring_version=job.prompt_version)
            else:
                study = db.get(StudySession, job.session_id)
                job.status = "needs_input" if flow["action"] == "ask" and utcnow() <= deadline(study) else "hold"
                job.error = flow["reason"]
                job.lease_owner = job.lease_until = None
            job.version += 1
            job.updated_at = utcnow()
            db.commit()
        if flow["action"] == "not_learning":
            self.complete({"job_id": job_id, "lease_owner": owner})
        return True

    def fail(self, job_id, owner, message):
        with self.factory() as db:
            user_id = db.scalar(select(EvaluationJob.user_id).where(EvaluationJob.id == job_id))
            if user_id is None:
                return
            db.scalar(select(User).where(User.id == user_id).with_for_update())
            job = db.get(EvaluationJob, job_id)
            if job.status != "running" or job.lease_owner != owner:
                return
            valid = current_input(db, job)
            retry = valid and job.prompt_version in {review_v5.QUALITY_VERSION, review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION} and job.attempts < 3
            job.status = "queued" if retry else "failed" if valid else "superseded"
            job.error = message[:1000] if valid else "复盘或课后记录已更新，本次评估已停止。"
            if valid and job.prompt_version in {review_v5.QUALITY_VERSION, review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION} and not retry:
                job.error = "评估失败，请重试。" + job.error[:980]
            job.version += 1
            job.updated_at = utcnow()
            job.lease_owner = job.lease_until = None
            db.commit()

    def run_once(self):
        claimed = self.claim()
        if claimed is None:
            return False
        job_id, owner = claimed
        with self.fence(job_id) as acquired:
            if not acquired:
                return False
            try:
                if self.prepare_branch(job_id, owner):
                    return True
                with self.factory() as db:
                    version = db.get(EvaluationJob, job_id).workflow_version
                if version == review_v7.WORKFLOW_VERSION:
                    asyncio.run(self.long_short_runtime.run(job_id, owner))
                elif version == review_v6.WORKFLOW_VERSION:
                    asyncio.run(self.summary_runtime.run(job_id, owner))
                elif version == review_v5.WORKFLOW_VERSION:
                    asyncio.run(self.direct_runtime.run(job_id, owner))
                elif version == "class-agents-v2":
                    asyncio.run(self.runtime.run(job_id, owner))
                elif version == "legacy-v1":
                    # Preserve already queued v1 contracts and their cached
                    # provider response. Every newly submitted job uses v2.
                    state = {"job_id": job_id, "lease_owner": owner}
                    self.assess(state)
                    self.complete(state)
                else:
                    raise ModelError("未知工作流版本，已保留输入，请联系管理员。")
            except ModelError as error:
                logger.warning("Evaluation failed job_id=%s status_code=%s error=%s",
                               job_id, error.status_code, error.message)
                self.fail(job_id, owner, error.message)
            except HTTPException as error:
                log_failure(logger, "Evaluation settlement failed job_id=%s status_code=%s",
                            error, job_id, error.status_code)
                self.fail(job_id, owner, "结算暂未完成，评估已保存，请重试。")
            except Exception as error:
                log_failure(logger, "Unexpected evaluation failure job_id=%s", error, job_id)
                self.fail(job_id, owner, "评估暂未完成，复盘与已生成结果已保存，请重试。")
        return True


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = Worker(Settings.from_env())
    try:
        while True:
            worker.backfill_pending()
            worked = worker.run_once()
            if args.once:
                break
            if not worked:
                time.sleep(2)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
