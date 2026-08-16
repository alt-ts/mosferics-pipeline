"""create pricing schema

Rules in, prices out. Three tables.

`ops` is built first, at 0009, so every foreign key here is a real one. The
build doc originally ordered `pricing` before `ops`, which would have left
`variant_prices.channel_id` pointing at a table that did not exist yet; the
order was swapped rather than deferring the constraint. Nothing in `ops`
depends on `pricing`, so the swap costs nothing.

Tiered pricing (`pricing.tiers`, `pricing.tier_prices`) is explicitly not v1
and is not built here. The data model records it as purely additive, so
deferring costs nothing.

Hard rule 7 — `gross_margin` is derived at query time, never a stored input
column — meets the data model's `margin_pct` column here. They are the same
quantity. The reading taken: `margin_pct` is a stored *output*, a snapshot of
the margin at the moment the price was calculated, which is what the approval
gate has to judge. It is written by the pricing engine and never by hand. The
live margin against today's cost is a join to `cost_basis`, not this column.
Flagged for review rather than decided quietly — the alternative is to drop
the column and expose a view.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCOPES = "('global', 'supplier', 'category', 'product')"
METHODS = "('percent_markup', 'fixed_margin', 'target_price')"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA pricing")

    # ------------------------------------------------------------------ rules
    op.create_table(
        "rules",
        sa.Column("rule_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("scope_type", sa.Text, nullable=False),
        # Polymorphic: the table scope_id points at depends on scope_type, so
        # it cannot be a single SQL FK. Enforced in the pipeline and by the
        # CHECK on the type column, the same treatment documents.attach_id gets.
        sa.Column("scope_id", sa.BigInteger),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("value", sa.Numeric(12, 4), nullable=False),
        # Breaks ties within a scope level. Scope precedence itself — product
        # beats category beats supplier beats global — is resolution logic in
        # price.py, not an ordering of this column.
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "valid_from",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(f"scope_type IN {SCOPES}", name="ck_rules_scope_type"),
        sa.CheckConstraint(f"method IN {METHODS}", name="ck_rules_method"),
        # The global rule is the one with no target. Anything else must name one.
        sa.CheckConstraint(
            "(scope_type = 'global') = (scope_id IS NULL)",
            name="ck_rules_global_has_no_scope_id",
        ),
        # What a sane value looks like depends on the method, so the check does
        # too. A fixed margin of 120% or a target price of zero is a typo, and
        # this is where it should stop rather than three tables downstream.
        sa.CheckConstraint(
            "(method = 'percent_markup' AND value >= 0)"
            " OR (method = 'fixed_margin' AND value >= 0 AND value < 100)"
            " OR (method = 'target_price' AND value > 0)",
            name="ck_rules_value_matches_method",
        ),
        schema="pricing",
    )
    # Resolution reads every rule that could apply to one scope, newest first.
    op.create_index(
        "ix_rules_scope",
        "rules",
        ["scope_type", "scope_id", "priority"],
        schema="pricing",
    )
    # Deliberately not unique on (scope_type, scope_id): two rules may target
    # the same scope with different priorities or effective dates, and picking
    # between them is exactly what priority is for.

    # ------------------------------------------------------------- cost_basis
    # One row per variant: the current cost, and which offer it came from.
    op.create_table(
        "cost_basis",
        sa.Column("variant_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("cost_per_base_unit", sa.Numeric(12, 4), nullable=False),
        # Nullable: a manually entered cost has no offer behind it.
        sa.Column("source_offer_id", sa.BigInteger),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["catalog.variants.variant_id"],
            name="fk_cost_basis_variant",
        ),
        sa.ForeignKeyConstraint(
            ["source_offer_id"],
            ["supplier.supplier_offers.offer_id"],
            name="fk_cost_basis_source_offer",
        ),
        sa.CheckConstraint(
            "cost_per_base_unit >= 0", name="ck_cost_basis_cost_not_negative"
        ),
        schema="pricing",
    )
    # "Which variants price off this offer" is what a supplier price change asks.
    op.create_index(
        "ix_cost_basis_source_offer", "cost_basis", ["source_offer_id"], schema="pricing"
    )

    # ---------------------------------------------------------- variant_prices
    op.create_table(
        "variant_prices",
        sa.Column("variant_id", sa.BigInteger, nullable=False),
        sa.Column("channel_id", sa.Integer, nullable=False),
        sa.Column("calculated_price", sa.Numeric(12, 2)),
        # the reviewer's column. A refresh writes calculated_price and never touches
        # this one, which is the whole reason they are separate.
        sa.Column("override_price", sa.Numeric(12, 2)),
        # Generated, not written: COALESCE(override_price, calculated_price) is
        # the definition, so making it a stored column the engine has to keep
        # in step would be inviting drift. An INSERT that tries to supply it
        # fails, which is the correct outcome.
        sa.Column(
            "final_price",
            sa.Numeric(12, 2),
            sa.Computed("COALESCE(override_price, calculated_price)", persisted=True),
        ),
        # A fraction, not a percentage: 0.2000 is UK standard rate.
        sa.Column("vat_rate", sa.Numeric(5, 4)),
        # A percentage, not a fraction: 15.000 is the margin floor. Written by
        # the pricing engine as a snapshot at calculation time, never by hand.
        sa.Column("margin_pct", sa.Numeric(6, 3)),
        sa.Column(
            "status", sa.Text, nullable=False, server_default="pending_approval"
        ),
        sa.Column("approved_by", sa.Text),
        sa.PrimaryKeyConstraint("variant_id", "channel_id", name="pk_variant_prices"),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["catalog.variants.variant_id"],
            name="fk_variant_prices_variant",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["ops.channels.channel_id"],
            name="fk_variant_prices_channel",
        ),
        # auto_approved and approved are both live prices; the distinction is
        # how they got there, which is what the materiality threshold decides.
        sa.CheckConstraint(
            "status IN ('pending_approval', 'auto_approved', 'approved', 'rejected')",
            name="ck_variant_prices_status",
        ),
        sa.CheckConstraint(
            "calculated_price IS NULL OR calculated_price >= 0",
            name="ck_variant_prices_calculated_not_negative",
        ),
        sa.CheckConstraint(
            "override_price IS NULL OR override_price >= 0",
            name="ck_variant_prices_override_not_negative",
        ),
        sa.CheckConstraint(
            "vat_rate IS NULL OR (vat_rate >= 0 AND vat_rate <= 1)",
            name="ck_variant_prices_vat_rate_is_a_fraction",
        ),
        # Gross margin cannot reach 100% — that would need zero cost — but it
        # can go negative when a price sits below cost, which is exactly the
        # case the margin floor exists to catch, so it is not excluded here.
        sa.CheckConstraint(
            "margin_pct IS NULL OR margin_pct < 100",
            name="ck_variant_prices_margin_pct_is_a_percentage",
        ),
        schema="pricing",
    )
    # The approval queue reads by state.
    op.create_index(
        "ix_variant_prices_status", "variant_prices", ["status"], schema="pricing"
    )
    # "Everything priced for this channel" — the Shopify export's read.
    op.create_index(
        "ix_variant_prices_channel", "variant_prices", ["channel_id"], schema="pricing"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SCHEMA pricing CASCADE")
