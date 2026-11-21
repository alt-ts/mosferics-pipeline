"""Stand in for the human steps of the demo, and say so in every row it signs.

In the real workflow a person does these in NocoDB: confirms each proposed
variant grouping, sets the pricing rules, and approves prices. The demo has no
person, so this script does them, signed "demo script" so the database records
that no human decided anything. Never point it at a real catalogue: approving
in bulk is exactly what the review gate exists to prevent.

    python docker/demo_review.py confirm-groups
    python docker/demo_review.py pricing-rule
    python docker/demo_review.py approve-prices
    python docker/demo_review.py summary
"""
from __future__ import annotations

import os
import sys

import sqlalchemy as sa

SIGNER = "demo script"
DEMO_SUPPLIER = "demo"


def engine() -> sa.Engine:
    e = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    with e.connect() as c:
        real = c.execute(sa.text("SELECT count(*) FROM supplier.suppliers WHERE code <> :d"),
                         {"d": DEMO_SUPPLIER}).scalar_one()
    if real:
        raise SystemExit("this database holds a supplier other than the demo one; refusing to sign for a human")
    return e


def confirm_groups(c: sa.Connection) -> None:
    n = c.execute(sa.text("UPDATE link.variant_links SET status = 'confirmed', confirmed_by = :s, "
                          "confirmed_at = now() WHERE status = 'proposed'"), {"s": SIGNER}).rowcount
    print(f"confirmed {n} proposed links, signed {SIGNER!r}")


def pricing_rule(c: sa.Connection) -> None:
    if c.execute(sa.text("SELECT count(*) FROM pricing.rules WHERE scope_type = 'global'")).scalar_one():
        print("a global pricing rule already exists")
        return
    c.execute(sa.text("INSERT INTO pricing.rules (scope_type, method, value) VALUES ('global', 'fixed_margin', 30)"))
    print("added the catch-all rule: a 30% gross margin (price = cost / 0.70)")


def approve_prices(c: sa.Connection) -> None:
    n = c.execute(sa.text("UPDATE pricing.variant_prices SET status = 'approved', approved_by = :s "
                          "WHERE status = 'pending_approval'"), {"s": SIGNER}).rowcount
    print(f"approved {n} pending prices, signed {SIGNER!r}")


def summary(c: sa.Connection) -> None:
    print("\nproducts and variants:")
    for r in c.execute(sa.text("""
        SELECT p.title, v.mosferics_sku,
               coalesce((SELECT string_agg(concat_ws(' ', o.value, o.qualifier), ' / '
                                           ORDER BY array_position(t.variant_axes, o.axis))
                         FROM catalog.variant_options o WHERE o.variant_id = v.variant_id), '-') AS options,
               lm.final_price, lm.gross_margin, lm.status
        FROM catalog.products p JOIN catalog.variants v USING (product_id)
        JOIN catalog.taxonomy t ON t.category_id = p.category_id
        LEFT JOIN pricing.live_margins lm ON lm.variant_id = v.variant_id
        ORDER BY p.title, v.mosferics_sku""")):
        print(f"  {r.title:<26} {r.mosferics_sku}  {r.options:<22} £{r.final_price:>6}  {r.gross_margin}%  {r.status}")
    print("\nimages (catalog.images):")
    for r in c.execute(sa.text("SELECT coalesce(status, 'unreviewed') AS status, count(*) n FROM catalog.images "
                               "GROUP BY 1 ORDER BY 1")):
        print(f"  {r.status:<36} {r.n}")
    print("\nwaiting for a human in ops.review_queue:")
    for r in c.execute(sa.text("SELECT issue_type, count(*) n FROM ops.review_queue WHERE status = 'open' "
                               "GROUP BY 1 ORDER BY 1")):
        print(f"  {r.issue_type:<36} {r.n}")
    print("\nsupplier categories with no mapping (review.unmapped_categories):")
    for r in c.execute(sa.text("SELECT * FROM review.unmapped_categories")):
        row = r._asdict()
        print("  " + ", ".join(f"{k}={v}" for k, v in row.items() if k in ("supplier_category_raw", "product_count", "supplier")))


STEPS = {"confirm-groups": confirm_groups, "pricing-rule": pricing_rule,
         "approve-prices": approve_prices, "summary": summary}


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        raise SystemExit(f"usage: demo_review.py {{{','.join(STEPS)}}}")
    with engine().begin() as conn:
        STEPS[sys.argv[1]](conn)
