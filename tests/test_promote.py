import copy
import json
import os

import pytest
import sqlalchemy as sa

from pipeline.config import load_supplier_config
from pipeline.promote import claims_of, run

DEMO = load_supplier_config("demo")
CODE = "test_promote"


def test_claims_are_what_the_extract_marked_for_review():
    specs = {
        "safety": {"dishwasher_safe": "pending_review", "microwave_safe": "pending_review"},
        "material": {"raw": "Porcelain", "status": "pending_review", "canonical": ["porcelain"]},
        "finish": {"raw": "Gloss", "status": None, "canonical": ["gloss"]},       # not a claim to review
        "colour": {"status": "proposed", "candidates": ["blue"]},                  # an option, not a claim
        "material_unknown": {"raw": "Bioresin", "status": "pending_review", "canonical": []},  # unmapped: nothing to claim
    }
    assert claims_of(specs) == {"dishwasher_safe": "true", "microwave_safe": "true", "material": "porcelain"}


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
        connection.execute(sa.text("INSERT INTO supplier.suppliers (name, code) VALUES ('T', :c)"), {"c": CODE})
        connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) VALUES ('T', 'test-pro', 'test-pro', 'ZZR')"))
        yield connection
        transaction.rollback()


def config():
    c = copy.deepcopy(DEMO)
    c["supplier"]["code"] = CODE
    return c


def safety(*flags):
    return {f: "pending_review" for f in flags}


def material(*names):
    return {"raw": "x", "status": "pending_review", "canonical": list(names)}


def product(conn, handle, *members):
    """members: (supplier_sku, specs) per variant."""
    pid = conn.execute(sa.text(
        "INSERT INTO catalog.products (title, handle, category_id, base_unit) "
        "SELECT :h, :h, category_id, 'each' FROM catalog.taxonomy WHERE slug = 'test-pro' RETURNING product_id"),
        {"h": handle}).scalar_one()
    for sku, specs in members:
        sp = conn.execute(sa.text(
            "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw, specs) "
            "SELECT supplier_id, :s, :s, CAST(:j AS jsonb) FROM supplier.suppliers WHERE code = :c "
            "RETURNING supplier_product_id"), {"s": sku, "j": json.dumps(specs), "c": CODE}).scalar_one()
        v = conn.execute(sa.text(
            "INSERT INTO catalog.variants (product_id, mosferics_sku, pack_size, base_units_per_variant) "
            "VALUES (:p, catalog.next_mosferics_sku('ZZR'), 6, 6) RETURNING variant_id"), {"p": pid}).scalar_one()
        conn.execute(sa.text("INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, status, "
                             "is_preferred) VALUES (:sp, :v, 'identical', 'confirmed', true)"), {"sp": sp, "v": v})
    return pid


def metadata(conn, pid):
    return {r.key: (r.value, r.status, r.source) for r in conn.execute(sa.text(
        "SELECT key, value, status, source FROM catalog.product_metadata WHERE product_id = :p"), {"p": pid})}


def eans(conn, pid):
    return conn.execute(sa.text("SELECT ean FROM catalog.variants WHERE product_id = :p ORDER BY variant_id"),
                        {"p": pid}).scalars().all()


def test_a_valid_barcode_reaches_the_variant_and_an_invalid_one_does_not(conn):
    pid = product(conn, "jug",
                  ("90-00-001", {"supplier_barcode": "5012345678900", "supplier_barcode_valid": True}),
                  ("90-00-002", {"supplier_barcode": "5012345678901", "supplier_barcode_valid": False}),
                  ("90-00-003", {}))
    result = run(conn, config(), dry_run=False)
    assert eans(conn, pid) == ["5012345678900", None, None]
    assert result["plan"]["barcode_invalid_not_copied"] == 1 and result["written"]["barcodes"] == 1


def test_a_barcode_already_on_the_variant_is_never_replaced(conn):
    pid = product(conn, "jug", ("90-00-001", {"supplier_barcode": "5012345678900", "supplier_barcode_valid": True}))
    conn.execute(sa.text("UPDATE catalog.variants SET ean = '4006381333931' WHERE product_id = :p"), {"p": pid})
    run(conn, config(), dry_run=False)
    assert eans(conn, pid) == ["4006381333931"]


def test_a_claim_every_variant_makes_becomes_product_metadata_pending_review(conn):
    pid = product(conn, "plate",
                  ("90-00-001", {"safety": safety("dishwasher_safe", "microwave_safe"), "material": material("porcelain")}),
                  ("90-00-002", {"safety": safety("dishwasher_safe", "microwave_safe"), "material": material("porcelain")}))
    result = run(conn, config(), dry_run=False)
    assert metadata(conn, pid) == {
        "dishwasher_safe": ("true", "pending_review", "supplier"),
        "microwave_safe": ("true", "pending_review", "supplier"),
        "material": ("porcelain", "pending_review", "supplier"),
    }
    assert result["written"]["claims"] == 3 and result["written"]["review_items"] == 0


def test_a_claim_some_variants_make_and_others_do_not_is_a_conflict_not_a_row(conn):
    pid = product(conn, "bowl",
                  ("90-00-001", {"safety": safety("dishwasher_safe", "microwave_safe")}),
                  ("90-00-002", {"safety": safety("dishwasher_safe")}))     # silent on microwave: not stated is not "no"
    result = run(conn, config(), dry_run=False)
    assert set(metadata(conn, pid)) == {"dishwasher_safe"}
    notes = conn.execute(sa.text("SELECT notes FROM ops.review_queue WHERE entity_type = 'product_metadata' "
                                 "AND entity_id = :p AND issue_type = 'metadata_conflict'"), {"p": pid}).scalar_one()
    assert "microwave_safe" in notes and "not stated" in notes
    assert run(conn, config(), dry_run=False)["written"]["review_items"] == 0     # not raised twice while open
    assert result["written"]["review_items"] == 1


def test_different_materials_are_a_conflict(conn):
    pid = product(conn, "pitcher", ("90-00-001", {"material": material("steel")}),
                  ("90-00-002", {"material": material("polycarbonate")}))
    run(conn, config(), dry_run=False)
    assert metadata(conn, pid) == {}


def test_a_verdict_stands_and_a_rerun_writes_nothing(conn):
    pid = product(conn, "plate", ("90-00-001", {"safety": safety("oven_safe")}))
    run(conn, config(), dry_run=False)
    conn.execute(sa.text("UPDATE catalog.product_metadata SET status = 'rejected' WHERE product_id = :p"), {"p": pid})
    again = run(conn, config(), dry_run=False)
    assert again["written"] == {"barcodes": 0, "claims": 0, "review_items": 0}
    assert metadata(conn, pid)["oven_safe"][1] == "rejected"


def test_a_dry_run_writes_nothing(conn):
    pid = product(conn, "plate", ("90-00-001", {"safety": safety("oven_safe"),
                                                "supplier_barcode": "5012345678900", "supplier_barcode_valid": True}))
    run(conn, config(), dry_run=True)
    assert metadata(conn, pid) == {} and eans(conn, pid) == [None]


# --------------------------------------------- 0020: verdicts sign themselves

def signature(conn, pid):
    return conn.execute(sa.text("SELECT status, verified_by, verified_at IS NOT NULL AS dated FROM "
                                "catalog.product_metadata WHERE product_id = :p"), {"p": pid}).one()


def test_verifying_signs_the_row_and_going_back_to_pending_clears_it(conn):
    pid = product(conn, "plate", ("90-00-001", {"safety": safety("oven_safe")}))
    run(conn, config(), dry_run=False)
    me = conn.execute(sa.text("SELECT current_user")).scalar()
    conn.execute(sa.text("UPDATE catalog.product_metadata SET status = 'verified' WHERE product_id = :p"), {"p": pid})
    assert tuple(signature(conn, pid)) == ("verified", me, True)
    conn.execute(sa.text("UPDATE catalog.product_metadata SET status = 'pending_review' WHERE product_id = :p"), {"p": pid})
    assert tuple(signature(conn, pid)) == ("pending_review", None, False)
    conn.execute(sa.text("UPDATE catalog.product_metadata SET status = 'rejected', verified_by = 'reviewer' "
                         "WHERE product_id = :p"), {"p": pid})
    assert tuple(signature(conn, pid)) == ("rejected", "reviewer", True)      # a named reviewer is kept


def test_unreviewed_claims_hold_a_product_back_from_publishing(conn):
    pid = product(conn, "plate", ("90-00-001", {"safety": safety("oven_safe")}))
    v = conn.execute(sa.text("SELECT variant_id FROM catalog.variants WHERE product_id = :p"), {"p": pid}).scalar_one()
    conn.execute(sa.text("INSERT INTO pricing.variant_prices (variant_id, channel_id, calculated_price, status, "
                         "approved_by) VALUES (:v, 1, 10, 'approved', 'test')"), {"v": v})
    conn.execute(sa.text("INSERT INTO catalog.images (product_id, s3_key, url, status) VALUES (:p, 'k', 'u', 'verified')"),
                 {"p": pid})
    conn.execute(sa.text("INSERT INTO ops.channel_publish (variant_id, channel_id, publish_status) VALUES (:v, 1, 'pending')"),
                 {"v": v})
    ready = lambda: conn.execute(sa.text("SELECT count(*) FROM review.ready_to_publish WHERE variant_id = :v"),
                                 {"v": v}).scalar_one()
    assert ready() == 1                       # nothing claimed yet
    run(conn, config(), dry_run=False)
    assert ready() == 0                       # an unreviewed safety claim now holds it back
    conn.execute(sa.text("UPDATE catalog.product_metadata SET status = 'verified' WHERE product_id = :p"), {"p": pid})
    assert ready() == 1
