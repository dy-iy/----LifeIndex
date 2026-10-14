"""Freeze each group's original creator's personal creation-day opening quote.

This migration deliberately reads immutable historical facts rather than an
account's current price. It does not backdate group membership or rewrite events.
Keep the resolver here independent of runtime market/helper implementations so
later application changes cannot alter the meaning of this migration.
"""
import uuid
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "0027_group_initial_open"
down_revision = "0026_study_group_management"
branch_labels = None
depends_on = None


def _creator(connection, group):
    events = connection.execute(sa.text("""
        SELECT event_type, actor_user_id, payload FROM study_group_events
        WHERE group_id = :group_id ORDER BY version
    """), {"group_id": group["id"]}).mappings().all()
    for event in events:
        if event["event_type"] == "created":
            owner = (event["payload"] or {}).get("owner_id") or event["actor_user_id"]
            if owner is not None:
                return uuid.UUID(str(owner))
    for event in events:
        if event["event_type"] == "owner_transferred":
            owner = (event["payload"] or {}).get("previous_owner_id") or event["actor_user_id"]
            if owner is not None:
                return uuid.UUID(str(owner))
    return group["owner_id"]  # Legacy groups have no creation ledger.


def _historical_open(connection, user_id, created_at):
    parameters = {"user_id": user_id, "created_at": created_at}
    state = connection.execute(sa.text("""
        SELECT activated_at, cursor_at, timezone FROM class_market_states
        WHERE user_id = :user_id
    """), parameters).mappings().first()
    active = state is not None and state["activated_at"] <= created_at
    if active:
        zone_name = state["timezone"]
    else:
        # Before continuous-market activation, reconstruct the owner's historical
        # calendar from recorded settlements, not a possibly changed account zone.
        zone_name = connection.scalar(sa.text("""
            SELECT timezone FROM class_index_events
            WHERE user_id = :user_id
              AND business_date = (CAST(:created_at AS timestamptz) AT TIME ZONE timezone)::date
            ORDER BY sequence LIMIT 1
        """), parameters)
        if zone_name is None:
            zone_name = connection.scalar(sa.text("""
                SELECT timezone FROM class_market_days
                WHERE user_id = :user_id
                  AND business_date = (CAST(:created_at AS timestamptz) AT TIME ZONE timezone)::date
                ORDER BY business_date LIMIT 1
            """), parameters)
        if zone_name is None:
            zone_name = connection.scalar(sa.text("""
                SELECT timezone FROM class_index_events
                WHERE user_id = :user_id AND created_at <= :created_at
                ORDER BY created_at DESC, sequence DESC LIMIT 1
            """), parameters)
        if zone_name is None:
            zone_name = connection.scalar(sa.text("SELECT timezone FROM users WHERE id = :user_id"), parameters)
        zone_name = zone_name or "Asia/Shanghai"
    zone = ZoneInfo(zone_name)
    calendar_date = created_at.astimezone(zone).date()
    boundary = datetime.combine(calendar_date, time.min, zone).astimezone(timezone.utc)
    parameters.update(calendar_date=calendar_date, boundary=boundary, zone=zone_name)

    if active:
        opening = connection.scalar(sa.text("""
            SELECT open FROM class_market_days
            WHERE user_id = :user_id AND business_date = :calendar_date
        """), parameters)
        if opening is not None:
            return Decimal(opening)
        # At activation, an earlier course settlement may have already moved the
        # account. Otherwise the activation observation is that day's true open.
        if state["activated_at"].astimezone(zone).date() == calendar_date:
            activation = connection.scalar(sa.text("""
                SELECT price FROM class_market_points
                WHERE user_id = :user_id AND kind = 'activation'
                  AND occurred_at = :activated_at ORDER BY sequence LIMIT 1
            """), {**parameters, "activated_at": state["activated_at"]})
            legacy_open = connection.scalar(sa.text("""
                SELECT previous_close FROM class_index_events
                WHERE user_id = :user_id AND business_date = :calendar_date
                  AND created_at <= :activated_at ORDER BY sequence LIMIT 1
            """), {**parameters, "activated_at": state["activated_at"]})
            if legacy_open is not None:
                return Decimal(legacy_open)
            if activation is not None:
                return Decimal(activation)
        if state["cursor_at"] < boundary:
            # A checkpoint before midnight has not yet produced the requested
            # opening. Maintenance must catch up before this migration is retried.
            raise RuntimeError("Group opening migration requires the creator market to catch up across its creation-day boundary")

    legacy_open = connection.scalar(sa.text("""
        SELECT previous_close FROM class_index_events
        WHERE user_id = :user_id AND business_date = :calendar_date
        ORDER BY sequence LIMIT 1
    """), parameters)
    if legacy_open is not None:
        return Decimal(legacy_open)

    # Latest real observation at the midnight boundary; a boundary 'tick' belongs
    # to the previous close, while after-message changes must not enter the open.
    prior = connection.scalar(sa.text("""
        SELECT price FROM (
            SELECT price, occurred_at AS observed_at, sequence, 3 AS priority
            FROM class_market_points
            WHERE user_id = :user_id
              AND (occurred_at < :boundary OR (occurred_at = :boundary AND kind IN ('tick', 'open')))
            UNION ALL
            SELECT price_after AS price, created_at AS observed_at, sequence, 2 AS priority
            FROM class_index_events
            WHERE user_id = :user_id AND created_at < :boundary
            UNION ALL
            SELECT close AS price,
                   ((business_date + 1)::timestamp AT TIME ZONE timezone) AS observed_at,
                   0 AS sequence, 1 AS priority
            FROM class_market_days
            WHERE user_id = :user_id
              AND ((business_date + 1)::timestamp AT TIME ZONE timezone) <= :boundary
        ) observations
        ORDER BY observed_at DESC, priority DESC, sequence DESC LIMIT 1
    """), parameters)
    if prior is None:
        if active:
            raise RuntimeError("Group opening migration could not recover the creator's historical market opening")
        return Decimal("1000")  # The personal index was not yet opened.
    # Dormant personal quotes use the displayed previous close; continuous day
    # openings retain the market's full precision.
    with localcontext() as context:
        context.prec = 48
        return Decimal(prior) if active else Decimal(prior).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def upgrade():
    connection = op.get_bind()
    op.add_column("study_groups", sa.Column("initial_price", sa.Numeric(36, 8), nullable=True))
    groups = connection.execute(sa.text("SELECT id, owner_id, created_at FROM study_groups ORDER BY id")).mappings().all()
    for group in groups:
        value = _historical_open(connection, _creator(connection, group), group["created_at"])
        if not value.is_finite() or value < 1:
            raise RuntimeError("Group opening migration found an invalid creator opening quote")
        connection.execute(sa.text("UPDATE study_groups SET initial_price = :price WHERE id = :group_id"),
                           {"price": value, "group_id": group["id"]})
    # The 0026 owner invariant is a deferred constraint trigger. Backfilling
    # existing groups queues it, and PostgreSQL refuses subsequent ALTER TABLE
    # while those events are pending. Validate them now, then restore its mode.
    op.execute("SET CONSTRAINTS ct_study_groups_active_owner IMMEDIATE")
    op.execute("SET CONSTRAINTS ct_study_groups_active_owner DEFERRED")
    op.alter_column("study_groups", "initial_price", nullable=False)
    op.create_check_constraint("ck_study_groups_initial_price", "study_groups",
                               "initial_price >= 1 AND initial_price <> 'NaN'::numeric")
    op.execute("""
        CREATE FUNCTION protect_study_group_initial_price() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.initial_price IS DISTINCT FROM OLD.initial_price THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'A study group creation-day opening quote is immutable',
                    CONSTRAINT = 'ck_study_groups_initial_price_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER tr_study_groups_initial_price_immutable
        BEFORE UPDATE OF initial_price ON study_groups FOR EACH ROW
        EXECUTE FUNCTION protect_study_group_initial_price()
    """)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM study_groups)")):
        raise RuntimeError("Study groups have immutable opening baselines; retain the new read logic instead of discarding them")
    op.execute("DROP TRIGGER tr_study_groups_initial_price_immutable ON study_groups")
    op.execute("DROP FUNCTION protect_study_group_initial_price()")
    op.drop_constraint("ck_study_groups_initial_price", "study_groups", type_="check")
    op.drop_column("study_groups", "initial_price")
