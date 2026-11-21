"""review.ready_to_publish: only a variant someone means to publish is ready

`ops.channel_publish.publish_status` is not_published / pending / published,
and `pending` is publish intent: review.missing_images already reads it that
way ("products with publish intent and no usable image"). ready_to_publish
instead tested `publish_status <> 'published'`, so a row set to
`not_published` (a product withdrawn from a channel, or never meant for it)
still counted as ready, and the export would have carried it. Found while
writing pipeline/publish.py, which is the first thing to write these rows;
until now the table was empty and the two readings could not disagree.

Same columns, same grants: CREATE OR REPLACE keeps the view's privileges.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-21

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0019'
down_revision: Union[str, Sequence[str], None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VIEW = """
    CREATE OR REPLACE VIEW review.ready_to_publish AS
    SELECT cp.variant_id::text || ':' || cp.channel_id::text AS id,
           cp.variant_id,
           cp.channel_id,
           v.mosferics_sku,
           p.title,
           pr.final_price,
           pr.margin_pct,
           cp.fulfilment_mode
      FROM ops.channel_publish cp
      JOIN catalog.variants v ON v.variant_id = cp.variant_id
      JOIN catalog.products p ON p.product_id = v.product_id
      JOIN pricing.variant_prices pr
           ON pr.variant_id = cp.variant_id
          AND pr.channel_id = cp.channel_id
     WHERE {intent}
       AND p.status = 'active'
       -- Both are live prices.
       AND pr.status IN ('approved', 'auto_approved')
       -- has a preferred source
       AND EXISTS (SELECT 1 FROM link.variant_links l
                    WHERE l.variant_id = cp.variant_id AND l.is_preferred)
       -- at least one verified image
       AND EXISTS (SELECT 1 FROM catalog.images i
                    WHERE i.product_id = p.product_id
                      AND i.status = 'verified')
       -- nothing still awaiting review
       AND NOT EXISTS (SELECT 1 FROM catalog.product_metadata m
                        WHERE m.product_id = p.product_id
                          AND m.status = 'pending_review')
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(VIEW.format(intent="cp.publish_status = 'pending'  -- publish intent, and not yet published"))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(VIEW.format(intent="cp.publish_status <> 'published'"))
