"""relax supplier description_status to free text

0002 constrained supplier_products.description_status to the review-state
vocabulary borrowed from product_metadata.status. The description review states
are not settled yet, so the column goes back to free text until they are.

0002 is already applied to live, so this is a forward migration rather than an
edit to that revision (hard rule 5). product_metadata.status keeps its CHECK —
the data model does enumerate those values.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "ck_supplier_products_description_status",
        "supplier_products",
        schema="supplier",
        type_="check",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.create_check_constraint(
        "ck_supplier_products_description_status",
        "supplier_products",
        "description_status IS NULL OR "
        "description_status IN ('pending_review', 'verified', 'rejected')",
        schema="supplier",
    )
