"""make raw.import_rows immutable

Enforces hard rule 11 structurally rather than by convention: the payload is the
proof of what a supplier actually sent, so UPDATE and DELETE are rejected at the
database. TRUNCATE gets its own statement-level trigger because row-level
triggers do not fire for it.

raw.import_batches is deliberately NOT covered — `status` is operational state
and must stay editable as a batch progresses.

Escape hatch, for a genuine correction (owner only, never the pipeline roles):

    ALTER TABLE raw.import_rows DISABLE TRIGGER trg_import_rows_immutable;
    -- ... corrective statement, then re-enable immediately ...
    ALTER TABLE raw.import_rows ENABLE TRIGGER trg_import_rows_immutable;

This is a guard against mistaken writes, not a security boundary: mos_owner can
disable it. mos_pipeline and mos_nocodb cannot.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE FUNCTION raw.reject_import_row_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                RAISE EXCEPTION
                    'raw.import_rows is append-only: TRUNCATE rejected'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RAISE EXCEPTION
                'raw.import_rows is append-only: % rejected (batch_id=%, row_number=%)',
                TG_OP, OLD.batch_id, OLD.row_number
                USING ERRCODE = 'restrict_violation';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_import_rows_immutable
        BEFORE UPDATE OR DELETE ON raw.import_rows
        FOR EACH ROW EXECUTE FUNCTION raw.reject_import_row_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_import_rows_no_truncate
        BEFORE TRUNCATE ON raw.import_rows
        FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_import_row_mutation();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER trg_import_rows_no_truncate ON raw.import_rows")
    op.execute("DROP TRIGGER trg_import_rows_immutable ON raw.import_rows")
    op.execute("DROP FUNCTION raw.reject_import_row_mutation()")
