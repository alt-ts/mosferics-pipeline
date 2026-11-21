"""Record which products are meant for a channel, and which are live there.

Publish intent is a merchandising decision, not a data one, and it is made per
product: every variant of a product moves together, because the export sends
whole products. `ops.channel_publish.publish_status` per variant and channel:

    not_published   known, not meant for this channel (or withdrawn)
    pending         publish intent: export it once everything else is ready
    published       live on the channel

Three commands, each a dry run unless --apply:

    intend      products -> pending. A variant already published is left alone.
    withdraw    pending -> not_published. A published product is not withdrawn
                here: taking down a live product is the API sync's job.
    published   after a human has imported an export CSV and checked it, its
                products -> published. Every SKU in the file must be a pending
                variant of the handle it sits under, or nothing is written.

Select products by --handle (repeatable), --category (a taxonomy path; covers
everything beneath it) or --all.

    python -m pipeline.publish intend --category glassware-drinkware [--fulfilment dropship] [--apply]
    python -m pipeline.publish withdraw --handle perception-wine [--apply]
    python -m pipeline.publish published --csv out/products.csv [--apply]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dotenv import load_dotenv

NOT_PUBLISHED, PENDING, PUBLISHED = "not_published", "pending", "published"
FULFILMENT_MODES = ("stocked", "on_demand", "dropship")


class PublishError(ValueError):
    """The selection or the file does not say unambiguously what to change."""


def channel_id(conn: sa.Connection, channel_type: str) -> int:
    ids = conn.execute(sa.text("SELECT channel_id FROM ops.channels WHERE active AND type = :t"),
                       {"t": channel_type}).scalars().all()
    if len(ids) != 1:
        raise PublishError(f"expected one active {channel_type} channel, found {len(ids)}")
    return ids[0]


def select_variants(conn: sa.Connection, channel: int, *, handles: list[str] | None = None,
                    category: str | None = None, everything: bool = False) -> list[sa.Row]:
    """Every variant of the selected active products, with its current state on the channel."""
    if sum(bool(x) for x in (handles, category, everything)) != 1:
        raise PublishError("choose exactly one of --handle, --category or --all")
    if category and not conn.execute(sa.text("SELECT 1 FROM catalog.taxonomy WHERE path = :p"),
                                     {"p": category}).first():
        raise PublishError(f"no taxonomy node at {category!r}")
    rows = conn.execute(sa.text("""
        SELECT p.handle, v.variant_id, v.mosferics_sku, cp.publish_status
        FROM catalog.products p
        JOIN catalog.taxonomy t ON t.category_id = p.category_id
        JOIN catalog.variants v ON v.product_id = p.product_id
        LEFT JOIN ops.channel_publish cp ON cp.variant_id = v.variant_id AND cp.channel_id = :c
        WHERE p.status = 'active'
          AND (:all OR p.handle = ANY(CAST(:h AS text[])) OR t.path = :cat OR t.path LIKE :under)
        ORDER BY p.handle, v.mosferics_sku
    """), {"c": channel, "all": everything, "h": handles or [], "cat": category,
           "under": f"{category}/%" if category else None}).all()
    if handles:
        missing = sorted(set(handles) - {r.handle for r in rows})
        if missing:
            raise PublishError(f"no active product with handle {missing}")
    return rows


def intend(conn: sa.Connection, channel: int, rows: list[sa.Row], fulfilment: str | None,
           apply: bool) -> dict[str, int]:
    if fulfilment is not None and fulfilment not in FULFILMENT_MODES:
        raise PublishError(f"--fulfilment must be one of {FULFILMENT_MODES}")
    counts: Counter[str] = Counter()
    for r in rows:
        if r.publish_status == PUBLISHED:
            counts["already_published"] += 1
            continue
        if r.publish_status == PENDING and fulfilment is None:
            counts["already_pending"] += 1
            continue
        counts["marked_pending"] += 1
        if apply:
            conn.execute(sa.text("""
                INSERT INTO ops.channel_publish (variant_id, channel_id, publish_status, fulfilment_mode)
                VALUES (:v, :c, 'pending', :f)
                ON CONFLICT (variant_id, channel_id) DO UPDATE SET publish_status = 'pending',
                    fulfilment_mode = COALESCE(EXCLUDED.fulfilment_mode, ops.channel_publish.fulfilment_mode)
                WHERE ops.channel_publish.publish_status <> 'published'
            """), {"v": r.variant_id, "c": channel, "f": fulfilment})
    counts["products"] = len({r.handle for r in rows})
    return dict(counts)


def withdraw(conn: sa.Connection, channel: int, rows: list[sa.Row], apply: bool) -> dict[str, int]:
    live = sorted({r.handle for r in rows if r.publish_status == PUBLISHED})
    if live:
        raise PublishError(f"published on this channel, so not withdrawn here: {live}")
    pending = [r for r in rows if r.publish_status == PENDING]
    if apply:
        for r in pending:
            conn.execute(sa.text("UPDATE ops.channel_publish SET publish_status = 'not_published' "
                                 "WHERE variant_id = :v AND channel_id = :c AND publish_status = 'pending'"),
                         {"v": r.variant_id, "c": channel})
    return {"withdrawn": len(pending), "not_pending": len(rows) - len(pending),
            "products": len({r.handle for r in rows})}


def mark_published(conn: sa.Connection, channel: int, csv_path: Path, apply: bool) -> dict[str, int]:
    """The products of an imported export CSV, checked SKU by SKU against what is pending."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        pairs = [(r["Handle"], r["Variant SKU"]) for r in csv.DictReader(f) if r.get("Variant SKU")]
    if not pairs:
        raise PublishError(f"{csv_path}: no variant rows")
    handles = sorted({h for h, _ in pairs})
    state = {(r.handle, r.mosferics_sku): r for r in select_variants(conn, channel, handles=handles)}
    errors = [f"{sku} under {handle}: " + ("not a variant of that product" if (handle, sku) not in state
                                           else f"is {state[handle, sku].publish_status or 'not marked'}, not pending")
              for handle, sku in pairs
              if (handle, sku) not in state or state[handle, sku].publish_status != PENDING]
    in_file = set(pairs)
    left_out = [f"{sku} under {handle}" for (handle, sku), r in state.items()
                if (handle, sku) not in in_file and r.publish_status != PUBLISHED]
    if left_out:
        errors.append("the file leaves out variants of its products: " + ", ".join(left_out))
    if errors:
        raise PublishError(f"{csv_path}: nothing marked published:\n  " + "\n  ".join(errors))
    if apply:
        for handle, sku in pairs:
            conn.execute(sa.text("UPDATE ops.channel_publish SET publish_status = 'published' "
                                 "WHERE variant_id = :v AND channel_id = :c AND publish_status = 'pending'"),
                         {"v": state[handle, sku].variant_id, "c": channel})
    return {"published": len(pairs), "products": len(handles)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("intend", "withdraw"):
        p = sub.add_parser(name)
        p.add_argument("--handle", action="append")
        p.add_argument("--category")
        p.add_argument("--all", action="store_true")
        if name == "intend":
            p.add_argument("--fulfilment", choices=FULFILMENT_MODES)
    p = sub.add_parser("published")
    p.add_argument("--csv", type=Path, required=True, help="the export CSV a human has imported and checked")
    for p in sub.choices.values():
        p.add_argument("--channel", default="trade_store", help="ops.channels type (default trade_store)")
        p.add_argument("--apply", action="store_true", help="write; without it, report only")
    args = parser.parse_args()

    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            if not args.apply:
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            channel = channel_id(conn, args.channel)
            result: dict[str, Any]
            if args.command == "published":
                result = mark_published(conn, channel, args.csv, args.apply)
            else:
                rows = select_variants(conn, channel, handles=args.handle, category=args.category,
                                       everything=args.all)
                result = intend(conn, channel, rows, args.fulfilment, args.apply) if args.command == "intend" \
                    else withdraw(conn, channel, rows, args.apply)
    except PublishError as exc:
        raise SystemExit(f"publish {args.command} failed, nothing written: {exc}") from exc
    print(json.dumps(result, indent=2))
    if not args.apply:
        print("report only: nothing written (add --apply)")


if __name__ == "__main__":
    main()
