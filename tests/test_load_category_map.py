import csv
import os

import pytest
import sqlalchemy as sa

from pipeline.load_category_map import COLUMNS, apply, parse_decisions, plan, read_decisions


def row(raw="Drinkware > Stemware > Flute", decision="map", override="",
        path="test-dept/flutes", category_id="1"):
    return {
        "supplier_category_raw": raw, "product_count": "3", "sample_names": "a | b",
        "suggested_category_id": category_id, "suggested_path": path, "confidence": "high",
        "note": "", "decision": decision, "override_path": override,
    }


def test_map_uses_the_suggestion_unless_overridden():
    plain, moved = parse_decisions([row(), row(raw="B", override="test-dept")])
    assert (plain.path, plain.overridden) == ("test-dept/flutes", False)
    assert (moved.path, moved.overridden) == ("test-dept", True)


def test_manual_has_no_path():
    (d,) = parse_decisions([row(decision="manual")])
    assert d.path is None


def test_every_bad_row_is_reported_at_once():
    with pytest.raises(ValueError, match="3 row") as e:
        parse_decisions([
            row(raw="A", decision=""),
            row(raw="B", decision="manual", override="test-dept"),
            row(raw="C", category_id="1.0"),          # what a spreadsheet does to an int
        ])
    assert "line 2 ('A')" in str(e.value) and "line 4 ('C')" in str(e.value)


def test_a_repeated_string_raises():
    with pytest.raises(ValueError, match="appears twice"):
        parse_decisions([row(), row()])


def test_a_changed_header_raises(tmp_path):
    path = tmp_path / "map.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS + ["my notes"])
        w.writeheader()
        w.writerow({**row(), "my notes": "x"})
    with pytest.raises(ValueError, match="columns are"):
        read_decisions(path)


def test_a_spreadsheet_bom_is_tolerated(tmp_path):
    path = tmp_path / "map.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerow(row())
    assert len(read_decisions(path)) == 1


# Needs TEST_DATABASE_URL pointing at a migrated scratch database. Everything
# runs in a transaction that is rolled back, so nothing is left behind.

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
        connection.execute(sa.text(
            "INSERT INTO supplier.suppliers (name, code) VALUES ('Test', 'test_catmap')"))
        dept = connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) "
            "VALUES ('Test Dept', 'test-dept', 'test-dept', 'ZZZ') RETURNING category_id")).scalar_one()
        for slug in ("flutes", "coupes"):
            connection.execute(sa.text(
                "INSERT INTO catalog.taxonomy (parent_id, name, slug, path, variant_axes) "
                "VALUES (:p, :slug, :slug, 'test-dept/' || :slug, '{}')"), {"p": dept, "slug": slug})
        for sku, raw in [("1", "Flute"), ("2", "Coupe"), ("3", "Mixed")]:
            connection.execute(sa.text(
                "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw, supplier_category_raw) "
                "SELECT supplier_id, :sku, 'x', :raw FROM supplier.suppliers WHERE code = 'test_catmap'"),
                {"sku": sku, "raw": raw})
        yield connection
        transaction.rollback()


def node(conn, path):
    return conn.execute(sa.text("SELECT category_id FROM catalog.taxonomy WHERE path = :p"),
                        {"p": path}).scalar_one()


def decisions(conn, *rows):
    return parse_decisions([
        row(raw=r[0], decision=r[1], override=r[2] if len(r) > 2 else "",
            path="test-dept/flutes", category_id=str(node(conn, "test-dept/flutes")))
        for r in rows
    ])


def mapped(conn):
    return dict(conn.execute(sa.text(
        "SELECT m.supplier_category_raw, t.path FROM link.category_map m "
        "JOIN catalog.taxonomy t USING (category_id) JOIN supplier.suppliers s USING (supplier_id) "
        "WHERE s.code = 'test_catmap'")).all())


def test_map_override_and_manual_land_as_reviewed(conn):
    p = plan(conn, "test_catmap", decisions(
        conn, ("Flute", "map"), ("Coupe", "map", "test-dept/coupes"), ("Mixed", "manual")))
    assert apply(conn, p, "Tom Storey") == 2
    assert mapped(conn) == {"Flute": "test-dept/flutes", "Coupe": "test-dept/coupes"}
    assert p.manual == ["Mixed"]
    signed = conn.execute(sa.text(
        "SELECT confirmed_by, confirmed_at FROM link.category_map m JOIN supplier.suppliers s "
        "USING (supplier_id) WHERE s.code = 'test_catmap'")).all()
    assert all(by == "Tom Storey" and at is not None for by, at in signed)


def test_a_rerun_inserts_nothing(conn):
    d = decisions(conn, ("Flute", "map"))
    apply(conn, plan(conn, "test_catmap", d), "Tom Storey")
    again = plan(conn, "test_catmap", d)
    assert (again.inserts, again.unchanged) == ([], ["Flute"])


def test_remapping_a_confirmed_string_raises(conn):
    apply(conn, plan(conn, "test_catmap", decisions(conn, ("Flute", "map"))), "Tom Storey")
    with pytest.raises(ValueError, match="already mapped"):
        plan(conn, "test_catmap", decisions(conn, ("Flute", "map", "test-dept/coupes")))


def test_marking_a_confirmed_string_manual_raises(conn):
    apply(conn, plan(conn, "test_catmap", decisions(conn, ("Flute", "map"))), "Tom Storey")
    with pytest.raises(ValueError, match="marked manual but already mapped"):
        plan(conn, "test_catmap", decisions(conn, ("Flute", "manual")))


def test_a_string_no_product_carries_raises(conn):
    # e.g. a spreadsheet trimmed the trailing "-" off "Tableware > - > -"
    with pytest.raises(ValueError, match="no test_catmap product"):
        plan(conn, "test_catmap", decisions(conn, ("Flute ", "map")))


def test_an_unknown_path_raises(conn):
    with pytest.raises(ValueError, match="not in catalog.taxonomy"):
        plan(conn, "test_catmap", decisions(conn, ("Flute", "map", "test-dept/flutez")))


def test_a_suggested_id_that_no_longer_matches_its_path_raises(conn):
    (d,) = parse_decisions([row(raw="Flute", path="test-dept/flutes",
                                category_id=str(node(conn, "test-dept/coupes")))])
    with pytest.raises(ValueError, match="does not match"):
        plan(conn, "test_catmap", [d])


def test_strings_missing_from_the_csv_are_reported_not_mapped(conn):
    p = plan(conn, "test_catmap", decisions(conn, ("Flute", "map")))
    assert p.not_in_csv == ["Coupe", "Mixed"]


def test_an_empty_reviewer_raises(conn):
    p = plan(conn, "test_catmap", decisions(conn, ("Flute", "map")))
    with pytest.raises(ValueError, match="confirmed_by is empty"):
        apply(conn, p, "  ")
