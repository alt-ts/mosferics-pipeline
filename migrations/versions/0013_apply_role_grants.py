"""apply role grants

The Stage 2 grant block, finally run. Until now `relacl` was empty on every
table in every schema and `pg_default_acl` had no rows at all: only `mos_owner`
could read or write anything, anywhere. Nothing had noticed because everything
so far runs as `mos_owner`.

This is a migration rather than the psql snippet the build doc prescribed, for
three reasons. Hard rule 5 says no ad-hoc DDL and GRANT is DDL. The build doc's
own Stage 4 exit criterion is that `alembic upgrade head` builds the whole
database from empty on a clean instance — which was not true in any useful
sense while the grants lived outside the chain, since the result worked for
`mos_owner` and for nobody else. And a migration is testable on scratch, which
rule 12 asks for and a hand-run snippet does not get.

`ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner` is the clause that matters most
here and the easiest to get wrong. Default privileges attach to the role that
will CREATE the object, defaulting to whoever runs the statement. The build doc
said to run this block as `postgres`, which would have recorded `postgres` —
while Alembic connects as `mos_owner`. Every table a later migration created
would still have been born ungranted, and the statement whose entire job is to
prevent that would have been the thing that silently failed. Naming the role
explicitly makes it correct regardless of who runs it.

Privileges follow the Stage 2 table, tightened in three places where the
schema knows something the table does not:

  * `raw` gets SELECT and INSERT only, never UPDATE, with UPDATE on
    `import_batches` added back by name. `raw.import_rows` is the audit trail
    (hard rule 11) and is already immutable by trigger from 0004; withholding
    the privilege makes the permission system state the same fact, so the rule
    survives someone dropping the trigger. `import_batches.status` is
    operational state and rule 11 says it may move, so it is granted.
  * `mos_pipeline` gets USAGE on `catalog.mosferics_sku_seq`. It is the one
    sequence not owned by an identity column, and `catalog.next_mosferics_sku`
    calls `nextval` on it as the caller, not as definer. Without this the
    pipeline allocates no SKUs — a failure that would only have appeared at
    runtime, on the first import.
  * `mos_nocodb` gets column-level UPDATE on `pricing.variant_prices`, limited
    to `override_price`, `status` and `approved_by`. The Stage 0 register locks
    the reviewer's price rights to the override; this expresses that exactly rather
    than granting the whole row and trusting the UI not to offer the rest.
    `calculated_price` is the pricing engine's column and stays out of reach.

No role gets DELETE on anything, matching the Stage 2 table.

Idempotent by construction — GRANT and ALTER DEFAULT PRIVILEGES both no-op on
a privilege already held — so re-running costs nothing. It raises rather than
skipping if a role is missing, because a silently partial grant is the failure
this migration exists to end.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROLES = ("mos_pipeline", "mos_nocodb", "mos_trigger")

# `stock` is not built yet; add it here when it lands.
DATA_SCHEMAS = ("supplier", "catalog", "link", "ops", "pricing")

# Tables the reviewer edits through NocoDB. Everything else in the schemas below is
# readable but not writable by them.
NOCODB_WRITABLE = (
    "catalog.products",
    "catalog.variants",
    "catalog.product_metadata",
    "catalog.compliance",
    "catalog.documents",
    "catalog.images",
    "catalog.taxonomy",
    "catalog.brands",
    "catalog.collections",
    "catalog.ranges",
    "catalog.product_relations",
    "link.variant_links",
    "link.category_map",
    "ops.review_queue",
    "ops.channel_publish",
    "ops.channels",
    "ops.delivery_config",
    "ops.metafield_map",
)


def _require_roles() -> None:
    op.execute(
        """
        DO $$
        DECLARE missing text;
        BEGIN
            SELECT string_agg(r, ', ') INTO missing
            FROM unnest(ARRAY['mos_pipeline', 'mos_nocodb', 'mos_trigger']) AS r
            WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r);
            IF missing IS NOT NULL THEN
                RAISE EXCEPTION
                    'missing role(s): %. Create them before granting.', missing;
            END IF;
        END $$
        """
    )


def upgrade() -> None:
    """Upgrade schema."""
    _require_roles()

    # ------------------------------------------------------------------- raw
    # Append-only. No UPDATE in the blanket grant; batches get it back by name.
    for role in ("mos_pipeline", "mos_trigger"):
        op.execute(f"GRANT USAGE ON SCHEMA raw TO {role}")
        op.execute(f"GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA raw TO {role}")
        op.execute(f"GRANT UPDATE (status) ON raw.import_batches TO {role}")
        op.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA raw "
            f"GRANT SELECT, INSERT ON TABLES TO {role}"
        )

    # NocoDB reads the audit trail and never writes to it.
    op.execute("GRANT USAGE ON SCHEMA raw TO mos_nocodb")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA raw TO mos_nocodb")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA raw "
        "GRANT SELECT ON TABLES TO mos_nocodb"
    )

    # `mos_trigger` stops here. It runs an import and does nothing else, so a
    # credential leaked in the staff's distributed executable reaches two tables.

    # --------------------------------------------------------- data schemas
    for schema in DATA_SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO mos_pipeline")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} "
            "TO mos_pipeline"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA {schema} "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO mos_pipeline"
        )

        # NocoDB reads all of it, writes only where review happens.
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO mos_nocodb")
        op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO mos_nocodb")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA {schema} "
            "GRANT SELECT ON TABLES TO mos_nocodb"
        )

    for table in NOCODB_WRITABLE:
        op.execute(f"GRANT INSERT, UPDATE ON {table} TO mos_nocodb")

    # The reviewer's price rights are the override and the approval gate, nothing more.
    op.execute(
        "GRANT UPDATE (override_price, status, approved_by) "
        "ON pricing.variant_prices TO mos_nocodb"
    )

    # The one sequence not owned by an identity column. next_mosferics_sku()
    # runs as the caller, so the caller needs it.
    op.execute("GRANT USAGE ON SEQUENCE catalog.mosferics_sku_seq TO mos_pipeline")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "REVOKE USAGE ON SEQUENCE catalog.mosferics_sku_seq FROM mos_pipeline"
    )

    for schema in DATA_SCHEMAS:
        for role in ("mos_pipeline", "mos_nocodb"):
            op.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA {schema} "
                f"REVOKE SELECT, INSERT, UPDATE ON TABLES FROM {role}"
            )
            op.execute(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}"
            )
            op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {role}")

    for role in ROLES:
        op.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA raw "
            f"REVOKE SELECT, INSERT, UPDATE ON TABLES FROM {role}"
        )
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA raw FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA raw FROM {role}")
