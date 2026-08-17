"""live_margins must multiply cost by base_units_per_variant

The view shipped in 0010 divided a whole-variant price by a single base unit's
cost, so every multi-unit variant reported a margin far higher than the truth.

    Box of 6, supplier case at £12, so cost_per_base_unit = £2.
    Sold at £18. True margin (18 - 12) / 18 = 33.3%.
    The 0010 view computed (18 - 2) / 18 = 88.9%.

The error is silent and always flattering, which is the dangerous direction:
it makes a thin margin look comfortable. It also scales with pack size, so the
bigger the box the wronger the number, and it vanishes entirely at
`base_units_per_variant = 1` — which is why the 0010 test did not catch it. The
test used a single implied unit and got the right answer for the wrong reason.

The fix is `cost_per_base_unit * base_units_per_variant`, which requires the
view to reach into `catalog.variants` for the pack factor.

Two consequences worth stating, because both are visible changes in what the
view returns:

  * `base_units_per_variant` is nullable, so a variant whose pack factor has
    not been populated now reports a NULL margin where it previously reported a
    confident wrong one. That is the honest answer — an unknown pack factor
    means an unknown margin — and it is deliberately NOT coalesced to 1, which
    would reproduce this exact bug for precisely the rows nobody has checked.
  * `base_units_per_variant` and the derived `variant_cost` are now columns of
    the view, so a NULL margin can be traced to its cause — missing cost, or
    missing pack factor — without a second query.

The join to `catalog.variants` is an inner join, unlike the one to
`cost_basis`. It cannot drop a row: `variant_prices.variant_id` is NOT NULL and
carries a foreign key to `catalog.variants`, so every price row has exactly one
variant. The invariant the 0010 test asserts — that the view returns as many
rows as `variant_prices` — still holds, and the test still checks it.

Replaced by DROP and CREATE rather than CREATE OR REPLACE: replacing a view can
only append columns at the end, and the new columns belong beside the cost they
derive from. Nothing depends on the view yet and it holds no data of its own.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LIVE_MARGINS_0011 = """
    CREATE VIEW pricing.live_margins AS
    SELECT vp.variant_id,
           vp.channel_id,
           vp.final_price,
           cb.cost_per_base_unit,
           v.base_units_per_variant,
           round(cb.cost_per_base_unit * v.base_units_per_variant, 4)
               AS variant_cost,
           round(
               (vp.final_price - cb.cost_per_base_unit * v.base_units_per_variant)
               / NULLIF(vp.final_price, 0) * 100,
               3
           ) AS gross_margin,
           vp.margin_pct AS margin_pct_at_calc,
           vp.status,
           cb.computed_at AS cost_computed_at
      FROM pricing.variant_prices vp
      JOIN catalog.variants v ON v.variant_id = vp.variant_id
      LEFT JOIN pricing.cost_basis cb ON cb.variant_id = vp.variant_id
"""

# Exactly as 0010 shipped it, so `downgrade` restores the bug rather than a
# third variant of the view. A downgrade should undo this migration, not
# improvise.
LIVE_MARGINS_0010 = """
    CREATE VIEW pricing.live_margins AS
    SELECT vp.variant_id,
           vp.channel_id,
           vp.final_price,
           cb.cost_per_base_unit,
           round(
               (vp.final_price - cb.cost_per_base_unit)
               / NULLIF(vp.final_price, 0) * 100,
               3
           ) AS gross_margin,
           vp.margin_pct AS margin_pct_at_calc,
           vp.status,
           cb.computed_at AS cost_computed_at
      FROM pricing.variant_prices vp
      LEFT JOIN pricing.cost_basis cb ON cb.variant_id = vp.variant_id
"""

COMMENT_0011 = """
    COMMENT ON VIEW pricing.live_margins IS
    'The live gross margin: final_price against today''s cost for the whole '
    'variant, i.e. cost_per_base_unit * base_units_per_variant. Derived at '
    'query time and never stored (hard rule 7). gross_margin is the current '
    'figure; margin_pct_at_calc is the stored snapshot the approval gate '
    'judged, and the two are expected to differ once cost moves. A NULL '
    'gross_margin means a missing input — check variant_cost to see which.'
"""

COMMENT_0010 = """
    COMMENT ON VIEW pricing.live_margins IS
    'The live gross margin: final_price against today''s cost. Derived at '
    'query time and never stored (hard rule 7). gross_margin is the current '
    'figure; margin_pct_at_calc is the stored snapshot the approval gate '
    'judged, and the two are expected to differ once cost moves.'
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("DROP VIEW pricing.live_margins")
    op.execute(LIVE_MARGINS_0011)
    op.execute(COMMENT_0011)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW pricing.live_margins")
    op.execute(LIVE_MARGINS_0010)
    op.execute(COMMENT_0010)
