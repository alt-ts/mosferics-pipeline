"""create raw schema

The audit trail. Rows land here exactly as the supplier sent them and are never
updated or deleted (CLAUDE.md hard rule 11). Deliberately carries NO foreign key
to supplier.suppliers: raw is written before anything is validated, and a file
from an unknown supplier must still be landable. `raw` is also created first, so
the supplier schema does not exist yet.

Revision ID: 0001
Revises:
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA raw")

    op.create_table(
        "import_batches",
        sa.Column("batch_id", sa.BigInteger, sa.Identity(), primary_key=True),
        # No FK by design — see module docstring.
        sa.Column("supplier_id", sa.Integer, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("file_hash", sa.Text, nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("row_count", sa.Integer),
        sa.Column("status", sa.Text),
        schema="raw",
    )
    op.create_index(
        "ix_import_batches_supplier_received",
        "import_batches",
        ["supplier_id", "received_at"],
        schema="raw",
    )
    # Not unique: the same file legitimately arrives twice. The index makes
    # "have we seen this exact file before?" cheap for ingest to answer.
    op.create_index(
        "ix_import_batches_file_hash", "import_batches", ["file_hash"], schema="raw"
    )

    op.create_table(
        "import_rows",
        sa.Column("batch_id", sa.BigInteger, nullable=False),
        sa.Column("row_number", sa.Integer, nullable=False),
        # The row exactly as received.
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.PrimaryKeyConstraint("batch_id", "row_number", name="pk_import_rows"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["raw.import_batches.batch_id"],
            name="fk_import_rows_batch",
            ondelete="RESTRICT",
        ),
        schema="raw",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SCHEMA raw CASCADE")
