"""Retain group membership history and add revocable invitations and events."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026_study_group_management"
down_revision = "0025_study_groups"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    # A missing owner membership cannot be repaired without guessing intent.
    # Raise a generic message rather than exposing existing account identifiers.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM study_groups g
                WHERE NOT EXISTS (
                    SELECT 1 FROM study_group_members m
                    WHERE m.group_id = g.id AND m.user_id = g.owner_id
                )
            ) THEN
                RAISE EXCEPTION 'Group migration requires every owner to be a member; review the existing data before retrying';
            END IF;
        END $$
    """)
    op.add_column("study_groups", sa.Column("max_members", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("study_groups", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("study_groups", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("study_groups", sa.Column("avatar_id", uid, nullable=True))
    op.add_column("study_groups", sa.Column("avatar_data", sa.LargeBinary(), nullable=True))
    op.add_column("study_groups", sa.Column("dissolved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_study_groups_max_members", "study_groups", "max_members BETWEEN 1 AND 10")
    op.create_check_constraint("ck_study_groups_status", "study_groups", "status IN ('active', 'dissolved')")
    op.create_check_constraint("ck_study_groups_version", "study_groups", "version >= 1")
    op.create_check_constraint("ck_study_groups_dissolved", "study_groups",
                               "(status = 'dissolved') = (dissolved_at IS NOT NULL)")
    op.create_check_constraint("ck_study_groups_avatar", "study_groups",
                               "(avatar_id IS NULL) = (avatar_data IS NULL)")
    op.create_unique_constraint("uq_study_groups_avatar_id", "study_groups", ["avatar_id"])
    op.drop_constraint("study_groups_owner_id_fkey", "study_groups", type_="foreignkey")
    op.create_foreign_key("fk_study_groups_owner_id", "study_groups", "users", ["owner_id"], ["id"],
                          ondelete="RESTRICT")

    op.add_column("study_group_members", sa.Column("nickname", sa.String(60), nullable=True))
    op.add_column("study_group_members", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("study_group_members", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("study_group_members", sa.Column("last_removed_version", sa.BigInteger(),
                                                 nullable=False, server_default="0"))
    op.add_column("study_group_members", sa.Column("updated_at", sa.DateTime(timezone=True),
                                                 nullable=False, server_default=sa.func.now()))
    op.create_check_constraint("ck_study_group_members_nickname", "study_group_members",
                               "nickname IS NULL OR (char_length(btrim(nickname)) BETWEEN 1 AND 60 "
                               "AND nickname = btrim(nickname))")
    op.create_check_constraint("ck_study_group_members_status", "study_group_members",
                               "status IN ('active', 'left', 'removed')")
    op.create_check_constraint("ck_study_group_members_ended", "study_group_members",
                               "(status = 'active') = (ended_at IS NULL)")
    op.create_check_constraint("ck_study_group_members_removed_version", "study_group_members",
                               "last_removed_version >= 0")
    op.create_index("ix_study_group_members_active_user", "study_group_members", ["user_id", "group_id"],
                    postgresql_where=sa.text("status = 'active'"))

    op.create_table("study_group_invites",
        sa.Column("id", uid, primary_key=True),
        sa.Column("group_id", uid, sa.ForeignKey("study_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inviter_user_id", uid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("created_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("key_id ~ '^[A-Za-z0-9_-]{1,32}$'", name="ck_study_group_invites_key_id"),
        sa.CheckConstraint("created_version >= 1", name="ck_study_group_invites_version"),
        sa.CheckConstraint("expires_at > created_at", name="ck_study_group_invites_expiry"),
    )
    op.create_index("ix_study_group_invites_creator_time", "study_group_invites",
                    ["group_id", "inviter_user_id", "created_at"])
    op.create_index("ix_study_group_invites_available", "study_group_invites", ["group_id", "expires_at"],
                    postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_table("study_group_events",
        sa.Column("id", uid, primary_key=True),
        sa.Column("group_id", uid, sa.ForeignKey("study_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor_user_id", uid, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "version", name="uq_study_group_events_version"),
        sa.CheckConstraint("version >= 1", name="ck_study_group_events_version"),
        sa.CheckConstraint("char_length(btrim(event_type)) BETWEEN 1 AND 40 AND event_type = btrim(event_type)",
                           name="ck_study_group_events_type"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_study_group_events_payload"),
    )
    # This records a migration baseline, not a fictional group creation event.
    # Deterministic migration-only UUIDs avoid requiring a PostgreSQL extension.
    # Counts above the new cap are deliberately retained and new joins are blocked
    # by the application while the current active membership exceeds the cap.
    op.execute("""
        INSERT INTO study_group_events (id, group_id, version, event_type, actor_user_id, payload, created_at)
        SELECT md5('study-group-migration-0026:' || g.id::text)::uuid,
               g.id, 1, 'group.migrated', NULL,
               jsonb_build_object(
                   'migration', '0026_study_group_management',
                   'original_created_at', g.created_at,
                   'max_members', g.max_members,
                   'over_capacity', (SELECT count(*) FROM study_group_members m WHERE m.group_id = g.id) > g.max_members,
                   'members', COALESCE((
                       SELECT jsonb_agg(jsonb_build_object('user_id', m.user_id, 'joined_at', m.joined_at)
                                        ORDER BY m.user_id)
                       FROM study_group_members m WHERE m.group_id = g.id
                   ), '[]'::jsonb)
               ), clock_timestamp()
        FROM study_groups g
    """)
    op.execute("""
        CREATE FUNCTION check_study_group_active_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            check_ids uuid[];
            check_id uuid;
            current_group record;
        BEGIN
            IF TG_TABLE_NAME = 'study_groups' THEN
                IF TG_OP = 'INSERT' THEN check_ids := ARRAY[NEW.id];
                ELSIF TG_OP = 'DELETE' THEN check_ids := ARRAY[OLD.id];
                ELSE check_ids := ARRAY[OLD.id, NEW.id]; END IF;
            ELSE
                IF TG_OP = 'INSERT' THEN check_ids := ARRAY[NEW.group_id];
                ELSIF TG_OP = 'DELETE' THEN check_ids := ARRAY[OLD.group_id];
                ELSE check_ids := ARRAY[OLD.group_id, NEW.group_id]; END IF;
            END IF;
            FOREACH check_id IN ARRAY check_ids LOOP
                SELECT id, owner_id, status INTO current_group
                FROM study_groups WHERE id = check_id FOR UPDATE;
                IF FOUND AND current_group.status = 'active' AND NOT EXISTS (
                    SELECT 1 FROM study_group_members
                    WHERE group_id = check_id AND user_id = current_group.owner_id AND status = 'active'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'An active study group must have an active owner membership',
                        CONSTRAINT = 'ck_study_groups_active_owner';
                END IF;
            END LOOP;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER ct_study_groups_active_owner
        AFTER INSERT OR UPDATE OR DELETE ON study_groups
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION check_study_group_active_owner()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER ct_study_group_members_active_owner
        AFTER INSERT OR UPDATE OR DELETE ON study_group_members
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION check_study_group_active_owner()
    """)


def downgrade():
    # Once stateful writes exist an old backend would resurrect ended memberships
    # and discard revocations. Only an untouched migration baseline is reversible.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM study_group_invites)
               OR EXISTS (SELECT 1 FROM study_group_events WHERE event_type <> 'group.migrated')
               OR EXISTS (SELECT 1 FROM study_groups WHERE status <> 'active' OR version <> 1 OR avatar_id IS NOT NULL)
               OR EXISTS (SELECT 1 FROM study_group_members
                          WHERE status <> 'active' OR nickname IS NOT NULL OR last_removed_version <> 0) THEN
                RAISE EXCEPTION 'Group management contains changes; disable writes and retain the current read implementation instead of downgrading';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER ct_study_group_members_active_owner ON study_group_members")
    op.execute("DROP TRIGGER ct_study_groups_active_owner ON study_groups")
    op.execute("DROP FUNCTION check_study_group_active_owner()")
    op.drop_table("study_group_events")
    op.drop_table("study_group_invites")
    op.drop_index("ix_study_group_members_active_user", table_name="study_group_members")
    for constraint in ("ck_study_group_members_nickname", "ck_study_group_members_status",
                       "ck_study_group_members_ended", "ck_study_group_members_removed_version"):
        op.drop_constraint(constraint, "study_group_members", type_="check")
    for column in ("updated_at", "last_removed_version", "ended_at", "status", "nickname"):
        op.drop_column("study_group_members", column)
    op.drop_constraint("fk_study_groups_owner_id", "study_groups", type_="foreignkey")
    op.create_foreign_key("study_groups_owner_id_fkey", "study_groups", "users", ["owner_id"], ["id"],
                          ondelete="CASCADE")
    op.drop_constraint("uq_study_groups_avatar_id", "study_groups", type_="unique")
    for constraint in ("ck_study_groups_max_members", "ck_study_groups_status", "ck_study_groups_version",
                       "ck_study_groups_dissolved", "ck_study_groups_avatar"):
        op.drop_constraint(constraint, "study_groups", type_="check")
    for column in ("dissolved_at", "avatar_data", "avatar_id", "version", "status", "max_members"):
        op.drop_column("study_groups", column)
