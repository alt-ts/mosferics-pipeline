import copy
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa

from pipeline.config import load_supplier_config
from pipeline.extract import (
    ExtractError,
    LineMarkSegment,
    Stager,
    column_references,
    extract,
    stage_rows,
    summarise,
    valid_gtin,
)
from pipeline.ingest import ingest

ARTIS = load_supplier_config("artis")


def blank_row(role, config=ARTIS):
    """Every column the config reads, blank, as a landed row would have them."""
    roles = set(config["files"])
    columns = set(config["files"][role]["required_columns"]) | {config["files"][role]["key"]}
    for section in ("product", "offer", "capacity", "size", "vocabulary", "safety", "specs", "barcode"):
        columns |= {c for r, c in column_references(config.get(section), roles) if r == role}
    return dict.fromkeys(columns, "")


def price_row(sku="04-21-301", **cells):
    row = blank_row("price")
    # A pack of 24 at 1.50 a piece: unit and pack price cannot be confused.
    row.update({"SKU": sku, "Product": "Price File Name", "Brand": "Price Brand",
                "Pack Size": "24", "List Price": "2.50", "Your Price": "1.50", "Your Pack Price": "36.00"})
    row.update(cells)
    return row


def product_row(sku="04-21-301", **cells):
    row = blank_row("product")
    row.update({"Part": sku, "Description": "Annie Hi-Ball 35cl [12.25oz]", "Brand": "Product Brand"})
    row.update(cells)
    return row


def stage_one(price=None, product=None):
    rows = stage_rows(ARTIS, {"price": [price or price_row()], "product": [product or product_row()]})
    assert len(rows) == 1
    return rows[0]


def issue_codes(row):
    return [i.code for i in row.issues]


# --------------------------------------------------------------------- mapping

def test_the_product_file_is_the_master_for_name_and_brand():
    row = stage_one()
    assert (row.supplier_sku, row.name_raw, row.brand) == (
        "04-21-301", "Annie Hi-Ball 35cl [12.25oz]", "Product Brand")


def test_name_raw_is_verbatim_and_name_clean_collapses_whitespace():
    row = stage_one(product=product_row(Description="Annie  Hi-Ball "))
    assert row.name_raw == "Annie  Hi-Ball "
    assert row.name_clean == "Annie Hi-Ball"


def test_category_key_joins_three_columns_and_marks_a_blank():
    row = stage_one(product=product_row(Category="Glassware", Style="Hi-Ball"))
    assert row.supplier_category_raw == "Glassware > - > Hi-Ball"


# ------------------------------------------------------------------------ join

def test_a_priced_sku_with_no_product_row_loads_without_a_category():
    rows = stage_rows(ARTIS, {"price": [price_row("04-21-301"), price_row("75-47-131")],
                              "product": [product_row("04-21-301")]})
    orphan = rows[1]
    assert orphan.supplier_sku == "75-47-131"
    assert orphan.name_raw == "Price File Name"  # falls back to the price file
    assert orphan.brand == "Price Brand"
    assert orphan.supplier_category_raw is None  # not "- > - > -", which would look mapped
    assert orphan.offer.cost_ex_vat == Decimal("36.00")
    assert orphan.issues == ()


def test_a_product_row_with_no_price_has_no_offer_and_goes_to_review():
    rows = stage_rows(ARTIS, {"price": [price_row("04-21-301")],
                              "product": [product_row("04-21-301"), product_row("11-11-111")]})
    orphan = rows[1]
    assert orphan.supplier_sku == "11-11-111"
    assert orphan.offer is None
    assert issue_codes(orphan) == ["no_price_row"]


def test_a_duplicate_key_raises():
    with pytest.raises(ExtractError, match="'04-21-301' appears twice"):
        stage_rows(ARTIS, {"price": [price_row(), price_row()], "product": [product_row()]})


def test_a_malformed_key_raises():
    with pytest.raises(ExtractError, match="'4-21-301' does not match key_format"):
        stage_rows(ARTIS, {"price": [price_row("4-21-301")], "product": [product_row()]})


# ----------------------------------------------------------------------- offer

def test_the_offer_cost_is_the_pack_not_the_piece():
    offer = stage_one().offer
    assert offer.cost_ex_vat == Decimal("36.00")
    assert offer.base_units_per_pack == Decimal("24")
    assert offer.cost_ex_vat / offer.base_units_per_pack == Decimal("1.50")


def test_a_pack_price_that_does_not_multiply_up_raises():
    # 1.50 x 24 is 36.00, so a pack price of 1.50 would be a piece price mislabelled
    with pytest.raises(ExtractError, match="pack_price_is_unit_price_times_pack"):
        stage_one(price=price_row(**{"Your Pack Price": "1.50"}))


def test_a_penny_of_rounding_is_within_tolerance_and_two_pence_is_not():
    assert stage_one(price=price_row(**{"Your Pack Price": "36.01"})).offer.cost_ex_vat == Decimal("36.01")
    with pytest.raises(ExtractError, match="out by 0.02"):
        stage_one(price=price_row(**{"Your Pack Price": "36.02"}))


@pytest.mark.parametrize("column, value, message", [
    ("Pack Size", "", "not a number"),
    ("Pack Size", "0", "must be above zero"),
    ("Your Pack Price", "POA", "not a number"),
    ("Your Pack Price", "NaN", "not a number"),
])
def test_an_unusable_cost_or_pack_raises(column, value, message):
    with pytest.raises(ExtractError, match=message):
        stage_one(price=price_row(**{column: value}))


def test_faults_are_collected_across_rows_before_raising():
    prices = [price_row("04-21-301", **{"Your Pack Price": "POA"}), price_row("75-47-131", **{"Your Pack Price": "TBC"})]
    with pytest.raises(ExtractError, match="2 faults: 04-21-301.*75-47-131"):
        stage_rows(ARTIS, {"price": prices, "product": [product_row()]})


def test_a_pack_size_disagreement_goes_to_review_and_the_price_file_wins():
    row = stage_one(product=product_row(**{"Artis Sales Box Qty (ea)": "12"}))
    assert row.offer.base_units_per_pack == Decimal("24")
    assert issue_codes(row) == ["pack_size_agrees_with_product_file"]


def test_a_blank_sales_box_quantity_is_not_a_disagreement():
    assert stage_one().issues == ()


def test_the_same_pack_size_written_differently_is_not_a_disagreement():
    assert stage_one(product=product_row(**{"Artis Sales Box Qty (ea)": "24.0"})).issues == ()


# -------------------------------------------------------------------- capacity

def test_brim_stays_in_the_suppliers_unit():
    row = stage_one(product=product_row(**{"Piece Fluid Capacity (cl)": "35.5",
                                           "Piece Fluid Capacity (ml)": "355"}))
    assert (row.brim.raw, row.brim.value, row.brim.unit) == ("35.5", Decimal("35.5"), "cl")
    assert (row.brim_cross_check.value, row.brim_cross_check.unit) == (Decimal("355"), "ml")


def test_a_zero_capacity_is_no_capacity():
    row = stage_one(product=product_row(**{"Piece Fluid Capacity (cl)": "0", "Piece Fluid Capacity (ml)": "0"}))
    assert row.brim is None and row.brim_cross_check is None


def test_a_non_numeric_capacity_raises():
    with pytest.raises(ExtractError, match="not a number"):
        stage_one(product=product_row(**{"Piece Fluid Capacity (cl)": "35cl"}))


@pytest.mark.parametrize("mark, expected", [
    ("(CE@175ml)", [LineMarkSegment("line", "CE", ("175",), "ml")]),
    ("(CE@125,175,250ml)", [LineMarkSegment("line", "CE", ("125", "175", "250"), "ml")]),
    ("(Lined@175, 250ml)", [LineMarkSegment("line", "Lined", ("175", "250"), "ml")]),
    ("(CE Brim@0.5pt)", [LineMarkSegment("brim", "CE Brim", ("0.5",), "imperial_pint")]),
    ("(LCE@1ltr)", [LineMarkSegment("line", "LCE", ("1",), "l")]),
    # thirds of a pint are written as rounded decimals; other pint values are left alone
    ("(CE@0.33,0.5,0.66pt)", [LineMarkSegment("line", "CE", ("1/3", "0.5", "2/3"), "imperial_pint")]),
    ("(CE@0.33pt+Nucl.)", [LineMarkSegment("line", "CE", ("1/3",), "imperial_pint")]),
    ("(CE@0.5pt,CE Brim@1pt)", [LineMarkSegment("line", "CE", ("0.5",), "imperial_pint"),
                                LineMarkSegment("brim", "CE Brim", ("1",), "imperial_pint")]),
])
def test_line_marks_parse(mark, expected):
    row = stage_one(product=product_row(Description=f"Nonic 57cl {mark}"))
    assert row.line_mark.raw == mark
    assert list(row.line_mark.segments) == expected
    assert row.issues == ()


@pytest.mark.parametrize("mark", [
    "(CE@125m)",                  # the three real typos in the August file
    "(Lined@0.33,0.5.0.66pt)",
    "(CE@0.33,0.5,CE Brim@1pt)",
    "(CE@175ml,)",                # consumed part of the way is not parsed
    "(CE@175ml junk)",
    "(LCE Brim@1pt)",             # the pattern allows this kind, the kinds map does not
])
def test_an_unparseable_line_mark_is_kept_raw_and_goes_to_review(mark):
    row = stage_one(product=product_row(Description=f"Nonic 57cl {mark}"))
    assert row.line_mark.raw == mark
    assert row.line_mark.segments == ()
    assert issue_codes(row) == ["line_mark_unparsed"]


def test_a_name_with_no_mark_has_no_line_mark():
    assert stage_one().line_mark is None


def test_ce_in_a_name_never_becomes_a_compliance_claim():
    row = stage_one(product=product_row(Description="Nonic (CE@175ml)"))
    assert row.safety == {}
    assert not any("compliance" in key or key == "ce" for key in row.specs)


@pytest.mark.parametrize("name, column, expected", [
    ("Nonic (CE@0.33pt+Nucl.)", "", True),
    ("Nonic", "Y", True),
    ("Nonic", "", False),
])
def test_nucleation_comes_from_the_name_or_the_column(name, column, expected):
    assert stage_one(product=product_row(Description=name, Nucleation=column)).nucleation is expected


# ------------------------------------------------------------------------ size

@pytest.mark.parametrize("name, raw, dimensions", [
    ('Plate 24cm [9.4"]', "24cm", ("24",)),
    ("Board 30x20cm", "30x20cm", ("30", "20")),
    ("Box 50x15x9cm", "50x15x9cm", ("50", "15", "9")),
    ("Bowl 8.2cm then 9cm", "8.2cm", ("8.2",)),
])
def test_size_takes_the_first_cm_measurement(name, raw, dimensions):
    size = stage_one(product=product_row(Description=name)).size
    assert size.raw == raw
    assert size.dimensions == tuple(Decimal(d) for d in dimensions)
    assert size.unit == "cm"


# ---------------------------------------------------------------- vocabularies

def test_material_stays_raw_and_lands_pending_review():
    row = stage_one(product=product_row(**{"Primary Material": "Soda Lime Glass"}))
    assert row.vocabulary["material"].raw == "Soda Lime Glass"
    assert row.vocabulary["material"].status == "pending_review"


def test_other_is_not_a_material():
    assert "material" not in stage_one(product=product_row(**{"Primary Material": "Other"})).vocabulary


def test_polywood_handle_moves_out_of_finish_and_into_specs():
    row = stage_one(product=product_row(Finish="Polywood Handle"))
    assert "finish" not in row.vocabulary
    assert row.specs["handle_material"] == "Polywood Handle"
    assert stage_one(product=product_row(Finish="Mirror")).vocabulary["finish"].raw == "Mirror"


@pytest.mark.parametrize("name, candidates", [
    ("Cooler Jug Blue 2.8ltr", ("blue",)),
    ("Cooler Jug blue", ("blue",)),                 # case-insensitive
    ("Bluebell Jug", ()),                           # whole words only
    ("2in1 Blue/Red Bowl", ("blue", "red")),        # a slash is a word boundary
    ("2in1 Brown/Black Bowl", ("brown_black",)),    # a pair is one colour, taken whole
    ("Nara Black&White Bowl 12cm", ("black_white",)),
    ("Plate Off White", ("white",)),                # the longer alias is the match
    ("Champagne Bucket", ()),                       # ignore_aliases
    ("Annie Red Wine 44cl", ()),                    # ignore_phrases
    ("Annie White Wine 32cl", ()),
    ("Ice Cream Cone", ()),
    ("Red White Wine Glass", ("red",)),             # only the phrase is removed
    ("Modern Rustic Natural Grey Plate 28.1cm", ("grey",)),  # a range name, not natural
    ("Modern Rustic Natural Wood Plate 28.1cm", ("natural",)),  # Wood still is
    ("Natural Stone Dip Dish 9cm", ()),
    ("Cake Stand 3-Tier Mirror Gold + Black Inserts", ("gold",)),  # a part, not the colour
    ("Acrylic Black Inserts (3 pcs)", ("black",)),  # but the spare inserts are black
])
def test_colour_is_found_in_the_name(name, candidates):
    assert stage_one(product=product_row(Description=name)).colour_candidates == candidates


def test_colour_is_only_ever_proposed():
    assert stage_one(product=product_row(Description="Jug Blue")).colour_status == "proposed"


def test_two_colours_in_one_name_go_to_review():
    row = stage_one(product=product_row(Description="2in1 Blue/Red Bowl"))
    assert issue_codes(row) == ["colour_ambiguous"]


# ---------------------------------------------------------------------- safety

def test_a_stated_safety_flag_is_pending_review_and_a_blank_writes_nothing():
    row = stage_one(product=product_row(**{"Dishwasher Safe": "Y"}))
    assert row.safety == {"dishwasher_safe": "pending_review"}  # microwave blank: absent, not False


def test_a_safety_value_that_is_neither_y_nor_blank_raises():
    with pytest.raises(ExtractError, match="neither 'Y' nor blank"):
        stage_one(product=product_row(**{"Oven Safe": "N"}))


def test_dishwasher_safe_and_handwash_only_together_go_to_review():
    row = stage_one(product=product_row(**{"Dishwasher Safe": "Y", "Handwash Only": "Y"}))
    assert issue_codes(row) == ["safety_contradiction"]


def test_the_config_cannot_auto_verify_safety_or_materials():
    config = copy.deepcopy(ARTIS)
    config["safety"]["status"] = "verified"
    with pytest.raises(ExtractError, match="safety.status must be pending_review"):
        Stager(config)
    config = copy.deepcopy(ARTIS)
    config["vocabulary"]["material"]["status"] = "verified"
    with pytest.raises(ExtractError, match="vocabulary.material.status must be pending_review"):
        Stager(config)


# ----------------------------------------------------------------------- specs

def test_specs_are_typed_and_zero_means_blank_only_where_the_config_says():
    row = stage_one(product=product_row(**{
        "Piece Weight (kg)": "0", "Piece Height (cm)": "11.5", "Stackable": "Y",
        "Range": "Annie", "Toughened": "", "Commodity Code": "0"}))
    assert "weight_kg" not in row.specs
    assert row.specs["height_cm"] == Decimal("11.5")
    assert row.specs["stackable"] is True
    assert row.specs["range"] == "Annie"
    assert "toughened" not in row.specs
    assert row.specs["commodity_code"] == "0"
    assert row.specs["list_price"] == Decimal("2.50")


def test_a_non_numeric_spec_raises():
    with pytest.raises(ExtractError, match="Piece Height"):
        stage_one(product=product_row(**{"Piece Height (cm)": "tall"}))


@pytest.mark.parametrize("code, valid", [
    ("4058019357574", True),
    ("4058019357575", False),  # wrong check digit
    ("058019357574", False),   # a lost leading zero leaves a wrong check digit
    ("96385074", True),        # GTIN-8
    ("12345", False),
    ("ABC", False),
])
def test_gtin_check_digit(code, valid):
    assert valid_gtin(code) is valid


def test_a_barcode_is_kept_raw_even_when_invalid_and_is_never_a_variant_ean():
    row = stage_one(product=product_row(**{"Artis Sales Barcode": "4058019357575"}))
    assert row.specs["supplier_barcode"] == "4058019357575"
    assert row.specs["supplier_barcode_valid"] is False


def test_the_config_cannot_make_the_barcode_a_match_key():
    config = copy.deepcopy(ARTIS)
    config["barcode"]["use_as_match_key"] = True
    with pytest.raises(ExtractError, match="use_as_match_key"):
        Stager(config)


# ---------------------------------------------------------------- broken config

def test_a_mapped_column_missing_from_the_data_raises_rather_than_loading_nulls():
    product = product_row()
    del product["Piece Height (cm)"]  # not a required column, only a specs mapping
    with pytest.raises(ExtractError, match=r"product\.Piece Height \(cm\)"):
        stage_rows(ARTIS, {"price": [price_row()], "product": [product]})


def test_a_column_named_only_in_the_pack_check_expr_is_still_checked():
    price = price_row()
    del price["Your Price"]
    config = copy.deepcopy(ARTIS)
    config["files"]["price"]["required_columns"].remove("Your Price")
    with pytest.raises(ExtractError, match=r"price\.Your Price"):
        stage_rows(config, {"price": [price], "product": [product_row()]})


def test_an_ignored_alias_the_vocabulary_does_not_have_raises():
    config = copy.deepcopy(ARTIS)
    config["vocabulary"]["colour"]["ignore_aliases"] = ["chartreuse-ish"]
    with pytest.raises(ExtractError, match="chartreuse-ish"):
        Stager(config)


def test_a_file_with_no_rows_raises():
    with pytest.raises(ExtractError, match="no rows for file role 'product'"):
        stage_rows(ARTIS, {"price": [price_row()], "product": []})


def test_summary_counts_fields_and_issues():
    rows = stage_rows(ARTIS, {"price": [price_row("04-21-301"), price_row("75-47-131")],
                              "product": [product_row("04-21-301", Description="Jug Blue/Red (CE@125,175ml)")]})
    summary = summarise(rows)
    assert summary["rows"] == 2
    assert summary["with line mark"] == 1
    assert summary["with several line values"] == 1
    assert summary["issue: colour_ambiguous"] == 1


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a migrated scratch database. Everything
# runs in a transaction that is rolled back, so nothing is left behind.

DB_CONFIG = {
    "supplier": {"code": "test_extract", "name": "Test", "currency": "GBP", "availability_source": "unknown"},
    "files": {
        "price": {"pattern": "Price_*.csv", "format": "csv", "encoding": "utf-8", "key": "SKU",
                  "required_columns": ["SKU", "Name", "Pack", "Cost"]},
        "product": {"pattern": "Product_*.csv", "format": "csv", "encoding": "utf-8", "key": "Part",
                    "required_columns": ["Part", "Category"]},
    },
    "key_format": r"^\d+$",
    "join": {"primary": "price", "secondary": "product", "duplicate_key": "raise",
             "primary_without_secondary": "load", "secondary_without_primary": "review"},
    "product": {
        "supplier_sku": "price.SKU",
        "name_raw": "price.Name",
        "brand": "price.Name",
        "supplier_category_raw": {"columns": ["product.Category"], "separator": " > ", "blank": "-"},
    },
    "offer": {"cost_ex_vat": "price.Cost", "base_units_per_pack": "price.Pack"},
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


def land_files(conn, directory, price, product):
    (directory / "Price_Aug.csv").write_text(price, encoding="utf-8")
    (directory / "Product_Aug.csv").write_text(product, encoding="utf-8")
    ingest(conn, DB_CONFIG, directory, "config/suppliers/test_extract.yaml")


def test_extract_reads_the_landed_batches_and_writes_nothing(conn, tmp_path):
    land_files(conn, tmp_path, "SKU,Name,Pack,Cost\r\n101,Jug,6,18.00\r\n102,Mug,12,30.00\r\n",
               "Part,Category\r\n101,Glass\r\n")
    before = conn.execute(sa.text("SELECT count(*) FROM supplier.supplier_products")).scalar_one()

    batches, rows = extract(conn, DB_CONFIG)

    assert set(batches) == {"price", "product"}
    assert [r.supplier_sku for r in rows] == ["101", "102"]  # file order
    assert rows[0].offer.cost_ex_vat == Decimal("18.00")
    assert rows[1].offer.base_units_per_pack == Decimal("12")
    assert rows[0].supplier_category_raw == "Glass"
    assert rows[1].supplier_category_raw is None
    assert conn.execute(sa.text("SELECT count(*) FROM supplier.supplier_products")).scalar_one() == before


def test_extract_uses_the_newest_batch_of_a_changed_file(conn, tmp_path):
    land_files(conn, tmp_path, "SKU,Name,Pack,Cost\r\n101,Jug,6,18.00\r\n", "Part,Category\r\n101,Glass\r\n")
    land_files(conn, tmp_path, "SKU,Name,Pack,Cost\r\n101,Jug,6,21.00\r\n", "Part,Category\r\n101,Glass\r\n")
    _, rows = extract(conn, DB_CONFIG)
    assert rows[0].offer.cost_ex_vat == Decimal("21.00")


def test_extract_raises_when_a_file_role_has_never_landed(conn):
    conn.execute(sa.text(
        "INSERT INTO supplier.suppliers (name, code) VALUES ('Test', 'test_extract')"))
    with pytest.raises(ExtractError, match=r"no landed batch for file roles \['price', 'product'\]"):
        extract(conn, DB_CONFIG)


def test_extract_raises_for_a_supplier_that_was_never_ingested(conn):
    with pytest.raises(ExtractError, match="no supplier row"):
        extract(conn, DB_CONFIG)
