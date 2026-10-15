"""Persistent study group state, invitations, and transactional change history."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Identity, Index, Integer, LargeBinary, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class StudyGroup(Base):
    __tablename__ = "study_groups"
    __table_args__ = (
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 60 AND name = btrim(name)",
                        name="ck_study_groups_name"),
        CheckConstraint("max_members BETWEEN 1 AND 10", name="ck_study_groups_max_members"),
        CheckConstraint("status IN ('active', 'dissolved')", name="ck_study_groups_status"),
        CheckConstraint("version >= 1", name="ck_study_groups_version"),
        CheckConstraint("initial_price >= 1 AND initial_price <> 'NaN'::numeric",
                        name="ck_study_groups_initial_price"),
        CheckConstraint("(status = 'dissolved') = (dissolved_at IS NOT NULL)",
                        name="ck_study_groups_dissolved"),
        CheckConstraint("(avatar_id IS NULL) = (avatar_data IS NULL)", name="ck_study_groups_avatar"),
        UniqueConstraint("avatar_id", name="uq_study_groups_avatar_id"),
        Index("ix_study_groups_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(60))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                              ForeignKey("users.id", ondelete="RESTRICT", name="fk_study_groups_owner_id"))
    max_members: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    # Immutable creation-day opening quote of the original group creator.
    # No default: every new group must explicitly take this snapshot.
    initial_price: Mapped[Decimal] = mapped_column(Numeric(36, 8), nullable=False)
    avatar_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    avatar_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dissolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def avatar_url(self) -> str | None:
        return f"/api/group-avatars/{self.avatar_id}" if self.avatar_id is not None else None


class StudyGroupMember(Base):
    __tablename__ = "study_group_members"
    __table_args__ = (
        CheckConstraint("nickname IS NULL OR (char_length(btrim(nickname)) BETWEEN 1 AND 60 "
                        "AND nickname = btrim(nickname))", name="ck_study_group_members_nickname"),
        CheckConstraint("status IN ('active', 'left', 'removed')", name="ck_study_group_members_status"),
        CheckConstraint("(status = 'active') = (ended_at IS NULL)", name="ck_study_group_members_ended"),
        CheckConstraint("last_removed_version >= 0", name="ck_study_group_members_removed_version"),
        Index("ix_study_group_members_user_id", "user_id", "group_id"),
        Index("ix_study_group_members_active_user", "user_id", "group_id",
              postgresql_where=text("status = 'active'")),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("study_groups.id", ondelete="CASCADE"),
                                             primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                                            primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    nickname: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_removed_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudyGroupCheckin(Base):
    """One durable self check-in per group and Shanghai calendar day."""
    __tablename__ = "study_group_checkins"
    __table_args__ = (
        CheckConstraint("local_date = (checked_at AT TIME ZONE 'Asia/Shanghai')::date",
                        name="ck_study_group_checkins_local_date"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
        ForeignKey("study_groups.id", ondelete="CASCADE"), primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudyGroupCheckinReward(Base):
    """Immutable full-attendance award, independent of subsequent membership."""
    __tablename__ = "study_group_checkin_rewards"
    __table_args__ = (
        CheckConstraint("local_date = (awarded_at AT TIME ZONE 'Asia/Shanghai')::date",
                        name="ck_group_checkin_rewards_local_date"),
        CheckConstraint("previous_close >= 1 AND previous_close <> 'NaN'::numeric",
                        name="ck_group_checkin_rewards_previous_close"),
        CheckConstraint("rate = 0.03", name="ck_group_checkin_rewards_rate"),
        CheckConstraint("reward_points > 0 AND reward_points <> 'NaN'::numeric "
                        "AND reward_points = round(previous_close * rate, 8)",
                        name="ck_group_checkin_rewards_points"),
        CheckConstraint("member_count > 0 AND jsonb_typeof(member_ids) = 'array' "
                        "AND jsonb_array_length(member_ids) = member_count",
                        name="ck_group_checkin_rewards_members"),
        CheckConstraint("char_length(btrim(rule_version)) BETWEEN 1 AND 80 "
                        "AND rule_version = btrim(rule_version)",
                        name="ck_group_checkin_rewards_rule"),
        Index("ix_group_checkin_rewards_time", "group_id", "awarded_at"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
        ForeignKey("study_groups.id", ondelete="CASCADE"), primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_close: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    reward_points: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 5))
    # Audit references deliberately have no user FKs: achievement survives exits
    # and later user cleanup without silently rewriting its frozen denominator.
    member_ids: Mapped[list] = mapped_column(JSONB)
    member_count: Mapped[int] = mapped_column(Integer)
    rule_version: Mapped[str] = mapped_column(String(80))


class StudyGroupInvite(Base):
    __tablename__ = "study_group_invites"
    __table_args__ = (
        CheckConstraint("key_id ~ '^[A-Za-z0-9_-]{1,32}$'", name="ck_study_group_invites_key_id"),
        CheckConstraint("created_version >= 1", name="ck_study_group_invites_version"),
        CheckConstraint("expires_at > created_at", name="ck_study_group_invites_expiry"),
        Index("ix_study_group_invites_creator_time", "group_id", "inviter_user_id", "created_at"),
        Index("ix_study_group_invites_available", "group_id", "expires_at",
              postgresql_where=text("revoked_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("study_groups.id", ondelete="CASCADE"))
    inviter_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    key_id: Mapped[str] = mapped_column(String(32))
    created_version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StudyGroupEvent(Base):
    __tablename__ = "study_group_events"
    __table_args__ = (
        UniqueConstraint("group_id", "version", name="uq_study_group_events_version"),
        CheckConstraint("version >= 1", name="ck_study_group_events_version"),
        CheckConstraint("char_length(btrim(event_type)) BETWEEN 1 AND 40 AND event_type = btrim(event_type)",
                        name="ck_study_group_events_type"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_study_group_events_payload"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("study_groups.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(40))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True),
                                                         ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudyGroupMessage(Base):
    __tablename__ = "study_group_messages"
    __table_args__ = (
        UniqueConstraint("sequence", name="uq_study_group_messages_sequence"),
        UniqueConstraint("sender_id", "client_key", name="uq_study_group_messages_sender_key"),
        CheckConstraint("sequence > 0", name="ck_study_group_messages_sequence"),
        CheckConstraint("char_length(content) BETWEEN 1 AND 2000 AND content ~ '[^[:space:]]'",
                        name="ck_study_group_messages_content"),
        CheckConstraint("kind IN ('text', 'review')", name="ck_study_group_messages_kind"),
        CheckConstraint("(kind = 'text' AND review_summary IS NULL AND review_snapshot IS NULL) OR "
                        "(kind = 'review' AND review_summary IS NOT NULL AND review_snapshot IS NOT NULL "
                        "AND jsonb_typeof(review_summary) = 'object' AND jsonb_typeof(review_snapshot) = 'object')",
                        name="ck_study_group_messages_review"),
        Index("ix_study_group_messages_group_sequence", "group_id", "sequence"),
        Index("ix_study_group_messages_sender_time", "sender_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Allocate only after holding the group lock: a later same-group sequence
    # must never commit before an earlier one and be skipped by an after cursor.
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                             ForeignKey("study_groups.id", ondelete="CASCADE"))
    sender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                              ForeignKey("users.id", ondelete="RESTRICT"))
    client_key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(String(2000))
    kind: Mapped[str] = mapped_column(String(16), default="text", server_default="text")
    review_summary: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # Message history reads only the compact card. The frozen detail is loaded
    # exclusively by the group-bound, member-only review endpoint.
    review_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True, deferred=True)
    sender_name: Mapped[str] = mapped_column(String(60))
    sender_avatar_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StudyGroupReviewCompletion(Base):
    """Record even an empty target set, so reprocessing never adds later joins."""
    __tablename__ = "study_group_review_completions"
    __table_args__ = (
        CheckConstraint("review_version > 0", name="ck_group_review_completions_version"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    review_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    evaluation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StudyGroupReviewOutbox(Base):
    """Frozen completion cards awaiting independent, retryable chat delivery."""
    __tablename__ = "study_group_review_outbox"
    __table_args__ = (
        UniqueConstraint("session_id", "review_version", "group_id", name="uq_group_review_outbox_completion"),
        CheckConstraint("review_version > 0 AND attempts >= 0", name="ck_group_review_outbox_versions"),
        CheckConstraint("status IN ('pending', 'sent', 'skipped')", name="ck_group_review_outbox_status"),
        CheckConstraint("(status = 'pending') = (finished_at IS NULL)", name="ck_group_review_outbox_finished"),
        CheckConstraint("jsonb_typeof(review_summary) = 'object' AND jsonb_typeof(review_snapshot) = 'object'",
                        name="ck_group_review_outbox_snapshot"),
        Index("ix_group_review_outbox_due", "available_at", "created_at", "id",
              postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Source IDs are audit references: frozen pending cards survive later
    # cleanup of personal study/evaluation records.
    evaluation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    review_version: Mapped[int] = mapped_column(Integer)
    # Deliberately no group FK: recording a completion while owning the User
    # lock must never acquire an implicit group key-share lock. Owner transfer
    # locks a group before checking the target User FK, which could deadlock.
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    member_joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sender_name: Mapped[str] = mapped_column(String(60))
    sender_avatar_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    review_summary: Mapped[dict] = mapped_column(JSONB)
    review_snapshot: Mapped[dict] = mapped_column(JSONB, deferred=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
