"""catalog.images: an image may belong to a variant; one row per file; known statuses

Suppliers name image files by SKU (`14-00-017_1.jpg`), and a SKU is a
variant. A colour range shows a different picture per colour, which Shopify
carries as the variant's image, so a product-level row loses what the file
said.

1. `variant_id`, nullable: null is an image of the product as a whole. The
   foreign key is composite, (variant_id, product_id) against catalog.variants,
   so an image cannot name a variant of another product. That needs a unique
   key on catalog.variants (variant_id, product_id), trivially true since
   variant_id is already the primary key, and added only so the FK has
   something to reference.
2. `uq_images_s3_key`: one row per stored file. pipeline/images.py re-runs
   against the same bucket, and this is what makes a re-run unable to add a
   file twice. Partial, as s3_key is nullable for images held elsewhere.
3. `ck_images_status`: NULL (not yet reviewed), 'verified' or 'rejected'. The
   review views were written against exactly these three (see the review
   schema migration); until now nothing stopped a fourth spelling, which those
   views would silently treat as unreviewed.

catalog.images is empty on every database this has run against, so nothing
needs reconciling.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(
        "uq_variants_variant_product", "variants", ["variant_id", "product_id"], schema="catalog"
    )
    op.add_column("images", sa.Column("variant_id", sa.BigInteger(), nullable=True), schema="catalog")
    op.create_foreign_key(
        "fk_images_variant_of_product", "images", "variants",
        ["variant_id", "product_id"], ["variant_id", "product_id"],
        source_schema="catalog", referent_schema="catalog",
    )
    op.create_index("ix_images_variant", "images", ["variant_id"], schema="catalog")
    op.create_index(
        "uq_images_s3_key", "images", ["s3_key"], unique=True, schema="catalog",
        postgresql_where=sa.text("s3_key IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_images_status", "images", "status IS NULL OR status IN ('verified', 'rejected')", schema="catalog"
    )
    op.execute(
        "COMMENT ON COLUMN catalog.images.variant_id IS "
        "'The variant this picture shows, or null for the product as a whole. "
        "Must be a variant of product_id (fk_images_variant_of_product).'"
    )
    op.execute(
        "COMMENT ON COLUMN catalog.images.status IS "
        "'NULL until reviewed, then verified or rejected. Only verified images publish.'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("COMMENT ON COLUMN catalog.images.status IS NULL")
    op.drop_constraint("ck_images_status", "images", schema="catalog")
    op.drop_index("uq_images_s3_key", "images", schema="catalog")
    op.drop_index("ix_images_variant", "images", schema="catalog")
    op.drop_constraint("fk_images_variant_of_product", "images", schema="catalog")
    op.drop_column("images", "variant_id", schema="catalog")
    op.drop_constraint("uq_variants_variant_product", "variants", schema="catalog")
