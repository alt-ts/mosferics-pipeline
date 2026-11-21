"""Write the synthetic demo supplier's files into sample_data/demo/.

Everything here is invented: the brand (Halden), the product names, SKUs,
barcodes and prices. It exists so the pipeline can run end to end in Docker
without real supplier files, which cannot be published because they hold trade
prices. Re-running it rewrites the same three files byte for byte.

Each row is chosen to exercise one thing the pipeline does (see the comments):
the demo is a tour of the cases, not a realistic catalogue.

    python sample_data/make_demo_files.py
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parent / "demo"
BRAND = "Halden"

PRICE_COLUMNS = ["SKU", "Product", "Brand", "Pack Size", "List Price", "Unit Price", "Pack Price"]
PRODUCT_COLUMNS = ["Code", "Description", "Brand", "Category", "Type", "Style", "Range", "Material", "Finish",
                   "Capacity (cl)", "Capacity (ml)", "Box Qty", "Sales Pack", "Barcode",
                   "Dishwasher Safe", "Handwash Only", "Microwave Safe", "Diameter (cm)", "Weight (kg)"]
MAP_COLUMNS = ["supplier_category_raw", "product_count", "sample_names", "suggested_category_id", "suggested_path",
               "confidence", "note", "decision", "override_path"]

WINE = ("Drinkware", "Stemware", "Wine")
HIBALL = ("Drinkware", "Tumbler", "Hi-Ball")
JUG = ("Drinkware", "Jug", "-")
PLATE = ("Tableware", "Plate", "Coupe")
BOWL = ("Tableware", "Bowl", "Deep")
BAR = ("Bar", "Tools", "-")
STOOL = ("Furniture", "Stool", "-")

# sku, name, category, range, material, finish, cl, ml, price pack, product box qty, unit price, safety flags, cm
ROWS = [
    # A capacity range, and a lined glass beside its unlined twin at the same brim.
    ("90-10-001", "Aurora Wine 25cl [8.75oz]", WINE, "Aurora", "Glass", "", 25, 250, 24, 24, "1.20", "DW", None),
    ("90-10-002", "Aurora Wine 35cl [12.25oz]", WINE, "Aurora", "Glass", "", 35, 350, 24, 24, "1.35", "DW", None),
    ("90-10-003", "Aurora Wine 35cl [12.25oz] (CE@175ml)", WINE, "Aurora", "Glass", "", 35, 350, 24, 24, "1.45", "DW", None),
    ("90-10-004", "Aurora Wine 45cl [15.75oz]", WINE, "Aurora", "Glass", "", 45, 450, 24, 24, "1.55", "DW", None),
    # Two hi-balls group; the third's brim column contradicts its name (10.5 is the oz figure).
    ("90-11-001", "Harbour Hi-Ball 30cl [10.5oz]", HIBALL, "Harbour", "Glass", "", 30, 300, 48, 48, "0.85", "DW", None),
    ("90-11-002", "Harbour Hi-Ball 40cl [14oz]", HIBALL, "Harbour", "Glass", "", 40, 400, 48, 48, "0.95", "DW", None),
    ("90-11-003", "Harbour Hi-Ball 50cl [17.5oz]", HIBALL, "Harbour", "Glass", "", 10.5, 105, 48, 48, "1.05", "DW", None),
    # The supplier claims both dishwasher safe and handwash only.
    ("90-11-010", "Gilded Rocks 30cl [10.5oz]", HIBALL, "Gilded", "Glass", "", 30, 300, 6, 6, "4.80", "DW HW", None),
    # A litre and over reads in litres: 100cl becomes "1L".
    ("90-12-001", "Ripple Jug 100cl [35oz]", JUG, "Ripple", "Glass", "", 100, 1000, 6, 6, "3.90", "DW", None),
    # Two SKUs nothing tells apart: the group is not built, both go to review.
    ("90-12-005", "Ripple Carafe 50cl [17.5oz]", JUG, "Ripple", "Glass", "", 50, 500, 12, 12, "2.10", "DW", None),
    ("90-12-006", "Ripple Carafe 50cl [17.5oz]", JUG, "Ripple", "Glass", "", 50, 500, 12, 12, "2.15", "DW", None),
    # A size-and-colour matrix with a gap: there is no Blue 21cm, and none is invented.
    # The 27cm White's two files disagree on the pack size (12 against 6).
    ("90-20-001", "Lumen Coupe Plate White 21cm", PLATE, "Lumen", "Porcelain", "Gloss", 0, 0, 12, 12, "2.60", "DW MW", 21),
    ("90-20-002", "Lumen Coupe Plate White 27cm", PLATE, "Lumen", "Porcelain", "Gloss", 0, 0, 12, 6, "3.40", "DW MW", 27),
    ("90-20-003", "Lumen Coupe Plate Blue 27cm", PLATE, "Lumen", "Porcelain", "Gloss", 0, 0, 12, 12, "3.60", "DW MW", 27),
    ("90-21-001", "Lumen Deep Bowl White 16cm", BOWL, "Lumen", "Porcelain", "Gloss", 0, 0, 12, 12, "2.20", "DW MW", 16),
    ("90-21-002", "Lumen Deep Bowl Grey 16cm", BOWL, "Lumen", "Porcelain", "Gloss", 0, 0, 12, 12, "2.30", "DW MW", 16),
    # A two-tone colour, matched whole rather than as grey and white.
    ("90-21-010", "Tidal Bowl Grey/White 16cm", BOWL, "Tidal", "Stoneware", "Reactive Glaze", 0, 0, 6, 6, "4.10", "DW", 16),
    # A material the vocabulary does not know: it goes to review, it is not guessed.
    ("90-21-020", "Verde Bowl Green 14cm", BOWL, "Verde", "Bioresin", "Matt", 0, 0, 6, 6, "3.20", "", 14),
    # Single items with no options: Shopify's "Default Title".
    ("90-30-001", "Halden Bar Spoon 30cm", BAR, "Bar Tools", "Stainless Steel", "Polished", 0, 0, 1, 1, "2.40", "DW", None),
    ("90-30-002", "Halden Hawthorne Strainer", BAR, "Bar Tools", "Stainless Steel", "Polished", 0, 0, 1, 1, "3.10", "DW", None),
    # A category the reviewer leaves unmapped, for a per-product decision.
    ("90-40-001", "Halden Bar Stool", STOOL, "Furniture", "Steel", "", 0, 0, 1, 1, "38.00", "", None),
]

# Where each category string goes. "manual" leaves it out of the map.
CATEGORY_MAP = {
    WINE: "glassware-drinkware/wine-glasses",
    HIBALL: "glassware-drinkware/tumblers-hi-balls/hi-ball-tall-tumblers",
    JUG: "glassware-drinkware/jugs-carafes-decanters",
    PLATE: "tableware-crockery/plates",
    BOWL: "tableware-crockery/bowls",
    BAR: "bar-cocktail/ice-garnish-bar-tools",
    STOOL: None,
}

# The demo bucket's keys, one per case pipeline/images.py handles.
IMAGES = [
    # exact, numbered: a product gallery in SKU then number order
    *[f"demo/{sku}_1.jpg" for sku in ("90-10-001", "90-10-002", "90-10-003", "90-10-004", "90-11-001", "90-11-002",
                                      "90-20-001", "90-20-002", "90-20-003", "90-21-001", "90-21-002", "90-21-010",
                                      "90-21-020", "90-30-001", "90-30-002")],
    "demo/90-10-001_2.jpg",             # a second picture of the same glass
    "demo/90-12-001.png",               # exact with no number
    "demo/90-11-010 front.jpg",         # loose: recorded for a human to verify
    "demo/99-99-999_1.jpg",             # orphan: no such SKU, reported not guessed
    "demo/90-30-001_1.tif",             # not an image type the supplier config allows
]


def gtin13(body12: str) -> str:
    """A valid EAN-13 in the 200-299 in-store range, which no real product carries."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def category(c: tuple[str, str, str]) -> str:
    return " > ".join(c)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with (OUT / "Demo_Price_File_Sample.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(PRICE_COLUMNS)
        for sku, name, _, _, _, _, _, _, pack, _, unit, _, _ in ROWS:
            unit_price = Decimal(unit)
            w.writerow([sku, name, BRAND, pack, f"{unit_price * Decimal('1.6'):.2f}", unit,
                        f"{unit_price * pack:.2f}"])
    with (OUT / "Demo_Product_Data_Sample.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(PRODUCT_COLUMNS)
        for n, (sku, name, cat, rng, material, finish, cl, ml, pack, box, _, flags, cm) in enumerate(ROWS):
            w.writerow([sku, name, BRAND, *cat, rng, material, finish, cl or 0, ml or 0, box,
                        f"1 Box of {box}pcs", gtin13(f"2009000{n:05d}"),
                        "Y" if "DW" in flags else "", "Y" if "HW" in flags else "", "Y" if "MW" in flags else "",
                        cm or 0, "0.25"])
    counts: dict[tuple[str, str, str], list[str]] = {}
    for row in ROWS:
        counts.setdefault(row[2], []).append(row[1])
    with (OUT / "demo_category_map.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(MAP_COLUMNS)
        for cat, names in counts.items():
            path = CATEGORY_MAP[cat]
            # override_path, not suggested_path: a suggestion carries a category_id
            # from one particular database, and the demo builds a fresh one.
            w.writerow([category(cat), len(names), "; ".join(names[:3]), 0, "", "", "synthetic demo data",
                        "map" if path else "manual", path or ""])
    with (OUT / "image_manifest.txt").open("w", encoding="utf-8", newline="\n") as f:
        f.write("# Synthetic image keys for the demo supplier: no files exist behind them.\n")
        f.write("\n".join(IMAGES) + "\n")
    print(f"wrote {len(ROWS)} SKUs, {len(counts)} category strings and {len(IMAGES)} image keys to {OUT}")


if __name__ == "__main__":
    main()
