"""create ops schema

Operational plumbing. Seven tables, two of them seeded here rather than later.

Built before `pricing`, which is a swap against the build doc's listed order.
The doc puts `pricing` fifth and `ops` sixth, but
`pricing.variant_prices.channel_id` is a real FK to `ops.channels`, so that
order cannot be executed without deferring a constraint. Nothing in `ops`
depends on `pricing`, so the swap costs nothing and removes the deferral. Same
class of conflict as `documents` before `compliance` inside 0005.

`channels` and `delivery_config` are created with their rows, deliberately.
Both hold parameters that will change as the business grows — a second
storefront, an extra delivery day, a bank holiday — and every one of those
should be a data edit the reviewer can make in NocoDB, not a migration.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The review queue's vocabulary, straight from the data model.
QUEUE_ENTITIES = (
    "('product_metadata', 'variant_link', 'variant_price', 'category_map', "
    "'vocabulary', 'image')"
)
WEEKDAYS = "['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA ops")

    # --------------------------------------------------------------- channels
    op.create_table(
        "channels",
        sa.Column("channel_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("shopify_domain", sa.Text),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            "type IN ('trade_store', 'event_store')", name="ck_channels_type"
        ),
        sa.UniqueConstraint("name", name="uq_channels_name"),
        schema="ops",
    )
    # One row now, so a second storefront is data rather than a migration
    # across four tables. The domain is left null until the store exists.
    op.execute(
        """
        INSERT INTO ops.channels (name, type, shopify_domain, active)
        VALUES ('Trade store', 'trade_store', NULL, true)
        """
    )

    # -------------------------------------------------------- channel_publish
    op.create_table(
        "channel_publish",
        sa.Column("variant_id", sa.BigInteger, nullable=False),
        sa.Column("channel_id", sa.Integer, nullable=False),
        sa.Column(
            "publish_status", sa.Text, nullable=False, server_default="not_published"
        ),
        # Product x channel, not product: a pallet of cups is dropshipped for
        # an event and routed through the warehouse for a trade order.
        sa.Column("fulfilment_mode", sa.Text),
        sa.PrimaryKeyConstraint("variant_id", "channel_id", name="pk_channel_publish"),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["catalog.variants.variant_id"],
            name="fk_channel_publish_variant",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["ops.channels.channel_id"], name="fk_channel_publish_channel"
        ),
        sa.CheckConstraint(
            "publish_status IN ('not_published', 'pending', 'published')",
            name="ck_channel_publish_status",
        ),
        # Three modes, not two. Trade is stocked or on_demand, both of which
        # transit our warehouse; events add true dropship, where goods never
        # touch us.
        sa.CheckConstraint(
            "fulfilment_mode IS NULL"
            " OR fulfilment_mode IN ('stocked', 'on_demand', 'dropship')",
            name="ck_channel_publish_fulfilment_mode",
        ),
        schema="ops",
    )
    # "Everything published on this channel" is the Shopify export's read.
    op.create_index(
        "ix_channel_publish_channel_status",
        "channel_publish",
        ["channel_id", "publish_status"],
        schema="ops",
    )

    # --------------------------------------------------------- delivery_config
    # Config only, no logic. The delivery promise engine that reads it is a
    # Stage 8 build. Single-row by construction: config_id is pinned to 1, so
    # there is no way to end up with two rows disagreeing about the cutoff.
    op.create_table(
        "delivery_config",
        sa.Column("config_id", sa.SmallInteger, primary_key=True, autoincrement=False),
        # Weekday names rather than ISO numbers: the reviewer edits this in NocoDB and
        # 'tue' is unambiguous where 2 is not.
        sa.Column("delivery_days", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("cutoff_time", sa.Time, nullable=False),
        # Non-negotiable and explicit: the cutoff and "today" are meaningless
        # without it, and BST/GMT shifts produce wrong dates.
        sa.Column("timezone", sa.Text, nullable=False),
        sa.Column("handling_days", sa.Integer, nullable=False),
        sa.Column(
            "holiday_calendar",
            postgresql.ARRAY(sa.Date),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.CheckConstraint("config_id = 1", name="ck_delivery_config_single_row"),
        sa.CheckConstraint(
            f"delivery_days <@ ARRAY{WEEKDAYS}::text[]",
            name="ck_delivery_config_weekday_names",
        ),
        # cardinality(), not array_length(): array_length of an empty array is
        # NULL, not 0, and a CHECK passes on NULL — so the obvious spelling of
        # this constraint lets through a config with no delivery days at all.
        sa.CheckConstraint(
            "cardinality(delivery_days) > 0",
            name="ck_delivery_config_has_a_delivery_day",
        ),
        sa.CheckConstraint(
            "handling_days >= 0", name="ck_delivery_config_handling_days_not_negative"
        ),
        schema="ops",
    )
    op.execute(
        """
        INSERT INTO ops.delivery_config
            (config_id, delivery_days, cutoff_time, timezone, handling_days,
             holiday_calendar)
        VALUES
            (1, ARRAY['tue','wed','thu'], TIME '12:00', 'Europe/London', 1, '{}')
        """
    )

    # ----------------------------------------------------------- review_queue
    op.create_table(
        "review_queue",
        sa.Column("queue_id", sa.BigInteger, sa.Identity(), primary_key=True),
        # Polymorphic: entity_id's table depends on entity_type, so it cannot
        # be a single SQL FK. CHECK on the type column, pipeline enforces the
        # reference — the same treatment catalog.documents.attach_id gets.
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("entity_id", sa.BigInteger, nullable=False),
        sa.Column("issue_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("assigned_to", sa.Text),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_by", sa.Text),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            f"entity_type IN {QUEUE_ENTITIES}", name="ck_review_queue_entity_type"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')", name="ck_review_queue_status"
        ),
        # Same pairing rule the link schema uses: a resolution with no resolver
        # is not a resolution.
        sa.CheckConstraint(
            "(resolved_by IS NULL) = (resolved_at IS NULL)",
            name="ck_review_queue_resolution_complete",
        ),
        # An open item has not been resolved by anyone. Dismissed counts as
        # resolved — someone decided, and that decision is worth attributing.
        sa.CheckConstraint(
            "status = 'open' OR resolved_by IS NOT NULL",
            name="ck_review_queue_closed_has_resolver",
        ),
        sa.CheckConstraint(
            "status <> 'open' OR resolved_by IS NULL",
            name="ck_review_queue_open_is_unresolved",
        ),
        schema="ops",
    )
    # The queue is read as "what is open, by kind".
    op.create_index(
        "ix_review_queue_status_type",
        "review_queue",
        ["status", "entity_type"],
        schema="ops",
    )
    # And "is this thing already queued?", which is the dedupe check.
    op.create_index(
        "ix_review_queue_entity",
        "review_queue",
        ["entity_type", "entity_id"],
        schema="ops",
    )

    # ---------------------------------------------------------- pipeline_runs
    op.create_table(
        "pipeline_runs",
        sa.Column("run_id", sa.BigInteger, sa.Identity(), primary_key=True),
        # Nullable: a run that spans every supplier belongs to none of them.
        sa.Column("supplier_id", sa.Integer),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column(
            "summary", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["supplier.suppliers.supplier_id"],
            name="fk_pipeline_runs_supplier",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_pipeline_runs_status",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_pipeline_runs_finished_after_started",
        ),
        # A run still going has no finish time, and a finished one has one.
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name="ck_pipeline_runs_finish_matches_status",
        ),
        schema="ops",
    )
    op.create_index(
        "ix_pipeline_runs_supplier_started",
        "pipeline_runs",
        ["supplier_id", "started_at"],
        schema="ops",
    )

    # ----------------------------------------------------------- shopify_sync
    op.create_table(
        "shopify_sync",
        # Required, not optional: without it the same variant cannot hold a
        # Shopify GID for two storefronts.
        sa.Column("channel_id", sa.Integer, nullable=False),
        sa.Column("entity", sa.Text, nullable=False),
        sa.Column("entity_id", sa.BigInteger, nullable=False),
        sa.Column("shopify_gid", sa.Text),
        sa.Column("shopify_handle", sa.Text),
        sa.Column("last_synced", sa.TIMESTAMP(timezone=True)),
        # Push only what changed, rather than the whole catalogue every time.
        sa.Column("payload_hash", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        # The natural identity, and it stops one entity holding two sync rows
        # for the same storefront. The data model does not name a key here.
        sa.PrimaryKeyConstraint(
            "channel_id", "entity", "entity_id", name="pk_shopify_sync"
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["ops.channels.channel_id"], name="fk_shopify_sync_channel"
        ),
        sa.CheckConstraint(
            "entity IN ('product', 'variant', 'collection', 'metaobject')",
            name="ck_shopify_sync_entity",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'synced', 'failed')", name="ck_shopify_sync_status"
        ),
        schema="ops",
    )
    op.create_index(
        "ix_shopify_sync_status", "shopify_sync", ["status"], schema="ops"
    )
    # "What is this Shopify object in our world" — the inbound direction.
    op.create_index(
        "ix_shopify_sync_gid",
        "shopify_sync",
        ["shopify_gid"],
        schema="ops",
        postgresql_where=sa.text("shopify_gid IS NOT NULL"),
    )

    # ---------------------------------------------------------- metafield_map
    op.create_table(
        "metafield_map",
        sa.Column("internal_key", sa.Text, nullable=False),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("shopify_key", sa.Text, nullable=False),
        sa.Column("shopify_type", sa.Text, nullable=False),
        # The column that gets forgotten: dimensions shared across colours
        # belong on the product, anything differing per variant must be
        # variant-level.
        sa.Column("owner_type", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("internal_key", "owner_type", name="pk_metafield_map"),
        sa.CheckConstraint(
            "owner_type IN ('product', 'variant')", name="ck_metafield_map_owner_type"
        ),
        # Two internal keys must not target one Shopify field on the same owner.
        sa.UniqueConstraint(
            "namespace", "shopify_key", "owner_type", name="uq_metafield_map_target"
        ),
        schema="ops",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SCHEMA ops CASCADE")
