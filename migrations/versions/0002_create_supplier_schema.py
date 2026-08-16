"""create supplier schema

Their world, standardised. `specs` stays JSONB until a field proves stable
enough to earn a real column. Supplier stock lives here and NEVER in
stock.levels (CLAUDE.md hard rule 4) — a supplier feed changes our confidence,
not our ownership.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Shared review-state vocabulary. The data model enumerates these for
# product_metadata.status; description_status is the same gate in a different
# table, so it uses the same three values.
REVIEW_STATES = "('pending_review', 'verified', 'rejected')"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA supplier")

    op.create_table(
        "suppliers",
        sa.Column("supplier_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        # The YAML in git that carries every supplier-specific behaviour.
        sa.Column("config_path", sa.Text),
        sa.Column("account_ref", sa.Text),
        sa.Column("min_order_value", sa.Numeric(12, 2)),
        sa.Column("carriage_paid_threshold", sa.Numeric(12, 2)),
        sa.Column("default_lead_time_days", sa.Integer),
        sa.Column("currency", sa.Text, nullable=False, server_default="GBP"),
        sa.Column(
            "availability_source",
            sa.Text,
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            "availability_source IN ('live_feed', 'static_lead_time', 'unknown')",
            name="ck_suppliers_availability_source",
        ),
        schema="supplier",
    )

    op.create_table(
        "supplier_products",
        sa.Column(
            "supplier_product_id", sa.BigInteger, sa.Identity(), primary_key=True
        ),
        sa.Column("supplier_id", sa.Integer, nullable=False),
        sa.Column("supplier_sku", sa.Text, nullable=False),
        sa.Column("name_raw", sa.Text, nullable=False),
        sa.Column("name_clean", sa.Text),
        # Free-text brand as the supplier sent it. Resolved to
        # catalog.brands.brand_id by the pipeline; never stored as text there.
        sa.Column("brand", sa.Text),
        sa.Column("supplier_category_raw", sa.Text),
        sa.Column("description_raw", sa.Text),
        sa.Column("description_enriched", sa.Text),
        sa.Column("description_source", sa.Text),
        sa.Column("description_status", sa.Text),
        sa.Column(
            "specs", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "first_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "discontinued", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["supplier.suppliers.supplier_id"],
            name="fk_supplier_products_supplier",
        ),
        # A supplier SKU identifies one product within that supplier. This is
        # the key re-runs match on, which is what makes ingest idempotent.
        sa.UniqueConstraint(
            "supplier_id", "supplier_sku", name="uq_supplier_products_sku"
        ),
        sa.CheckConstraint(
            "description_source IS NULL OR "
            "description_source IN ('supplier', 'ai', 'manual')",
            name="ck_supplier_products_description_source",
        ),
        sa.CheckConstraint(
            f"description_status IS NULL OR description_status IN {REVIEW_STATES}",
            name="ck_supplier_products_description_status",
        ),
        schema="supplier",
    )
    # Drives the "unmapped categories" review view.
    op.create_index(
        "ix_supplier_products_category_raw",
        "supplier_products",
        ["supplier_id", "supplier_category_raw"],
        schema="supplier",
    )
    op.create_index(
        "ix_supplier_products_brand", "supplier_products", ["brand"], schema="supplier"
    )
    # Specs are queried by key before any field is promoted to a column.
    op.create_index(
        "ix_supplier_products_specs",
        "supplier_products",
        ["specs"],
        postgresql_using="gin",
        schema="supplier",
    )

    op.create_table(
        "supplier_offers",
        sa.Column("offer_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("supplier_product_id", sa.BigInteger, nullable=False),
        sa.Column("pack_description", sa.Text),
        # Mandatory: cost-per-base-unit is what makes a case of 6 comparable
        # to a case of 10. Numeric, not integer — a base unit can be a litre.
        sa.Column("base_units_per_pack", sa.Numeric(12, 4), nullable=False),
        sa.Column("cost_ex_vat", sa.Numeric(12, 4)),
        sa.Column("moq", sa.Integer),
        # Customer-facing promise, not an internal note.
        sa.Column("lead_time_days", sa.Integer),
        sa.Column(
            "valid_from",
            sa.Date,
            nullable=False,
            server_default=sa.text("CURRENT_DATE"),
        ),
        sa.Column("valid_to", sa.Date),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["supplier_product_id"],
            ["supplier.supplier_products.supplier_product_id"],
            name="fk_supplier_offers_product",
        ),
        sa.CheckConstraint(
            "base_units_per_pack > 0", name="ck_supplier_offers_base_units_positive"
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_supplier_offers_valid_range",
        ),
        schema="supplier",
    )
    # At most one current offer per supplier product — otherwise cost_basis has
    # no single answer to "what do we pay today".
    op.create_index(
        "uq_supplier_offers_one_current",
        "supplier_offers",
        ["supplier_product_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        schema="supplier",
    )

    op.create_table(
        "supplier_stock",
        # One current snapshot per supplier product, hence the PK.
        sa.Column(
            "supplier_product_id", sa.BigInteger, primary_key=True, autoincrement=False
        ),
        sa.Column("qty_available", sa.Integer),
        sa.Column("next_delivery_date", sa.Date),
        # Staleness window is read off this: feeds fail quietly, so an old row
        # falls back to static lead time rather than being believed.
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("feed_status", sa.Text),
        sa.ForeignKeyConstraint(
            ["supplier_product_id"],
            ["supplier.supplier_products.supplier_product_id"],
            name="fk_supplier_stock_product",
        ),
        schema="supplier",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SCHEMA supplier CASCADE")
