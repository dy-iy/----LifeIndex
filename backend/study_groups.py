"""Persistent groups, revocable invitations and serialized membership management."""
import base64
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased
from starlette.concurrency import run_in_threadpool

from auth import CurrentUser, Db, hash_token, unauthorized, utcnow
from avatars import ALLOWED_CONTENT_TYPES, MAX_AVATAR_BYTES, normalize_avatar
from db import IdempotencyKey, Input, Version, check_version, mutate
from group_initial_open import complete_creation_midnight, resolve_group_initial_open
from index_models import ClassIndexAccount
from models import AuthSession, User
from rules import CENT, INITIAL_PRICE
from study_group_models import StudyGroup, StudyGroupCheckinReward, StudyGroupEvent, StudyGroupInvite, StudyGroupMember

router = APIRouter()
MAX_GROUPS_PER_USER = 20
INVITE_TTL = timedelta(hours=1)
INVITES_PER_MINUTE = 5
TOKEN_VERSION = "1"
TOKEN_PURPOSE = "lifefund.study-group.invite"


def validate_text(value: str, label: str) -> str:
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError(f"{label}不能包含控制字符。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label}包含无效字符。") from None
    return value.strip()


class GroupCreate(Input):
    name: str = Field(strict=True, min_length=1, max_length=60)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value):
        return validate_text(value, "群聊名称") if isinstance(value, str) else value


class GroupPatch(GroupCreate):
    expected_version: Version


class GroupVersionInput(Input):
    expected_version: Version


class GroupNicknamePatch(Input):
    nickname: str = Field(strict=True, max_length=60)

    @field_validator("nickname", mode="before")
    @classmethod
    def validate_nickname(cls, value):
        return validate_text(value, "本群昵称") if isinstance(value, str) else value


class GroupMembersRemove(GroupVersionInput):
    member_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)

    @field_validator("member_ids")
    @classmethod
    def unique_members(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("请勿重复选择成员。")
        return value


class GroupOwnerTransfer(GroupVersionInput):
    user_id: uuid.UUID
    leave_after: bool = Field(default=False, strict=True)


class GroupInviteToken(Input):
    token: str = Field(strict=True, min_length=1, max_length=256)


class GroupPermissions(BaseModel):
    can_invite: bool
    can_edit: bool
    can_remove: bool
    can_transfer: bool
    can_dissolve: bool


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    avatar_url: str | None = None
    index_price: str
    initial_price: str
    member_count: int
    max_members: int
    version: int
    created_at: datetime
    permissions: GroupPermissions


class GroupListResponse(BaseModel):
    items: list[GroupResponse]


class GroupMemberResponse(BaseModel):
    id: uuid.UUID
    display_name: str | None
    nickname: str | None
    effective_name: str
    avatar_url: str | None
    is_owner: bool
    is_self: bool


class GroupDetailResponse(GroupResponse):
    members: list[GroupMemberResponse]
    my_nickname: str | None


class GroupMutationResponse(BaseModel):
    group_id: uuid.UUID
    version: int


class GroupInviteResponse(BaseModel):
    id: uuid.UUID
    token: str
    expires_at: datetime
    created_version: int


class GroupInviteSummary(BaseModel):
    id: uuid.UUID
    name: str
    avatar_url: str | None
    member_count: int
    max_members: int


class GroupInvitePreviewResponse(BaseModel):
    group: GroupInviteSummary
    inviter_name: str
    expires_at: datetime
    already_member: bool
    can_join: bool
    reason: str | None = None


def group_query(user_id):
    own = aliased(StudyGroupMember)
    # Aggregate rewards separately so joining the current roster cannot repeat
    # a group's earned points once per member or multiply the member count.
    rewards = (select(func.coalesce(func.sum(StudyGroupCheckinReward.reward_points), 0))
               .where(StudyGroupCheckinReward.group_id == StudyGroup.id,
                      StudyGroupCheckinReward.awarded_at <= func.clock_timestamp())
               .correlate(StudyGroup).scalar_subquery())
    # Historical memberships never grant access or contribute to aggregates.
    return (select(StudyGroup, func.count(StudyGroupMember.user_id),
                   func.avg(func.coalesce(ClassIndexAccount.price, INITIAL_PRICE)) + rewards)
            .join(own, and_(own.group_id == StudyGroup.id, own.user_id == user_id, own.status == "active"))
            .join(StudyGroupMember, and_(StudyGroupMember.group_id == StudyGroup.id,
                                        StudyGroupMember.status == "active"))
            .outerjoin(ClassIndexAccount, ClassIndexAccount.user_id == StudyGroupMember.user_id)
            .where(StudyGroup.status == "active").group_by(StudyGroup.id)
            .order_by(StudyGroup.created_at, StudyGroup.id))


def group_result(row, user_id):
    group, count, index_price = row
    with localcontext() as context:
        context.prec = 40
        price = Decimal(index_price).quantize(CENT, rounding=ROUND_HALF_UP)
        initial = Decimal(group.initial_price).quantize(CENT, rounding=ROUND_HALF_UP)
    owner = group.owner_id == user_id
    return GroupResponse(id=group.id, name=group.name, owner_id=group.owner_id, avatar_url=group.avatar_url,
                         index_price=format(price, ".2f"), initial_price=format(initial, ".2f"),
                         member_count=count, max_members=group.max_members,
                         version=group.version, created_at=group.created_at,
                         permissions=GroupPermissions(can_invite=True, can_edit=owner, can_remove=owner,
                                                      can_transfer=owner, can_dissolve=owner))


def active_group_count(db, user_id):
    return db.scalar(select(func.count()).select_from(StudyGroupMember)
                     .join(StudyGroup, StudyGroup.id == StudyGroupMember.group_id)
                     .where(StudyGroupMember.user_id == user_id, StudyGroupMember.status == "active",
                            StudyGroup.status == "active"))


def member_count(db, group_id):
    return db.scalar(select(func.count()).select_from(StudyGroupMember)
                     .where(StudyGroupMember.group_id == group_id, StudyGroupMember.status == "active"))


def group_not_found():
    return HTTPException(404, "群聊不存在或你尚未加入。")


def assert_current_auth(db, user_id, request):
    # Recheck after lock waits and long uploads without storing credentials.
    authorization = request.headers.get("authorization", "").split(" ", 1)
    if len(authorization) != 2 or authorization[0].lower() != "bearer":
        raise unauthorized()
    auth = db.scalar(select(AuthSession).where(AuthSession.user_id == user_id,
                     AuthSession.token_hash == hash_token(authorization[1]))
                     .execution_options(populate_existing=True))
    if auth is None or auth.revoked_at is not None or auth.expires_at <= utcnow():
        raise unauthorized()


def ensure_writes_enabled(request):
    if not request.app.state.settings.group_management_enabled:
        raise HTTPException(503, "群聊管理功能暂未开放，请稍后重试。")


def group_mutate(db, user, request, key, body, status, action, render_response=None):
    ensure_writes_enabled(request)

    def authenticated_action():
        assert_current_auth(db, user.id, request)
        return action()

    def authenticated_response(result, replayed):
        assert_current_auth(db, user.id, request)
        return render_response(result, replayed) if render_response else result

    return mutate(db, user, request, key, body, status, authenticated_action,
                  no_key_update=True, render_response=authenticated_response)


def lock_group(db, group_id, user_id, request, *, require_member=True):
    group = db.scalar(select(StudyGroup).where(StudyGroup.id == group_id).with_for_update()
                      .execution_options(populate_existing=True))
    assert_current_auth(db, user_id, request)
    if group is None or group.status != "active":
        raise group_not_found()
    membership = db.scalar(select(StudyGroupMember).where(StudyGroupMember.group_id == group.id,
                           StudyGroupMember.user_id == user_id).with_for_update()
                           .execution_options(populate_existing=True))
    if require_member and (membership is None or membership.status != "active"):
        raise group_not_found()
    return group, membership


def require_owner(group, user_id):
    if group.owner_id != user_id:
        raise HTTPException(403, "只有群主可以执行此操作。")


def record_event(db, group, actor_id, event_type, payload=None, *, initial=False, at=None):
    if not initial:
        group.version += 1
    now = at or utcnow()
    group.updated_at = now
    db.add(StudyGroupEvent(group_id=group.id, version=group.version, event_type=event_type,
                           actor_user_id=actor_id, payload=payload or {}, created_at=now))
    db.flush()


def receipt(group):
    return {"group_id": str(group.id), "version": group.version}


def revoke_member_invites(db, group_id, user_ids, at):
    # Every invite write holds the group lock, so the rows cannot change here.
    invitations = db.scalars(select(StudyGroupInvite).where(StudyGroupInvite.group_id == group_id,
                             StudyGroupInvite.inviter_user_id.in_(user_ids),
                             StudyGroupInvite.revoked_at.is_(None)).with_for_update()).all()
    for invitation in invitations:
        invitation.revoked_at = at


def invite_secret(settings, key_id=None):
    if key_id is not None and key_id != settings.group_invite_key_id:
        raise HTTPException(410, "群聊邀请已失效，请联系群成员重新邀请。")
    secret = settings.group_invite_signing_key.encode("utf-8")
    if len(secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", settings.group_invite_key_id):
        raise HTTPException(503, "群聊邀请尚未配置。")
    return secret


def sign_invite(invitation, settings):
    secret = invite_secret(settings, invitation.key_id)
    message = f"{TOKEN_PURPOSE}:{TOKEN_VERSION}:{invitation.key_id}:{invitation.id}"
    signature = base64.urlsafe_b64encode(hmac.digest(secret, message.encode("ascii"), "sha256")).decode().rstrip("=")
    return f"{TOKEN_VERSION}.{invitation.key_id}.{invitation.id}.{signature}"


def parse_invite_token(token, settings):
    invalid = HTTPException(410, "群聊邀请已失效，请联系群成员重新邀请。")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        raise invalid
    version, key_id, invite_id_text, signature = parts
    try:
        invite_id = uuid.UUID(invite_id_text)
    except ValueError:
        raise invalid from None
    if str(invite_id) != invite_id_text or not re.fullmatch(r"[A-Za-z0-9_-]{43}", signature):
        raise invalid
    secret = invite_secret(settings, key_id)
    message = f"{TOKEN_PURPOSE}:{version}:{key_id}:{invite_id}"
    expected = base64.urlsafe_b64encode(hmac.digest(secret, message.encode("ascii"), "sha256")).decode().rstrip("=")
    if not hmac.compare_digest(signature, expected):
        raise invalid
    return invite_id, key_id


def invite_expires_at(invitation):
    # Older invitations may retain a longer stored TTL. Apply the current limit
    # to every card, without extending cards that already expire sooner.
    return min(invitation.expires_at, invitation.created_at + INVITE_TTL)


def valid_invite(db, invite_id, key_id, group_id, *, lock=False):
    query = select(StudyGroupInvite).where(StudyGroupInvite.id == invite_id, StudyGroupInvite.group_id == group_id)
    if lock:
        query = query.with_for_update()
    invitation = db.scalar(query.execution_options(populate_existing=True))
    # Current time must be evaluated after acquiring the group/invitation locks.
    if invitation is None or invitation.key_id != key_id:
        raise HTTPException(410, "群聊邀请已失效，请联系群成员重新邀请。")
    if invite_expires_at(invitation) <= utcnow():
        raise HTTPException(410, "邀请已过期，请联系群成员重新邀请。")
    if invitation.revoked_at is not None:
        raise HTTPException(410, "群聊邀请已失效，请联系群成员重新邀请。")
    inviter = db.scalar(select(StudyGroupMember).where(StudyGroupMember.group_id == group_id,
                        StudyGroupMember.user_id == invitation.inviter_user_id, StudyGroupMember.status == "active"))
    if inviter is None:
        raise HTTPException(410, "邀请人已离开群聊，请联系群成员重新邀请。")
    return invitation, inviter


def locate_invite(db, invite_id):
    group_id = db.scalar(select(StudyGroupInvite.group_id).where(StudyGroupInvite.id == invite_id))
    if group_id is None:
        raise HTTPException(410, "群聊邀请已失效，请联系群成员重新邀请。")
    return group_id


@router.get("/api/groups", response_model=GroupListResponse)
def list_groups(user: CurrentUser, db: Db):
    return GroupListResponse(items=[group_result(row, user.id) for row in db.execute(group_query(user.id))])


@router.get("/api/groups/{group_id}", response_model=GroupDetailResponse)
def get_group(group_id: uuid.UUID, user: CurrentUser, db: Db):
    row = db.execute(group_query(user.id).where(StudyGroup.id == group_id)).one_or_none()
    if row is None:
        raise group_not_found()
    group = row[0]
    members = db.execute(select(User, StudyGroupMember).join(StudyGroupMember, StudyGroupMember.user_id == User.id)
                         .where(StudyGroupMember.group_id == group.id, StudyGroupMember.status == "active")
                         .order_by((User.id == group.owner_id).desc(), StudyGroupMember.joined_at, User.id)).all()
    return GroupDetailResponse(**group_result(row, user.id).model_dump(),
        my_nickname=next(membership.nickname for member, membership in members if member.id == user.id),
        members=[GroupMemberResponse(id=member.id, display_name=member.display_name, nickname=membership.nickname,
                 effective_name=membership.nickname or member.display_name or "群成员", avatar_url=member.avatar_url,
                 is_owner=member.id == group.owner_id, is_self=member.id == user.id) for member, membership in members])


@router.post("/api/groups", status_code=201, response_model=GroupResponse)
def create_group(body: GroupCreate, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        if active_group_count(db, user.id) >= MAX_GROUPS_PER_USER:
            raise HTTPException(409, "最多可加入 20 个群聊。")
        # mutate has already acquired and refreshed the creator's user row.
        created_at = utcnow()
        complete_creation_midnight(db, user, created_at)
        initial_open = resolve_group_initial_open(db, user, created_at)
        group = StudyGroup(name=body.name, owner_id=user.id, initial_price=initial_open.price,
                           created_at=created_at, updated_at=created_at)
        db.add(group)
        db.flush()
        db.add(StudyGroupMember(group_id=group.id, user_id=user.id, joined_at=created_at,
                                updated_at=created_at))
        record_event(db, group, user.id, "created",
                     {"owner_id": str(user.id), **initial_open.event_payload()},
                     initial=True, at=created_at)
        return receipt(group)

    def render(result, replayed):
        # Replaying an old creation receipt cannot restore access after leaving.
        # Earlier versions persisted the complete GroupResponse with `id`.
        group_id = result.get("group_id", result.get("id"))
        group, _ = lock_group(db, uuid.UUID(group_id), user.id, request)
        row = db.execute(group_query(user.id).where(StudyGroup.id == group.id)).one()
        return group_result(row, user.id).model_dump(mode="json")

    return group_mutate(db, user, request, key, body, 201, action, render)


@router.post("/api/groups/{group_id}/invites", status_code=201, response_model=GroupInviteResponse)
def create_invite(group_id: uuid.UUID, body: Input, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    settings = request.app.state.settings

    def action():
        group, _ = lock_group(db, group_id, user.id, request)
        invite_secret(settings)
        now = utcnow()
        count = db.scalar(select(func.count()).select_from(StudyGroupInvite).where(
            StudyGroupInvite.group_id == group.id, StudyGroupInvite.inviter_user_id == user.id,
            StudyGroupInvite.created_at > now - timedelta(minutes=1)))
        if count >= INVITES_PER_MINUTE:
            raise HTTPException(429, "邀请生成过于频繁，请一分钟后再试。")
        invitation = StudyGroupInvite(group_id=group.id, inviter_user_id=user.id,
            key_id=settings.group_invite_key_id, created_version=group.version + 1,
            created_at=now, expires_at=now + INVITE_TTL)
        db.add(invitation)
        db.flush()
        record_event(db, group, user.id, "invited", {"invite_id": str(invitation.id)})
        return {"group_id": str(group.id), "invite_id": str(invitation.id)}

    def render(result, replayed):
        group, _ = lock_group(db, uuid.UUID(result["group_id"]), user.id, request)
        invitation = db.scalar(select(StudyGroupInvite).where(StudyGroupInvite.id == uuid.UUID(result["invite_id"]),
                               StudyGroupInvite.group_id == group.id).execution_options(populate_existing=True))
        if invitation is None or invitation.inviter_user_id != user.id:
            raise HTTPException(410, "群聊邀请已失效，请生成新邀请。")
        if invite_expires_at(invitation) <= utcnow():
            raise HTTPException(410, "邀请已过期，请重新邀请。")
        if invitation.revoked_at is not None:
            raise HTTPException(410, "群聊邀请已失效，请生成新邀请。")
        return GroupInviteResponse(id=invitation.id, token=sign_invite(invitation, settings),
                                   expires_at=invite_expires_at(invitation),
                                   created_version=invitation.created_version).model_dump(mode="json")

    return group_mutate(db, user, request, key, body, 201, action, render)


@router.post("/api/group-invites/preview", response_model=GroupInvitePreviewResponse)
def preview_invite(body: GroupInviteToken, request: Request, user: CurrentUser, db: Db):
    invite_id, key_id = parse_invite_token(body.token, request.app.state.settings)
    group_id = locate_invite(db, invite_id)
    # This read creates no membership. FOR SHARE excludes group modifications.
    group = db.scalar(select(StudyGroup).where(StudyGroup.id == group_id).with_for_update(read=True)
                      .execution_options(populate_existing=True))
    assert_current_auth(db, user.id, request)
    if group is None or group.status != "active":
        raise HTTPException(410, "群聊已解散，无法加入。")
    invitation, inviter = valid_invite(db, invite_id, key_id, group.id)
    membership = db.get(StudyGroupMember, (group.id, user.id))
    already_member = membership is not None and membership.status == "active"
    removed = membership is not None and invitation.created_version <= membership.last_removed_version
    if removed and not already_member:
        raise HTTPException(410, "你已被移出群聊，请使用群成员重新发出的邀请。")
    count = member_count(db, group.id)
    reason = None
    if not already_member:
        if count >= group.max_members:
            reason = "群聊人数已满。"
        elif active_group_count(db, user.id) >= MAX_GROUPS_PER_USER:
            reason = "最多可加入 20 个群聊。"
    inviter_user = db.get(User, invitation.inviter_user_id)
    return GroupInvitePreviewResponse(group=GroupInviteSummary(id=group.id, name=group.name,
        avatar_url=group.avatar_url, member_count=count, max_members=group.max_members),
        inviter_name=inviter.nickname or (inviter_user.display_name if inviter_user else None) or "群成员",
        expires_at=invite_expires_at(invitation), already_member=already_member,
        can_join=already_member or reason is None, reason=reason)


@router.post("/api/group-invites/accept", response_model=GroupMutationResponse)
def accept_invite(body: GroupInviteToken, request: Request, user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        invite_id, key_id = parse_invite_token(body.token, request.app.state.settings)
        group_id = locate_invite(db, invite_id)
        try:
            group, membership = lock_group(db, group_id, user.id, request, require_member=False)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(410, "群聊已解散，无法加入。") from None
            raise
        invitation, _ = valid_invite(db, invite_id, key_id, group.id, lock=True)
        if membership is not None and membership.status == "active":
            return receipt(group)
        if membership is not None and invitation.created_version <= membership.last_removed_version:
            raise HTTPException(410, "你已被移出群聊，请使用群成员重新发出的邀请。")
        if member_count(db, group.id) >= group.max_members:
            raise HTTPException(409, "群聊人数已满。")
        if active_group_count(db, user.id) >= MAX_GROUPS_PER_USER:
            raise HTTPException(409, "最多可加入 20 个群聊。")
        if membership is None:
            db.add(StudyGroupMember(group_id=group.id, user_id=user.id, joined_at=utcnow()))
        else:
            membership.status = "active"
            membership.joined_at = utcnow()
            membership.ended_at = None
            membership.updated_at = utcnow()
            # The last removal threshold survives rejoin and subsequent leave.
        record_event(db, group, user.id, "joined", {"invite_id": str(invitation.id)})
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)


@router.patch("/api/groups/{group_id}", response_model=GroupMutationResponse)
def update_group(group_id: uuid.UUID, body: GroupPatch, request: Request,
                 user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        group, _ = lock_group(db, group_id, user.id, request)
        require_owner(group, user.id)
        check_version(group, body.expected_version)
        if group.name != body.name:
            previous = group.name
            group.name = body.name
            record_event(db, group, user.id, "profile_updated", {"previous_name": previous, "name": body.name})
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)


def save_group_avatar(group_id, data, expected_version, request, user, db, key):
    def action():
        group, _ = lock_group(db, group_id, user.id, request)
        require_owner(group, user.id)
        check_version(group, expected_version)
        normalized = normalize_avatar(data)
        if group.avatar_data != normalized:
            group.avatar_id = uuid.uuid4()
            group.avatar_data = normalized
            record_event(db, group, user.id, "avatar_updated", {"avatar_id": str(group.avatar_id)})
        return receipt(group)

    return group_mutate(db, user, request, key,
                        {"content_sha256": hashlib.sha256(data).hexdigest()}, 200, action)


@router.post("/api/groups/{group_id}/avatar", response_model=GroupMutationResponse,
             openapi_extra={"requestBody": {"required": True, "content": {
                 mime: {"schema": {"type": "string", "format": "binary"}} for mime in ALLOWED_CONTENT_TYPES}}})
async def upload_group_avatar(group_id: uuid.UUID, request: Request, user: CurrentUser, db: Db,
                               key: IdempotencyKey, expected_version: int = Query(ge=1)):
    ensure_writes_enabled(request)
    content_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "头像仅支持 JPEG、PNG 或 WebP 图片。")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            size = int(content_length)
        except ValueError:
            raise HTTPException(400, "无效的头像请求长度。") from None
        if size < 0:
            raise HTTPException(400, "无效的头像请求长度。")
        if size > MAX_AVATAR_BYTES:
            raise HTTPException(413, "头像文件不能超过 2 MiB。")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_AVATAR_BYTES:
            raise HTTPException(413, "头像文件不能超过 2 MiB。")
        data.extend(chunk)
    return await run_in_threadpool(save_group_avatar, group_id, bytes(data), expected_version, request, user, db, key)


@router.get("/api/group-avatars/{avatar_id}", response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "头像不存在"}})
def get_group_avatar(avatar_id: uuid.UUID, db: Db):
    data = db.scalar(select(StudyGroup.avatar_data).where(StudyGroup.avatar_id == avatar_id,
                                                        StudyGroup.status == "active"))
    if data is None:
        raise HTTPException(404, "头像不存在。")
    return Response(content=data, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})


@router.patch("/api/groups/{group_id}/members/me", response_model=GroupMutationResponse)
def update_group_nickname(group_id: uuid.UUID, body: GroupNicknamePatch, request: Request,
                          user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        group, membership = lock_group(db, group_id, user.id, request)
        nickname = body.nickname or None
        if membership.nickname != nickname:
            previous = membership.nickname
            membership.nickname = nickname
            membership.updated_at = utcnow()
            record_event(db, group, user.id, "nickname_updated", {"previous_nickname": previous, "nickname": nickname})
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)


@router.post("/api/groups/{group_id}/members/remove", response_model=GroupMutationResponse)
def remove_group_members(group_id: uuid.UUID, body: GroupMembersRemove, request: Request,
                          user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        group, _ = lock_group(db, group_id, user.id, request)
        require_owner(group, user.id)
        check_version(group, body.expected_version)
        if group.owner_id in body.member_ids:
            raise HTTPException(403, "群主不能被移除，请先转让或解散群聊。")
        memberships = db.scalars(select(StudyGroupMember).where(StudyGroupMember.group_id == group.id,
                                StudyGroupMember.user_id.in_(body.member_ids)).order_by(StudyGroupMember.user_id)
                                .with_for_update().execution_options(populate_existing=True)).all()
        if len(memberships) != len(body.member_ids) or any(member.status != "active" for member in memberships):
            raise HTTPException(409, "成员列表已更新，请刷新后重新选择。")
        now = utcnow()
        for membership in memberships:
            membership.status = "removed"
            membership.ended_at = membership.updated_at = now
            membership.last_removed_version = group.version + 1
        revoke_member_invites(db, group.id, body.member_ids, now)
        record_event(db, group, user.id, "members_removed", {"user_ids": [str(item) for item in body.member_ids]})
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)


@router.post("/api/groups/{group_id}/leave", response_model=GroupMutationResponse)
def leave_group(group_id: uuid.UUID, body: Input, request: Request,
                 user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        group, membership = lock_group(db, group_id, user.id, request)
        if group.owner_id == user.id:
            raise HTTPException(409, "群主请先转让群聊，或明确解散群聊。")
        now = utcnow()
        membership.status = "left"
        membership.ended_at = membership.updated_at = now
        revoke_member_invites(db, group.id, [user.id], now)
        record_event(db, group, user.id, "left")
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)


@router.post("/api/groups/{group_id}/owner/transfer", response_model=GroupMutationResponse)
def transfer_group_owner(group_id: uuid.UUID, body: GroupOwnerTransfer, request: Request,
                           user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        group, membership = lock_group(db, group_id, user.id, request)
        require_owner(group, user.id)
        check_version(group, body.expected_version)
        if body.user_id == user.id:
            raise HTTPException(422, "请选择其他群成员接任群主。")
        successor = db.scalar(select(StudyGroupMember).where(StudyGroupMember.group_id == group.id,
                              StudyGroupMember.user_id == body.user_id).with_for_update()
                              .execution_options(populate_existing=True))
        if successor is None or successor.status != "active":
            raise HTTPException(409, "接任成员已离开群聊，请刷新后重新选择。")
        group.owner_id = body.user_id
        if body.leave_after:
            now = utcnow()
            membership.status = "left"
            membership.ended_at = membership.updated_at = now
            revoke_member_invites(db, group.id, [user.id], now)
        record_event(db, group, user.id, "owner_transferred", {
            "previous_owner_id": str(user.id), "owner_id": str(body.user_id), "left": body.leave_after})
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)


@router.post("/api/groups/{group_id}/dissolve", response_model=GroupMutationResponse)
def dissolve_group(group_id: uuid.UUID, body: GroupVersionInput, request: Request,
                    user: CurrentUser, db: Db, key: IdempotencyKey):
    def action():
        group, _ = lock_group(db, group_id, user.id, request)
        require_owner(group, user.id)
        check_version(group, body.expected_version)
        now = utcnow()
        group.status = "dissolved"
        group.dissolved_at = now
        invitations = db.scalars(select(StudyGroupInvite).where(StudyGroupInvite.group_id == group.id,
                                 StudyGroupInvite.revoked_at.is_(None)).with_for_update()).all()
        for invitation in invitations:
            invitation.revoked_at = now
        memberships = db.scalars(select(StudyGroupMember).where(StudyGroupMember.group_id == group.id,
                                  StudyGroupMember.status == "active").with_for_update()).all()
        for membership in memberships:
            membership.status = "left"
            membership.ended_at = membership.updated_at = now
        record_event(db, group, user.id, "dissolved")
        return receipt(group)

    return group_mutate(db, user, request, key, body, 200, action)
