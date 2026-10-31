"""let the review queue point at a supplier product

`ops.review_queue.entity_type` allowed six kinds of thing, and none of them is
the row normalise.py has in hand. A pack size that disagrees between two files,
an unreadable line mark, a capacity that contradicts itself, a pair of safety
claims that cannot both be true: each is a fault in what one supplier sent about
one SKU, found before any catalog product exists to hang it on.

Filing them under `vocabulary` would have fitted the constraint and broken the
view: that view's instruction is "add the alias to the YAML", which is no answer
to any of them. So `supplier_product` is its own kind, with `entity_id` holding
`supplier.supplier_products.supplier_product_id`.

The downgrade raises if any such row exists, rather than deleting them: a queue
item may hold someone's judgement in `notes`, and no migration may discard it.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENTITIES_BEFORE = (
    "('product_metadata', 'variant_link', 'variant_price', 'category_map', "
    "'vocabulary', 'image')"
)
ENTITIES_AFTER = (
    "('product_metadata', 'variant_link', 'variant_price', 'category_map', "
    "'vocabulary', 'image', 'supplier_product')"
)


def _replace_check(entities: str) -> None:
    op.drop_constraint(
        "ck_review_queue_entity_type", "review_queue", type_="check", schema="ops"
    )
    op.create_check_constraint(
        "ck_review_queue_entity_type",
        "review_queue",
        f"entity_type IN {entities}",
        schema="ops",
    )


def upgrade() -> None:
    """Upgrade schema."""
    _replace_check(ENTITIES_AFTER)


def downgrade() -> None:
    """Downgrade schema."""
    _replace_check(ENTITIES_BEFORE)
