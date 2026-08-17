"""add notes to review_queue

`ops.review_queue` records what was flagged and who closed it, but had nowhere
to say why. The queue's whole value is the judgement applied to an item —
"supplier sent the wrong pack size, corrected against the data sheet" — and
without a note that reasoning lives in the reviewer's head and is gone by the next
review.

Nullable, matching `assigned_to` and `resolved_by`: most rows will not carry
one, and an absent note is meaningfully different from an empty one.

The blank guard is there because the field will be edited through NocoDB, where
clicking into a text box and back out writes an empty string rather than a
null. Without it, `notes IS NOT NULL` would stop meaning "somebody wrote
something", which is the only question anyone will ask of this column.
`btrim` also catches a note that is nothing but spaces.

Deliberately not required on resolution. A note is worth having, but forcing
one on every close turns it into "ok" typed a hundred times, and that is worse
than an empty column — it looks like a record and is not one.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "review_queue",
        sa.Column("notes", sa.Text(), nullable=True),
        schema="ops",
    )
    op.create_check_constraint(
        "ck_review_queue_notes_not_blank",
        "review_queue",
        "notes IS NULL OR btrim(notes) <> ''",
        schema="ops",
    )
    op.execute(
        """
        COMMENT ON COLUMN ops.review_queue.notes IS
        'Why this was flagged, or what was done about it. Free text, written '
        'by whoever works the queue. Null means no note; it can never be blank.'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_review_queue_notes_not_blank",
        "review_queue",
        schema="ops",
        type_="check",
    )
    op.drop_column("review_queue", "notes", schema="ops")
