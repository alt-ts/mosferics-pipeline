import copy
import os

import pytest
import sqlalchemy as sa

from pipeline.config import load_supplier_config
from pipeline.images import EXACT, LOOSE, ImageError, Matcher, Target, list_keys, run

DEMO = load_supplier_config("demo")
MATCHER = Matcher(DEMO)
TARGETS = {sku.casefold(): Target(sku, n, 100 + n, f"MOS-TST-{n:07d}-0")
           for n, sku in enumerate(["14-00-017", "14-00-018", "AB-1"], start=1)}


def outcome(key):
    match, kind, detail = MATCHER.match(key, TARGETS)
    return kind, (match.target.supplier_sku, match.number) if match else detail


# ------------------------------------------------------------------ matching

@pytest.mark.parametrize("key, expected", [
    ("14-00-017.jpg", (EXACT, ("14-00-017", 0))),
    ("a/b/14-00-017_1.jpg", (EXACT, ("14-00-017", 1))),        # the folder is not part of the name
    ("14-00-017_12.PNG", (EXACT, ("14-00-017", 12))),         # extensions and case are folded
    ("ab-1_2.webp", (EXACT, ("AB-1", 2))),
    ("14-00-017 CE.png", (LOOSE, ("14-00-017", 0))),
    ("14-00-017-angle.jpg", (LOOSE, ("14-00-017", 0))),
])
def test_files_named_by_sku_match(key, expected):
    assert outcome(key) == expected


@pytest.mark.parametrize("key", [
    "14-00-0170.jpg",    # the SKU then a digit is another SKU, not this one
    "14-00-01.jpg",      # the old rule 3: a SKU starting with the name would claim both 14-00-017 and 018
    "14.jpg",
    "99-99-999_1.jpg",
])
def test_a_name_that_is_not_one_sku_is_an_orphan(key):
    assert outcome(key)[0] == "orphan"


def test_a_file_type_the_config_does_not_allow_is_skipped():
    assert outcome("14-00-017_1.tif")[0] == "skipped"


def test_a_name_two_skus_could_claim_is_ambiguous_not_guessed():
    targets = {**TARGETS, "14-00-017 ce": Target("14-00-017 CE", 9, 109, "MOS-TST-0000009-0")}
    match, kind, detail = MATCHER.match("14-00-017 CE front.jpg", targets)
    assert (match, kind) == (None, "ambiguous") and detail == "14-00-017, 14-00-017 CE"


def test_the_config_is_checked():
    config = copy.deepcopy(DEMO)
    config["images"]["base_url"] = "https://images.example.com"
    with pytest.raises(ImageError, match="must end with /"):
        Matcher(config)
    config = copy.deepcopy(DEMO)
    del config["images"]
    with pytest.raises(ImageError, match="no images section"):
        Matcher(config)


def test_the_demo_manifest_lists_keys_and_skips_comments():
    keys = list_keys(MATCHER)
    assert "demo/90-10-001_1.jpg" in keys and not any(k.startswith("#") for k in keys)


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a scratch database migrated to head.

CODE = "test_images"


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
        connection.execute(sa.text("INSERT INTO supplier.suppliers (name, code) VALUES ('Test', :c)"), {"c": CODE})
        connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) VALUES ('T', 'test-img', 'test-img', 'ZZI')"))
        yield connection
        transaction.rollback()


def config():
    c = copy.deepcopy(DEMO)
    c["supplier"]["code"] = CODE
    return c


def product(conn, *skus, link_status="confirmed"):
    """One product with a variant per supplier SKU."""
    product_id = conn.execute(sa.text(
        "INSERT INTO catalog.products (title, handle, category_id, base_unit) "
        "SELECT :h, :h, category_id, 'each' FROM catalog.taxonomy WHERE slug = 'test-img' RETURNING product_id"),
        {"h": f"p-{skus[0]}"}).scalar_one()
    for sku in skus:
        sp = conn.execute(sa.text(
            "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw) "
            "SELECT supplier_id, :s, :s FROM supplier.suppliers WHERE code = :c RETURNING supplier_product_id"),
            {"s": sku, "c": CODE}).scalar_one()
        v = conn.execute(sa.text(
            "INSERT INTO catalog.variants (product_id, mosferics_sku, pack_size, base_units_per_variant) "
            "VALUES (:p, catalog.next_mosferics_sku('ZZI'), 6, 6) RETURNING variant_id"), {"p": product_id}).scalar_one()
        conn.execute(sa.text("INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, status) "
                             "VALUES (:sp, :v, 'identical', :s)"), {"sp": sp, "v": v, "s": link_status})
    return product_id


def images(conn, product_id):
    return conn.execute(sa.text(
        "SELECT i.s3_key, i.position, i.status, i.source, sp.supplier_sku FROM catalog.images i "
        "JOIN link.variant_links l ON l.variant_id = i.variant_id "
        "JOIN supplier.supplier_products sp USING (supplier_product_id) "
        "WHERE i.product_id = :p ORDER BY i.position"), {"p": product_id}).all()


def test_images_land_on_their_variants_in_gallery_order(conn):
    p = product(conn, "90-00-001", "90-00-002")
    keys = ["x/90-00-002_1.jpg", "x/90-00-001_2.jpg", "x/90-00-001_1.jpg", "x/90-00-001 detail.jpg"]
    result = run(conn, config(), dry_run=False, keys=keys)
    assert result["written"]["images"] == 4
    assert [(r.s3_key, r.position, r.status, r.supplier_sku) for r in images(conn, p)] == [
        ("x/90-00-001_1.jpg", 0, "verified", "90-00-001"),
        ("x/90-00-001_2.jpg", 1, "verified", "90-00-001"),
        ("x/90-00-001 detail.jpg", 2, None, "90-00-001"),      # loose: waits for a human
        ("x/90-00-002_1.jpg", 3, "verified", "90-00-002"),
    ]
    url = conn.execute(sa.text("SELECT url FROM catalog.images WHERE s3_key = 'x/90-00-001_1.jpg'")).scalar_one()
    assert url == "https://images.example.com/x/90-00-001_1.jpg"


def test_a_rerun_adds_only_new_files_and_leaves_a_verdict_alone(conn):
    p = product(conn, "90-00-001")
    run(conn, config(), dry_run=False, keys=["90-00-001 a.jpg"])
    conn.execute(sa.text("UPDATE catalog.images SET status = 'rejected' WHERE s3_key = '90-00-001 a.jpg'"))
    again = run(conn, config(), dry_run=False, keys=["90-00-001 a.jpg", "90-00-001_1.jpg"])
    assert again["plan"]["files_already_stored"] == 1 and again["written"]["images"] == 1
    assert [(r.s3_key, r.position, r.status) for r in images(conn, p)] == [
        ("90-00-001 a.jpg", 0, "rejected"), ("90-00-001_1.jpg", 1, "verified")]


def test_a_dry_run_writes_nothing_and_reports_variants_without_a_picture(conn):
    p = product(conn, "90-00-001", "90-00-002")
    result = run(conn, config(), dry_run=True, keys=["90-00-001_1.jpg"])
    assert result["plan"]["variants_without_image"] == 1 and images(conn, p) == []


def test_a_rejected_link_is_not_a_target(conn):
    product(conn, "90-00-001", link_status="rejected")
    assert run(conn, config(), dry_run=True, keys=["90-00-001_1.jpg"])["plan"]["files_orphan"] == 1


def test_an_image_cannot_name_a_variant_of_another_product(conn):
    a, b = product(conn, "90-00-001"), product(conn, "90-00-002")
    variant_of_b = conn.execute(sa.text("SELECT variant_id FROM catalog.variants WHERE product_id = :p"),
                                {"p": b}).scalar_one()
    with pytest.raises(sa.exc.IntegrityError, match="fk_images_variant_of_product"):
        conn.execute(sa.text("INSERT INTO catalog.images (product_id, variant_id, s3_key) VALUES (:a, :v, 'k')"),
                     {"a": a, "v": variant_of_b})


def test_an_unknown_status_is_refused(conn):
    a = product(conn, "90-00-001")
    with pytest.raises(sa.exc.IntegrityError, match="ck_images_status"):
        conn.execute(sa.text("INSERT INTO catalog.images (product_id, s3_key, status) VALUES (:a, 'k', 'approved')"),
                     {"a": a})
