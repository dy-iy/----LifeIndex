"""Transactional completion outbox and independent automatic chat delivery."""
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from auth import utcnow
from db import make_engine
from diagnostics import log_failure
from group_messages import shared_review_snapshot
from models import Review, User
from study_group_models import (StudyGroup, StudyGroupMember, StudyGroupMessage,
                                StudyGroupReviewCompletion, StudyGroupReviewOutbox)

logger = logging.getLogger("lifefund.review.messages")


def enqueue_completed_review(db, job, now):
    """Called only by completion, under User lock, before its existing commit.

    Freeze the same result used by the personal modal, including automatic
    absence/leave outcomes whose committed review version differs from input.
    No group lock (including FK key-share) is taken in this transaction.
    """
    if job.status != "succeeded":
        return
    # The snapshot reader refreshes records from the DB. Persist all final job
    # and outcome versions first, without committing the completion separately.
    db.flush()
    review = db.get(Review, job.session_id)
    first_completion = db.execute(insert(StudyGroupReviewCompletion).values(
        session_id=job.session_id, review_version=review.version, user_id=job.user_id,
        evaluation_id=job.id, completed_at=now,
    ).on_conflict_do_nothing().returning(StudyGroupReviewCompletion.session_id)).scalar_one_or_none()
    if first_completion is None:
        return
    targets = db.execute(select(StudyGroupMember.group_id, StudyGroupMember.joined_at,
                                StudyGroupMember.nickname).join(
        StudyGroup, StudyGroup.id == StudyGroupMember.group_id).where(
        StudyGroupMember.user_id == job.user_id, StudyGroupMember.status == "active",
        StudyGroup.status == "active", StudyGroupMember.joined_at <= now,
    ).order_by(StudyGroupMember.group_id)).all()
    if not targets:
        return
    summary, snapshot = shared_review_snapshot(db, job.user_id, job.session_id, review.version, now)
    if summary["evaluation_id"] != str(job.id):
        raise RuntimeError("Completion snapshot does not match its evaluation")
    user = db.get(User, job.user_id)
    for target in targets:
        db.execute(insert(StudyGroupReviewOutbox).values(
            id=uuid.uuid4(), evaluation_id=job.id, user_id=job.user_id, session_id=job.session_id,
            review_version=review.version, group_id=target.group_id, member_joined_at=target.joined_at,
            sender_name=target.nickname or user.display_name or "群成员", sender_avatar_url=user.avatar_url,
            review_summary=summary, review_snapshot=snapshot, status="pending", attempts=0,
            created_at=now, available_at=now,
        ).on_conflict_do_nothing(constraint="uq_group_review_outbox_completion"))


def deliver_completed_review(db, item, now):
    """The caller locks the outbox; group lock orders every message sequence."""
    group = db.scalar(select(StudyGroup).where(StudyGroup.id == item.group_id)
                      .with_for_update().execution_options(populate_existing=True))
    member = None if group is None or group.status != "active" else db.scalar(
        select(StudyGroupMember).where(StudyGroupMember.group_id == item.group_id,
            StudyGroupMember.user_id == item.user_id).with_for_update()
            .execution_options(populate_existing=True))
    if (member is None or member.status != "active" or member.joined_at != item.member_joined_at):
        item.status, item.finished_at = "skipped", now
        return
    key = f"auto-review:{item.session_id}:{item.review_version}:{item.group_id}"
    existing = db.scalar(select(StudyGroupMessage).where(
        StudyGroupMessage.sender_id == item.user_id, StudyGroupMessage.client_key == key))
    if existing is None:
        db.add(StudyGroupMessage(
            group_id=item.group_id, sender_id=item.user_id, client_key=key, kind="review",
            content="我完成了复盘", review_summary=item.review_summary, review_snapshot=item.review_snapshot,
            sender_name=item.sender_name, sender_avatar_url=item.sender_avatar_url, created_at=now,
        ))
        db.flush()
    elif existing.group_id != item.group_id or existing.kind != "review":
        raise RuntimeError("Automatic review receipt conflicts with message history")
    item.status, item.finished_at = "sent", now


class Worker:
    """Retries persisted deliveries without blocking evaluation or requiring UI."""
    def __init__(self, settings):
        self.engine = make_engine(settings.database_url)
        if self.engine is None:
            raise ValueError("DATABASE_URL is required for automatic review messages")
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def close(self):
        self.engine.dispose()

    def run_once(self):
        identifier = None
        try:
            with self.factory() as db:
                now = utcnow()
                item = db.scalar(select(StudyGroupReviewOutbox).where(
                    StudyGroupReviewOutbox.status == "pending", StudyGroupReviewOutbox.available_at <= now,
                ).order_by(StudyGroupReviewOutbox.available_at, StudyGroupReviewOutbox.created_at,
                           StudyGroupReviewOutbox.id).with_for_update(skip_locked=True).limit(1))
                if item is None:
                    return False
                identifier = item.id
                deliver_completed_review(db, item, now)
                db.commit()
        except Exception as error:
            if identifier is None:
                raise
            log_failure(logger, "Automatic review delivery failed outbox_id=%s; retrying", error, identifier)
            # The failed send transaction rolls back before scheduling a retry.
            # A poison item waits its own turn instead of starving newer cards.
            with self.factory() as db:
                item = db.scalar(select(StudyGroupReviewOutbox).where(StudyGroupReviewOutbox.id == identifier)
                                 .with_for_update().execution_options(populate_existing=True))
                if item is not None and item.status == "pending":
                    item.attempts += 1
                    item.available_at = utcnow() + timedelta(seconds=min(300, 2 ** min(item.attempts, 9)))
                db.commit()
        return True
