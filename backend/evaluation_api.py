"""Owned, versioned evaluation control; the model never receives database access."""
from db import IdempotencyKey, Input, Version, mutate, owned
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from sqlalchemy import select

from auth import CurrentUser, Db
from evaluation_model import MODEL_NAME, score_maxima
from agents.prompts import WORKFLOW_VERSION
from evaluation_models import EvaluationJob
from models import Review, StudySession
from rules import CORRECTION_WINDOW_DAYS
import review_flow
import review_v5
import review_v6
import review_v7
from study_sessions import session_result, session_segments

router = APIRouter()


def utcnow():
    return datetime.now(timezone.utc)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def deadline(study):
    return study.ends_at + timedelta(days=CORRECTION_WINDOW_DAYS)


def require_window(study, now):
    if now > deadline(study):
        raise HTTPException(409, "已超过课后 7 天，记录仍会保留，暂不支持新评分或改价。")


def current_input(db, job):
    review = db.get(Review, job.session_id)
    study = db.get(StudySession, job.session_id)
    return bool(review and study and not study.voided and study.phase == "finished"
                and review.status == "pending_evaluation" and review.version == job.review_version
                and study.version == job.session_version)


def latest_job(db, user_id, study, review):
    if review is None or review.status != "pending_evaluation":
        return None
    if review.outcome_evaluation_id:
        return db.scalar(select(EvaluationJob).where(EvaluationJob.id == review.outcome_evaluation_id,
            EvaluationJob.user_id == user_id, EvaluationJob.session_id == study.id, EvaluationJob.status == "succeeded"))
    return db.scalar(select(EvaluationJob).where(
        EvaluationJob.user_id == user_id, EvaluationJob.session_id == study.id,
        EvaluationJob.review_version == review.version, EvaluationJob.session_version == study.version,
    ).execution_options(populate_existing=True))


def enqueue_evaluation(db, user_id, review, study, now, *, legacy=False, direct_version=None):
    """Called under the owning User lock, in the same transaction as the input."""
    if study.voided or study.phase != "finished" or review.status != "pending_evaluation":
        return None
    previous = latest_job(db, user_id, study, review)
    if previous:
        return previous
    summary = session_result(study, session_segments(db, user_id, study.id), now)
    snapshot = {"course": study.name, "question_version": review.question_version,
                "answers": list(review.answers), "review_version": review.version,
                "session_version": study.version, "lesson_id": str(study.lesson_id),
                "planned_seconds": summary["planned_seconds"], "effective_seconds": summary["effective_seconds"],
                "elapsed_seconds": summary["elapsed_seconds"], "is_makeup": study.is_makeup,
                "source": summary["source"], "submitted_at": review.submitted_at.isoformat(),
                "starts_at": study.starts_at.isoformat(), "ends_at": study.ends_at.isoformat()}
    snapshot["study"] = {key: summary[key] for key in ("id", "lesson_id", "name", "local_date", "start_time", "end_time",
        "effective_seconds", "planned_seconds", "elapsed_seconds", "is_makeup", "source", "version", "reported_seconds")}
    if review.question_version == review_flow.QUESTION_VERSION:
        snapshot["review_context"] = review.review_context
    expired = review.submitted_at > deadline(study)
    direct = review.question_version == review_flow.QUESTION_VERSION and not legacy
    direct_version = direct_version or review_v7.QUALITY_VERSION
    if direct_version not in {review_v5.QUALITY_VERSION, review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION}:
        raise ValueError("Unsupported direct evaluation version")
    if direct and expired:
        require_window(study, review.submitted_at)
    job = EvaluationJob(user_id=user_id, session_id=study.id, review_version=review.version,
                        session_version=study.version, input_snapshot=snapshot, input_hash=fingerprint(snapshot),
                        model_version=MODEL_NAME,
                        prompt_version=direct_version if direct else review_flow.QUALITY_VERSION,
                        workflow_version={review_v5.QUALITY_VERSION: review_v5.WORKFLOW_VERSION,
                            review_v6.QUALITY_VERSION: review_v6.WORKFLOW_VERSION,
                            review_v7.QUALITY_VERSION: review_v7.WORKFLOW_VERSION}[direct_version] if direct else WORKFLOW_VERSION,
                        status="hold" if expired else "queued",
                        error="原始提交已超过课后 7 天，保留记录且不自动评分或改价。" if expired else "",
                        created_at=now, updated_at=now)
    db.add(job)
    db.flush()
    if not direct and not expired and review.question_version == review_flow.QUESTION_VERSION:
        flow = review_flow.plan(snapshot)
        if flow["action"] in {"hold", "outcome", "ask"}:
            job.status = "needs_input" if flow["action"] == "ask" else "hold"
            job.error = flow["reason"]
    # Fence old inputs immediately. Their completed ledger events stay immutable.
    for prior in db.scalars(select(EvaluationJob).where(EvaluationJob.user_id == user_id,
            EvaluationJob.session_id == study.id, EvaluationJob.id != job.id,
            EvaluationJob.status.in_(["queued", "running", "needs_input", "hold", "failed", "invalid_answer"]))):
        prior.status, prior.error = "superseded", "复盘或课后记录已更新，本次评估已停止。"
        prior.version += 1
        prior.lease_owner = prior.lease_until = None
        prior.updated_at = now
    return job


def settlement_summary(value):
    if value is None:
        return None
    return {key: item for key, item in value.items() if key not in {"evidence", "rule_snapshot"}}



def job_result(job):
    if job is None:
        return None
    if job.prompt_version in {review_v6.QUALITY_VERSION, review_v7.QUALITY_VERSION}:
        return summary_job_result(job)
    if job.prompt_version == review_v5.QUALITY_VERSION:
        return direct_job_result(job)
    assessment = job.assessment or job.quality_result
    scores = ({name: assessment[name]["score"] for name in ("recall", "understanding", "focus")}
              if assessment else None)
    if scores is not None:
        scores["total"] = sum(scores.values())
    flow = None
    if job.input_snapshot.get("question_version") == review_flow.QUESTION_VERSION:
        flow = review_flow.plan(job.input_snapshot, job.dialogue, assessment if job.status in {"needs_input", "hold"} else None)
        if job.status != "needs_input":
            flow = {**flow, "key": None, "question": None}
    return {"id": str(job.id), "session_id": str(job.session_id), "review_version": job.review_version,
            "version": job.version, "status": job.status, "scores": scores, "score_maxima": score_maxima(job.prompt_version),
            "evidence": {name: assessment[name]["evidence"] for name in ("recall", "understanding", "focus")} if assessment else None,
            "confidence": assessment["confidence"] if assessment else None,
            "evidence_status": {name: assessment[name]["evidence_status"] for name in ("recall", "understanding", "focus")} if assessment else None,
            "missing_information": assessment["missing_information"] if assessment else [],
            "question": flow["question"] if flow else ((assessment.get("question") or "能补充一个本次学习的具体片段吗？")
                         if assessment and job.status == "needs_input" else None),
            "clarification": job.clarification,
            "dialogue": job.dialogue, "flow": flow,
            "scoring_basis": "not_learning" if flow and flow["action"] == "not_learning" else "learning_evidence",
            "managers": assessment.get("managers") if assessment else None,
            "settlement": settlement_summary(job.settlement), "error": job.error,
            "model_version": job.model_version, "prompt_version": job.prompt_version,
            "workflow_version": job.workflow_version,
            "manager_sources": (job.agent_context or {}).get("manager_sources", {}),
            "created_at": job.created_at.isoformat(), "updated_at": job.updated_at.isoformat()}


def direct_job_result(job):
    assessment = job.assessment or job.quality_result or {}
    scored = assessment.get("kind") == "scored"
    scores = {name: assessment[name]["score"] for name in ("recall", "understanding", "focus")} if scored else None
    if scores is not None:
        scores["total"] = sum(scores.values())
    return {"id": str(job.id), "session_id": str(job.session_id), "review_version": job.review_version,
        "version": job.version, "status": job.status, "scores": scores, "score_maxima": score_maxima(job.prompt_version),
        "evidence": {name: assessment[name]["evidence"] for name in scores if name != "total"} if scored else None,
        "evidence_status": {name: assessment[name]["basis"] for name in scores if name != "total"} if scored else None,
        "confidence": None, "missing_information": [], "question": None, "dialogue": [], "flow": None,
        "result_kind": assessment.get("kind"), "invalid_step": (review_v7 if job.prompt_version == review_v7.QUALITY_VERSION else review_v5).text_step(job.input_snapshot) if job.status == "invalid_answer" else None,
        "outcome": {"decision": assessment["decision"], "label": {"absent": "缺席", "leave": "因故未到课 · 免罚", "cancelled": "停课 · 免罚"}[assessment["decision"]]}
                   if assessment.get("kind") == "outcome" else None,
        "scoring_basis": "actual_activity", "managers": assessment.get("managers"), "settlement": settlement_summary(job.settlement),
        "error": job.error, "prompt_version": job.prompt_version, "model_version": job.model_version,
        "workflow_version": job.workflow_version, "manager_sources": (job.agent_context or {}).get("manager_sources", {}),
        "created_at": job.created_at.isoformat(), "updated_at": job.updated_at.isoformat()}


def summary_job_result(job):
    from manager_signal import combine
    assessment = job.assessment or job.quality_result or {}
    summary = assessment.get("kind") == "summary"
    # Non-learning results share the existing result and absence contract.
    value = direct_job_result(job)
    value.update(score_maxima=None, scores=None, evidence=None, evidence_status=None,
        scoring_basis="manager_signal", summary=(review_v7 if job.prompt_version == review_v7.QUALITY_VERSION else review_v6).summary_view(assessment, job.input_snapshot) if summary else None,
        signal=combine(assessment["managers"]) if summary and assessment.get("managers") else None)
    return value


def response(study, review, job, now):
    value = job_result(job)
    if value and review.outcome_evaluation_id == job.id:
        value["result_review_version"] = review.version
    return {"evaluation": value, "can_revise": bool(review and review.status == "pending_evaluation"
                and not study.voided and now <= deadline(study)), "correction_deadline": deadline(study).isoformat()}


def records(db, user_id, session_id, *, reading=False):
    study = owned(db, StudySession, user_id, session_id)
    if study.voided and not reading:
        raise HTTPException(409, "这节课已改为缺席、请假或停课，请查看最新课后记录。")
    if study.phase != "finished":
        raise HTTPException(409, "请先结束计时，再填写复盘。")
    review = db.scalar(select(Review).where(Review.user_id == user_id, Review.session_id == session_id)
                       .execution_options(populate_existing=True))
    if study.voided and not (reading and review and review.outcome_evaluation_id):
        raise HTTPException(409, "这节课已改为缺席、请假或停课，请查看最新课后记录。")
    return study, review, latest_job(db, user_id, study, review)


@router.get("/api/reviews/{session_id}/evaluation")
def get_evaluation(session_id: uuid.UUID, user: CurrentUser, db: Db):
    study, review, job = records(db, user.id, session_id, reading=True)
    now = utcnow()
    value = response(study, review, job, now)
    if review and review.outcome_evaluation_id:
        from reviews import result
        value["review"] = result(study, review, session_segments(db, user.id, study.id), now, job)
    return value


class RetryInput(Input):
    version: Version


class ClarifyInput(RetryInput):
    answer: str = Field(default="", max_length=2000)
    participation: review_flow.Participation = "unspecified"


def versioned_job(db, user_id, session_id, version):
    study, review, job = records(db, user_id, session_id)
    if not job:
        raise HTTPException(409, "复盘尚未进入评估，请先提交或稍后刷新。")
    if job.version != version:
        raise HTTPException(409, "评估状态已更新，请刷新后重试。")
    return study, review, job


@router.post("/api/reviews/{session_id}/evaluation/retry")
def retry_evaluation(session_id: uuid.UUID, body: RetryInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        study, review, job = versioned_job(db, user.id, session_id, body.version)
        if job.status in {"succeeded", "queued", "running"}:
            return response(study, review, job, utcnow())
        if job.status != "failed":
            raise HTTPException(409, "当前状态无需重试，请补充资料或查看已保存的结果。")
        job.status, job.error, job.attempts = "queued", "", 0
        job.version += 1
        job.updated_at = utcnow()
        return response(study, review, job, job.updated_at)
    return mutate(db, user, request, key, body, 200, action)


@router.post("/api/reviews/{session_id}/evaluation/clarify")
def clarify_evaluation(session_id: uuid.UUID, body: ClarifyInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        study, review, job = versioned_job(db, user.id, session_id, body.version)
        now = utcnow()
        require_window(study, now)
        modern = job.input_snapshot.get("question_version") == review_flow.QUESTION_VERSION
        flow = review_flow.plan(job.input_snapshot, job.dialogue, job.assessment or job.quality_result) if modern else None
        if job.status != "needs_input" or (modern and flow["action"] != "ask") or (not modern and job.clarification is not None):
            raise HTTPException(409, "本次补充已结束，记录已保留，可以直接返回。")
        if not body.answer.strip() and (not modern or body.participation == "unspecified"):
            raise HTTPException(422, "请填写补充回答。")
        if modern:
            job.dialogue = [*job.dialogue, {"key": flow["key"], "question": flow["question"],
                            "answer": body.answer, "participation": body.participation}]
        job.clarification = body.answer
        job.status, job.error, job.attempts = "queued", "", 0
        job.assessment = None
        job.quality_result = None
        if job.agent_context:
            # This is a new graph/input, while the original source snapshot and
            # prompt versions remain frozen. Old manifests live in trace rows.
            job.agent_context = {key: value for key, value in job.agent_context.items()
                                 if key not in {"context_manifests", "manager_sources"}}
        job.version += 1
        job.updated_at = now
        from learning_lifecycle import reopen_lesson
        reopen_lesson(db, user.id, study.lesson_id, now)
        return response(study, review, job, now)
    return mutate(db, user, request, key, body, 200, action)


@router.post("/api/reviews/{session_id}/evaluation/finish")
def finish_review(session_id: uuid.UUID, body: RetryInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        study, review, job = versioned_job(db, user.id, session_id, body.version)
        if job.status == "needs_input":
            job.status, job.error = "hold", "本次记录已保存，评分暂缓；有新信息时可在期限内补充。"
            job.version += 1
            job.updated_at = utcnow()
        return response(study, review, job, utcnow())
    return mutate(db, user, request, key, body, 200, action)


# Imported after the enqueue helpers to keep reviews -> enqueue free of cycles.
from reviews import ReviewInput


@router.post("/api/reviews/{session_id}/evaluation/revise")
def revise_evaluation(session_id: uuid.UUID, body: ReviewInput, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        from reviews import result, validate_submission, input_context
        study, review, job = records(db, user.id, session_id)
        now = utcnow()
        require_window(study, now)
        if review is None or review.status != "pending_evaluation":
            raise HTTPException(409, "请先提交复盘，再申请修订。")
        if review.version != body.version:
            raise HTTPException(409, "云端复盘已更新，请刷新并核对答案。")
        validate_submission(body)
        if review.answers == body.answers and review.question_version == body.question_version and review.review_context == input_context(body):
            # Reposting identical evidence must never draw another, luckier score.
            return {**response(study, review, job, now), "review": result(study, review, session_segments(db, user.id, study.id), now)}
        review.answers, review.updated_at, review.submitted_at = body.answers, now, now
        review.question_version, review.review_context = body.question_version, input_context(body)
        review.version += 1
        db.flush()
        job = enqueue_evaluation(db, user.id, review, study, now)
        from learning_lifecycle import reopen_lesson
        reopen_lesson(db, user.id, study.lesson_id, now)
        return {**response(study, review, job, now), "review": result(study, review, session_segments(db, user.id, study.id), now)}
    return mutate(db, user, request, key, body, 200, action)
