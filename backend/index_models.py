"""Append-only class index ledger; prices are never model-generated."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from models import Base


class ClassIndexAccount(Base):
    __tablename__ = "class_index_accounts"
    __table_args__ = (CheckConstraint("price >= 1 AND previous_close >= 1 AND sequence >= 0", name="ck_class_index_account_values"),)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(36, 8), nullable=False)
    previous_close: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClassIndexEvent(Base):
    __tablename__ = "class_index_events"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_class_index_event_owner_id"),
        UniqueConstraint("user_id", "sequence", name="uq_class_index_event_sequence"),
        UniqueConstraint("user_id", "source_key", name="uq_class_index_event_source"),
        UniqueConstraint("user_id", "lesson_id", "revision", name="uq_class_index_event_revision"),
        UniqueConstraint("previous_event_id", name="uq_class_index_event_successor"),
        ForeignKeyConstraint(["user_id", "lesson_id"], ["lessons.user_id", "lessons.id"], name="fk_class_index_event_lesson", ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id", "original_event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_index_event_original"),
        ForeignKeyConstraint(["user_id", "previous_event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_index_event_previous"),
        CheckConstraint("kind IN ('attendance', 'absence', 'excluded')", name="ck_class_index_event_kind"),
        CheckConstraint("revision >= 0 AND sequence > 0 AND price_before >= 1 AND price_after >= 1", name="ck_class_index_event_values"),
        CheckConstraint("(revision = 0 AND original_event_id IS NULL AND previous_event_id IS NULL) OR (revision > 0 AND original_event_id IS NOT NULL AND previous_event_id IS NOT NULL)", name="ck_class_index_event_links"),
        Index("ix_class_index_event_owner_date", "user_id", "business_date", "sequence"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_key: Mapped[str] = mapped_column(String(200))
    sequence: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    business_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lesson_name: Mapped[str] = mapped_column(String(100))
    lesson_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lesson_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price_before: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    price_after: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    previous_close: Mapped[Decimal] = mapped_column(Numeric(30, 2))
    raw_points: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    actual_points: Mapped[Decimal] = mapped_column(Numeric(30, 2))
    clipped_points: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    rounding_points: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    raw_rate: Mapped[Decimal] = mapped_column(Numeric(24, 18))
    quality: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    duration_ratio: Mapped[Decimal | None] = mapped_column(Numeric(24, 18), nullable=True)
    participation_score: Mapped[Decimal | None] = mapped_column(Numeric(24, 18), nullable=True)
    effective_seconds: Mapped[int] = mapped_column(Integer)
    planned_seconds: Mapped[int] = mapped_column(Integer)
    makeup: Mapped[bool] = mapped_column(Boolean)
    rule_version: Mapped[str] = mapped_column(String(40))
    rule_snapshot: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
    original_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    previous_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_points: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    previous_target_points: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    original_price_before: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    original_previous_close: Mapped[Decimal] = mapped_column(Numeric(30, 2))
    cumulative_actual_points: Mapped[Decimal] = mapped_column(Numeric(30, 2))
    cumulative_discarded_points: Mapped[Decimal] = mapped_column(Numeric(36, 8))


class ClassIndexReceipt(Base):
    """Even a first excluded result has a durable idempotency receipt."""
    __tablename__ = "class_index_receipts"
    __table_args__ = (ForeignKeyConstraint(["user_id", "event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_index_receipt_event", ondelete="CASCADE"),)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ClassMarketState(Base):
    """Committed checkpoint; seed is server-only and never serialized by APIs."""
    __tablename__ = "class_market_states"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    seed: Mapped[str] = mapped_column(String(64))
    mood: Mapped[Decimal] = mapped_column(Numeric(40, 24))
    rule_version: Mapped[str] = mapped_column(String(40))
    rule_snapshot: Mapped[dict] = mapped_column(JSONB)
    timezone: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClassMarketAction(Base):
    """Append-only timer facts, separate from mutable learning evidence."""
    __tablename__ = "class_market_actions"
    __table_args__ = (UniqueConstraint("user_id", "source_key", name="uq_class_market_action_source"),)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(24))
    snapshot: Mapped[dict] = mapped_column(JSONB)


class ClassMarketPoint(Base):
    __tablename__ = "class_market_points"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "event_id"], ["class_index_events.user_id", "class_index_events.id"], name="fk_class_market_point_event"),
        Index("ix_class_market_point_day", "user_id", "business_date", "sequence"),
        CheckConstraint("price >= 1", name="ck_class_market_point_price"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    business_date: Mapped[date] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    kind: Mapped[str] = mapped_column(String(24))
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ClassMarketDay(Base):
    __tablename__ = "class_market_days"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64))
    previous_close: Mapped[Decimal] = mapped_column(Numeric(30, 2))
    open: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    high: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    low: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
