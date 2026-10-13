import os

import pytest
import sqlalchemy as sa

from pipeline.config import load_supplier_config
from pipeline.ingest import FileSpec, file_specs, find_file, ingest, parse_file

SPEC = FileSpec("price", "Price_*.csv", "utf-8", ("SKU", "Pack Size", "Your Price"))

# Cells chosen so that any trimming, typing or null-conversion changes them.
PRICE_CSV = (
    "SKU,Pack Size,Your Price,Customer Reference\r\n"
    "04-21-301,6,1.50,\r\n"
    "00-00-007, 12 ,0.10,NA\r\n"
    '75-47-131,1,33.89,"Square, with lid"\r\n'
)
PRODUCT_CSV = "Part,Description\r\n04-21-301,Café Glass 35cl\r\n00-00-007,Plate\r\n"


def write(tmp_path, name, text, encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def test_rows_land_as_the_exact_strings_sent(tmp_path):
    parsed = parse_file(write(tmp_path, "Price_Aug.csv", PRICE_CSV), SPEC)
    assert parsed.columns == ("SKU", "Pack Size", "Your Price", "Customer Reference")
    assert len(parsed.rows) == 3
    assert parsed.rows[0] == {
        "SKU": "04-21-301", "Pack Size": "6", "Your Price": "1.50", "Customer Reference": ""}
    # leading zeros, padding, a trailing zero and the string "NA" all survive
    assert parsed.rows[1] == {
        "SKU": "00-00-007", "Pack Size": " 12 ", "Your Price": "0.10", "Customer Reference": "NA"}
    assert parsed.rows[2]["Customer Reference"] == "Square, with lid"


def test_hash_is_of_the_bytes_and_changes_with_them(tmp_path):
    a = parse_file(write(tmp_path, "Price_a.csv", PRICE_CSV), SPEC)
    b = parse_file(write(tmp_path, "Price_b.csv", PRICE_CSV), SPEC)
    c = parse_file(write(tmp_path, "Price_c.csv", PRICE_CSV.replace("1.50", "1.51")), SPEC)
    assert a.file_hash == b.file_hash != c.file_hash
    assert len(a.file_hash) == 64


def test_blank_lines_are_not_rows(tmp_path):
    parsed = parse_file(write(tmp_path, "Price_Aug.csv", PRICE_CSV + "\r\n\r\n"), SPEC)
    assert len(parsed.rows) == 3


@pytest.mark.parametrize("text, message", [
    ("SKU,Pack Size\r\n04-21-301,6\r\n", "required columns missing"),
    ("SKU,Pack Size,Your Price,SKU\r\n1,6,1.50,1\r\n", "repeated column headers"),
    ("SKU,Pack Size,Your Price,\r\n1,6,1.50,x\r\n", "header cell is blank"),
    ("SKU,Pack Size,Your Price\r\n1,6,1.50\r\n2,12\r\n", "line 3: 2 cells"),
    ("SKU,Pack Size,Your Price\r\n1,6,1.50,extra\r\n", "line 2: 4 cells"),
    ("SKU,Pack Size,Your Price\r\n", "no data rows"),
    ("", "file is empty"),
])
def test_a_structurally_broken_file_raises(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        parse_file(write(tmp_path, "Price_Aug.csv", text), SPEC)


def test_wrong_encoding_raises_rather_than_mangling(tmp_path):
    path = write(tmp_path, "Price_Aug.csv", "SKU,Pack Size,Your Price\r\nCafé,6,1.50\r\n", "latin-1")
    with pytest.raises(ValueError, match="not valid utf-8"):
        parse_file(path, SPEC)


def test_find_file_needs_exactly_one_match(tmp_path):
    with pytest.raises(FileNotFoundError, match="found nothing"):
        find_file(tmp_path, SPEC)
    august = write(tmp_path, "Price_Aug.csv", PRICE_CSV)
    assert find_file(tmp_path, SPEC) == august
    write(tmp_path, "Price_Sep.csv", PRICE_CSV)
    with pytest.raises(FileNotFoundError, match="Price_Aug.csv.*Price_Sep.csv"):
        find_file(tmp_path, SPEC)


def test_file_specs_rejects_what_ingest_cannot_read():
    good = {"pattern": "a*.csv", "format": "csv", "encoding": "utf-8", "required_columns": ["A"]}
    assert file_specs({"files": {"price": good}}) == [FileSpec("price", "a*.csv", "utf-8", ("A",))]
    with pytest.raises(ValueError, match="no `files:` section"):
        file_specs({})
    with pytest.raises(ValueError, match="not supported"):
        file_specs({"files": {"price": {**good, "format": "xlsx"}}})
    with pytest.raises(ValueError, match="required_columns"):
        file_specs({"files": {"price": {**good, "required_columns": []}}})


def test_artis_config_declares_both_files():
    specs = {s.role: s for s in file_specs(load_supplier_config("artis"))}
    assert set(specs) == {"price", "product"}
    assert "Your Pack Price" in specs["price"].required_columns
    assert "Part" in specs["product"].required_columns


def test_config_must_match_the_code_asked_for(tmp_path):
    (tmp_path / "bbp.yaml").write_text("supplier:\n  code: artis\n", encoding="utf-8")
    with pytest.raises(ValueError, match="supplier.code is 'artis', expected 'bbp'"):
        load_supplier_config("bbp", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_supplier_config("nobody", tmp_path)


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a migrated scratch database. Everything
# runs in a transaction that is rolled back, so nothing is left behind.

CONFIG = {
    "supplier": {"code": "test_ingest", "name": "Test", "currency": "GBP",
                 "availability_source": "unknown"},
    "files": {
        "price": {"pattern": "Price_*.csv", "format": "csv", "encoding": "utf-8",
                  "required_columns": ["SKU", "Pack Size", "Your Price"]},
        "product": {"pattern": "Product_*.csv", "format": "csv", "encoding": "utf-8",
                    "required_columns": ["Part"]},
    },
}


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
        yield connection
        transaction.rollback()


def test_ingest_lands_both_files_and_is_idempotent(conn, tmp_path):
    write(tmp_path, "Price_Aug.csv", PRICE_CSV)
    write(tmp_path, "Product_Aug.csv", PRODUCT_CSV)

    first = {b.role: b for b in ingest(conn, CONFIG, tmp_path, "config/suppliers/test_ingest.yaml")}
    assert (first["price"].row_count, first["product"].row_count) == (3, 2)
    assert not first["price"].already_landed and not first["product"].already_landed
    assert first["price"].batch_id != first["product"].batch_id

    supplier = conn.execute(sa.text(
        "SELECT supplier_id, config_path FROM supplier.suppliers WHERE code = 'test_ingest'")).one()
    assert supplier.config_path == "config/suppliers/test_ingest.yaml"
    batch = conn.execute(sa.text(
        "SELECT supplier_id, filename, row_count, status, length(file_hash) AS hash_len "
        "FROM raw.import_batches WHERE batch_id = :b"), {"b": first["price"].batch_id}).one()
    assert tuple(batch) == (supplier.supplier_id, "Price_Aug.csv", 3, "landed", 64)

    rows = conn.execute(sa.text(
        "SELECT row_number, payload FROM raw.import_rows WHERE batch_id = :b ORDER BY row_number"),
        {"b": first["price"].batch_id}).all()
    assert [r.row_number for r in rows] == [1, 2, 3]
    assert rows[1].payload == {
        "SKU": "00-00-007", "Pack Size": " 12 ", "Your Price": "0.10", "Customer Reference": "NA"}
    assert rows[0].payload["Customer Reference"] == ""  # blank is a string, never null
    accented = conn.execute(sa.text(
        "SELECT payload->>'Description' FROM raw.import_rows WHERE batch_id = :b AND row_number = 1"),
        {"b": first["product"].batch_id}).scalar_one()
    assert accented == "Café Glass 35cl"

    # the same two files again: no new batch, no new rows, no second supplier
    again = {b.role: b for b in ingest(conn, CONFIG, tmp_path, "config/suppliers/test_ingest.yaml")}
    assert again["price"].already_landed and again["product"].already_landed
    assert again["price"].batch_id == first["price"].batch_id
    counts = conn.execute(sa.text(
        "SELECT (SELECT count(*) FROM raw.import_batches WHERE supplier_id = :s), "
        "       (SELECT count(*) FROM raw.import_rows r JOIN raw.import_batches b USING (batch_id) "
        "         WHERE b.supplier_id = :s)"), {"s": supplier.supplier_id}).one()
    assert tuple(counts) == (2, 5)

    # a changed price file is a new batch; the unchanged product file is not
    write(tmp_path, "Price_Aug.csv", PRICE_CSV.replace("1.50", "1.55"))
    third = {b.role: b for b in ingest(conn, CONFIG, tmp_path, "config/suppliers/test_ingest.yaml")}
    assert not third["price"].already_landed and third["price"].batch_id != first["price"].batch_id
    assert third["product"].already_landed


def test_a_bad_second_file_stops_the_first_from_landing(conn, tmp_path):
    write(tmp_path, "Price_Aug.csv", PRICE_CSV)
    write(tmp_path, "Product_Aug.csv", "Description\r\nPlate\r\n")  # no Part column
    with pytest.raises(ValueError, match="required columns missing"):
        ingest(conn, CONFIG, tmp_path, "config/suppliers/test_ingest.yaml")
    assert conn.execute(sa.text(
        "SELECT count(*) FROM supplier.suppliers WHERE code = 'test_ingest'")).scalar_one() == 0
