"""catalog.product_metadata: a verdict records who gave it and when

The data model has flagged this since 0005: `verified_by` / `verified_at` have
no pairing constraint and no signing trigger, so flipping `status` to
`verified` succeeds with both null and the audit trail for a safety-relevant
field has no author. It mattered less while the table was empty; from here
pipeline/promote.py fills it with the supplier's safety and material claims,
and those are the fields hard rule 3 most needs a signature on.

The same shape as link.sign_confirmation() (0008) and
pricing.sign_price_decision() (0017):

1. catalog.sign_metadata_verdict(), BEFORE INSERT OR UPDATE: moving `status` to
   `verified` or `rejected` with no `verified_by` signs `current_user` and
   `now()`; a caller that names the reviewer is kept (and gets `now()` if it
   left the time out); moving back to `pending_review` clears both. Only a
   change of status signs.
2. ck_product_metadata_verdict_signed: a verified or rejected row has a
   signer, and a pending one has none.
3. ck_product_metadata_signature_complete: `verified_by` and `verified_at`
   are set together or not at all.

catalog.product_metadata is empty on every database this has run against.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-21

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0020'
down_revision: Union[str, Sequence[str], None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE FUNCTION catalog.sign_metadata_verdict() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;
            IF NEW.status IN ('verified', 'rejected') THEN
                NEW.verified_by := COALESCE(NEW.verified_by, current_user);
                NEW.verified_at := COALESCE(NEW.verified_at, now());
            ELSE
                NEW.verified_by := NULL;
                NEW.verified_at := NULL;
            END IF;
            RETURN NEW;
        END;
        $fn$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_product_metadata_sign_verdict
        BEFORE INSERT OR UPDATE ON catalog.product_metadata
        FOR EACH ROW EXECUTE FUNCTION catalog.sign_metadata_verdict()
        """
    )
    op.create_check_constraint(
        "ck_product_metadata_verdict_signed", "product_metadata",
        "(status = 'pending_review') = (verified_by IS NULL)", schema="catalog",
    )
    op.create_check_constraint(
        "ck_product_metadata_signature_complete", "product_metadata",
        "(verified_by IS NULL) = (verified_at IS NULL)", schema="catalog",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_product_metadata_signature_complete", "product_metadata", schema="catalog")
    op.drop_constraint("ck_product_metadata_verdict_signed", "product_metadata", schema="catalog")
    op.execute("DROP TRIGGER trg_product_metadata_sign_verdict ON catalog.product_metadata")
    op.execute("DROP FUNCTION catalog.sign_metadata_verdict()")
