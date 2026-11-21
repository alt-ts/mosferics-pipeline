"""sign price decisions; let the pipeline read review; map capacity to volume

Three small things the Shopify export (pipeline/shopify/export.py) and the
approval workflow need.

1. pricing.sign_price_decision(), a BEFORE INSERT OR UPDATE trigger on
   pricing.variant_prices. A price moved to `approved` or `rejected` with no
   `approved_by` is signed `current_user`, the way link.sign_confirmation()
   (0008) signs a confirmed link: a NocoDB approval becomes `mos_nocodb`, and
   a caller that names the approver is left alone. A price moved back to
   `pending_approval` has its `approved_by` cleared, so a stale approver never
   sits beside a price nobody has approved. `auto_approved` is the pipeline's
   and is not touched. `approved_by` on a rejected row names who rejected it.
   Only a change of status is signed: a recalculation or an override edit
   that leaves status alone keeps the signer it had.

   No CHECK that a decided price has a signer: 10 prices were approved in
   NocoDB on 21 Nov 2026 before this trigger existed, and nobody recorded who.
   They stay unsigned rather than being given a name after the fact.

2. mos_pipeline may read the `review` schema. The export takes its rows from
   review.ready_to_publish, which only mos_nocodb could read. SELECT only,
   and for views added later via default privileges.

3. ops.metafield_map gets its first row: capacity -> custom.volume, a
   variant-level `volume` metafield, so capacity can be filtered numerically
   (option strings sort as text). Seeded here, as ops.delivery_config was, so a
   rebuilt database has it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-21

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE FUNCTION pricing.sign_price_decision() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            -- Only a decision is signed: an update that leaves status alone
            -- (a recalculation, an override) keeps whatever signer it had.
            IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;
            IF NEW.status IN ('approved', 'rejected') AND NEW.approved_by IS NULL THEN
                NEW.approved_by := current_user;
            ELSIF NEW.status = 'pending_approval' THEN
                NEW.approved_by := NULL;
            END IF;
            RETURN NEW;
        END;
        $fn$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_variant_prices_sign_decision
        BEFORE INSERT OR UPDATE ON pricing.variant_prices
        FOR EACH ROW EXECUTE FUNCTION pricing.sign_price_decision()
        """
    )
    op.execute(
        "COMMENT ON COLUMN pricing.variant_prices.approved_by IS "
        "'Who decided the price: the approver, or for a rejected price the rejecter. "
        "Signed current_user by trg_variant_prices_sign_decision when left null; "
        "''pipeline'' on auto_approved; cleared on pending_approval.'"
    )

    op.execute("GRANT USAGE ON SCHEMA review TO mos_pipeline")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA review TO mos_pipeline")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA review "
        "GRANT SELECT ON TABLES TO mos_pipeline"
    )

    op.execute(
        """
        INSERT INTO ops.metafield_map (internal_key, namespace, shopify_key, shopify_type, owner_type, active)
        VALUES ('capacity', 'custom', 'volume', 'volume', 'variant', true)
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM ops.metafield_map WHERE internal_key = 'capacity' AND owner_type = 'variant'"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA review "
        "REVOKE SELECT ON TABLES FROM mos_pipeline"
    )
    op.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA review FROM mos_pipeline")
    op.execute("REVOKE USAGE ON SCHEMA review FROM mos_pipeline")
    op.execute("COMMENT ON COLUMN pricing.variant_prices.approved_by IS NULL")
    op.execute("DROP TRIGGER trg_variant_prices_sign_decision ON pricing.variant_prices")
    op.execute("DROP FUNCTION pricing.sign_price_decision()")
