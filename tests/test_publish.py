import csv
import os

import pytest
import sqlalchemy as sa

from pipeline.publish import PublishError, channel_id, intend, mark_published, select_variants, withdraw

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
        dept = connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) "
            "VALUES ('Pub', 'test-pub', 'test-pub', 'ZZU') RETURNING category_id")).scalar_one()
        connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (parent_id, name, slug, path) "
            "VALUES (:d, 'Glasses', 'glasses', 'test-pub/glasses')"), {"d": dept})
        connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) "
            "VALUES ('Other', 'test-other', 'test-other', 'ZZV')"))
        yield connection
        transaction.rollback()


def product(conn, handle, variants=2, path="test-pub/glasses", ready=False):
    """A product with n variants; ready=True makes each one pass every other export gate."""
    pid = conn.execute(sa.text(
        "INSERT INTO catalog.products (title, handle, category_id, base_unit) "
        "SELECT :h, :h, category_id, 'each' FROM catalog.taxonomy WHERE path = :t RETURNING product_id"),
        {"h": handle, "t": path}).scalar_one()
    prefix = "ZZU" if path.startswith("test-pub") else "ZZV"
    for _ in range(variants):
        v = conn.execute(sa.text(
            "INSERT INTO catalog.variants (product_id, mosferics_sku, pack_size, base_units_per_variant) "
            "VALUES (:p, catalog.next_mosferics_sku(:x), 6, 6) RETURNING variant_id"),
            {"p": pid, "x": prefix}).scalar_one()
        if ready:
            sp = conn.execute(sa.text(
                "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw) "
                "SELECT supplier_id, :s, :s FROM supplier.suppliers LIMIT 1 RETURNING supplier_product_id"),
                {"s": f"pub-{v}"}).scalar_one()
            conn.execute(sa.text("INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, "
                                 "status, is_preferred) VALUES (:sp, :v, 'identical', 'confirmed', true)"),
                         {"sp": sp, "v": v})
            conn.execute(sa.text("INSERT INTO pricing.variant_prices (variant_id, channel_id, calculated_price, "
                                 "status, approved_by) VALUES (:v, 1, 10, 'approved', 'test')"), {"v": v})
    if ready:
        conn.execute(sa.text("INSERT INTO catalog.images (product_id, s3_key, url, status) "
                             "VALUES (:p, :k, :k, 'verified')"), {"p": pid, "k": f"img-{handle}"})
    return pid


def states(conn, handle):
    return conn.execute(sa.text("""
        SELECT cp.publish_status FROM catalog.variants v JOIN catalog.products p USING (product_id)
        LEFT JOIN ops.channel_publish cp ON cp.variant_id = v.variant_id AND cp.channel_id = 1
        WHERE p.handle = :h ORDER BY v.variant_id"""), {"h": handle}).scalars().all()


def chan(conn):
    return channel_id(conn, "trade_store")


def test_intent_covers_every_variant_of_a_product(conn):
    product(conn, "wine", variants=3)
    result = intend(conn, chan(conn), select_variants(conn, chan(conn), handles=["wine"]), None, apply=True)
    assert (result["marked_pending"], result["products"]) == (3, 1)
    assert states(conn, "wine") == ["pending"] * 3


def test_a_category_covers_everything_beneath_it_and_nothing_else(conn):
    product(conn, "wine")
    product(conn, "plate", path="test-other")
    intend(conn, chan(conn), select_variants(conn, chan(conn), category="test-pub"), None, apply=True)
    assert states(conn, "wine") == ["pending", "pending"] and states(conn, "plate") == [None, None]


def test_without_apply_nothing_is_written(conn):
    product(conn, "wine")
    assert intend(conn, chan(conn), select_variants(conn, chan(conn), handles=["wine"]), None, apply=False)["marked_pending"] == 2
    assert states(conn, "wine") == [None, None]


def test_intent_leaves_a_published_variant_alone(conn):
    product(conn, "wine")
    intend(conn, chan(conn), select_variants(conn, chan(conn), handles=["wine"]), None, apply=True)
    conn.execute(sa.text("UPDATE ops.channel_publish SET publish_status = 'published' WHERE variant_id = "
                         "(SELECT min(variant_id) FROM catalog.variants v JOIN catalog.products p USING (product_id) "
                         "WHERE p.handle = 'wine')"))
    result = intend(conn, chan(conn), select_variants(conn, chan(conn), handles=["wine"]), "dropship", apply=True)
    assert result["already_published"] == 1 and states(conn, "wine") == ["published", "pending"]


def test_withdrawing_moves_intent_back_but_never_a_live_product(conn):
    product(conn, "wine")
    rows = lambda: select_variants(conn, chan(conn), handles=["wine"])
    intend(conn, chan(conn), rows(), None, apply=True)
    assert withdraw(conn, chan(conn), rows(), apply=True)["withdrawn"] == 2
    assert states(conn, "wine") == ["not_published", "not_published"]
    conn.execute(sa.text("UPDATE ops.channel_publish SET publish_status = 'published'"))
    with pytest.raises(PublishError, match="published on this channel"):
        withdraw(conn, chan(conn), rows(), apply=True)


def test_a_bad_selection_raises(conn):
    with pytest.raises(PublishError, match="exactly one"):
        select_variants(conn, chan(conn))
    with pytest.raises(PublishError, match="no taxonomy node"):
        select_variants(conn, chan(conn), category="nowhere")
    with pytest.raises(PublishError, match="no active product"):
        select_variants(conn, chan(conn), handles=["nope"])


# ------------------------------------------------- 0019: intent is required

def ready(conn, handle):
    return conn.execute(sa.text("SELECT count(*) FROM review.ready_to_publish r JOIN catalog.variants v "
                                "USING (variant_id) JOIN catalog.products p USING (product_id) WHERE p.handle = :h"),
                        {"h": handle}).scalar_one()


def test_only_a_pending_variant_is_ready_to_publish(conn):
    conn.execute(sa.text("INSERT INTO supplier.suppliers (name, code) VALUES ('Pub', 'test_pub')"))
    product(conn, "wine", variants=1, ready=True)
    assert ready(conn, "wine") == 0                                   # no intent: not ready
    rows = lambda: select_variants(conn, chan(conn), handles=["wine"])
    intend(conn, chan(conn), rows(), None, apply=True)
    assert ready(conn, "wine") == 1
    withdraw(conn, chan(conn), rows(), apply=True)
    assert ready(conn, "wine") == 0                                   # not_published is not ready (0019)


# ------------------------------------------------------- marking published

def export_csv(tmp_path, pairs):
    path = tmp_path / "products.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Handle", "Title", "Variant SKU"])
        w.writerows([h, "", sku] for h, sku in pairs)
    return path


def skus(conn, handle):
    return conn.execute(sa.text("SELECT v.mosferics_sku FROM catalog.variants v JOIN catalog.products p "
                                "USING (product_id) WHERE p.handle = :h ORDER BY v.variant_id"), {"h": handle}).scalars().all()


def test_an_imported_export_marks_its_products_published(conn, tmp_path):
    product(conn, "wine")
    intend(conn, chan(conn), select_variants(conn, chan(conn), handles=["wine"]), None, apply=True)
    path = export_csv(tmp_path, [("wine", s) for s in skus(conn, "wine")] + [("wine", "")])  # an image-only row
    assert mark_published(conn, chan(conn), path, apply=True) == {"published": 2, "products": 1}
    assert states(conn, "wine") == ["published", "published"]


def test_a_file_that_does_not_match_what_is_pending_changes_nothing(conn, tmp_path):
    product(conn, "wine")
    product(conn, "jug", variants=1)
    intend(conn, chan(conn), select_variants(conn, chan(conn), handles=["wine"]), None, apply=True)
    wine, jug = skus(conn, "wine"), skus(conn, "jug")
    for pairs, message in [
        ([("wine", wine[0])], "leaves out variants"),
        ([("wine", s) for s in wine] + [("wine", jug[0])], "not a variant of that product"),
        ([("jug", jug[0])], "not marked, not pending"),
    ]:
        with pytest.raises(PublishError, match=message):
            mark_published(conn, chan(conn), export_csv(tmp_path, pairs), apply=True)
    assert states(conn, "wine") == ["pending", "pending"]
