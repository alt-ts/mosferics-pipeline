"""create catalog schema

What Mosferics sells. Eleven tables.

Creation order differs from the build doc's listing: `documents` is created
before `compliance`, because compliance.evidence_document_id is a real FK to it.
The build doc lists compliance first, which cannot be built in that order.

The Mosferics SKU carries a mod-11 check digit: `MOS-<DEPT>-<NNNNNNN>-<C>`.
The data model offers "mod-11 or Luhn"; mod-11 is chosen because Luhn misses the
09<->90 transposition, and catching transposition on manual warehouse entry is
the whole point of the digit.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCES = "('supplier', 'ai', 'manual')"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA catalog")

    # ---------------------------------------------------------------- taxonomy
    op.create_table(
        "taxonomy",
        sa.Column("category_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("parent_id", sa.Integer),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        # Ordered, applicable nodes only. Drives Shopify option positions.
        sa.Column("variant_axes", postgresql.ARRAY(sa.Text)),
        # Immutable, level-1 department nodes only. Source of <DEPT> in the
        # Mosferics SKU — never derived from the name at runtime.
        sa.Column("sku_prefix", sa.Text),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["catalog.taxonomy.category_id"], name="fk_taxonomy_parent"
        ),
        sa.UniqueConstraint("path", name="uq_taxonomy_path"),
        # Siblings must have distinct slugs. NULLS NOT DISTINCT so the rule
        # also binds level-1 nodes, which all share parent_id = NULL.
        sa.UniqueConstraint(
            "parent_id",
            "slug",
            name="uq_taxonomy_sibling_slug",
            postgresql_nulls_not_distinct=True,
        ),
        # Level 1 is exactly "has no parent".
        sa.CheckConstraint(
            "sku_prefix IS NULL OR parent_id IS NULL",
            name="ck_taxonomy_sku_prefix_level_1_only",
        ),
        sa.CheckConstraint(
            "sku_prefix IS NULL OR sku_prefix ~ '^[A-Z]{3}$'",
            name="ck_taxonomy_sku_prefix_format",
        ),
        schema="catalog",
    )
    op.create_index("ix_taxonomy_parent", "taxonomy", ["parent_id"], schema="catalog")
    op.create_index(
        "uq_taxonomy_sku_prefix",
        "taxonomy",
        ["sku_prefix"],
        unique=True,
        postgresql_where=sa.text("sku_prefix IS NOT NULL"),
        schema="catalog",
    )

    # ------------------------------------------------- brand / collection / range
    op.create_table(
        "brands",
        sa.Column("brand_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        # Once per brand, not 60,000 times per product.
        sa.Column("logo_s3_key", sa.Text),
        sa.Column("description", sa.Text),
        schema="catalog",
    )

    op.create_table(
        "collections",
        sa.Column("collection_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("brand_id", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("banner_s3_key", sa.Text),
        sa.Column("description", sa.Text),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["catalog.brands.brand_id"], name="fk_collections_brand"
        ),
        # Two brands may both have a "Classic" collection.
        sa.UniqueConstraint("brand_id", "slug", name="uq_collections_brand_slug"),
        schema="catalog",
    )

    op.create_table(
        "ranges",
        sa.Column("range_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("collection_id", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["catalog.collections.collection_id"],
            name="fk_ranges_collection",
        ),
        sa.UniqueConstraint("collection_id", "slug", name="uq_ranges_collection_slug"),
        schema="catalog",
    )

    # ---------------------------------------------------------------- products
    op.create_table(
        "products",
        sa.Column("product_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        # Frozen after first Shopify publish (hard rule 8). Never regenerated
        # from the title.
        sa.Column("handle", sa.Text, nullable=False, unique=True),
        sa.Column("category_id", sa.Integer, nullable=False),
        # Mandatory and internal only — never surfaces on the storefront.
        sa.Column("base_unit", sa.Text, nullable=False),
        sa.Column("brand_id", sa.Integer),
        sa.Column("collection_id", sa.Integer),
        sa.Column("range_id", sa.Integer),
        sa.Column("description", sa.Text),
        sa.Column("description_source", sa.Text),
        # Free text: the review-state vocabulary is not settled yet.
        sa.Column("description_status", sa.Text),
        sa.Column("seo_title", sa.Text),
        sa.Column("seo_description", sa.Text),
        sa.Column(
            "is_multi_source", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        # Lifecycle only. Publish state is per channel, in ops.channel_publish.
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["catalog.taxonomy.category_id"],
            name="fk_products_category",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["catalog.brands.brand_id"], name="fk_products_brand"
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["catalog.collections.collection_id"],
            name="fk_products_collection",
        ),
        sa.ForeignKeyConstraint(
            ["range_id"], ["catalog.ranges.range_id"], name="fk_products_range"
        ),
        sa.CheckConstraint(
            f"description_source IS NULL OR description_source IN {SOURCES}",
            name="ck_products_description_source",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'discontinued')", name="ck_products_status"
        ),
        schema="catalog",
    )
    op.create_index("ix_products_category", "products", ["category_id"], schema="catalog")
    op.create_index("ix_products_brand", "products", ["brand_id"], schema="catalog")
    op.create_index("ix_products_range", "products", ["range_id"], schema="catalog")
    op.create_index("ix_products_status", "products", ["status"], schema="catalog")

    # ---------------------------------------------------------------- variants
    # Single global sequence: the numeric part must be unique on its own,
    # because the prefix will get stripped somewhere eventually.
    op.execute("CREATE SEQUENCE catalog.mosferics_sku_seq AS bigint START 1 MINVALUE 1")

    # Mod-11 check digit over the 7-digit body ONLY — deliberately not over the
    # department prefix. The body has to stay self-checking after the prefix is
    # stripped, which the data model says will happen eventually.
    #
    # Weights 8..2 left to right, check = (11 - (sum mod 11)) mod 11. Because 11
    # is prime and every weight difference is < 11, this catches every
    # single-digit substitution and every transposition, adjacent or not.
    #
    # Returns 10 for a body whose check digit is unrepresentable as one decimal
    # digit; the allocator below skips those rather than rendering them 'X'.
    #
    # Returns NULL for anything that is not exactly 7 digits — which only
    # reaches here when ck_variants_sku_format has already rejected the row.
    # The digit guard is a WHERE, not a CASE: a CASE cannot stop Postgres
    # evaluating the aggregate inside it, so a short body would raise a cast
    # error instead of returning NULL. WHERE filters the rows first.
    op.execute(
        """
        CREATE FUNCTION catalog.sku_check_digit(seq_part text) RETURNS integer
        LANGUAGE sql IMMUTABLE STRICT
        AS $fn$
            SELECT (11 - (SUM(substr(seq_part, 8 - i, 1)::integer * (i + 1))::integer % 11)) % 11
            FROM generate_series(1, 7) AS i
            WHERE seq_part ~ '^[0-9]{7}$'
        $fn$
        """
    )

    # Allocation lives here, not in the pipeline, so the skip rule and the
    # format exist in exactly one place. Fails loudly on an unknown department
    # rather than minting a SKU under a prefix nobody has defined.
    op.execute(
        """
        CREATE FUNCTION catalog.next_mosferics_sku(p_dept text) RETURNS text
        LANGUAGE plpgsql VOLATILE
        AS $fn$
        DECLARE
            v_seq   bigint;
            v_body  text;
            v_check integer;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM catalog.taxonomy
                WHERE sku_prefix = p_dept AND parent_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'unknown department prefix %: no level-1 taxonomy node carries it',
                    p_dept;
            END IF;

            LOOP
                v_seq := nextval('catalog.mosferics_sku_seq');
                IF v_seq > 9999999 THEN
                    RAISE EXCEPTION
                        'mosferics_sku_seq has outgrown the 7-digit body at %', v_seq;
                END IF;
                v_body  := lpad(v_seq::text, 7, '0');
                v_check := catalog.sku_check_digit(v_body);
                EXIT WHEN v_check < 10;
            END LOOP;

            RETURN 'MOS-' || p_dept || '-' || v_body || '-' || v_check::text;
        END;
        $fn$
        """
    )

    op.create_table(
        "variants",
        sa.Column("variant_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("product_id", sa.BigInteger, nullable=False),
        sa.Column("variant_title", sa.Text),
        # Allocated once at creation, immutable thereafter, never reused.
        # MOS-<DEPT>-<NNNNNNN>-<C>, allocated by catalog.next_mosferics_sku().
        sa.Column("mosferics_sku", sa.Text, nullable=False, unique=True),
        # Primary multi-source matching signal — manufacturer-assigned.
        # NOT unique: two variants may legitimately share one before a
        # multi-source merge is confirmed.
        sa.Column("ean", sa.Text),
        # What we sell in. A field, not a variant axis.
        sa.Column("pack_size", sa.Integer),
        sa.Column("base_units_per_variant", sa.Numeric(12, 4)),
        # Shipping carton logistics — distinct from the spec dimensions that
        # live in product_metadata.
        sa.Column("weight_g", sa.Integer),
        sa.Column("dims_mm", postgresql.JSONB),
        sa.ForeignKeyConstraint(
            ["product_id"], ["catalog.products.product_id"], name="fk_variants_product"
        ),
        sa.CheckConstraint(
            "mosferics_sku ~ '^MOS-[A-Z]{3}-[0-9]{7}-[0-9]$'",
            name="ck_variants_sku_format",
        ),
        # Kept separate from the format check so a mistyped SKU says which of
        # the two it failed. Postgres does not promise to evaluate the two
        # constraints in any order, so each side of this one has to abstain
        # rather than raise on input the format check would have caught: a bad
        # body makes sku_check_digit() return NULL, and the substring() guard
        # returns NULL rather than casting '' or 'X' to integer. NULL = NULL is
        # NULL, a CHECK passes on NULL, and the format check does the rejecting.
        sa.CheckConstraint(
            "catalog.sku_check_digit(split_part(mosferics_sku, '-', 3))"
            " = substring(split_part(mosferics_sku, '-', 4) from '^[0-9]$')::integer",
            name="ck_variants_sku_check_digit",
        ),
        sa.CheckConstraint(
            "pack_size IS NULL OR pack_size > 0", name="ck_variants_pack_size_positive"
        ),
        schema="catalog",
    )
    op.create_index("ix_variants_product", "variants", ["product_id"], schema="catalog")
    op.create_index("ix_variants_ean", "variants", ["ean"], schema="catalog")

    # -------------------------------------------------------- product_metadata
    op.create_table(
        "product_metadata",
        sa.Column("product_id", sa.BigInteger, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        # Raw, as received.
        sa.Column("value", sa.Text),
        # Normalised number + canonical unit: what lets "285ml", "28.5cl" and
        # "10oz" sort and filter together. Null for non-dimensional keys.
        sa.Column("canonical_value", sa.Numeric),
        sa.Column("unit", sa.Text),
        sa.Column("source", sa.Text),
        # Hard rule 3: safety-relevant metadata lands pending_review.
        sa.Column(
            "status", sa.Text, nullable=False, server_default="pending_review"
        ),
        sa.Column("verified_by", sa.Text),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True)),
        sa.PrimaryKeyConstraint("product_id", "key", name="pk_product_metadata"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["catalog.products.product_id"],
            name="fk_product_metadata_product",
        ),
        sa.CheckConstraint(
            f"source IS NULL OR source IN {SOURCES}", name="ck_product_metadata_source"
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'verified', 'rejected')",
            name="ck_product_metadata_status",
        ),
        schema="catalog",
    )
    # Drives the "needs review" view, grouped by field type.
    op.create_index(
        "ix_product_metadata_status_key",
        "product_metadata",
        ["status", "key"],
        schema="catalog",
    )

    # ------------------------------------------------------------------ images
    op.create_table(
        "images",
        sa.Column("image_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("product_id", sa.BigInteger, nullable=False),
        sa.Column("s3_key", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("source", sa.Text),
        sa.Column("status", sa.Text),
        sa.ForeignKeyConstraint(
            ["product_id"], ["catalog.products.product_id"], name="fk_images_product"
        ),
        schema="catalog",
    )
    op.create_index(
        "ix_images_product_position", "images", ["product_id", "position"], schema="catalog"
    )

    # --------------------------------------------------------------- documents
    # Before compliance: evidence_document_id points here.
    op.create_table(
        "documents",
        sa.Column("document_id", sa.BigInteger, sa.Identity(), primary_key=True),
        # Polymorphic: attach_id's target table depends on attach_level, so it
        # cannot be a single SQL FK. Enforced in the pipeline + this CHECK.
        sa.Column("attach_level", sa.Text, nullable=False),
        sa.Column("attach_id", sa.BigInteger, nullable=False),
        sa.Column("doc_type", sa.Text, nullable=False),
        sa.Column("s3_key", sa.Text, nullable=False),
        # A dated artefact that gets superseded — a plain URL loses all of this.
        sa.Column("version", sa.Text),
        sa.Column("issued_date", sa.Date),
        sa.Column("expires_date", sa.Date),
        sa.Column("superseded_by", sa.BigInteger),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["catalog.documents.document_id"],
            name="fk_documents_superseded_by",
        ),
        sa.CheckConstraint(
            "attach_level IN ('product', 'range', 'collection', 'brand')",
            name="ck_documents_attach_level",
        ),
        sa.CheckConstraint(
            "doc_type IN ('care_guide', 'warranty', 'brochure', 'spec_sheet', "
            "'coshh', 'declaration_of_conformity')",
            name="ck_documents_doc_type",
        ),
        sa.CheckConstraint(
            "expires_date IS NULL OR issued_date IS NULL OR expires_date >= issued_date",
            name="ck_documents_date_range",
        ),
        sa.CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> document_id",
            name="ck_documents_not_self_superseding",
        ),
        schema="catalog",
    )
    op.create_index(
        "ix_documents_attach", "documents", ["attach_level", "attach_id"], schema="catalog"
    )

    # -------------------------------------------------------------- compliance
    op.create_table(
        "compliance",
        sa.Column("product_id", sa.BigInteger, nullable=False),
        sa.Column("claim", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("evidence_document_id", sa.BigInteger),
        sa.Column("stated_by", sa.Text),
        sa.Column("recorded_by", sa.Text, nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("product_id", "claim", name="pk_compliance"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["catalog.products.product_id"], name="fk_compliance_product"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"],
            ["catalog.documents.document_id"],
            name="fk_compliance_evidence",
        ),
        sa.CheckConstraint(
            "claim IN ('ce_mark', 'ca_mark', 'reach', 'rohs', 'food_contact', "
            "'coshh_required', 'age_restricted')",
            name="ck_compliance_claim",
        ),
        sa.CheckConstraint(
            "stated_by IS NULL OR stated_by IN ('supplier', 'manufacturer')",
            name="ck_compliance_stated_by",
        ),
        # Hard rule 2, enforced structurally: no evidence, no row. The single
        # exception is age_restricted, which the data model defines as a policy
        # rule (all knives are 18+) rather than a manufacturer statement, so it
        # has no data sheet behind it.
        sa.CheckConstraint(
            "evidence_document_id IS NOT NULL OR claim = 'age_restricted'",
            name="ck_compliance_evidence_required",
        ),
        schema="catalog",
    )
    op.create_index(
        "ix_compliance_evidence",
        "compliance",
        ["evidence_document_id"],
        schema="catalog",
    )

    # -------------------------------------------------------- product_relations
    op.create_table(
        "product_relations",
        sa.Column("from_product_id", sa.BigInteger, nullable=False),
        sa.Column("to_product_id", sa.BigInteger, nullable=False),
        sa.Column("relation_type", sa.Text, nullable=False),
        # True where derived from a shared range_id rather than entered.
        sa.Column(
            "auto_derived", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.PrimaryKeyConstraint(
            "from_product_id", "to_product_id", "relation_type", name="pk_product_relations"
        ),
        sa.ForeignKeyConstraint(
            ["from_product_id"],
            ["catalog.products.product_id"],
            name="fk_product_relations_from",
        ),
        sa.ForeignKeyConstraint(
            ["to_product_id"],
            ["catalog.products.product_id"],
            name="fk_product_relations_to",
        ),
        sa.CheckConstraint(
            "relation_type IN ('matching_cup', 'matching_saucer', 'matching_plate', "
            "'matching_bowl', 'matching_lid', 'matching_teapot', 'spare_part', "
            "'accessory')",
            name="ck_product_relations_type",
        ),
        sa.CheckConstraint(
            "from_product_id <> to_product_id", name="ck_product_relations_not_self"
        ),
        schema="catalog",
    )
    # The inverse relation is created automatically, so this direction is
    # queried as often as the forward one.
    op.create_index(
        "ix_product_relations_to", "product_relations", ["to_product_id"], schema="catalog"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SCHEMA catalog CASCADE")
