"""Incomplete learning records and immutable versions of explicit decisions."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class LearningArchive(Base):
    __tablename__ = "learning_archives"
    __table_args__ = (ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"],
                        ondelete="CASCADE", name="fk_learning_archives_owner"),)
    lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(24))
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutcomeRevision(Base):
    __tablename__ = "outcome_revisions"
    __table_args__ = (ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"],
                        ondelete="CASCADE", name="fk_outcome_revisions_owner"),)
    lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    input_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
