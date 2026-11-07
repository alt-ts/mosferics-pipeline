"""Load a reviewed category-map CSV into link.category_map.

The CSV is the export of one supplier's `supplier_category_raw` strings with a
suggested taxonomy node each, marked up by a human: every row says `map` (insert
it, at `override_path` if given, else `suggested_path`) or `manual` (leave it out
of the map, so its products go to the review queue for a per-product category).
A blank decision raises; the loader never guesses what the reviewer meant.

It only inserts. A string already mapped to the same node is left alone, so a
re-run is a no-op; one mapped to a different node, or now marked `manual`,
raises — changing a confirmed mapping is an UPDATE or DELETE of live data and
is done deliberately, not by re-running a loader (hard rule 11).

    python -m pipeline.load_category_map artis data/review/artis_category_map_suggestions.csv \\
        --confirmed-by "Tom Storey"            # print the plan, touch nothing
    ... --apply                                 # insert, in one transaction
"""
from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv

COLUMNS = [
    "supplier_category_raw", "product_count", "sample_names",
    "suggested_category_id", "suggested_path", "confidence", "note",
    "decision", "override_path",
]
DECISIONS = {"map", "manual"}


@dataclass(frozen=True)
class Decision:
    """One reviewed CSV row. `path` is None for a string left to manual review."""

    raw: str
    path: str | None
    suggested_category_id: int | None
    overridden: bool


@dataclass(frozen=True)
class Plan:
    supplier_id: int
    inserts: list[tuple[str, int, str]]   # (raw, category_id, path)
    unchanged: list[str]
    manual: list[str]
    not_in_csv: list[str]                 # supplier strings the CSV does not mention


def read_decisions(path: Path) -> list[Decision]:
    """Parse and check the CSV. Collects every bad row, then raises once."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ValueError(
                f"{path}: columns are {reader.fieldnames}, expected {COLUMNS}. "
                "Was it saved from a spreadsheet that added or renamed a column?"
            )
        rows = list(reader)
    return parse_decisions(rows)


def parse_decisions(rows: list[dict[str, str]]) -> list[Decision]:
    errors: list[str] = []
    decisions: list[Decision] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):   # line 1 is the header
        raw = row["supplier_category_raw"]
        decision = row["decision"].strip().lower()
        override = row["override_path"].strip()
        where = f"line {line} ({raw!r})"
        if raw in seen:
            errors.append(f"{where}: appears twice")
            continue
        seen.add(raw)
        if decision not in DECISIONS:
            errors.append(f"{where}: decision is {row['decision']!r}, expected one of {sorted(DECISIONS)}")
            continue
        if decision == "manual":
            if override:
                errors.append(f"{where}: marked manual but has override_path {override!r}")
                continue
            decisions.append(Decision(raw, None, None, False))
            continue
        suggested_id = row["suggested_category_id"].strip()
        if not suggested_id.isdigit():
            errors.append(f"{where}: suggested_category_id {suggested_id!r} is not an integer")
            continue
        decisions.append(Decision(
            raw, override or row["suggested_path"].strip(), int(suggested_id), bool(override)))
    if errors:
        raise ValueError(f"{len(errors)} row(s) not loadable:\n  " + "\n  ".join(errors))
    return decisions


def plan(conn: sa.Connection, supplier_code: str, decisions: list[Decision]) -> Plan:
    """Check every decision against the live tables and work out what to insert."""
    supplier_id = conn.execute(
        sa.text("SELECT supplier_id FROM supplier.suppliers WHERE code = :code"),
        {"code": supplier_code},
    ).scalar_one_or_none()
    if supplier_id is None:
        raise ValueError(f"no supplier with code {supplier_code!r}")

    nodes = dict(conn.execute(sa.text("SELECT path, category_id FROM catalog.taxonomy")).all())
    raws = set(conn.execute(sa.text(
        "SELECT DISTINCT supplier_category_raw FROM supplier.supplier_products "
        "WHERE supplier_id = :s AND supplier_category_raw IS NOT NULL"), {"s": supplier_id},
    ).scalars())
    mapped = dict(conn.execute(sa.text(
        "SELECT supplier_category_raw, category_id FROM link.category_map WHERE supplier_id = :s"),
        {"s": supplier_id},
    ).all())

    errors: list[str] = []
    inserts: list[tuple[str, int, str]] = []
    unchanged: list[str] = []
    manual: list[str] = []
    for d in decisions:
        where = repr(d.raw)
        if d.raw not in raws:
            # Most likely a spreadsheet trimmed or re-encoded the string; the
            # map keys on it exactly, so a near-miss would never match a row.
            errors.append(f"{where}: no {supplier_code} product carries this string")
            continue
        if d.path is None:
            if d.raw in mapped:
                errors.append(f"{where}: marked manual but already mapped to category {mapped[d.raw]}")
            else:
                manual.append(d.raw)
            continue
        if d.path not in nodes:
            errors.append(f"{where}: path {d.path!r} is not in catalog.taxonomy")
            continue
        if not d.overridden and nodes[d.path] != d.suggested_category_id:
            errors.append(
                f"{where}: suggested_category_id {d.suggested_category_id} does not match "
                f"{d.path!r} (category {nodes[d.path]}); the CSV has been altered or the tree moved")
            continue
        category_id = nodes[d.path]
        if d.raw in mapped:
            if mapped[d.raw] == category_id:
                unchanged.append(d.raw)
            else:
                errors.append(
                    f"{where}: already mapped to category {mapped[d.raw]}, CSV says {category_id}")
            continue
        inserts.append((d.raw, category_id, d.path))
    if errors:
        raise ValueError(f"{len(errors)} decision(s) conflict with the database:\n  "
                         + "\n  ".join(errors))

    in_csv = {d.raw for d in decisions}
    return Plan(supplier_id, inserts, unchanged, manual, sorted(raws - in_csv))


def apply(conn: sa.Connection, p: Plan, confirmed_by: str) -> int:
    """Insert the planned rows, all signed by one reviewer at one timestamp."""
    if not confirmed_by.strip():
        raise ValueError("confirmed_by is empty; every map row is signed by a person")
    if not p.inserts:
        return 0
    conn.execute(
        sa.text(
            "INSERT INTO link.category_map "
            "(supplier_id, supplier_category_raw, category_id, confirmed_by, confirmed_at) "
            "VALUES (:supplier_id, :raw, :category_id, :confirmed_by, now())"
        ),
        [{"supplier_id": p.supplier_id, "raw": raw, "category_id": category_id,
          "confirmed_by": confirmed_by.strip()} for raw, category_id, _ in p.inserts],
    )
    return len(p.inserts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier", help="supplier code, e.g. artis")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--confirmed-by", required=True, help="the reviewer, written to confirmed_by")
    parser.add_argument("--apply", action="store_true", help="insert; without it, only print the plan")
    args = parser.parse_args()
    if not args.confirmed_by.strip():
        parser.error("--confirmed-by is empty")

    decisions = read_decisions(args.csv)
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    with engine.begin() as conn:
        if not args.apply:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        p = plan(conn, args.supplier, decisions)
        for raw, category_id, path in p.inserts:
            print(f"insert  {category_id:>4}  {path:<60}  {raw}")
        print(f"\n{len(p.inserts)} to insert, {len(p.unchanged)} already mapped, "
              f"{len(p.manual)} left for manual review")
        if p.not_in_csv:
            print(f"{len(p.not_in_csv)} {args.supplier} string(s) are not in the CSV and stay "
                  f"unmapped: {p.not_in_csv}")
        if not args.apply:
            print("dry run: nothing written. Re-run with --apply to insert.")
            return
        count = apply(conn, p, args.confirmed_by)
    print(f"inserted {count} rows into link.category_map on {engine.url.database}")


if __name__ == "__main__":
    main()
