import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from pipeline.price import (
    AUTO,
    PENDING,
    Candidate,
    PriceError,
    Rule,
    RuleBook,
    Settings,
    apply_rule,
    choose_source,
    gate,
    load_settings,
    margin_pct,
    run,
)

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
SETTINGS = Settings(materiality_pct=Decimal(5), margin_floor_pct=Decimal(15), vat_rate=Decimal("0.20"),
                    rounding=Decimal("0.01"))
PENNY = Decimal("0.01")

# A small tree: Glassware (1) > Beer Glasses (2) > Pint Glasses (3).
ANCESTORS = {1: [1], 2: [2, 1], 3: [3, 2, 1]}
ARTIS, BBP = 10, 20


def rule(rule_id, scope_type, scope_id=None, value="30", method="fixed_margin", supplier_id=None, priority=0,
         valid_from=NOW - timedelta(days=1)):
    return Rule(rule_id, scope_type, scope_id, supplier_id, method, Decimal(value), priority, valid_from)


GLOBAL = rule(1, "global")


def winner(*rules, product_id=100, category_id=3, supplier_id=ARTIS):
    return RuleBook([GLOBAL, *rules], ANCESTORS, NOW).resolve(product_id, category_id, supplier_id)[0].rule_id


# ----------------------------------------------------------- rule resolution
# The four rows of the data model's worked table, for an Artis pint glass.

def test_supplier_specific_beats_generic_at_any_depth():
    assert winner(rule(2, "category", 1, "45", supplier_id=ARTIS), rule(3, "category", 2, "50")) == 2


def test_same_kind_the_nearest_node_wins():
    assert winner(rule(2, "category", 1, "45", supplier_id=ARTIS), rule(3, "category", 2, "48", supplier_id=ARTIS)) == 3
    assert winner(rule(2, "category", 1, "40"), rule(3, "category", 2, "50")) == 3


def test_a_category_rule_beats_a_supplier_wide_one():
    assert winner(rule(2, "supplier", ARTIS, "35"), rule(3, "category", 1, "40")) == 3


def test_a_product_rule_beats_everything():
    assert winner(rule(2, "category", 3, supplier_id=ARTIS), rule(3, "product", 100)) == 3


def test_a_rule_for_another_supplier_or_branch_does_not_apply():
    assert winner(rule(2, "category", 1, supplier_id=BBP), rule(3, "supplier", BBP), rule(4, "category", 99)) == 1


def test_priority_breaks_a_tie_on_the_same_scope():
    assert winner(rule(2, "category", 2, priority=1), rule(3, "category", 2, priority=5)) == 3


def test_a_tie_priority_does_not_break_raises():
    with pytest.raises(PriceError, match="rules tie"):
        winner(rule(2, "category", 2), rule(3, "category", 2))


def test_a_rule_not_yet_in_force_is_ignored():
    assert winner(rule(2, "product", 100, valid_from=NOW + timedelta(days=1))) == 1


def test_no_global_rule_raises():
    with pytest.raises(PriceError, match="no global rule"):
        RuleBook([rule(2, "category", 1)], ANCESTORS, NOW)


# ------------------------------------------------------------------ methods

def test_fixed_margin_is_a_margin_not_a_markup():
    # 30% margin on a cost of 7.00 is 10.00 (7 / 0.70), not 9.10 (7 * 1.30)
    assert apply_rule(rule(1, "global", value="30"), Decimal("7.00"), PENNY) == Decimal("10.00")
    assert margin_pct(Decimal("10.00"), Decimal("7.00")) == Decimal("30.000")


def test_percent_markup_and_target_price():
    assert apply_rule(rule(1, "global", method="percent_markup", value="30"), Decimal("7.00"), PENNY) == Decimal("9.10")
    assert apply_rule(rule(1, "global", method="target_price", value="12.5"), Decimal("7.00"), PENNY) == Decimal("12.50")


def test_rounding_is_half_up():
    assert apply_rule(rule(1, "global", method="percent_markup", value="0"), Decimal("1.005"), PENNY) == Decimal("1.01")


# ------------------------------------------------------------------ sourcing

def cand(sp, cost, units="6", lead=None, preferred=False, offer=True):
    return Candidate(sp, ARTIS, f"sku{sp}", preferred, sp if offer else None, Decimal(cost), Decimal(units), lead)


def test_the_cheapest_per_base_unit_wins_not_the_cheapest_pack():
    # 12 for 18.00 is 1.50 each; 6 for 10.00 is 1.67 each
    assert choose_source([cand(1, "10.00", "6"), cand(2, "18.00", "12")]).supplier_product_id == 2


def test_a_cost_tie_goes_to_lead_time_then_to_the_current_winner():
    assert choose_source([cand(1, "6", lead=5), cand(2, "6", lead=2)]).supplier_product_id == 2
    assert choose_source([cand(1, "6"), cand(2, "6", preferred=True)]).supplier_product_id == 2


def test_no_current_offer_means_no_source():
    assert choose_source([cand(1, "6", offer=False)]) is None


# ---------------------------------------------------------------------- gate

def old(calculated, status):
    return {"calculated_price": Decimal(calculated), "status": status, "override_price": None}


def decide(prior, calculated, margin="30", override=None):
    calc = Decimal(calculated)
    final = Decimal(override) if override else calc
    return gate(old=prior, calculated=calc, override=Decimal(override) if override else None, final=final,
                margin=Decimal(margin), settings=SETTINGS)[::2]  # (status, write)


def test_a_new_price_always_waits():
    assert decide(None, "10.00") == (PENDING, True)


def test_a_small_change_to_a_live_price_is_auto_approved():
    assert decide(old("10.00", "approved"), "10.40") == (AUTO, True)


def test_a_change_at_the_threshold_waits():
    assert decide(old("10.00", "auto_approved"), "10.50") == (PENDING, True)


def test_any_change_under_the_floor_waits():
    assert decide(old("10.00", "approved"), "10.10", margin="14.9") == (PENDING, True)


def test_a_small_change_to_a_price_never_approved_does_not_approve_it():
    assert decide(old("10.00", PENDING), "10.10") == (PENDING, True)
    assert decide(old("10.00", "rejected"), "10.10") == (PENDING, True)


def test_an_unchanged_price_writes_nothing():
    assert decide(old("10.00", "approved"), "10.00") == ("approved", False)
    assert decide(old("10.00", "rejected"), "10.00") == ("rejected", False)


def test_a_live_price_whose_cost_rose_under_the_floor_waits_again():
    assert decide(old("10.00", "approved"), "10.00", margin="12") == (PENDING, True)


def test_an_override_stands_and_keeps_its_approval():
    assert decide(old("10.00", "approved"), "11.00", override="12.00") == ("approved", True)
    assert decide(old("10.00", "approved"), "11.00", override="12.00", margin="10") == (PENDING, True)


# -------------------------------------------------------------------- config

def test_the_shipped_settings_are_the_stage_0_decisions():
    s = load_settings()
    assert (s.materiality_pct, s.margin_floor_pct, s.vat_rate) == (5, 15, Decimal("0.2"))


def test_vat_inclusive_prices_are_refused(tmp_path: Path):
    path = tmp_path / "pricing.yaml"
    path.write_text("materiality_pct: 5\nmargin_floor_pct: 15\nprices_include_vat: true\nvat_rate: 0.2\nrounding: '0.01'\n")
    with pytest.raises(PriceError, match="prices_include_vat"):
        load_settings(path)


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a scratch database migrated to head.
# Everything runs in a transaction that is rolled back.

CODE = "test_price"


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
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) VALUES ('Test', 'test-price', 'test-price', 'ZZP')"))
        # Outranks any global rule the scratch database already holds.
        connection.execute(sa.text(
            "INSERT INTO pricing.rules (scope_type, method, value, priority) VALUES ('global', 'fixed_margin', 30, 1000000)"))
        yield connection
        transaction.rollback()


def variant(conn, sku, cost, pack="6", status="confirmed", preferred=None):
    """A product of one variant from one supplier SKU. A pack of 6, never 1: a missing factor must show."""
    sp = conn.execute(sa.text(
        "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw) "
        "SELECT supplier_id, :sku, :sku FROM supplier.suppliers WHERE code = :c RETURNING supplier_product_id"),
        {"sku": sku, "c": CODE}).scalar_one()
    conn.execute(sa.text("INSERT INTO supplier.supplier_offers (supplier_product_id, base_units_per_pack, cost_ex_vat) "
                         "VALUES (:p, :n, :cost)"), {"p": sp, "n": pack, "cost": cost})
    product = conn.execute(sa.text(
        "INSERT INTO catalog.products (title, handle, category_id, base_unit) "
        "SELECT :t, :t, category_id, 'each' FROM catalog.taxonomy WHERE slug = 'test-price' RETURNING product_id"),
        {"t": f"p-{sku}"}).scalar_one()
    v = conn.execute(sa.text(
        "INSERT INTO catalog.variants (product_id, mosferics_sku, pack_size, base_units_per_variant) "
        "VALUES (:p, catalog.next_mosferics_sku('ZZP'), :n, :n) RETURNING variant_id"),
        {"p": product, "n": pack}).scalar_one()
    preferred = (status == "confirmed") if preferred is None else preferred
    conn.execute(sa.text(
        "INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, status, is_preferred) "
        "VALUES (:sp, :v, 'identical', :s, :pref)"), {"sp": sp, "v": v, "s": status, "pref": preferred})
    return v, sp


def price_row(conn, v):
    return conn.execute(sa.text(
        "SELECT calculated_price, override_price, final_price, margin_pct, vat_rate, status, approved_by "
        "FROM pricing.variant_prices WHERE variant_id = :v"), {"v": v}).one()


def set_cost(conn, sp, cost):
    conn.execute(sa.text("UPDATE supplier.supplier_offers SET cost_ex_vat = :c WHERE supplier_product_id = :sp"),
                 {"c": cost, "sp": sp})


def test_a_variant_is_costed_per_base_unit_and_priced_per_variant(conn):
    v, _ = variant(conn, "01", cost="42.00")          # 6 at 7.00 each
    run(conn, SETTINGS, dry_run=False)
    cpbu = conn.execute(sa.text("SELECT cost_per_base_unit FROM pricing.cost_basis WHERE variant_id = :v"),
                        {"v": v}).scalar_one()
    assert cpbu == Decimal("7.000000")
    row = price_row(conn, v)
    # 42.00 / 0.70, not 7.00 / 0.70: the price is for the pack of 6
    assert (row.calculated_price, row.final_price, row.margin_pct) == (Decimal("60.00"), Decimal("60.00"), Decimal("30.000"))
    assert (row.status, row.approved_by, row.vat_rate) == (PENDING, None, Decimal("0.20"))
    margin = conn.execute(sa.text("SELECT gross_margin FROM pricing.live_margins WHERE variant_id = :v"),
                          {"v": v}).scalar_one()
    assert margin == Decimal("30.000")                # the view agrees with the snapshot


def test_a_confirmed_link_without_preference_becomes_preferred(conn):
    v, sp = variant(conn, "01", cost="42.00", preferred=False)
    result = run(conn, SETTINGS, dry_run=False)
    assert result["written"]["preferred_set"] == 1
    assert conn.execute(sa.text("SELECT is_preferred FROM link.variant_links WHERE supplier_product_id = :sp"),
                        {"sp": sp}).scalar_one() is True


def test_a_proposed_group_is_not_priced(conn):
    v, _ = variant(conn, "01", cost="42.00", status="proposed")
    run(conn, SETTINGS, dry_run=False)
    assert conn.execute(sa.text("SELECT count(*) FROM pricing.variant_prices WHERE variant_id = :v"),
                        {"v": v}).scalar_one() == 0


def test_a_cost_that_does_not_divide_evenly_is_not_rewritten_on_every_run(conn):
    variant(conn, "01", cost="10.00", pack="6")       # 1.666... each
    run(conn, SETTINGS, dry_run=False)
    assert run(conn, SETTINGS, dry_run=False)["written"]["cost_basis"] == 0


def test_a_dry_run_writes_nothing_and_a_rerun_is_a_no_op(conn):
    v, _ = variant(conn, "01", cost="42.00")
    assert run(conn, SETTINGS, dry_run=True)["plan"]["prices_written"] == 1
    assert conn.execute(sa.text("SELECT count(*) FROM pricing.variant_prices WHERE variant_id = :v"),
                        {"v": v}).scalar_one() == 0
    run(conn, SETTINGS, dry_run=False)
    again = run(conn, SETTINGS, dry_run=False)["written"]
    assert (again["prices"], again["cost_basis"], again["preferred_set"]) == (0, 0, 0)


def test_a_small_cost_change_on_an_approved_price_is_auto_approved(conn):
    v, sp = variant(conn, "01", cost="42.00")
    run(conn, SETTINGS, dry_run=False)
    conn.execute(sa.text("UPDATE pricing.variant_prices SET status = 'approved', approved_by = 'reviewer' WHERE variant_id = :v"),
                 {"v": v})
    set_cost(conn, sp, "43.20")                       # +2.9%
    run(conn, SETTINGS, dry_run=False)
    row = price_row(conn, v)
    assert (row.calculated_price, row.status, row.approved_by) == (Decimal("61.71"), AUTO, "pipeline")


def test_a_large_cost_change_on_an_approved_price_waits(conn):
    v, sp = variant(conn, "01", cost="42.00")
    run(conn, SETTINGS, dry_run=False)
    conn.execute(sa.text("UPDATE pricing.variant_prices SET status = 'approved', approved_by = 'reviewer' WHERE variant_id = :v"),
                 {"v": v})
    set_cost(conn, sp, "48.00")                       # +14%
    run(conn, SETTINGS, dry_run=False)
    row = price_row(conn, v)
    assert (row.status, row.approved_by) == (PENDING, None)


def test_an_override_is_never_overwritten(conn):
    v, sp = variant(conn, "01", cost="42.00")
    run(conn, SETTINGS, dry_run=False)
    conn.execute(sa.text("UPDATE pricing.variant_prices SET override_price = 75, status = 'approved', "
                         "approved_by = 'reviewer' WHERE variant_id = :v"), {"v": v})
    set_cost(conn, sp, "45.00")
    run(conn, SETTINGS, dry_run=False)
    row = price_row(conn, v)
    assert (row.override_price, row.final_price, row.calculated_price) == (Decimal("75"), Decimal("75"), Decimal("64.29"))
    assert (row.status, row.approved_by) == ("approved", "reviewer")


def test_a_variant_with_no_current_offer_goes_to_review(conn):
    v, sp = variant(conn, "01", cost="42.00")
    conn.execute(sa.text("UPDATE supplier.supplier_offers SET is_current = false, valid_to = now() "
                         "WHERE supplier_product_id = :sp"), {"sp": sp})
    result = run(conn, SETTINGS, dry_run=False)
    assert result["written"]["review_items"] == 1
    item = conn.execute(sa.text("SELECT entity_type, issue_type FROM ops.review_queue WHERE entity_id = :v "
                                "AND issue_type = 'no_preferred_source'"), {"v": v}).one()
    assert item == ("variant_link", "no_preferred_source")
    assert run(conn, SETTINGS, dry_run=False)["written"]["review_items"] == 0   # not raised twice
