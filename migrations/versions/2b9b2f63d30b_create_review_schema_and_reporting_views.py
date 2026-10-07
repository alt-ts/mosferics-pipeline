"""create review schema and reporting views

Part A of `docs/mosferics_nocodb_views.md`. Three read-only report views, each
answering a question defined by an *absence* — a row with no match in another
table — which is the one thing a NocoDB filter view cannot express, because it
cannot join. The other four of that doc's seven views are filter views built in
the NocoDB UI (Part B) and need no DDL.

Their own schema rather than `ops.v_*` so NocoDB connects `review` as a
separate data source with **Allow Data Edit off**, making every report view
read-only in one toggle instead of per-view locking someone has to remember for
each new one.

Views run with their *owner's* privileges. Alembic connects as `mos_owner`,
which owns every schema these read, so `mos_nocodb` needs `SELECT` on the views
alone and no grant at all on the underlying tables.

Three places this departs from the SQL in the views doc, each because the
migrations are authoritative over the prose and contradict it:

1.  `pricing.variant_prices.status` has no value `'approved'` alone that means
    "live". `0010` constrains it to
    `('pending_approval', 'auto_approved', 'approved', 'rejected')` and says in
    its own comment that *auto_approved and approved are both live prices; the
    distinction is how they got there*. The doc's `pr.status = 'approved'`
    would therefore hide every price that cleared the materiality threshold
    automatically — which, the threshold being 5%, is the majority of them.
    `ready_to_publish` uses `IN ('approved', 'auto_approved')`.

    This is the failure the doc warns about ("a silently-empty view because the
    literal is `pending_approval` and you wrote `'pending'`"), landing one step
    further along than it expected: not a wrong literal, a short list.

2.  `catalog.images.status` is nullable free text with no CHECK and no default
    — unlike `product_metadata.status`, which `0005` constrains. So the doc's
    `i.status <> 'rejected'` evaluates to NULL, not true, for an image whose
    status has not been set; the `EXISTS` then fails and a product that *has*
    an image is reported as missing one. `missing_images` uses
    `i.status IS DISTINCT FROM 'rejected'`, which keeps the doc's intent (an
    image awaiting review is pending, not missing) and extends it to an unset
    status, the state every image the matcher writes will arrive in until
    something sets one.

    `ready_to_publish` keeps strict `= 'verified'` on the same column, and the
    asymmetry is deliberate: a publish gate must require positive
    verification, so NULL failing is correct there. Worth knowing that nothing
    in the schema guarantees `'verified'` is the literal the image matcher will
    write — there is no constraint to check it against, and the table is empty.
    If `ready_to_publish` is unexpectedly empty after Stage 7, this is the
    first thing to check.

3.  `ALTER DEFAULT PRIVILEGES` carries `FOR ROLE mos_owner`, which the doc's
    snippet omits. Stage 2 of the build instructions calls that omission "the
    trap inside the trap": without it the clause records whoever ran the
    statement as the creating role, the statement succeeds, `pg_default_acl`
    gains a row, and every view a later migration creates is still born
    invisible to NocoDB. `0013` spells it out everywhere for the same reason.

`missing_images` ships with the publish-intent filter the doc says to add
"before you show it to anyone", not as an afterthought. Unscoped it is every
active product without an image — most of 60k rows — which is an inventory, not
a worklist. The data model gates enrichment and image work on publish intent,
so the view does too.

One thing these views do not do. `ready_to_publish` expresses "everything
verified" as `NOT EXISTS (... status = 'pending_review')`, which passes a
product with *zero* metadata rows — nobody has described it, which is empty
rather than verified. The real check is minimum-required-fields per taxonomy
node, which does not exist yet. This view is not that check and must not be
mistaken for it.

Every table these read is empty at `0013`, so the status literals above were
settled against the CHECK constraints in `0005`, `0007`, `0009` and `0010`
rather than against `SELECT DISTINCT status`, which returns nothing useful
until Stage 7 runs.

Revision ID: 2b9b2f63d30b
Revises: 0013
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b9b2f63d30b'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


VIEWS = ("unmapped_categories", "missing_images", "ready_to_publish")

# --------------------------------------------------------- unmapped_categories
# Supplier categories with no row in link.category_map. product_count DESC is
# the point of the view rather than decoration: it puts the raw category
# covering 400 SKUs above the one covering 2, so an hour of mapping clears the
# most catalogue. example_product saves guessing what "GLASS-HB-MISC" means.
#
# The id is a row_number because there is no natural key — NocoDB wants one
# unique column per relation. It is not stable between refreshes, which is
# acceptable only because nothing joins to it. supplier_category_raw is in the
# ORDER BY purely so the numbering is deterministic within one query when two
# of a supplier's categories tie on count.
#
# Only covers suppliers whose YAML declares `classification_method:
# mapped_column`. BBP sends no category column, so it will never appear here
# and its absence is correct: BBP's unmapped products surface as
# `derived_from_name` proposals in ops.review_queue instead.
UNMAPPED_CATEGORIES = """
    CREATE VIEW review.unmapped_categories AS
    SELECT row_number() OVER (
               ORDER BY count(*) DESC, s.name, sp.supplier_category_raw
           )                        AS id,
           sp.supplier_id,
           s.name                   AS supplier,
           sp.supplier_category_raw,
           count(*)                 AS product_count,
           min(sp.name_raw)         AS example_product
      FROM supplier.supplier_products sp
      JOIN supplier.suppliers s ON s.supplier_id = sp.supplier_id
      LEFT JOIN link.category_map cm
             ON cm.supplier_id           = sp.supplier_id
            AND cm.supplier_category_raw = sp.supplier_category_raw
     WHERE sp.supplier_category_raw IS NOT NULL
       AND cm.category_id IS NULL
       AND sp.discontinued IS NOT TRUE
     GROUP BY sp.supplier_id, s.name, sp.supplier_category_raw
"""

# ------------------------------------------------------------- missing_images
# Active products with publish intent on at least one variant and no usable
# image. Scoped to publish intent deliberately — see the module docstring.
MISSING_IMAGES = """
    CREATE VIEW review.missing_images AS
    SELECT p.product_id        AS id,
           p.product_id,
           p.title,
           t.path              AS category_path,
           b.name              AS brand,
           count(v.variant_id) AS variant_count
      FROM catalog.products p
      LEFT JOIN catalog.taxonomy t ON t.category_id = p.category_id
      LEFT JOIN catalog.brands   b ON b.brand_id    = p.brand_id
      LEFT JOIN catalog.variants v ON v.product_id  = p.product_id
     WHERE p.status = 'active'
       AND NOT EXISTS (
               SELECT 1
                 FROM catalog.images i
                WHERE i.product_id = p.product_id
                  AND i.status IS DISTINCT FROM 'rejected'
           )
       AND EXISTS (
               SELECT 1
                 FROM catalog.variants v2
                 JOIN ops.channel_publish cp ON cp.variant_id = v2.variant_id
                WHERE v2.product_id = p.product_id
                  AND cp.publish_status = 'pending'
           )
     GROUP BY p.product_id, p.title, t.path, b.name
"""

# ---------------------------------------------------------- ready_to_publish
# Keyed by variant x channel, not by product: publish_status and price both
# live per channel, so "ready to publish" is only answerable for a given
# storefront. The synthetic id is cast explicitly rather than relying on
# bigint || text resolving, so the view says what it builds.
#
# Both joins are to tables whose PK is (variant_id, channel_id), so neither
# fans the row out.
#
# `l.is_preferred` carries no status test because it does not need one:
# `0007`'s ck_variant_links_preferred_is_confirmed makes is_preferred imply
# status = 'confirmed', and its demotion trigger maintains that.
READY_TO_PUBLISH = """
    CREATE VIEW review.ready_to_publish AS
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
     WHERE cp.publish_status <> 'published'
       AND p.status = 'active'
       -- Both are live prices; see note 1 in the module docstring.
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

COMMENTS = (
    """
    COMMENT ON SCHEMA review IS
    'Read-only reporting surface for NocoDB. Every view here answers a question '
    'defined by an absence, which a NocoDB filter view cannot express because '
    'it cannot join. Connected to NocoDB as its own data source with Allow '
    'Data Edit off. Nothing writes here and nothing should join to these views.'
    """,
    """
    COMMENT ON VIEW review.unmapped_categories IS
    'Supplier categories with no link.category_map row, heaviest first so '
    'mapping effort clears the most catalogue. Pairs with an editable NocoDB '
    'grid on link.category_map: this says what is missing, that is where the '
    'mapping is added. Only covers suppliers that send a category column; '
    'BBP''s unmapped products surface in ops.review_queue instead. id is a '
    'row_number and is not stable between refreshes.'
    """,
    """
    COMMENT ON VIEW review.missing_images IS
    'Active products we are trying to publish and cannot, for want of an '
    'image. Scoped to variants with channel_publish.publish_status = pending, '
    'because image work is gated on publish intent, not ingest — unscoped this '
    'would be most of 60k rows. An image awaiting review counts as present, '
    'not missing: that is the Needs review view''s job.'
    """,
    """
    COMMENT ON VIEW review.ready_to_publish IS
    'Variant x channel rows cleared to sync: live price, preferred source, a '
    'verified image, no metadata still pending review. Keyed per channel '
    'because publish_status and price both are. NOT a minimum-required-fields '
    'check — it passes a product with zero metadata rows, which is empty '
    'rather than verified. That check does not exist yet.'
    """,
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA review")
    op.execute(UNMAPPED_CATEGORIES)
    op.execute(MISSING_IMAGES)
    op.execute(READY_TO_PUBLISH)
    for comment in COMMENTS:
        op.execute(comment)

    # mos_nocodb reads these; ALL TABLES covers views, Postgres treating them
    # as relations here. FOR ROLE mos_owner is load-bearing — see note 3.
    op.execute("GRANT USAGE ON SCHEMA review TO mos_nocodb")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA review TO mos_nocodb")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA review "
        "GRANT SELECT ON TABLES TO mos_nocodb"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Default privileges are revoked before the schema goes, rather than left
    # to the DROP, so no pg_default_acl row can outlive the schema it names.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA review "
        "REVOKE SELECT ON TABLES FROM mos_nocodb"
    )
    for view in reversed(VIEWS):
        op.execute(f"DROP VIEW review.{view}")
    # CASCADE matches the other schema migrations. The views are dropped by
    # name above, so it has nothing left to reach.
    op.execute("DROP SCHEMA review CASCADE")
