"""auto-sign confirmations, require a confirmer

Closes the hole 0007 left open: `status = 'confirmed'` with both audit columns
null was legal, recording a confirmation nobody made.

The CHECK alone would have made the reviewer's confirm a two-field edit — tick the
status, and also supply a name — which is NocoDB configuration plus an error
message every time he forgets. So the constraint arrives with a trigger that
signs the row, and the pair behaves the same way as the preferred-link design
in 0007: the constraint states the fact, the trigger makes the honest action a
single write, and the constraint is left as a backstop it cannot normally hit.

`current_user` records the ROLE, not the person: a NocoDB edit signs as
`mos_nocodb`, the pipeline as `mos_pipeline` unless it names itself. For a shop
where the reviewer is the reviewer that is the distinction that matters — human via
NocoDB versus machine — and it needs no per-user database roles. If per-person
attribution is ever wanted, the place to add it is NocoDB writing the logged-in
user into `confirmed_by`, which this trigger will then leave alone.

Independent of `enforce_preferred_link` by construction: that trigger reads
`status` and writes `is_preferred`; this one reads `status` and `confirmed_by`
and writes the two audit columns. No column is both read by one and written by
the other, so firing order cannot matter and the 0007 ordering problem is not
reintroduced. There is a test for exactly this.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `confirmed_at` is filled with COALESCE rather than assigned outright, for
    # the same reason the WHEN clause tests `confirmed_by IS NULL`: the trigger
    # fills in what the caller left out and never overwrites what it supplied.
    # A caller that provides a run timestamp keeps it.
    op.execute(
        """
        CREATE FUNCTION link.sign_confirmation() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            NEW.confirmed_by := current_user;
            NEW.confirmed_at := COALESCE(NEW.confirmed_at, now());
            RETURN NEW;
        END;
        $fn$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_variant_links_sign_confirmation
        BEFORE INSERT OR UPDATE ON link.variant_links
        FOR EACH ROW WHEN (NEW.status = 'confirmed' AND NEW.confirmed_by IS NULL)
        EXECUTE FUNCTION link.sign_confirmation();
        """
    )

    # Deliberately narrower than the trigger: it names only `confirmed_by`,
    # because `ck_variant_links_confirmation_complete` already ties
    # `confirmed_at` to it. Stating the same pairing twice would mean two
    # constraint names for one mistake.
    #
    # link.variant_links is empty, so there is nothing to reconcile. Against
    # rows, any `confirmed` row with a null `confirmed_by` must be signed or
    # moved off `confirmed` before this ALTER will pass — it does not backfill.
    op.create_check_constraint(
        "ck_variant_links_confirmed_has_confirmer",
        "variant_links",
        "status <> 'confirmed' OR confirmed_by IS NOT NULL",
        schema="link",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_variant_links_confirmed_has_confirmer",
        "variant_links",
        schema="link",
        type_="check",
    )
    op.execute(
        "DROP TRIGGER trg_variant_links_sign_confirmation ON link.variant_links"
    )
    op.execute("DROP FUNCTION link.sign_confirmation()")
