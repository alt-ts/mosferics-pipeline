"""Match a supplier's image files to its SKUs and record them in catalog.images.

Stage 7, step 9: a port of an earlier standalone script that filled an image
URL column in a spreadsheet by matching filenames to SKUs. The matching is the
same idea, made strict, and the result goes to the database instead.

Files are named by supplier SKU. Two kinds of match, from the supplier's
`images` config:

    exact   the name is the SKU, or the SKU then a numbered suffix:
            14-00-017.jpg, 14-00-017_1.jpg, 14-00-017_2.jpg
    loose   the name starts with the SKU and a separator, then anything:
            "14-00-017 CE.png", "14-00-017-angle.jpg"

An exact match is recorded `verified`, a decision taken for the supplier whose
files are named this way (it is a rule about filenames, not a look at the
picture). A loose match is recorded unreviewed, for a human to verify or
reject. Two rules from the old script are deliberately not kept: matching a SKU
that starts with a filename (a file called "31.jpg" would claim every SKU
beginning 31), and taking the first match in folder order (a SKU with three
pictures gets all three, numbered order first).

Each image belongs to the variant its SKU is linked to, and so to that
variant's product; the gallery is ordered by variant SKU then file number. A
file already recorded is left as it is, so a human's verdict on it stands. A
file that matches no SKU, or more than one, is reported, never guessed.

Listing comes from S3 (boto3, credentials from the environment) or from a
manifest file of keys, one per line.

    python -m pipeline.images artis --dry-run [--report images.csv]
    python -m pipeline.images artis
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import sqlalchemy as sa
from dotenv import load_dotenv

from pipeline.config import REPO_ROOT, load_supplier_config

VERIFIED = "verified"
EXACT, LOOSE = "exact", "loose"


class ImageError(ValueError):
    """The images config or the listing cannot be matched as written."""


@dataclass(frozen=True)
class Target:
    supplier_sku: str
    variant_id: int
    product_id: int
    mosferics_sku: str


@dataclass(frozen=True)
class Match:
    key: str
    target: Target
    kind: str      # exact | loose
    number: int    # the _<n> suffix; 0 for none or a loose match


@dataclass
class Plan:
    matches: list[Match] = field(default_factory=list)
    outcomes: list[tuple[str, str, str]] = field(default_factory=list)  # key, outcome, detail
    without_image: list[Target] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for _, outcome, _ in self.outcomes:
            counts[outcome] += 1
        return {**{f"files_{k}": v for k, v in sorted(counts.items())},
                "variants_without_image": len(self.without_image)}


# ------------------------------------------------------------------- config

class Matcher:
    def __init__(self, config: dict) -> None:
        code = config["supplier"]["code"]
        rule = config.get("images")
        if not rule:
            raise ImageError(f"{code}: no images section in the supplier config")
        self.code = code
        self.rule = rule
        if rule.get("source") not in ("s3", "manifest"):
            raise ImageError(f"{code}: images.source must be s3 or manifest")
        if not rule.get("base_url", "").endswith("/"):
            raise ImageError(f"{code}: images.base_url must end with /")
        self.extensions = {e.lower().lstrip(".") for e in rule.get("extensions", [])}
        if not self.extensions:
            raise ImageError(f"{code}: images.extensions is empty")
        self.suffix = re.compile(rule.get("exact_suffix", r"_(?P<n>\d+)"))
        if "n" not in self.suffix.groupindex:
            raise ImageError(f"{code}: images.exact_suffix needs a named group n")
        self.separators = rule.get("loose_separators", " -_")
        self.prefix = rule.get("prefix", "")

    def match(self, key: str, targets: dict[str, Target]) -> tuple[Match | None, str, str]:
        """(match, outcome, detail) for one stored file."""
        name = PurePosixPath(key).name
        stem, _, ext = name.rpartition(".")
        if not stem or ext.lower() not in self.extensions:
            return None, "skipped", f"not one of {sorted(self.extensions)}"
        folded = stem.casefold()
        if folded in targets:
            return Match(key, targets[folded], EXACT, 0), EXACT, targets[folded].supplier_sku
        for m in self.suffix.finditer(stem):
            if m.end() == len(stem) and stem[:m.start()].casefold() in targets:
                t = targets[stem[:m.start()].casefold()]
                return Match(key, t, EXACT, int(m["n"])), EXACT, t.supplier_sku
        loose = [t for sku, t in targets.items()
                 if folded.startswith(sku) and len(folded) > len(sku) and folded[len(sku)] in self.separators]
        if len(loose) == 1:
            return Match(key, loose[0], LOOSE, 0), LOOSE, loose[0].supplier_sku
        if len(loose) > 1:
            return None, "ambiguous", ", ".join(sorted(t.supplier_sku for t in loose))
        return None, "orphan", "matches no SKU"

    def url(self, key: str) -> str:
        return self.rule["base_url"] + key


def list_keys(matcher: Matcher) -> list[str]:
    rule = matcher.rule
    if rule["source"] == "manifest":
        path = REPO_ROOT / rule["manifest"]
        if not path.is_file():
            raise ImageError(f"{matcher.code}: images.manifest {path} not found")
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
        return [l for l in lines if l and not l.startswith("#")]
    try:
        import boto3  # optional: only the s3 source needs it
    except ImportError as exc:
        raise ImageError("images.source s3 needs boto3: pip install boto3") from exc
    client = boto3.client("s3", region_name=rule.get("region"))
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=rule["bucket"], Prefix=matcher.prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []) if not o["Key"].endswith("/"))
    return keys


# --------------------------------------------------------------------- plan

def load_targets(conn: sa.Connection, code: str) -> dict[str, Target]:
    """Every supplier SKU with a link that is not rejected, by folded SKU."""
    rows = conn.execute(sa.text("""
        SELECT DISTINCT ON (sp.supplier_product_id) sp.supplier_sku, l.variant_id, v.product_id, v.mosferics_sku
        FROM supplier.supplier_products sp
        JOIN supplier.suppliers s USING (supplier_id)
        JOIN link.variant_links l ON l.supplier_product_id = sp.supplier_product_id AND l.status <> 'rejected'
        JOIN catalog.variants v ON v.variant_id = l.variant_id
        WHERE s.code = :c
        ORDER BY sp.supplier_product_id, l.status = 'confirmed' DESC
    """), {"c": code}).all()
    targets: dict[str, Target] = {}
    for r in rows:
        folded = r.supplier_sku.casefold()
        if folded in targets:
            raise ImageError(f"{code}: SKUs {targets[folded].supplier_sku!r} and {r.supplier_sku!r} differ only in case")
        targets[folded] = Target(r.supplier_sku, r.variant_id, r.product_id, r.mosferics_sku)
    return targets


def load_plan(conn: sa.Connection, matcher: Matcher, keys: list[str]) -> Plan:
    targets = load_targets(conn, matcher.code)
    stored = set(conn.execute(sa.text("SELECT s3_key FROM catalog.images WHERE s3_key = ANY(:k)"),
                              {"k": keys}).scalars())
    plan = Plan()
    imaged: set[int] = set(conn.execute(sa.text(
        "SELECT DISTINCT variant_id FROM catalog.images WHERE variant_id IS NOT NULL")).scalars())
    for key in sorted(set(keys)):
        match, outcome, detail = matcher.match(key, targets)
        if match and key in stored:
            outcome, detail = "already_stored", match.target.supplier_sku
            imaged.add(match.target.variant_id)
        elif match:
            plan.matches.append(match)
            imaged.add(match.target.variant_id)
        plan.outcomes.append((key, outcome, detail))
    plan.without_image = sorted((t for t in targets.values() if t.variant_id not in imaged),
                                key=lambda t: t.mosferics_sku)
    return plan


def write_plan(conn: sa.Connection, matcher: Matcher, plan: Plan) -> dict[str, int]:
    """Append each product's new images after any it already has, in gallery order."""
    by_product: dict[int, list[Match]] = defaultdict(list)
    for m in plan.matches:
        by_product[m.target.product_id].append(m)
    written = 0
    for product_id, matches in by_product.items():
        start = conn.execute(sa.text("SELECT coalesce(max(position) + 1, 0) FROM catalog.images WHERE product_id = :p"),
                             {"p": product_id}).scalar_one()
        ordered = sorted(matches, key=lambda m: (m.target.mosferics_sku, m.kind != EXACT, m.number, m.key))
        for offset, m in enumerate(ordered):
            written += conn.execute(sa.text("""
                INSERT INTO catalog.images (product_id, variant_id, s3_key, url, position, source, status)
                VALUES (:p, :v, :k, :u, :pos, :src, :st)
                ON CONFLICT (s3_key) WHERE s3_key IS NOT NULL DO NOTHING
            """), {"p": product_id, "v": m.target.variant_id, "k": m.key, "u": matcher.url(m.key),
                   "pos": start + offset, "src": f"supplier_file:{m.kind}",
                   "st": VERIFIED if m.kind == EXACT else None}).rowcount
    return {"images": written}


def write_report(plan: Plan, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "outcome", "detail"])
        w.writerows(plan.outcomes)
        for t in plan.without_image:
            w.writerow(["", "variant_without_image", f"{t.mosferics_sku} ({t.supplier_sku})"])


def run(conn: sa.Connection, config: dict, *, dry_run: bool, report: Path | None = None,
        keys: list[str] | None = None) -> dict[str, Any]:
    matcher = Matcher(config)
    keys = list_keys(matcher) if keys is None else keys
    plan = load_plan(conn, matcher, keys)
    if report:
        write_report(plan, report)
    result: dict[str, Any] = {"plan": plan.summary()}
    if not dry_run:
        result["written"] = write_plan(conn, matcher, plan)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier")
    parser.add_argument("--dry-run", action="store_true", help="match and report, write nothing")
    parser.add_argument("--report", type=Path, help="every file and its outcome, and variants left without one")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            if args.dry_run:
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            result = run(conn, config, dry_run=args.dry_run, report=args.report)
    except ImageError as exc:
        raise SystemExit(f"images failed, nothing written: {exc}") from exc
    print(json.dumps(result, indent=2))
    if args.dry_run:
        print("dry run: nothing written")


if __name__ == "__main__":
    main()
