import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from pipeline.config import load_supplier_config
from pipeline.extract import Issue, LineMark, LineMarkSegment, Quantity, Size, SourceBatch, StagedOffer, StagedRow, VocabularyValue
from pipeline.normalise import (
    NormaliseError,
    normalise_rows,
    specs_json,
    summarise,
    to_ml,
    write_rows,
)

ARTIS = load_supplier_config("artis")


def staged(sku="04-21-301", **fields):
    defaults = dict(
        supplier_sku=sku, name_raw="Nonic  57cl", name_clean="Nonic 57cl", brand="Artis",
        supplier_category_raw="Glassware > Beer > Nonic",
        offer=StagedOffer(Decimal("36.00"), Decimal("24"), "1 Box of 24pcs"),
        brim=None, brim_cross_check=None, line_mark=None, nucleation=False, size=None,
        vocabulary={}, colour_candidates=(), colour_status="proposed", safety={}, specs={}, issues=(),
    )
    return StagedRow(**{**defaults, **fields})


def normalise_one(**fields):
    return normalise_rows(ARTIS, [staged(**fields)])[0]


def codes(row):
    return [i.code for i in row.issues]


# ----------------------------------------------------------------------- units

@pytest.mark.parametrize("value, unit, ml", [
    ("35.5", "cl", "355.00"),
    ("175", "ml", "175.00"),
    ("2.8", "l", "2800.00"),
    ("1", "imperial_pint", "568.26"),
    ("0.5", "imperial_pint", "284.13"),
    ("1/3", "imperial_pint", "189.42"),
    ("2/3", "imperial_pint", "378.84"),
    ("10", "imperial_fl_oz", "284.13"),
])
def test_to_ml(value, unit, ml):
    assert to_ml(value, unit) == Decimal(ml)


def test_an_unknown_unit_raises():
    with pytest.raises(NormaliseError, match="no conversion from 'gallon'"):
        to_ml("1", "gallon")


def test_brim_keeps_raw_and_canonical_side_by_side():
    row = normalise_one(brim=Quantity("35.5", Decimal("35.5"), "cl"))
    assert row.specs["capacity_brim"] == {
        "value": "35.5", "source_unit": "cl", "canonical_value": Decimal("355.00"), "unit": "ml"}


def test_cl_and_ml_within_tolerance_agree():
    row = normalise_one(brim=Quantity("35", Decimal("35"), "cl"),
                        brim_cross_check=Quantity("355", Decimal("355"), "ml"))
    assert row.issues == ()


def test_cl_and_ml_beyond_tolerance_go_to_review():
    # the real 14-31-121: says 35cl and 450ml. Compared as bare numbers, every row would fail.
    row = normalise_one(brim=Quantity("35", Decimal("35"), "cl"),
                        brim_cross_check=Quantity("450", Decimal("450"), "ml"))
    assert codes(row) == ["brim_capacity_conflict"]
    assert row.issues[0].detail == "35 cl against 450 ml"
    assert row.specs["capacity_brim"]["canonical_value"] == Decimal("350.00")  # the column named as the source wins


def test_a_gap_of_exactly_the_tolerance_is_not_a_conflict():
    row = normalise_one(brim=Quantity("35", Decimal("35"), "cl"),
                        brim_cross_check=Quantity("355", Decimal("355"), "ml"))
    assert row.issues == ()
    row = normalise_one(brim=Quantity("35", Decimal("35"), "cl"),
                        brim_cross_check=Quantity("355.01", Decimal("355.01"), "ml"))
    assert codes(row) == ["brim_capacity_conflict"]


def test_line_mark_values_convert_each_in_their_own_unit():
    mark = LineMark("(CE@0.33,0.5pt,CE Brim@1pt)", (
        LineMarkSegment("line", "CE", ("1/3", "0.5"), "imperial_pint"),
        LineMarkSegment("brim", "CE Brim", ("1",), "imperial_pint"),
    ))
    specs = normalise_one(line_mark=mark).specs["line_mark"]
    assert specs["parsed"] is True
    assert specs["segments"][0]["canonical_values"] == [Decimal("189.42"), Decimal("284.13")]
    assert specs["segments"][1]["kind"] == "brim"
    assert specs["segments"][1]["canonical_values"] == [Decimal("568.26")]


def test_an_unparsed_line_mark_is_kept_raw_with_no_values():
    row = normalise_one(line_mark=LineMark("(CE@125m)", ()),
                        issues=(Issue("line_mark_unparsed", "capacity", "(CE@125m)"),))
    assert row.specs["line_mark"] == {"raw": "(CE@125m)", "parsed": False, "segments": []}
    assert codes(row) == ["line_mark_unparsed"]


# ---------------------------------------------------------------- vocabularies

@pytest.mark.parametrize("raw, canonical", [
    ("SodaLime", ["glass"]),
    ("  sodalime ", ["glass"]),                        # alias matching trims and ignores case
    ("Stainless Steel 18-10", ["stainless_steel"]),    # grades collapse
    ("Glass, Wood", ["glass", "wood"]),                # compound values split
    ("Glass;Wood", ["glass", "wood"]),
    ("Glass, SodaLime", ["glass"]),                    # two tokens, one canonical
])
def test_material_resolves_through_the_aliases(raw, canonical):
    row = normalise_one(vocabulary={"material": VocabularyValue(raw, "pending_review")})
    assert row.specs["material"] == {"raw": raw, "canonical": canonical, "status": "pending_review"}
    assert row.issues == ()


def test_an_unmapped_material_goes_to_review_and_keeps_what_did_resolve():
    row = normalise_one(vocabulary={"material": VocabularyValue("Glass, Unobtainium", "pending_review")})
    assert row.specs["material"]["canonical"] == ["glass"]
    assert row.specs["material"]["raw"] == "Glass, Unobtainium"
    assert codes(row) == ["unmapped_material"]


def test_finish_resolves_through_its_own_file():
    row = normalise_one(vocabulary={"finish": VocabularyValue("Mirror", None)})
    assert row.specs["finish"]["canonical"] == ["mirror"]
    assert codes(normalise_one(vocabulary={"finish": VocabularyValue("Glass", None)})) == ["unmapped_finish"]


def test_colour_stays_a_proposal():
    row = normalise_one(colour_candidates=("blue",))
    assert row.specs["colour"] == {"candidates": ["blue"], "status": "proposed"}


def test_safety_stays_pending_review():
    row = normalise_one(safety={"oven_safe": "pending_review"})
    assert row.specs["safety"] == {"oven_safe": "pending_review"}


def test_a_blank_safety_flag_writes_nothing():
    assert "safety" not in normalise_one().specs


# ----------------------------------------------------------------------- specs

def test_extract_specs_are_carried_through():
    row = normalise_one(specs={"height_cm": Decimal("11.5"), "range": "Annie"}, nucleation=True,
                        size=Size("30x20cm", (Decimal("30"), Decimal("20")), "cm"))
    assert row.specs["height_cm"] == Decimal("11.5")
    assert row.specs["range"] == "Annie"
    assert row.specs["nucleation"] is True
    assert row.specs["size"]["dimensions"] == [Decimal("30"), Decimal("20")]


def test_a_spec_that_clashes_with_a_derived_field_raises():
    with pytest.raises(NormaliseError, match="clash.*capacity_brim"):
        normalise_one(specs={"capacity_brim": "x"}, brim=Quantity("35", Decimal("35"), "cl"))


def test_specs_serialise_decimals_as_exact_json_numbers():
    decoded = json.loads(specs_json({"a": Decimal("189.42"), "b": Decimal("0.1"), "c": [Decimal("11.5")]}))
    assert decoded == {"a": 189.42, "b": 0.1, "c": [11.5]}
    assert '"a": 189.42' in specs_json({"a": Decimal("189.42")})


def test_an_alias_with_a_separator_in_it_raises(tmp_path):
    (tmp_path / "material.yaml").write_text('glass:\n  aliases: ["Glass, Clear"]\n', encoding="utf-8")
    (tmp_path / "finish.yaml").write_text('matt:\n  aliases: ["Matt"]\n', encoding="utf-8")
    with pytest.raises(NormaliseError, match="token separator"):
        normalise_rows(ARTIS, [], vocabulary_dir=tmp_path)


def test_summary_counts_issues():
    rows = normalise_rows(ARTIS, [staged(vocabulary={"material": VocabularyValue("Foo", "pending_review")}),
                                  staged("75-47-131", brim=Quantity("35", Decimal("35"), "cl"))])
    summary = summarise(rows)
    assert summary["rows"] == 2
    assert summary["brim capacity in ml"] == 1
    assert summary["issue: unmapped_material"] == 1


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a migrated scratch database. Everything
# runs in a transaction that is rolled back, so nothing is left behind.

CONFIG = {"supplier": {"code": "test_normalise"}}
AUGUST = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)


def batch(received_at=AUGUST):
    return SourceBatch("price", 1, "Price_Aug.csv", 1, received_at)


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
            "INSERT INTO supplier.suppliers (name, code) VALUES ('Test', 'test_normalise')"))
        yield connection
        transaction.rollback()


def write(conn, *rows, received_at=AUGUST):
    return write_rows(conn, CONFIG, batch(received_at), normalise_rows(ARTIS, list(rows)))


def product(conn, sku="04-21-301"):
    return conn.execute(sa.text(
        "SELECT p.* FROM supplier.supplier_products p JOIN supplier.suppliers s USING (supplier_id) "
        "WHERE s.code = 'test_normalise' AND p.supplier_sku = :sku"), {"sku": sku}).one()


def offers(conn, sku="04-21-301"):
    return conn.execute(sa.text(
        "SELECT o.* FROM supplier.supplier_offers o WHERE o.supplier_product_id = :id ORDER BY o.offer_id"),
        {"id": product(conn, sku).supplier_product_id}).all()


def queue(conn):
    return conn.execute(sa.text(
        "SELECT q.* FROM ops.review_queue q ORDER BY q.queue_id DESC")).all()


def test_a_row_lands_in_both_tables_with_the_pack_cost_and_batch_dates(conn):
    result = write(conn, staged(brim=Quantity("35.5", Decimal("35.5"), "cl")))
    assert (result.products_inserted, result.offers_inserted) == (1, 1)

    row = product(conn)
    assert (row.name_raw, row.name_clean) == ("Nonic  57cl", "Nonic 57cl")  # raw stays raw
    assert row.first_seen == row.last_seen == AUGUST
    assert row.specs["capacity_brim"]["canonical_value"] == 355.0
    assert row.discontinued is False

    offer = offers(conn)[0]
    assert (offer.cost_ex_vat, offer.base_units_per_pack) == (Decimal("36.0000"), Decimal("24.0000"))
    assert offer.valid_from == AUGUST.date()
    assert offer.is_current is True


def test_the_same_rows_twice_change_nothing(conn):
    row = staged(issues=(Issue("safety_contradiction", "safety", "a and b"),))
    write(conn, row)
    before = (product(conn), offers(conn), len(queue(conn)))
    again = write(conn, row)
    assert (again.products_inserted, again.products_updated) == (0, 1)
    assert (again.offers_inserted, again.offers_replaced, again.offers_unchanged) == (0, 0, 1)
    assert (again.queue_inserted, again.queue_already_raised) == (0, 1)
    assert (product(conn), offers(conn), len(queue(conn))) == before


def test_a_cost_change_closes_the_old_offer_and_opens_a_new_one(conn):
    write(conn, staged())
    result = write(conn, staged(offer=StagedOffer(Decimal("42.00"), Decimal("24"), "1 Box of 24pcs")),
                   received_at=SEPTEMBER)
    assert result.offers_replaced == 1
    old, new = offers(conn)
    assert (old.is_current, old.valid_to, old.cost_ex_vat) == (False, SEPTEMBER.date(), Decimal("36.0000"))
    assert (new.is_current, new.valid_from, new.valid_to, new.cost_ex_vat) == (
        True, SEPTEMBER.date(), None, Decimal("42.0000"))


def test_a_pack_size_change_at_the_same_cost_is_a_new_offer(conn):
    # the cost per piece has moved even though the pack price has not
    write(conn, staged())
    result = write(conn, staged(offer=StagedOffer(Decimal("36.00"), Decimal("12"), "1 Box of 24pcs")),
                   received_at=SEPTEMBER)
    assert result.offers_replaced == 1


def test_a_refresh_moves_last_seen_and_keeps_first_seen_and_other_owners_columns(conn):
    write(conn, staged())
    conn.execute(sa.text(
        "UPDATE supplier.supplier_products SET description_enriched = 'Kept', discontinued = true "
        "WHERE supplier_sku = '04-21-301'"))
    write(conn, staged(name_clean="Renamed"), received_at=SEPTEMBER)
    row = product(conn)
    assert (row.first_seen, row.last_seen, row.name_clean) == (AUGUST, SEPTEMBER, "Renamed")
    assert (row.description_enriched, row.discontinued) == ("Kept", True)


def test_a_product_with_no_price_row_lands_with_no_offer(conn):
    result = write(conn, staged(offer=None, issues=(Issue("no_price_row", "offer", "no row"),)))
    assert (result.products_inserted, result.offers_inserted, result.queue_inserted) == (1, 0, 1)
    assert offers(conn) == []


def test_issues_route_to_the_right_kind_of_queue_entity(conn):
    write(conn, staged(issues=(
        Issue("colour_ambiguous", "colour", "two colours"),
        Issue("pack_size_agrees_with_product_file", "offer", "6 against 12"),
    )))
    product_id = product(conn).supplier_product_id
    items = {q.issue_type: q for q in queue(conn) if q.entity_id == product_id}
    assert items["colour_ambiguous"].entity_type == "vocabulary"
    assert items["pack_size_agrees_with_product_file"].entity_type == "supplier_product"
    assert items["pack_size_agrees_with_product_file"].notes == "04-21-301: 6 against 12"
    assert items["pack_size_agrees_with_product_file"].status == "open"


def test_one_unmapped_value_on_many_products_is_one_queue_item(conn):
    foo = {"material": VocabularyValue("Foo", "pending_review")}
    result = write(conn, staged("04-21-301", vocabulary=foo), staged("75-47-131", vocabulary=foo),
                   staged("11-11-111", vocabulary={"material": VocabularyValue("Bar", "pending_review")}))
    assert result.queue_inserted == 2
    item = queue(conn)[1]
    assert (item.entity_type, item.issue_type) == ("vocabulary", "unmapped_material")
    assert item.notes == "material 'Foo' is not in material.yaml"
    assert item.entity_id == product(conn, "04-21-301").supplier_product_id


def test_a_dismissed_item_is_not_raised_again_even_after_someone_adds_a_note(conn):
    row = staged(issues=(Issue("safety_contradiction", "safety", "a and b"),))
    write(conn, row)
    queue_id = queue(conn)[0].queue_id
    conn.execute(sa.text(
        "UPDATE ops.review_queue SET status = 'dismissed', resolved_by = 'reviewer', resolved_at = now(), "
        "notes = notes || ' Checked the data sheet.' WHERE queue_id = :q"), {"q": queue_id})
    assert write(conn, row).queue_inserted == 0
    assert queue(conn)[0].queue_id == queue_id


def test_a_changed_value_is_a_new_item(conn):
    write(conn, staged(issues=(Issue("pack_size_agrees_with_product_file", "offer", "6 against 12"),)))
    result = write(conn, staged(issues=(Issue("pack_size_agrees_with_product_file", "offer", "6 against 24"),)))
    assert result.queue_inserted == 1


def test_a_bad_row_rolls_back_the_rows_before_it(conn):
    good, bad = staged("04-21-301"), staged("75-47-131", name_raw=None)
    savepoint = conn.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        write(conn, good, bad)
    savepoint.rollback()
    assert conn.execute(sa.text(
        "SELECT count(*) FROM supplier.supplier_products WHERE supplier_sku = '04-21-301' "
        "AND supplier_id = (SELECT supplier_id FROM supplier.suppliers WHERE code = 'test_normalise')"
    )).scalar_one() == 0


def test_supplier_stock_is_never_touched(conn):
    before = conn.execute(sa.text("SELECT count(*) FROM supplier.supplier_stock")).scalar_one()
    write(conn, staged())
    assert conn.execute(sa.text("SELECT count(*) FROM supplier.supplier_stock")).scalar_one() == before
