"""Durable, versioned review inputs and independently accounted model attempts."""
import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_evaluation_jobs_owner"),
        UniqueConstraint("session_id", "review_version", "session_version", name="uq_evaluation_jobs_input"),
        ForeignKeyConstraint(["user_id", "session_id"], ["study_sessions.user_id", "study_sessions.id"], ondelete="CASCADE", name="fk_evaluation_jobs_session"),
        CheckConstraint("review_version > 0 AND session_version > 0 AND version > 0 AND attempts >= 0", name="ck_evaluation_jobs_versions"),
        CheckConstraint("status IN ('queued','running','needs_input','hold','failed','succeeded','superseded','invalid_answer')", name="ck_evaluation_jobs_status"),
        Index("ix_evaluation_jobs_queue", "status", "lease_until", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    review_version: Mapped[int] = mapped_column(Integer)
    session_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), default="queued", server_default="queued")
    input_snapshot: Mapped[dict] = mapped_column(JSONB)
    input_hash: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(80))
    workflow_version: Mapped[str] = mapped_column(String(40), default="legacy-v1", server_default="legacy-v1")
    agent_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    quality_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    clarification: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    dialogue: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    assessment: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    settlement: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationDailyUsage(Base):
    __tablename__ = "evaluation_daily_usage"
    __table_args__ = (CheckConstraint("reserved_micro >= 0 AND charged_micro >= 0 AND request_count >= 0", name="ck_evaluation_daily_usage_nonnegative"),)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_micro: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    charged_micro: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    request_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class EvaluationCall(Base):
    __tablename__ = "evaluation_calls"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "job_id"], ["evaluation_jobs.user_id", "evaluation_jobs.id"], ondelete="CASCADE", name="fk_evaluation_calls_job"),
        CheckConstraint("status IN ('reserved','completed','unknown')", name="ck_evaluation_calls_status"),
        Index("ix_evaluation_calls_input", "job_id", "input_hash", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    input_hash: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(24), default="legacy", server_default="legacy")
    usage_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="reserved", server_default="reserved")
    reserved_micro: Mapped[int] = mapped_column(Integer)
    charged_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_rate: Mapped[int] = mapped_column(Integer)
    output_rate: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
