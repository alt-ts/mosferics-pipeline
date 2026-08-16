"""single preferred-link trigger, preferred implies confirmed

Two changes to `link.variant_links`, both about `is_preferred`.

1. The two trigger functions added in 0006 become one, which sequences the
   behaviours explicitly — demote, then clear — instead of relying on Postgres
   firing BEFORE row triggers in name order. The old arrangement was correct
   but its correctness was invisible: it lived in the fact that
   `demote_on_reject` sorts before `single_preferred`. Sequencing it in a
   single function says what it means.

2. `is_preferred` now implies `status = 'confirmed'`, not merely
   `status <> 'rejected'`. A proposed link can no longer be the preferred
   source, so sourcing can never be pointed at a match no human has agreed to.

**Consequence for `link.py`.** The data model says single-source items get an
auto-created 1:1 link at confidence 1.0, preferred by default. Under this
constraint those rows must be written with `status = 'confirmed'`,
`confirmed_by = 'pipeline'` and `confirmed_at` set to the run timestamp —
both audit columns, every auto-confirmed link, because the pairing constraint
from 0006 admits no half-written audit trail.

Each way of getting it wrong fails differently and only one is loud: writing
one audit column alone fails the pairing check; writing neither and leaving
`status` at its `proposed` default succeeds but has the trigger silently drop
`is_preferred`, so every single-source variant ends up with no preferred
source; writing `status = 'confirmed'` with both columns null succeeds, stays
preferred, and records a confirmation nobody made.

Recording an auto-created 1:1 link as confirmed is consistent with the data
model, which says only genuine multi-source candidates and proposed groupings
enter the review queue — such a link is never something a human is asked
about. But it is the pipeline asserting a confirmation, which is why it has to
sign it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------- one trigger, sequenced
    op.execute("DROP TRIGGER trg_variant_links_demote_on_reject ON link.variant_links")
    op.execute("DROP TRIGGER trg_variant_links_single_preferred ON link.variant_links")
    op.execute(
        "DROP TRIGGER trg_variant_links_single_preferred_upd ON link.variant_links"
    )
    op.execute("DROP FUNCTION link.demote_rejected_link()")
    op.execute("DROP FUNCTION link.clear_preferred_siblings()")

    # The WHEN clause below means this only ever runs on a row that is trying
    # to be preferred, so an import writing thousands of proposed links never
    # enters it.
    op.execute(
        """
        CREATE FUNCTION link.enforce_preferred_link() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            -- Step 1, demote. Preference is only meaningful on a link a human
            -- has confirmed, so a row arriving preferred but unconfirmed loses
            -- the flag rather than raising. Rejecting or un-confirming the
            -- current winner is therefore a single write.
            IF NEW.status <> 'confirmed' THEN
                NEW.is_preferred := false;
            END IF;

            -- Step 2, clear. At most one preferred link per variant. Skipped
            -- when the row was already preferred, so re-marking the current
            -- winner stays a no-op.
            IF NEW.is_preferred AND (TG_OP = 'INSERT' OR NOT OLD.is_preferred) THEN
                UPDATE link.variant_links
                   SET is_preferred = false
                 WHERE variant_id = NEW.variant_id
                   AND is_preferred
                   AND supplier_product_id <> NEW.supplier_product_id;
            END IF;

            RETURN NEW;
        END;
        $fn$
        """
    )
    # Step 2's UPDATE sets is_preferred = false on the siblings, which fails
    # this WHEN clause, so the recursive pass stops immediately rather than
    # depending on a guard inside the function.
    op.execute(
        """
        CREATE TRIGGER trg_variant_links_preferred
        BEFORE INSERT OR UPDATE ON link.variant_links
        FOR EACH ROW WHEN (NEW.is_preferred)
        EXECUTE FUNCTION link.enforce_preferred_link();
        """
    )

    # ------------------------------------------- preferred implies confirmed
    # link.variant_links is empty, so there is nothing to reconcile before
    # tightening. If it ever runs against rows, any preferred-but-unconfirmed
    # link must be demoted first — the ALTER will not do it for you.
    op.drop_constraint(
        "ck_variant_links_preferred_not_rejected",
        "variant_links",
        schema="link",
        type_="check",
    )
    op.create_check_constraint(
        "ck_variant_links_preferred_is_confirmed",
        "variant_links",
        "NOT is_preferred OR status = 'confirmed'",
        schema="link",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_variant_links_preferred_is_confirmed",
        "variant_links",
        schema="link",
        type_="check",
    )
    op.create_check_constraint(
        "ck_variant_links_preferred_not_rejected",
        "variant_links",
        "NOT is_preferred OR status <> 'rejected'",
        schema="link",
    )

    op.execute("DROP TRIGGER trg_variant_links_preferred ON link.variant_links")
    op.execute("DROP FUNCTION link.enforce_preferred_link()")

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
    op.execute(
        """
        CREATE TRIGGER trg_variant_links_demote_on_reject
        BEFORE INSERT OR UPDATE ON link.variant_links
        FOR EACH ROW WHEN (NEW.status = 'rejected' AND NEW.is_preferred)
        EXECUTE FUNCTION link.demote_rejected_link();
        """
    )
