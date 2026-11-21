"""Carry the supplier's facts onto the catalog: barcodes, and claims for review.

Run after link.py, once supplier products have variants. Two things that until
now stayed in supplier_products.specs, where the catalog could not see them:

Barcodes. A variant with no `ean` takes the barcode of the supplier SKU it is
linked to, if that barcode passed its GTIN check digit when it was extracted.
An `ean` already set is never changed, and an invalid barcode is never copied
(it stays in specs, as received). A variant linked to several supplier SKUs
with different valid barcodes takes its preferred source's; with no preferred
source to decide, it is left empty and reported.

Claims. Every value extract and normalise marked `pending_review` is a claim a
human must check before it is published (hard rule 3): the safety flags, and
vocabulary values such as material whose config says so. Which fields those are
is the supplier YAML's decision, not this module's. A claim becomes a
catalog.product_metadata row for the product, still `pending_review`, sourced
`supplier`, so review.ready_to_publish holds the product until someone verifies
it. A product-level row needs every variant of the product to say the same
thing: if some variants claim "dishwasher safe" and others are silent, or the
materials differ, no row is written and the product goes to the review queue
as `metadata_conflict`, because a claim for the product would be untrue of
some of what it sells. A row already present is left alone, so a verdict
stands and a re-run writes nothing.

    python -m pipeline.promote artis --dry-run [--report promote.csv]
    python -m pipeline.promote artis
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dotenv import load_dotenv

from pipeline.config import load_supplier_config

PENDING = "pending_review"
TRUE = "true"
ISSUE_CONFLICT = "metadata_conflict"


class PromoteError(ValueError):
    """The config does not say where to find what should be promoted."""


@dataclass(frozen=True)
class Member:
    product_id: int
    handle: str
    variant_id: int
    mosferics_sku: str
    ean: str | None
    supplier_sku: str
    link_status: str
    is_preferred: bool
    specs: dict[str, Any]


@dataclass
class Plan:
    barcodes: list[tuple[Member, str]] = field(default_factory=list)   # (variant's member, barcode)
    claims: list[tuple[int, str, str]] = field(default_factory=list)    # (product_id, key, value)
    conflicts: list[tuple[int, str]] = field(default_factory=list)      # (product_id, notes)
    report: list[tuple[str, str, str, str]] = field(default_factory=list)  # (sku or handle, what, outcome, detail)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for _, what, outcome, _ in self.report:
            counts[f"{what}_{outcome}"] += 1
        return dict(sorted(counts.items()))


def claims_of(specs: dict[str, Any]) -> dict[str, str]:
    """Every value the extract marked for review, as key -> value."""
    found: dict[str, str] = {}
    for flag, status in (specs.get("safety") or {}).items():
        if status == PENDING:
            found[flag] = TRUE
    for key, value in specs.items():
        if isinstance(value, dict) and value.get("status") == PENDING and value.get("canonical"):
            found[key] = ", ".join(sorted(value["canonical"]))
    return found


def load_members(conn: sa.Connection, code: str) -> list[Member]:
    rows = conn.execute(sa.text("""
        SELECT v.product_id, p.handle, v.variant_id, v.mosferics_sku, v.ean, sp.supplier_sku,
               l.status AS link_status, l.is_preferred, sp.specs
        FROM link.variant_links l
        JOIN catalog.variants v USING (variant_id)
        JOIN catalog.products p USING (product_id)
        JOIN supplier.supplier_products sp USING (supplier_product_id)
        JOIN supplier.suppliers s ON s.supplier_id = sp.supplier_id
        WHERE s.code = :c AND l.status <> 'rejected'
        ORDER BY p.handle, v.mosferics_sku, sp.supplier_sku
    """), {"c": code}).all()
    return [Member(**r._mapping) for r in rows]


def plan_barcodes(plan: Plan, members: list[Member], key: str) -> None:
    by_variant: dict[int, list[Member]] = defaultdict(list)
    for m in members:
        by_variant[m.variant_id].append(m)
    for links in by_variant.values():
        v = links[0]
        if v.ean:
            continue
        valid = {m.supplier_sku: m.specs.get(key) for m in links if m.specs.get(key) and m.specs.get(f"{key}_valid")}
        invalid = [m.supplier_sku for m in links if m.specs.get(key) and not m.specs.get(f"{key}_valid")]
        if not valid:
            if invalid:
                plan.report.append((v.mosferics_sku, "barcode", "invalid_not_copied", ", ".join(invalid)))
            continue
        if len(set(valid.values())) > 1:
            preferred = [m for m in links if m.is_preferred and m.supplier_sku in valid]
            if not preferred:
                plan.report.append((v.mosferics_sku, "barcode", "ambiguous", "; ".join(f"{s}={b}" for s, b in valid.items())))
                continue
            barcode = valid[preferred[0].supplier_sku]
        else:
            barcode = next(iter(valid.values()))
        plan.barcodes.append((v, barcode))
        plan.report.append((v.mosferics_sku, "barcode", "copied", barcode))


def plan_claims(plan: Plan, members: list[Member], existing: set[tuple[int, str]]) -> None:
    by_product: dict[int, dict[int, Member]] = defaultdict(dict)
    for m in members:
        # One voice per variant: its preferred source if it has one, else its first link.
        current = by_product[m.product_id].get(m.variant_id)
        if current is None or (m.is_preferred and not current.is_preferred):
            by_product[m.product_id][m.variant_id] = m
    for product_id, variants in by_product.items():
        voices = sorted(variants.values(), key=lambda m: m.mosferics_sku)
        handle = voices[0].handle
        said = {m.mosferics_sku: claims_of(m.specs) for m in voices}
        for key in sorted({k for c in said.values() for k in c}):
            if (product_id, key) in existing:
                plan.report.append((handle, "claim", "already_recorded", key))
                continue
            values = {sku: c.get(key) for sku, c in said.items()}
            if len(set(values.values())) == 1:
                value = next(iter(values.values()))
                plan.claims.append((product_id, key, value))
                plan.report.append((handle, "claim", "recorded", f"{key} = {value}"))
            else:
                notes = f"{handle}: {key}: " + "; ".join(f"{sku} {v or 'not stated'}" for sku, v in values.items())
                plan.conflicts.append((product_id, notes))
                plan.report.append((handle, "claim", "conflict", notes))


def load_plan(conn: sa.Connection, config: dict) -> Plan:
    code = config["supplier"]["code"]
    barcode = config.get("barcode")
    if barcode and not str(barcode.get("store_as", "")).startswith("specs."):
        raise PromoteError(f"{code}: barcode.store_as must name a specs key")
    members = load_members(conn, code)
    existing = set(conn.execute(sa.text("SELECT product_id, key FROM catalog.product_metadata "
                                        "WHERE product_id = ANY(CAST(:p AS bigint[]))"),
                                {"p": list({m.product_id for m in members})}).all())
    plan = Plan()
    if barcode:
        plan_barcodes(plan, members, barcode["store_as"].removeprefix("specs."))
    plan_claims(plan, members, {tuple(e) for e in existing})
    return plan


def write_plan(conn: sa.Connection, plan: Plan) -> dict[str, int]:
    for v, barcode in plan.barcodes:
        conn.execute(sa.text("UPDATE catalog.variants SET ean = :b WHERE variant_id = :v AND ean IS NULL"),
                     {"b": barcode, "v": v.variant_id})
    claims = 0
    for product_id, key, value in plan.claims:
        claims += conn.execute(sa.text("""
            INSERT INTO catalog.product_metadata (product_id, key, value, source, status)
            VALUES (:p, :k, :v, 'supplier', 'pending_review')
            ON CONFLICT (product_id, key) DO NOTHING
        """), {"p": product_id, "k": key, "v": value}).rowcount
    open_items = set(conn.execute(sa.text(
        "SELECT entity_id, notes FROM ops.review_queue WHERE status = 'open' AND entity_type = 'product_metadata' "
        "AND issue_type = :t"), {"t": ISSUE_CONFLICT}).all())
    raised = 0
    for product_id, notes in plan.conflicts:
        if (product_id, notes) in open_items:
            continue
        conn.execute(sa.text("INSERT INTO ops.review_queue (entity_type, entity_id, issue_type, notes) "
                             "VALUES ('product_metadata', :p, :t, :n)"), {"p": product_id, "t": ISSUE_CONFLICT, "n": notes})
        raised += 1
    return {"barcodes": len(plan.barcodes), "claims": claims, "review_items": raised}


def write_report(plan: Plan, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["subject", "what", "outcome", "detail"])
        w.writerows(plan.report)


def run(conn: sa.Connection, config: dict, *, dry_run: bool, report: Path | None = None) -> dict[str, Any]:
    plan = load_plan(conn, config)
    if report:
        write_report(plan, report)
    result: dict[str, Any] = {"plan": plan.summary()}
    if not dry_run:
        result["written"] = write_plan(conn, plan)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier")
    parser.add_argument("--dry-run", action="store_true", help="plan and report, write nothing")
    parser.add_argument("--report", type=Path, help="every barcode and claim, and what happened to it")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            if args.dry_run:
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            result = run(conn, config, dry_run=args.dry_run, report=args.report)
    except PromoteError as exc:
        raise SystemExit(f"promote failed, nothing written: {exc}") from exc
    print(json.dumps(result, indent=2))
    if args.dry_run:
        print("dry run: nothing written")


if __name__ == "__main__":
    main()
