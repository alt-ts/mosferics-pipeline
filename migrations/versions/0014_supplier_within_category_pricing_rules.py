"""supplier-within-category pricing rules

`pricing.rules` could name one scope per rule — a supplier, or a category —
and never both. So "Artis glassware at 45%" had no home: a category rule
priced every supplier's products in that category alike, and the only finer
level was a rule per product. The data model promised "per category within
supplier"; this is the column that makes it true.

`supplier_id` narrows a category rule to one supplier's products. Null keeps
the existing meaning — the category, whoever supplies it — so no existing row
changes sense. It is allowed on category rules only: a supplier rule already
names its supplier in `scope_id`, and a product rule is as specific as a rule
gets.

It is a real foreign key, unlike `scope_id`. That column is polymorphic and
cannot be one; this one always means a supplier, so the database can hold it
to that.

Three resolution decisions are recorded here as column comments, because
`price.py` does not exist yet and the comments are what it will be built to:

- Category rules cascade. A rule on a node covers every node beneath it — a
  rule on a department prices the whole department until something more
  specific says otherwise.
- Supplier-specific beats generic, at any depth. "Artis + Glassware" on the
  department beats "Beer Glasses, any supplier" one level nearer the product:
  a supplier's terms hold across the branch however many generic rules are
  added beneath. Depth only breaks ties between rules of the same kind, the
  nearest node winning. So the full order is product > supplier-within-category
  > category > supplier > global.
- The supplier a rule is matched against is the variant's preferred source
  (`link.variant_links.is_preferred`), since that is whose cost is in
  `pricing.cost_basis`.

Revision ID: 0014
Revises: 2b9b2f63d30b
Create Date: 2026-10-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '2b9b2f63d30b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "rules",
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        schema="pricing",
    )
    op.create_foreign_key(
        "fk_rules_supplier",
        "rules",
        "suppliers",
        ["supplier_id"],
        ["supplier_id"],
        source_schema="pricing",
        referent_schema="supplier",
    )
    op.create_check_constraint(
        "ck_rules_supplier_narrows_category_only",
        "rules",
        "supplier_id IS NULL OR scope_type = 'category'",
        schema="pricing",
    )
    op.execute(
        """
        COMMENT ON COLUMN pricing.rules.supplier_id IS
        'Category rules only. Narrows the rule to products whose preferred '
        'source is this supplier. Null means the category whoever supplies it. '
        'A rule naming a supplier beats any category rule that does not, even '
        'one on a nearer node.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN pricing.rules.scope_id IS
        'What the rule targets, by scope_type: supplier.suppliers.supplier_id, '
        'catalog.taxonomy.category_id or catalog.products.product_id; null for '
        'global. Not a foreign key, so a wrong id is accepted and matches '
        'nothing. A category rule cascades to every node beneath it; among '
        'rules of the same kind the nearest node wins. Order: product > '
        'supplier-within-category > category > supplier > global.'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("COMMENT ON COLUMN pricing.rules.scope_id IS NULL")
    op.drop_constraint(
        "ck_rules_supplier_narrows_category_only",
        "rules",
        schema="pricing",
        type_="check",
    )
    op.drop_constraint("fk_rules_supplier", "rules", schema="pricing", type_="foreignkey")
    op.drop_column("rules", "supplier_id", schema="pricing")
