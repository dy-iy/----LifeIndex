"""User-scoped long-term memory and execution metadata, without provider secrets."""
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from models import Base


class AgentMemory(Base):
    __tablename__ = "agent_memories"
    __table_args__ = (Index("ix_agent_memories_scope", "user_id", "domain", "scope", "updated_at"),)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    domain: Mapped[str] = mapped_column(String(24), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(80))
    source_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentTrace(Base):
    __tablename__ = "agent_traces"
    __table_args__ = (Index("ix_agent_traces_run", "user_id", "run_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workflow: Mapped[str] = mapped_column(String(40))
    node: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
