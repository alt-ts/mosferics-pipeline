"""Resolve staged rows to canonical values and write the supplier tables.

The third pipeline step. It converts capacities to ml, resolves material and
finish through the alias files, then writes supplier.supplier_products,
supplier.supplier_offers, and ops.review_queue for anything a human must see.
Nothing here reaches catalog.*: the canonical values wait in `specs` for link.py,
which alone has a product to attach them to.

Safety claims and materials stay `pending_review` and colour stays `proposed`
(hard rule 3). Supplier stock is not touched (hard rule 4).

Idempotent. Dates come from the batch rather than the clock, so running the same
batches twice changes nothing: a product is matched on its SKU, an offer is
replaced only when its terms differ, and a queue item is not raised a second time
for the same fact, even after someone has closed it.

    python -m pipeline.normalise artis --dry-run   # resolve and report, write nothing
    python -m pipeline.normalise artis             # write the supplier tables
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dotenv import load_dotenv

from pipeline.config import load_supplier_config
from pipeline.extract import (
    VOCABULARY_DIR,
    ExtractError,
    Issue,
    Quantity,
    SourceBatch,
    StagedRow,
    extract,
    load_aliases,
)

CANONICAL_UNIT = "ml"
# Millilitres per unit, exact. An imperial pint is 568.26125 ml by definition.
ML_PER_UNIT = {
    "ml": Fraction(1),
    "cl": Fraction(10),
    "l": Fraction(1000),
    "imperial_pint": Fraction("568.26125"),
    "imperial_fl_oz": Fraction("28.4130625"),
}
ML_PLACES = Decimal("0.01")
# material.yaml: compound values split on comma or semicolon, each token aliased.
TOKEN_SEPARATOR = re.compile(r"[,;]")

ENTITY_SUPPLIER_PRODUCT = "supplier_product"
ENTITY_VOCABULARY = "vocabulary"
# Only vocabulary faults belong in the view that says "add the alias".
VOCABULARY_ISSUE = re.compile(r"^(colour_ambiguous|unmapped_.+)$")


class NormaliseError(ValueError):
    """A value cannot be made canonical, because the config or the code is wrong."""


@dataclass(frozen=True)
class NormalisedRow:
    staged: StagedRow
    specs: dict[str, Any]
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class WriteResult:
    products_inserted: int
    products_updated: int
    offers_inserted: int
    offers_replaced: int
    offers_unchanged: int
    queue_inserted: int
    queue_already_raised: int


def to_ml(value: str | Decimal, unit: str) -> Decimal:
    """A quantity in ml to 2 places. `value` may be a fraction, such as '1/3' of a pint."""
    if unit not in ML_PER_UNIT:
        raise NormaliseError(f"no conversion from {unit!r} to {CANONICAL_UNIT}")
    exact = Fraction(str(value)) * ML_PER_UNIT[unit]
    return (Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(ML_PLACES)


def _quantity(quantity: Quantity) -> dict[str, Any]:
    """Raw value, canonical value and canonical unit: the data model's three columns."""
    return {
        "value": quantity.raw,
        "source_unit": quantity.unit,
        "canonical_value": to_ml(quantity.value, quantity.unit),
        "unit": CANONICAL_UNIT,
    }


def _line_mark(row: StagedRow) -> dict[str, Any]:
    mark = row.line_mark
    corrected = {"corrected": mark.corrected} if mark.corrected else {}
    return {
        "raw": mark.raw,
        **corrected,
        "parsed": bool(mark.segments),
        "segments": [
            {
                "kind": s.kind,
                "kind_raw": s.kind_raw,
                "values": list(s.values),
                "source_unit": s.unit,
                "canonical_values": [to_ml(v, s.unit) for v in s.values],
                "unit": CANONICAL_UNIT,
            }
            for s in mark.segments
        ],
    }


def _resolve_tokens(field: str, raw: str, aliases: dict[str, str]) -> tuple[list[str], list[Issue]]:
    canonical: list[str] = []
    issues = []
    for token in TOKEN_SEPARATOR.split(raw):
        token = token.strip()
        if not token:
            continue
        match = aliases.get(token.lower())
        if match is None:
            # Worded without the SKU, so one bad value is one queue item, not hundreds.
            issues.append(Issue(f"unmapped_{field}", field, f"{field} {token!r} is not in {field}.yaml"))
        elif match not in canonical:
            canonical.append(match)
    return canonical, issues


def normalise_rows(
    config: dict, rows: list[StagedRow], vocabulary_dir: Path = VOCABULARY_DIR
) -> list[NormalisedRow]:
    """Resolve every staged row. Pure: touches no database."""
    rules = config.get("vocabulary", {})
    aliases = {
        field: load_aliases(field, [], vocabulary_dir)
        for field, rule in rules.items()
        if rule.get("method") != "alias_in_name"
    }
    for field, table in aliases.items():
        compound = [a for a in table if TOKEN_SEPARATOR.search(a)]
        if compound:
            raise NormaliseError(f"{field}.yaml aliases contain a token separator, so cannot match: {compound}")
    cross_check = config.get("capacity", {}).get("brim", {}).get("cross_check")
    if cross_check and cross_check["on_fail"] != "review":
        raise NormaliseError(f"capacity.brim.cross_check.on_fail: {cross_check['on_fail']!r} is not supported")
    return [_normalise_row(row, aliases, cross_check) for row in rows]


def _normalise_row(row: StagedRow, aliases: dict[str, dict[str, str]], cross_check: dict | None) -> NormalisedRow:
    derived: dict[str, Any] = {}
    issues = list(row.issues)

    if row.brim is not None:
        derived["capacity_brim"] = _quantity(row.brim)
        if row.brim_cross_check is not None and cross_check:
            brim_ml = derived["capacity_brim"]["canonical_value"]
            other_ml = to_ml(row.brim_cross_check.value, row.brim_cross_check.unit)
            if abs(brim_ml - other_ml) > Decimal(str(cross_check["tolerance_ml"])):
                issues.append(Issue(
                    "brim_capacity_conflict", "capacity",
                    f"{row.brim.raw} {row.brim.unit} against {row.brim_cross_check.raw} {row.brim_cross_check.unit}",
                ))
    if row.line_mark is not None:
        derived["line_mark"] = _line_mark(row)
    if row.nucleation:
        derived["nucleation"] = True
    if row.size is not None:
        derived["size"] = {"raw": row.size.raw, "dimensions": list(row.size.dimensions), "unit": row.size.unit}
    for field, value in row.vocabulary.items():
        canonical, unmapped = _resolve_tokens(field, value.raw, aliases[field])
        issues.extend(unmapped)
        derived[field] = {"raw": value.raw, "canonical": canonical, "status": value.status}
    if row.colour_candidates:
        derived["colour"] = {"candidates": list(row.colour_candidates), "status": row.colour_status}
    if row.safety:
        derived["safety"] = dict(row.safety)

    # A column mapped to one of these names would silently vanish beneath it.
    clashes = sorted(set(derived) & set(row.specs))
    if clashes:
        raise NormaliseError(f"{row.supplier_sku}: specs keys clash with derived fields: {clashes}")
    return NormalisedRow(row, {**row.specs, **derived}, tuple(issues))


def specs_json(specs: dict[str, Any]) -> str:
    """Decimals become JSON numbers. A float's shortest repr is the decimal that was sent."""
    return json.dumps(specs, default=float, ensure_ascii=False, sort_keys=True)


def _write_product(conn: sa.Connection, supplier_id: int, row: NormalisedRow, seen: datetime) -> tuple[int, bool]:
    """Insert or refresh one product. Returns its id and whether it was new.

    Only the columns the file owns are written. Enriched descriptions and
    `discontinued` belong to other processes and are never overwritten here.
    """
    staged = row.staged
    result = conn.execute(
        sa.text(
            "INSERT INTO supplier.supplier_products AS p "
            "  (supplier_id, supplier_sku, name_raw, name_clean, brand, supplier_category_raw, "
            "   specs, first_seen, last_seen) "
            "VALUES (:supplier_id, :sku, :name_raw, :name_clean, :brand, :category, "
            "        CAST(:specs AS jsonb), :seen, :seen) "
            "ON CONFLICT ON CONSTRAINT uq_supplier_products_sku DO UPDATE SET "
            "  name_raw = EXCLUDED.name_raw, name_clean = EXCLUDED.name_clean, brand = EXCLUDED.brand, "
            "  supplier_category_raw = EXCLUDED.supplier_category_raw, specs = EXCLUDED.specs, "
            "  last_seen = GREATEST(p.last_seen, EXCLUDED.last_seen) "
            "RETURNING supplier_product_id, (xmax = 0) AS inserted"
        ),
        {
            "supplier_id": supplier_id,
            "sku": staged.supplier_sku,
            "name_raw": staged.name_raw,
            "name_clean": staged.name_clean,
            "brand": staged.brand,
            "category": staged.supplier_category_raw,
            "specs": specs_json(row.specs),
            "seen": seen,
        },
    ).one()
    return result.supplier_product_id, result.inserted


def _write_offer(conn: sa.Connection, product_id: int, row: StagedRow, valid_from: datetime) -> str:
    """Make the file's offer the current one. Returns 'inserted', 'replaced' or 'unchanged'."""
    offer = row.offer
    current = conn.execute(
        sa.text(
            "SELECT offer_id, cost_ex_vat, base_units_per_pack, pack_description "
            "FROM supplier.supplier_offers WHERE supplier_product_id = :id AND is_current"
        ),
        {"id": product_id},
    ).one_or_none()
    if current is not None and (current.cost_ex_vat, current.base_units_per_pack, current.pack_description) == (
        offer.cost_ex_vat, offer.base_units_per_pack, offer.pack_description
    ):
        return "unchanged"
    if current is not None:
        # Closed first: a partial unique index allows only one current offer per product.
        conn.execute(
            sa.text(
                "UPDATE supplier.supplier_offers SET is_current = false, valid_to = :valid_to "
                "WHERE offer_id = :offer_id"
            ),
            {"valid_to": valid_from.date(), "offer_id": current.offer_id},
        )
    conn.execute(
        sa.text(
            "INSERT INTO supplier.supplier_offers "
            "  (supplier_product_id, pack_description, base_units_per_pack, cost_ex_vat, valid_from) "
            "VALUES (:id, :pack_description, :units, :cost, :valid_from)"
        ),
        {
            "id": product_id,
            "pack_description": offer.pack_description,
            "units": offer.base_units_per_pack,
            "cost": offer.cost_ex_vat,
            "valid_from": valid_from.date(),
        },
    )
    return "replaced" if current is not None else "inserted"


def _raise_issues(conn: sa.Connection, issues: list[tuple[int, str, Issue]]) -> tuple[int, int]:
    """Queue what is not already open. Returns (inserted, already open).

    Only an open item holds a problem back. One resolved or dismissed whose
    problem is still in the file is raised again: closing an item says it was
    dealt with, so a problem that is back is news. To stop a value being raised
    for good, record the decision in the supplier YAML. Matching on the start of
    `notes` survives someone adding to the note, while a changed supplier value
    words differently and so is a new item.
    """
    existing: dict[tuple[str, int, str], list[str]] = {}
    for q in conn.execute(sa.text(
            "SELECT entity_type, entity_id, issue_type, notes FROM ops.review_queue WHERE status = 'open'")):
        existing.setdefault((q.entity_type, q.entity_id, q.issue_type), []).append(q.notes or "")

    inserted = already = 0
    for product_id, sku, issue in issues:
        entity_type = ENTITY_VOCABULARY if VOCABULARY_ISSUE.match(issue.code) else ENTITY_SUPPLIER_PRODUCT
        notes = issue.detail if entity_type == ENTITY_VOCABULARY and issue.code.startswith("unmapped_") \
            else f"{sku}: {issue.detail}"
        key = (entity_type, product_id, issue.code)
        if any(n.startswith(notes) for n in existing.get(key, [])):
            already += 1
            continue
        conn.execute(
            sa.text(
                "INSERT INTO ops.review_queue (entity_type, entity_id, issue_type, notes) "
                "VALUES (:entity_type, :entity_id, :issue_type, :notes)"
            ),
            {"entity_type": entity_type, "entity_id": product_id, "issue_type": issue.code, "notes": notes},
        )
        existing.setdefault(key, []).append(notes)
        inserted += 1
    return inserted, already


def write_rows(conn: sa.Connection, config: dict, batch: SourceBatch, rows: list[NormalisedRow]) -> WriteResult:
    """Write every row, dated by the primary batch. The caller owns the transaction."""
    supplier_id = conn.execute(
        sa.text("SELECT supplier_id FROM supplier.suppliers WHERE code = :code"),
        {"code": config["supplier"]["code"]},
    ).scalar_one()

    products = Counter()
    offers = Counter()
    issues: list[tuple[int, str, Issue]] = []
    unmapped_seen: dict[tuple[str, str], int] = {}
    for row in rows:
        product_id, inserted = _write_product(conn, supplier_id, row, batch.received_at)
        products["inserted" if inserted else "updated"] += 1
        if row.staged.offer is not None:
            offers[_write_offer(conn, product_id, row.staged, batch.received_at)] += 1
        for issue in row.issues:
            if issue.code.startswith("unmapped_"):
                # One item per value, held against the first product that has it.
                entity = unmapped_seen.setdefault((issue.code, issue.detail), product_id)
                if entity != product_id:
                    continue
            issues.append((product_id, row.staged.supplier_sku, issue))
    queue_inserted, queue_already = _raise_issues(conn, issues)
    return WriteResult(
        products["inserted"], products["updated"], offers["inserted"], offers["replaced"], offers["unchanged"],
        queue_inserted, queue_already,
    )


def normalise(conn: sa.Connection, config: dict) -> tuple[SourceBatch, list[NormalisedRow], WriteResult]:
    """Extract, resolve and write the supplier's latest batches. The caller owns the transaction."""
    batches, staged = extract(conn, config)
    batch = batches[config["join"]["primary"]]
    rows = normalise_rows(config, staged)
    return batch, rows, write_rows(conn, config, batch, rows)


def summarise(rows: list[NormalisedRow]) -> dict[str, int]:
    """What resolved, and how many rows carry each kind of issue."""
    counts = {
        "rows": len(rows),
        "brim capacity in ml": sum("capacity_brim" in r.specs for r in rows),
        "material resolved": sum(bool(r.specs.get("material", {}).get("canonical")) for r in rows),
        "finish resolved": sum(bool(r.specs.get("finish", {}).get("canonical")) for r in rows),
    }
    issues = Counter(i.code for r in rows for i in r.issues)
    counts.update({f"issue: {code}": n for code, n in sorted(issues.items())})
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier", help="supplier code, the name of its YAML in config/suppliers/")
    parser.add_argument("--dry-run", action="store_true", help="resolve and report, write nothing")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    try:
        if args.dry_run:
            with engine.connect() as conn:
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
                _, staged = extract(conn, config)
            rows = normalise_rows(config, staged)
            result = None
        else:
            with engine.begin() as conn:
                _, rows, result = normalise(conn, config)
    except (ExtractError, NormaliseError) as exc:
        raise SystemExit(f"normalise failed, nothing written: {exc}") from exc
    for label, count in summarise(rows).items():
        print(f"{label}: {count}")
    if result is not None:
        print(result)
    print(f"database {engine.url.database}" + ("  (dry run, nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
