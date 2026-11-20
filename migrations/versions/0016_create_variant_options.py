"""create catalog.variant_options

A variant's option values had no home. `catalog.variants` carries
`variant_title` and nothing structured, and `product_metadata` is keyed by
product, so a glass in 25cl and 35cl could not say which variant is which. Yet
Shopify needs the option values per variant, and capacity is also a filterable
variant-level `volume` metafield (build instructions, Stage 7).

One row per variant per axis. `value` is the selector label as the supplier
wrote it ("35.5cl"); `canonical_value` + `unit` are the number it filters and
sorts on (355, 'ml'), set together or not at all, the same pairing
product_metadata uses. `qualifier` is the second distinguishing value: the
line mark that keeps a lined glass apart from its unlined twin at the same
brim capacity.

Option position is not stored. It is `taxonomy.variant_axes` order, fixed per
node; a copy here could disagree with it.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "variant_options",
        sa.Column("variant_id", sa.BigInteger, nullable=False),
        # A taxonomy axis name. Which axes a variant may carry is its node's
        # variant_axes, which a CHECK cannot reach; link.py enforces it.
        sa.Column("axis", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("canonical_value", sa.Numeric),
        sa.Column("unit", sa.Text),
        sa.Column("qualifier", sa.Text),
        sa.PrimaryKeyConstraint("variant_id", "axis", name="pk_variant_options"),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["catalog.variants.variant_id"], name="fk_variant_options_variant"
        ),
        sa.CheckConstraint("axis ~ '^[a-z][a-z_]*$'", name="ck_variant_options_axis_format"),
        sa.CheckConstraint("value <> ''", name="ck_variant_options_value_not_empty"),
        sa.CheckConstraint(
            "(canonical_value IS NULL) = (unit IS NULL)",
            name="ck_variant_options_canonical_has_unit",
        ),
        schema="catalog",
    )
    # The storefront filter reads by axis and value ("glasses 300-400 ml").
    op.create_index(
        "ix_variant_options_axis_value",
        "variant_options",
        ["axis", "canonical_value"],
        schema="catalog",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Raises rather than discarding option values: they cannot be re-derived
    # once a human has corrected one, and no migration may drop live data.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM catalog.variant_options) THEN
                RAISE EXCEPTION 'catalog.variant_options holds rows; refusing to drop it';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_variant_options_axis_value", table_name="variant_options", schema="catalog")
    op.drop_table("variant_options", schema="catalog")
