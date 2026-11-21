"""Write the products ready to publish as a Shopify product CSV.

The last pilot step (Stage 7, step 11), and the catalogue-sync path: the CSV is
imported by a human, which is the gate (hard rules 9 and 13). This module only
reads the database and writes files; it never touches a store.

What is ready is `review.ready_to_publish`, per variant and channel: an
approved price, a preferred source, a verified image, nothing pending review,
not yet published. A product is exported only when every one of its variants
is ready. A product with some variants not ready is held whole and listed with
the reason, because a CSV that carried half a product would publish it with
real variants missing; and a product with a variant already published is held
too, since changing a live product is the API sync's job (Stage 10), not a
re-import's.

Each product's options are its taxonomy node's variant axes, in that order,
holding exactly the combinations its variants have (hard rule 10). A product of
one variant with no option values is Shopify's "Title / Default Title".
Metafield columns come from ops.metafield_map; capacity's numeric value goes in
the variant-level volume metafield, because option strings sort as text.

Prices are ex VAT, as stored. Stock is left untracked: availability is the
daily sync's, never the catalogue CSV's.

    python -m pipeline.shopify.export --out products.csv [--report held.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from dotenv import load_dotenv

from pipeline.config import REPO_ROOT

SHOPIFY_CONFIG = REPO_ROOT / "config" / "shopify.yaml"
AXES = ("capacity", "colour", "size")  # what catalog.variant_options can hold
VOLUME_UNITS = {"ml": "MILLILITERS", "l": "LITERS"}
MAX_OPTIONS = 3

COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value", "Option3 Name", "Option3 Value",
    "Variant SKU", "Variant Grams", "Variant Inventory Tracker", "Variant Fulfillment Service",
    "Variant Price", "Variant Requires Shipping", "Variant Taxable", "Variant Barcode", "Variant Weight Unit",
    "Image Src", "Image Position", "Image Alt Text", "SEO Title", "SEO Description", "Status",
]


class ExportError(ValueError):
    """The catalogue or the config cannot be written as a valid Shopify CSV."""


@dataclass(frozen=True)
class Mapping:
    internal_key: str
    namespace: str
    shopify_key: str
    shopify_type: str
    owner_type: str

    def header(self, template: str) -> str:
        name = self.shopify_key.replace("_", " ").capitalize()
        return template.format(name=name, owner=self.owner_type, namespace=self.namespace, key=self.shopify_key)


@dataclass
class Variant:
    variant_id: int
    sku: str
    ean: str | None
    weight_g: int | None
    price: Decimal | None
    price_status: str | None = None
    publish_status: str | None = None
    options: dict[str, dict[str, Any]] = field(default_factory=dict)  # axis -> option row


@dataclass
class Product:
    product_id: int
    handle: str
    title: str
    description: str | None
    seo_title: str | None
    seo_description: str | None
    vendor: str | None
    node_name: str
    variant_axes: tuple[str, ...]
    variants: list[Variant] = field(default_factory=list)
    images: list[tuple[str, int]] = field(default_factory=list)  # (url, position)
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)  # verified product_metadata by key


@dataclass
class Export:
    products: list[Product] = field(default_factory=list)
    held: list[tuple[Product, str]] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {"products_exported": len(self.products),
                "variants_exported": sum(len(p.variants) for p in self.products),
                "products_held": len(self.held)}


# ------------------------------------------------------------------- config

def load_settings(path: Path = SHOPIFY_CONFIG) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = sorted({"channel_type", "status", "published", "option_names", "metafield_header",
                      "weight_unit", "fulfillment_service"} - set(raw))
    if missing:
        raise ExportError(f"{path.name}: missing {missing}")
    if raw["status"] not in ("draft", "active"):
        raise ExportError(f"{path.name}: status must be draft or active, not {raw['status']!r}")
    return raw


# --------------------------------------------------------------------- load

def load_export(conn: sa.Connection, settings: dict[str, Any]) -> Export:
    channels = conn.execute(sa.text("SELECT channel_id FROM ops.channels WHERE active AND type = :t"),
                            {"t": settings["channel_type"]}).scalars().all()
    if len(channels) != 1:
        raise ExportError(f"expected one active {settings['channel_type']} channel, found {len(channels)}")
    channel_id = channels[0]

    ready = set(conn.execute(sa.text("SELECT variant_id FROM review.ready_to_publish WHERE channel_id = :c"),
                             {"c": channel_id}).scalars())
    if not ready:
        return Export()
    product_ids = conn.execute(sa.text(
        "SELECT DISTINCT product_id FROM catalog.variants WHERE variant_id = ANY(:v)"), {"v": list(ready)}).scalars().all()

    products: dict[int, Product] = {}
    for r in conn.execute(sa.text("""
        SELECT p.product_id, p.handle, p.title, p.description, p.seo_title, p.seo_description,
               b.name AS vendor, t.name AS node_name, t.variant_axes
        FROM catalog.products p
        JOIN catalog.taxonomy t ON t.category_id = p.category_id
        LEFT JOIN catalog.brands b ON b.brand_id = p.brand_id
        WHERE p.product_id = ANY(:p)
    """), {"p": product_ids}):
        products[r.product_id] = Product(r.product_id, r.handle, r.title, r.description, r.seo_title,
                                         r.seo_description, r.vendor, r.node_name, tuple(r.variant_axes or ()))

    variants: dict[int, Variant] = {}
    for r in conn.execute(sa.text("""
        SELECT v.variant_id, v.product_id, v.mosferics_sku, v.ean, v.weight_g, vp.final_price,
               vp.status AS price_status, cp.publish_status
        FROM catalog.variants v
        LEFT JOIN pricing.variant_prices vp ON vp.variant_id = v.variant_id AND vp.channel_id = :c
        LEFT JOIN ops.channel_publish cp ON cp.variant_id = v.variant_id AND cp.channel_id = :c
        WHERE v.product_id = ANY(:p)
        ORDER BY v.product_id, v.mosferics_sku
    """), {"c": channel_id, "p": product_ids}):
        v = Variant(r.variant_id, r.mosferics_sku, r.ean, r.weight_g, r.final_price, r.price_status, r.publish_status)
        variants[r.variant_id] = v
        products[r.product_id].variants.append(v)
    for r in conn.execute(sa.text("""
        SELECT variant_id, axis, value, canonical_value, unit, qualifier FROM catalog.variant_options
        WHERE variant_id = ANY(:v)
    """), {"v": list(variants)}):
        variants[r.variant_id].options[r.axis] = r._asdict()
    for r in conn.execute(sa.text("""
        SELECT product_id, url, position FROM catalog.images
        WHERE product_id = ANY(:p) AND status = 'verified' ORDER BY product_id, position, image_id
    """), {"p": product_ids}):
        products[r.product_id].images.append((r.url, r.position))
    for r in conn.execute(sa.text("""
        SELECT product_id, key, value, canonical_value, unit FROM catalog.product_metadata
        WHERE product_id = ANY(:p) AND status = 'verified'
    """), {"p": product_ids}):
        products[r.product_id].metadata[r.key] = r._asdict()

    export = Export()
    for p in sorted(products.values(), key=lambda p: p.handle):
        reason = _held_reason(p, ready)
        if reason:
            export.held.append((p, reason))
        else:
            export.products.append(p)
    return export


def _held_reason(p: Product, ready: set[int]) -> str | None:
    if any(v.publish_status == "published" for v in p.variants):
        return "a variant is already published: changes to a live product go through the API sync"
    waiting = [v for v in p.variants if v.variant_id not in ready]
    if not waiting:
        return None
    why = []
    for v in waiting:
        if v.price_status not in ("approved", "auto_approved"):
            why.append(f"{v.sku} has no live price")
        elif v.publish_status is None:
            why.append(f"{v.sku} has no publish intent for this channel")
        else:
            why.append(f"{v.sku} is not ready (image, metadata review or source)")
    return f"{len(waiting)} of {len(p.variants)} variants not ready: " + "; ".join(why)


# -------------------------------------------------------------------- write

def load_mappings(conn: sa.Connection) -> list[Mapping]:
    mappings = [Mapping(*r) for r in conn.execute(sa.text(
        "SELECT internal_key, namespace, shopify_key, shopify_type, owner_type FROM ops.metafield_map "
        "WHERE active ORDER BY owner_type, internal_key"))]
    for m in mappings:
        if m.owner_type == "variant" and m.internal_key not in AXES:
            raise ExportError(f"metafield_map {m.internal_key}: a variant metafield must be one of {AXES}")
        if m.owner_type == "variant" and m.internal_key == "colour":
            raise ExportError("metafield_map colour: colour is an option, not a metafield (two sources of truth)")
    return mappings


def metafield_value(m: Mapping, row: dict[str, Any] | None) -> str:
    """One metafield cell from an option or metadata row; blank when there is no value."""
    if row is None:
        return ""
    if m.shopify_type == "volume":
        if row.get("canonical_value") is None:
            return ""
        unit = VOLUME_UNITS.get(row.get("unit") or "")
        if unit is None:
            raise ExportError(f"{m.internal_key}: unit {row.get('unit')!r} has no Shopify volume unit")
        return json.dumps({"value": float(row["canonical_value"]), "unit": unit})
    if m.shopify_type == "number_decimal":
        return "" if row.get("canonical_value") is None else str(row["canonical_value"])
    if m.shopify_type == "single_line_text_field":
        return _label(row) if "axis" in row else str(row.get("value") or "")
    raise ExportError(f"metafield_map {m.internal_key}: type {m.shopify_type!r} is not supported")


def _label(option: dict[str, Any]) -> str:
    """The selector label, as link.py writes it: the value, then the line mark that tells twins apart."""
    return f"{option['value']} {option['qualifier']}" if option.get("qualifier") else option["value"]


def product_rows(p: Product, settings: dict[str, Any], mappings: list[Mapping]) -> list[dict[str, str]]:
    axes = [a for a in p.variant_axes if any(a in v.options for v in p.variants)]
    stray = {a for v in p.variants for a in v.options} - set(axes)
    if stray:
        raise ExportError(f"{p.handle}: options {sorted(stray)} are not axes of its node {p.variant_axes}")
    if len(axes) > MAX_OPTIONS:
        raise ExportError(f"{p.handle}: {len(axes)} option axes; Shopify allows {MAX_OPTIONS}")
    names = settings["option_names"]
    for a in axes:
        if a not in names:
            raise ExportError(f"config/shopify.yaml option_names has no name for axis {a!r}")
    combos = set()
    for v in p.variants:
        combo = tuple(_label(v.options[a]) if a in v.options else "" for a in axes)
        if axes and "" in combo:
            raise ExportError(f"{p.handle}: {v.sku} has no value on {axes[combo.index('')]}, which its siblings have")
        if combo in combos:
            raise ExportError(f"{p.handle}: two variants share the options {combo}")
        combos.add(combo)
    if not axes and len(p.variants) > 1:
        raise ExportError(f"{p.handle}: {len(p.variants)} variants and no option to tell them apart")

    rows: list[dict[str, str]] = []
    for i, v in enumerate(p.variants):
        row = {c: "" for c in COLUMNS}
        row["Handle"] = p.handle
        if i == 0:
            row.update({
                "Title": p.title, "Body (HTML)": p.description or "", "Vendor": p.vendor or "",
                "Type": p.node_name, "Published": "TRUE" if settings["published"] else "FALSE",
                "SEO Title": p.seo_title or "", "SEO Description": p.seo_description or "",
                "Status": settings["status"],
            })
        if axes:
            for n, a in enumerate(axes, start=1):
                if i == 0:
                    row[f"Option{n} Name"] = names[a]
                row[f"Option{n} Value"] = _label(v.options[a])
        else:
            row["Option1 Name"], row["Option1 Value"] = "Title", "Default Title"
        if v.price is None:
            raise ExportError(f"{p.handle}: {v.sku} has no price")
        row.update({
            "Variant SKU": v.sku, "Variant Grams": "" if v.weight_g is None else str(v.weight_g),
            "Variant Fulfillment Service": settings["fulfillment_service"],
            "Variant Price": f"{v.price:.2f}", "Variant Requires Shipping": "TRUE", "Variant Taxable": "TRUE",
            "Variant Barcode": v.ean or "", "Variant Weight Unit": settings["weight_unit"],
        })
        for m in mappings:
            source = v.options.get(m.internal_key) if m.owner_type == "variant" else \
                (p.metadata.get(m.internal_key) if i == 0 else None)
            row[m.header(settings["metafield_header"])] = metafield_value(m, source)
        if i < len(p.images):
            row["Image Src"], row["Image Position"] = p.images[i][0], str(i + 1)
            row["Image Alt Text"] = p.title
        rows.append(row)
    for i in range(len(p.variants), len(p.images)):  # more images than variants: image-only rows
        rows.append({**{c: "" for c in COLUMNS}, "Handle": p.handle, "Image Src": p.images[i][0],
                     "Image Position": str(i + 1), "Image Alt Text": p.title})
    return rows


def write_csv(export: Export, settings: dict[str, Any], mappings: list[Mapping], path: Path) -> int:
    headers = COLUMNS + [m.header(settings["metafield_header"]) for m in mappings]
    rows = [r for p in export.products for r in product_rows(p, settings, mappings)]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, restval="")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def write_report(export: Export, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["handle", "title", "variants", "outcome", "reason"])
        for p in export.products:
            w.writerow([p.handle, p.title, len(p.variants), "exported", ""])
        for p, reason in export.held:
            w.writerow([p.handle, p.title, len(p.variants), "held", reason])


def run(conn: sa.Connection, settings: dict[str, Any], out: Path, report: Path | None = None) -> dict[str, int]:
    mappings = load_mappings(conn)
    export = load_export(conn, settings)
    result = export.summary()
    result["csv_rows"] = write_csv(export, settings, mappings, out)
    if report:
        write_report(export, report)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="the Shopify product CSV to write")
    parser.add_argument("--report", type=Path, help="every candidate product, exported or held, and why")
    args = parser.parse_args()

    settings = load_settings()
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))  # an export reads; it never writes
            result = run(conn, settings, args.out, args.report)
    except ExportError as exc:
        raise SystemExit(f"export failed: {exc}") from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
