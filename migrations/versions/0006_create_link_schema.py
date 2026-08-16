"""create link schema

The join between their world and ours. Two tables.

`category_map` is deterministic and hand-maintained; `variant_links` is fuzzy,
proposed by the pipeline and confirmed by a human. They are only in the same
schema because they both translate supplier facts into catalogue facts.

Hard rule 1 lives here: links resolve at VARIANT level. The FK is to
`catalog.variants`, never to `catalog.products`, because the two operations
this table carries — multi-source matching and variant grouping — can only both
be expressed variant-side.

Preferred source is stated as constraints and maintained by triggers, twice
over: a partial unique index says at most one preferred link per variant and a
trigger demotes the siblings; a CHECK says a rejected link is never preferred
and a trigger demotes on rejection. Both pairs exist so that the human-facing
action is a single write and the invariant still cannot be violated.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA link")

    # ----------------------------------------------------------- category_map
    # Deterministic and maintained by hand, so there is no confidence or
    # proposed/confirmed lifecycle here — an unmapped raw string goes to the
    # review queue rather than producing an uncategorised product, which is a
    # pipeline behaviour, not a row state.
    op.create_table(
        "category_map",
        sa.Column("supplier_id", sa.Integer, nullable=False),
        # Exactly as the supplier sent it. The key is the raw string because
        # that is what arrives on the next import; normalising it here would
        # mean re-deriving the same normalisation before every lookup.
        sa.Column("supplier_category_raw", sa.Text, nullable=False),
        sa.Column("category_id", sa.Integer, nullable=False),
        sa.Column("confirmed_by", sa.Text),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True)),
        sa.PrimaryKeyConstraint(
            "supplier_id", "supplier_category_raw", name="pk_category_map"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["supplier.suppliers.supplier_id"],
            name="fk_category_map_supplier",
        ),
        # The map translates; catalog.products.category_id is where the
        # category actually lives.
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["catalog.taxonomy.category_id"],
            name="fk_category_map_category",
        ),
        # Either both audit columns or neither — a confirmation with no
        # confirmer is not a confirmation.
        sa.CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_category_map_confirmation_complete",
        ),
        schema="link",
    )
    # "Everything this supplier maps to" is the maintenance view; "everything
    # mapped to this node" is what a taxonomy restructure needs.
    op.create_index(
        "ix_category_map_category", "category_map", ["category_id"], schema="link"
    )

    # ---------------------------------------------------------- variant_links
    op.create_table(
        "variant_links",
        sa.Column("supplier_product_id", sa.BigInteger, nullable=False),
        # Hard rule 1: variant level, never product level.
        sa.Column("variant_id", sa.BigInteger, nullable=False),
        sa.Column("match_type", sa.Text, nullable=False),
        # 1.0 for the auto-created 1:1 link every single-source item gets.
        sa.Column("match_confidence", sa.Numeric(4, 3)),
        sa.Column("status", sa.Text, nullable=False, server_default="proposed"),
        sa.Column("confirmed_by", sa.Text),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True)),
        # Preferred source lives on the link, never as a pointer column on the
        # variant: a pointer could name a supplier the variant is not actually
        # linked to, and this cannot.
        sa.Column(
            "is_preferred", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.PrimaryKeyConstraint(
            "supplier_product_id", "variant_id", name="pk_variant_links"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_product_id"],
            ["supplier.supplier_products.supplier_product_id"],
            name="fk_variant_links_supplier_product",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["catalog.variants.variant_id"], name="fk_variant_links_variant"
        ),
        sa.CheckConstraint(
            "match_type IN ('identical', 'equivalent', 'substitute')",
            name="ck_variant_links_match_type",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')",
            name="ck_variant_links_status",
        ),
        sa.CheckConstraint(
            "match_confidence IS NULL"
            " OR (match_confidence >= 0 AND match_confidence <= 1)",
            name="ck_variant_links_confidence_range",
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_variant_links_confirmation_complete",
        ),
        # Sourcing cannot prefer a link a human has rejected. Proposed is
        # deliberately still allowed: the single-source 1:1 link is preferred
        # from birth and never needs a human to look at it.
        #
        # The demotion trigger below means this can no longer be reached from
        # a normal write. That is the intent — the constraint states the
        # invariant, the trigger is how it gets maintained, exactly as the
        # partial unique index and the sibling-clearing trigger pair up.
        sa.CheckConstraint(
            "NOT is_preferred OR status <> 'rejected'",
            name="ck_variant_links_preferred_not_rejected",
        ),
        schema="link",
    )
    op.create_index(
        "ix_variant_links_variant", "variant_links", ["variant_id"], schema="link"
    )
    # The review queue reads by state; the pipeline reads by supplier product.
    op.create_index(
        "ix_variant_links_status", "variant_links", ["status"], schema="link"
    )

    # At most one preferred link per variant. "At least one" is a business rule
    # the Sourcing engine and the single-source default guarantee — not
    # something the schema can assert, since a variant exists before its first
    # link does.
    op.create_index(
        "uq_variant_links_preferred",
        "variant_links",
        ["variant_id"],
        unique=True,
        postgresql_where=sa.text("is_preferred"),
        schema="link",
    )

    # Sibling-clearing trigger: setting is_preferred = true demotes the
    # variant's other links in the same statement. Without it, changing the
    # preferred source is an unset-then-set dance that trips the unique index
    # in between — and the reviewer ticking one box in NocoDB would just see an error.
    #
    # BEFORE, not AFTER: the demotion must land before the unique index is
    # checked for the row being promoted.
    op.execute(
        """
        CREATE FUNCTION link.clear_preferred_siblings() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            UPDATE link.variant_links
               SET is_preferred = false
             WHERE variant_id = NEW.variant_id
               AND is_preferred
               AND supplier_product_id <> NEW.supplier_product_id;
            RETURN NEW;
        END;
        $fn$
        """
    )
    # WHEN (NEW.is_preferred) keeps the trigger off the hot path: an import
    # writing thousands of proposed links never fires it. The UPDATE branch
    # also requires the flag to be changing, so the recursive UPDATE this
    # function issues cannot re-fire the trigger on its own targets.
    op.execute(
        """
        CREATE TRIGGER trg_variant_links_single_preferred
        BEFORE INSERT ON link.variant_links
        FOR EACH ROW WHEN (NEW.is_preferred)
        EXECUTE FUNCTION link.clear_preferred_siblings();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_variant_links_single_preferred_upd
        BEFORE UPDATE ON link.variant_links
        FOR EACH ROW WHEN (NEW.is_preferred AND NOT OLD.is_preferred)
        EXECUTE FUNCTION link.clear_preferred_siblings();
        """
    )

    # Rejecting the preferred link demotes it rather than raising. Without
    # this, rejecting the winning source is an order-dependent two-step —
    # demote, then reject — and someone ticking "rejected" in NocoDB on the
    # source that happens to be preferred just gets a constraint error.
    #
    # The demotion is silent and leaves the variant with no preferred link.
    # That is the correct resting state: "at least one preferred link" is a
    # business rule the Sourcing engine satisfies on its next pass, not a
    # schema invariant — a variant has no links at all when it is first
    # created, so the schema could never have asserted it.
    op.execute(
        """
        CREATE FUNCTION link.demote_rejected_link() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            NEW.is_preferred := false;
            RETURN NEW;
        END;
        $fn$
        """
    )
    # Named to sort before trg_variant_links_single_preferred*: Postgres fires
    # BEFORE row triggers in name order and re-evaluates each WHEN clause
    # against the NEW row as earlier triggers have left it. So a contradictory
    # write that sets is_preferred and status = 'rejected' together demotes
    # first, which then makes the promotion trigger's WHEN false and leaves
    # the variant's existing winner alone — rather than clearing the winner on
    # behalf of a row that is about to be demoted anyway.
    op.execute(
        """
        CREATE TRIGGER trg_variant_links_demote_on_reject
        BEFORE INSERT OR UPDATE ON link.variant_links
        FOR EACH ROW WHEN (NEW.status = 'rejected' AND NEW.is_preferred)
        EXECUTE FUNCTION link.demote_rejected_link();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SCHEMA link CASCADE")
