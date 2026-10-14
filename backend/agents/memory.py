"""Durable source-backed memories; never promote an assistant guess into a fact."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from agents.models import AgentMemory
from models import StudySession, Review
from evaluation_models import EvaluationJob
from auth import utcnow


def remember(db, *, user_id, domain, source_id, scope, version, content):
    statement = insert(AgentMemory).values(user_id=user_id, domain=domain, source_id=source_id,
        scope=str(scope), source_version=version, content=content, updated_at=utcnow())
    db.execute(statement.on_conflict_do_update(index_elements=["user_id", "domain", "source_id"],
        set_={"scope": statement.excluded.scope, "source_version": statement.excluded.source_version,
              "content": statement.excluded.content, "updated_at": statement.excluded.updated_at},
        where=AgentMemory.source_version <= version))


def remember_evaluation(db, job):
    remember(db, user_id=job.user_id, domain="learning", source_id=job.id, scope=job.input_snapshot["course"][:80],
        version=job.version, content={"session_id": str(job.session_id), "course": job.input_snapshot["course"],
            "review_version": job.review_version, "status": job.status, "assessment": job.assessment})


def learning_sources(db, user_id, course, exclude_session, before):
    # Only current, submitted, earlier records of this course from this user.
    return db.execute(select(StudySession, Review).join(Review, Review.session_id == StudySession.id).where(
        StudySession.user_id == user_id, Review.user_id == user_id, StudySession.name == course,
        StudySession.id != exclude_session, StudySession.voided.is_(False), Review.status == "pending_evaluation",
        Review.submitted_at <= before).order_by(Review.submitted_at.desc()).limit(40)).all()


def recall_learning(db, user_id, course, exclude_session, before):
    """Recall completed evaluations only while their underlying input is current."""
    query = select(AgentMemory, EvaluationJob).join(EvaluationJob,
        EvaluationJob.id == AgentMemory.source_id).join(StudySession,
        StudySession.id == EvaluationJob.session_id).join(Review,
        Review.session_id == StudySession.id).where(
        AgentMemory.user_id == user_id, EvaluationJob.user_id == user_id,
        StudySession.user_id == user_id, Review.user_id == user_id,
        AgentMemory.domain == "learning", AgentMemory.scope == course[:80], StudySession.name == course,
        StudySession.id != exclude_session, StudySession.voided.is_(False),
        Review.status == "pending_evaluation", Review.submitted_at <= before,
        EvaluationJob.review_version == Review.version, EvaluationJob.session_version == StudySession.version,
        EvaluationJob.status.in_(["succeeded", "hold"]), AgentMemory.source_version == EvaluationJob.version)
    result = []
    for memory, job in db.execute(query.order_by(AgentMemory.updated_at.desc()).limit(20)):
        if memory.content.get("status") != job.status:
            continue
        assessment = memory.content.get("assessment") or {}
        if assessment.get("kind") == "summary":
            result.append({"source_id": str(job.id), "session_id": str(job.session_id),
                "version": memory.source_version, "evidence": {key: {
                    "evidence": assessment[key]["text"], "evidence_status": assessment[key]["basis"]}
                    for key in ("activity", "recall", "understanding", "focus")}})
            continue
        # Recall qualitative evidence, never put an old numeric score into the
        # current assessor's context. Memories remain labelled historical.
        result.append({"source_id": str(job.id), "session_id": str(job.session_id),
            "version": memory.source_version, "evidence": {
                key: {"evidence": assessment[key]["evidence"],
                      "evidence_status": assessment[key].get("evidence_status", assessment[key].get("basis"))}
                for key in ("recall", "understanding", "focus") if key in assessment}})
    return result
