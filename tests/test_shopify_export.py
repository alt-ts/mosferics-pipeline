import csv
import json
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa

from pipeline.shopify.export import (
    ExportError,
    Mapping,
    Product,
    Variant,
    load_settings,
    metafield_value,
    product_rows,
    run,
)

SETTINGS = load_settings()
VOLUME = Mapping("capacity", "custom", "volume", "volume", "variant")
HEADER = "Volume (variant.metafields.custom.volume)"


def option(axis, value, canonical=None, unit=None, qualifier=None):
    return {"axis": axis, "value": value, "canonical_value": canonical, "unit": unit, "qualifier": qualifier}


def glass(sku, value, ml, price="10.00", qualifier=None, colour=None):
    v = Variant(1, sku, None, 250, Decimal(price))
    v.options["capacity"] = option("capacity", value, Decimal(ml), "ml", qualifier)
    if colour:
        v.options["colour"] = option("colour", colour)
    return v


def product(*variants, axes=("capacity", "colour"), images=()):
    p = Product(1, "perception-wine", "Perception Wine", None, None, None, "Artis", "Wine Glasses", axes)
    p.variants.extend(variants)
    p.images.extend(images)
    return p


def rows(p, mappings=(VOLUME,)):
    return product_rows(p, SETTINGS, list(mappings))


# ------------------------------------------------------------------- shaping

def test_a_group_is_one_product_with_only_the_combinations_it_has():
    p = product(glass("A", "25cl", 250, colour="Clear"), glass("B", "35cl", 350, colour="Clear"),
                glass("C", "35cl", 350, colour="Blue"))  # no 25cl Blue: the gap is kept, not filled
    r = rows(p)
    assert [x["Variant SKU"] for x in r] == ["A", "B", "C"]
    assert (r[0]["Option1 Name"], r[0]["Option2 Name"]) == ("Capacity", "Colour")
    assert r[1]["Option1 Name"] == ""                 # names on the first row only
    assert [(x["Option1 Value"], x["Option2 Value"]) for x in r] == [("25cl", "Clear"), ("35cl", "Clear"), ("35cl", "Blue")]
    assert r[0]["Title"] == "Perception Wine" and r[1]["Title"] == ""
    assert {x["Handle"] for x in r} == {"perception-wine"}


def test_the_line_mark_keeps_lined_and_unlined_twins_apart():
    r = rows(product(glass("A", "35cl", 350), glass("B", "35cl", 350, qualifier="(CE@175ml)"), axes=("capacity",)))
    assert [x["Option1 Value"] for x in r] == ["35cl", "35cl (CE@175ml)"]


def test_a_single_variant_without_options_is_default_title():
    v = Variant(1, "A", "5012345678900", None, Decimal("4.50"))
    (r,) = rows(product(v, axes=()), mappings=())
    assert (r["Option1 Name"], r["Option1 Value"], r["Variant Barcode"], r["Variant Price"]) == \
        ("Title", "Default Title", "5012345678900", "4.50")


def test_capacity_goes_in_the_volume_metafield_as_a_number():
    (r,) = rows(product(glass("A", "1.18L", 1182.9), axes=("capacity",)))
    assert json.loads(r[HEADER]) == {"value": 1182.9, "unit": "MILLILITERS"}


def test_a_new_product_is_a_draft_and_stock_is_not_tracked():
    (r,) = rows(product(glass("A", "25cl", 250), axes=("capacity",)))
    assert (r["Status"], r["Published"], r["Variant Inventory Tracker"]) == ("draft", "FALSE", "")


def test_extra_images_get_rows_of_their_own():
    p = product(glass("A", "25cl", 250), axes=("capacity",), images=[("https://x/1.jpg", 0), ("https://x/2.jpg", 1)])
    r = rows(p)
    assert [(x["Variant SKU"], x["Image Src"], x["Image Position"]) for x in r] == \
        [("A", "https://x/1.jpg", "1"), ("", "https://x/2.jpg", "2")]


# ---------------------------------------------------------------- refusals

def test_two_variants_with_the_same_options_raise():
    with pytest.raises(ExportError, match="share the options"):
        rows(product(glass("A", "25cl", 250), glass("B", "25cl", 250), axes=("capacity",)))


def test_a_variant_missing_an_axis_its_siblings_have_raises():
    with pytest.raises(ExportError, match="no value on colour"):
        rows(product(glass("A", "25cl", 250, colour="Blue"), glass("B", "35cl", 350)))


def test_an_option_off_the_nodes_axes_raises():
    with pytest.raises(ExportError, match="not axes of its node"):
        rows(product(glass("A", "25cl", 250, colour="Blue"), axes=("capacity",)))


def test_an_unsupported_metafield_type_raises():
    with pytest.raises(ExportError, match="not supported"):
        metafield_value(Mapping("capacity", "custom", "volume", "json", "variant"), option("capacity", "1", 1, "ml"))


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a scratch database migrated to head.

@pytest.fixture
def conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    if engine.url.database == "mosferics":
        pytest.fail("TEST_DATABASE_URL points at the live database")
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(sa.text("INSERT INTO supplier.suppliers (name, code) VALUES ('Test', 'test_export')"))
        connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix, variant_axes) "
            "VALUES ('Wine', 'test-export', 'test-export', 'ZZE', '{capacity}')"))
        yield connection
        transaction.rollback()


def make(conn, handle, *capacities, approved=True, intent=True, image=True):
    """A product with one variant per capacity, each sourced, priced and (by default) ready."""
    product_id = conn.execute(sa.text(
        "INSERT INTO catalog.products (title, handle, category_id, base_unit) "
        "SELECT :h, :h, category_id, 'each' FROM catalog.taxonomy WHERE slug = 'test-export' RETURNING product_id"),
        {"h": handle}).scalar_one()
    for cl in capacities:
        sp = conn.execute(sa.text(
            "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw) "
            "SELECT supplier_id, :s, :s FROM supplier.suppliers WHERE code = 'test_export' RETURNING supplier_product_id"),
            {"s": f"{handle}-{cl}"}).scalar_one()
        v = conn.execute(sa.text(
            "INSERT INTO catalog.variants (product_id, mosferics_sku, pack_size, base_units_per_variant) "
            "VALUES (:p, catalog.next_mosferics_sku('ZZE'), 6, 6) RETURNING variant_id"), {"p": product_id}).scalar_one()
        conn.execute(sa.text("INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, status, "
                             "is_preferred) VALUES (:sp, :v, 'identical', 'confirmed', true)"), {"sp": sp, "v": v})
        conn.execute(sa.text("INSERT INTO catalog.variant_options (variant_id, axis, value, canonical_value, unit) "
                             "VALUES (:v, 'capacity', :l, :ml, 'ml')"), {"v": v, "l": f"{cl}cl", "ml": cl * 10})
        conn.execute(sa.text("INSERT INTO pricing.variant_prices (variant_id, channel_id, calculated_price, status, "
                             "approved_by) VALUES (:v, 1, 12.5, :s, :by)"),
                     {"v": v, "s": "approved" if approved else "pending_approval", "by": "test" if approved else None})
        if intent:
            conn.execute(sa.text("INSERT INTO ops.channel_publish (variant_id, channel_id, publish_status) "
                                 "VALUES (:v, 1, 'pending')"), {"v": v})
    if image:
        conn.execute(sa.text("INSERT INTO catalog.images (image_id, product_id, url, position, status) "
                             "VALUES ((SELECT coalesce(max(image_id), 0) + 1 FROM catalog.images), :p, :u, 0, 'verified')"),
                     {"p": product_id, "u": f"https://img/{handle}.jpg"})
    return product_id


def export_rows(conn, tmp_path):
    out, report = tmp_path / "products.csv", tmp_path / "held.csv"
    result = run(conn, SETTINGS, out, report)
    with out.open(newline="", encoding="utf-8") as f:
        exported = list(csv.DictReader(f))
    with report.open(newline="", encoding="utf-8") as f:
        held = {r["handle"]: r for r in csv.DictReader(f)}
    return result, exported, held


def test_a_ready_product_is_exported_with_its_price_and_volume(conn, tmp_path):
    make(conn, "ready", 25, 35)
    result, exported, _ = export_rows(conn, tmp_path)
    mine = [r for r in exported if r["Handle"] == "ready"]
    assert [(r["Option1 Value"], r["Variant Price"], json.loads(r[HEADER])["value"]) for r in mine] == \
        [("25cl", "12.50", 250.0), ("35cl", "12.50", 350.0)]
    assert mine[0]["Image Src"] == "https://img/ready.jpg"


def test_a_product_half_ready_is_held_whole(conn, tmp_path):
    product_id = make(conn, "half", 25, 35)
    conn.execute(sa.text("UPDATE pricing.variant_prices SET status = 'pending_approval' WHERE variant_id = "
                         "(SELECT min(variant_id) FROM catalog.variants WHERE product_id = :p)"), {"p": product_id})
    _, exported, held = export_rows(conn, tmp_path)
    assert not [r for r in exported if r["Handle"] == "half"]
    assert held["half"]["outcome"] == "held" and "1 of 2 variants not ready" in held["half"]["reason"]


def test_without_a_verified_image_nothing_is_ready(conn, tmp_path):
    make(conn, "no-image", 25, image=False)
    _, exported, held = export_rows(conn, tmp_path)
    assert not [r for r in exported if r["Handle"] == "no-image"] and "no-image" not in held


def test_the_export_writes_nothing_to_the_database(conn, tmp_path):
    make(conn, "ready", 25)
    before = conn.execute(sa.text("SELECT publish_status FROM ops.channel_publish")).scalars().all()
    export_rows(conn, tmp_path)
    assert conn.execute(sa.text("SELECT publish_status FROM ops.channel_publish")).scalars().all() == before


# ------------------------------------------------- 0017: signing decisions

def test_an_approval_with_no_name_is_signed_and_a_named_one_kept(conn):
    make(conn, "sign", 25, 35, approved=False)
    ids = conn.execute(sa.text("SELECT variant_id FROM catalog.variants v JOIN catalog.products p USING (product_id) "
                               "WHERE p.handle = 'sign' ORDER BY 1")).scalars().all()
    conn.execute(sa.text("UPDATE pricing.variant_prices SET status = 'approved' WHERE variant_id = :v"), {"v": ids[0]})
    conn.execute(sa.text("UPDATE pricing.variant_prices SET status = 'rejected', approved_by = 'reviewer' "
                         "WHERE variant_id = :v"), {"v": ids[1]})
    signers = conn.execute(sa.text("SELECT approved_by FROM pricing.variant_prices WHERE variant_id = ANY(:v) "
                                   "ORDER BY variant_id"), {"v": ids}).scalars().all()
    assert signers == [conn.execute(sa.text("SELECT current_user")).scalar(), "reviewer"]


def test_back_to_pending_clears_the_signer_and_an_edit_keeps_it(conn):
    make(conn, "sign", 25)
    v = conn.execute(sa.text("SELECT variant_id FROM catalog.variants v JOIN catalog.products p USING (product_id) "
                             "WHERE p.handle = 'sign'")).scalar_one()
    conn.execute(sa.text("UPDATE pricing.variant_prices SET override_price = 14 WHERE variant_id = :v"), {"v": v})
    assert conn.execute(sa.text("SELECT approved_by FROM pricing.variant_prices WHERE variant_id = :v"),
                        {"v": v}).scalar() == "test"
    conn.execute(sa.text("UPDATE pricing.variant_prices SET status = 'pending_approval' WHERE variant_id = :v"), {"v": v})
    assert conn.execute(sa.text("SELECT approved_by FROM pricing.variant_prices WHERE variant_id = :v"),
                        {"v": v}).scalar() is None
