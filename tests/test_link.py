import copy
import json
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa

from pipeline.config import load_supplier_config
from pipeline.link import (
    Grouper,
    LinkError,
    Member,
    Node,
    build_plan,
    resolve_group,
    run,
)

ARTIS = load_supplier_config("artis")
GROUPER = Grouper(ARTIS)

GLASS = Node(10, "gla/wine", ("capacity", "colour"), "GLA")
PLATE = Node(20, "tab/plates", ("size", "colour"), "TAB")
DISH = Node(30, "tab/dessert", (), "TAB")
JIGGER = Node(40, "bar/jiggers", ("measure_size",), "BAR")


def brim(cl, ml=None):
    return {"value": str(cl), "source_unit": "cl", "unit": "ml",
            "canonical_value": float(ml if ml is not None else Decimal(str(cl)) * 10)}


def member(sku, name, *, category="Drinkware > Stemware > Red Wine", rng="Perception", brand="Artis",
           pack="24", product_id=None, **specs):
    specs = {"range": rng, **specs} if rng else specs
    return Member(hash(sku) % 10**6, sku, name, brand, category, specs,
                  Decimal(pack) if pack else None, product_id)


def colour(*candidates):
    return {"status": "proposed", "candidates": list(candidates)}


def size(raw, *dims):
    return {"raw": raw, "unit": "cm", "dimensions": list(dims)}


def line(raw):
    return {"raw": raw, "parsed": True, "segments": []}


# ---------------------------------------------------------------- the stem

@pytest.mark.parametrize("name, stem", [
    ("Perception Wine 32cl [11.25oz]", "Perception Wine"),
    ("Perception Wine 32cl [11.25oz] (CE@175ml)", "Perception Wine"),
    ("Munique Beer 0.5pt/ 28cl [9.75oz] (CE Brim@0.5pt)", "Munique Beer"),
    ("Purity Pearls Gold Plate 30.8cm [12.1\"]", "Purity Pearls Plate"),
    ("Marie Christine Rimmed Oval Plate 30x19cm", "Marie Christine Rimmed Oval Plate"),
    ("Squashy Mugs Medium Yellow 22cl [7.75oz]", "Squashy Mugs"),
    ("Grandioso White Wine 45cl [15.75oz]", "Grandioso White Wine"),  # a shape, not a colour
    ("Mini Milk Bottle 15cl [5.25oz]", "Mini Milk Bottle"),           # Mini is not a size word
])
def test_the_stem_removes_every_option_and_nothing_else(name, stem):
    assert GROUPER.stem(name) == stem


def test_range_is_part_of_the_key():
    a = member("1", "Wine 32cl", rng="Perception")
    b = member("2", "Wine 41cl", rng="Brilliance")
    assert GROUPER.key(a) != GROUPER.key(b)


# ------------------------------------------------------------- the options

def test_capacity_keeps_the_label_the_number_and_the_line_mark():
    o = GROUPER.options(member("1", "x", capacity_brim=brim("23.5"), line_mark=line("(CE@175ml)")), GLASS.variant_axes)
    cap = o["capacity"]
    assert (cap.value, cap.canonical_value, cap.unit, cap.qualifier) == ("23.5cl", Decimal("235.0"), "ml", "(CE@175ml)")
    assert cap.label == "23.5cl (CE@175ml)"


def test_one_colour_is_a_value_and_two_are_none():
    one = GROUPER.options(member("1", "x", colour=colour("black")), GLASS.variant_axes)
    two = GROUPER.options(member("2", "x", colour=colour("black", "white")), GLASS.variant_axes)
    assert one["colour"].value == "Black" and "colour" not in two


def test_size_is_numeric_only_when_it_has_one_dimension():
    round_ = GROUPER.options(member("1", "x", size=size("27cm", 27.0)), PLATE.variant_axes)["size"]
    oval = GROUPER.options(member("2", "x", size=size("30x19cm", 30.0, 19.0)), PLATE.variant_axes)["size"]
    word = GROUPER.options(member("3", "Squashy Mugs Medium"), PLATE.variant_axes)["size"]
    assert (round_.canonical_value, oval.canonical_value, word.value) == (Decimal("27.0"), None, "Medium")


def test_an_axis_the_yaml_cannot_supply_is_not_an_axis():
    assert GROUPER.axes_for(JIGGER) == ()
    assert GROUPER.axes_for(GLASS) == ("capacity", "colour")


# ------------------------------------------------------- what may be grouped

def options_for(*members, node=GLASS):
    return list(members), [GROUPER.options(m, node.variant_axes) for m in members]


def test_a_lined_glass_and_its_unlined_twin_are_two_variants():
    ms, os_ = options_for(member("1", "x", capacity_brim=brim(32)),
                          member("2", "x", capacity_brim=brim(32), line_mark=line("(CE@175ml)")))
    assert resolve_group(ms, os_, GLASS.variant_axes) is None


def test_the_same_capacity_in_different_units_is_the_same_option():
    ms, os_ = options_for(member("1", "x", capacity_brim=brim(40)),
                          member("2", "x", capacity_brim={"value": "400", "source_unit": "ml",
                                                          "unit": "ml", "canonical_value": 400.0}))
    assert "same options" in resolve_group(ms, os_, GLASS.variant_axes)


def test_a_value_on_some_members_only_is_a_conflict():
    ms, os_ = options_for(member("1", "x", capacity_brim=brim(26)),
                          member("2", "x", capacity_brim=brim(26), colour=colour("blue")))
    assert resolve_group(ms, os_, GLASS.variant_axes) == "no colour on 1, which other SKUs in the group have"


# ---------------------------------------------------------------- the plan

def plan_of(*members, node=GLASS, category_of=None, product_nodes=None):
    category_of = category_of or {m.category_raw: node for m in members}
    return build_plan(GROUPER, list(members), category_of, product_nodes or {})


def test_a_sparse_matrix_stays_sparse():
    # Blue in two sizes, grey in one: three variants, never a fourth.
    plate = dict(category="Tableware > Plate > Coupe", rng="Purity")
    p = plan_of(member("1", "Purity Blue Plate 21cm", size=size("21cm", 21.0), colour=colour("blue"), **plate),
                member("2", "Purity Blue Plate 27cm", size=size("27cm", 27.0), colour=colour("blue"), **plate),
                member("3", "Purity Grey Plate 27cm", size=size("27cm", 27.0), colour=colour("grey"), **plate),
                node=PLATE)
    (product,) = p.new_products
    assert product.title == "Purity Plate" and product.grouped
    assert [(m.sku, [o.label for o in opts.values()]) for m, opts in product.members] == [
        ("1", ["21cm", "Blue"]), ("2", ["27cm", "Blue"]), ("3", ["27cm", "Grey"])]


def test_a_single_sku_keeps_its_full_name_as_title():
    (product,) = plan_of(member("1", "Perception Wine 32cl [11.25oz]", capacity_brim=brim(32))).new_products
    assert product.title == "Perception Wine 32cl [11.25oz]" and not product.grouped


def test_a_node_without_axes_never_groups():
    dish = dict(category="Tableware > Other Tableware > Dessert", rng="Delice")
    p = plan_of(member("1", "Delice Dessert Dish 8.3cm", size=size("8.3cm", 8.3), **dish),
                member("2", "Delice Dessert Dish 9.65cm", size=size("9.65cm", 9.65), **dish), node=DISH)
    assert [(len(x.members), x.title) for x in p.new_products] == [
        (1, "Delice Dessert Dish 8.3cm"), (1, "Delice Dessert Dish 9.65cm")]
    assert not p.conflicts


def test_a_conflicted_group_creates_nothing():
    p = plan_of(member("1", "Mini Milk Bottle 15cl", capacity_brim=brim(15)),
                member("2", "Mini Milk Bottle 15cl", capacity_brim=brim(15)))
    assert not p.new_products and [m.sku for m in p.conflicts[0].members] == ["1", "2"]


def test_unmapped_and_already_linked_are_counted_not_placed():
    linked = member("1", "Perception Wine 32cl", capacity_brim=brim(32), product_id=7)
    unmapped = member("2", "Thing", category="Nowhere")
    new = member("3", "Perception Wine 41cl", capacity_brim=brim(41))
    p = build_plan(GROUPER, [linked, unmapped, new], {linked.category_raw: GLASS}, {7: GLASS})
    assert (p.already_linked, [m.sku for m in p.unmapped]) == (1, ["2"])
    (joiner,) = p.joiners                      # same key as the linked one: it joins product 7
    assert (joiner.product_id, joiner.member.sku, joiner.options["capacity"].value) == (7, "3", "41cl")


def test_joining_an_existing_product_is_checked_against_its_variants():
    linked = member("1", "Perception Wine 32cl", capacity_brim=brim(32), product_id=7)
    twin = member("2", "Perception Wine 32cl", capacity_brim=brim(32))
    p = build_plan(GROUPER, [linked, twin], {linked.category_raw: GLASS}, {7: GLASS})
    assert not p.joiners and "cannot join product 7" in p.conflicts[0].reason


def test_a_config_without_base_unit_raises():
    config = copy.deepcopy(ARTIS)
    del config["base_unit"]
    with pytest.raises(LinkError, match="base_unit"):
        Grouper(config)


def test_an_unknown_axis_source_raises():
    config = copy.deepcopy(ARTIS)
    config["variant_axes"]["colour"]["value"] = "vocabulary.color"
    with pytest.raises(LinkError, match="vocabulary.color"):
        Grouper(config)


# Needs TEST_DATABASE_URL pointing at a scratch database migrated to head.
# Everything runs in a transaction that is rolled back. SKUs drawn from the
# sequence stay drawn, which is how the live sequence behaves too.

CODE = "test_link"


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
        dept = connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (name, slug, path, sku_prefix) "
            "VALUES ('Test', 'test-link', 'test-link', 'ZZY') RETURNING category_id")).scalar_one()
        node = connection.execute(sa.text(
            "INSERT INTO catalog.taxonomy (parent_id, name, slug, path, variant_axes) "
            "VALUES (:p, 'Wine', 'wine', 'test-link/wine', '{capacity,colour}') RETURNING category_id"),
            {"p": dept}).scalar_one()
        connection.execute(sa.text(
            "INSERT INTO link.category_map (supplier_id, supplier_category_raw, category_id, confirmed_by, confirmed_at) "
            "SELECT supplier_id, 'Wine', :n, 'test', now() FROM supplier.suppliers WHERE code = :c"),
            {"n": node, "c": CODE})
        yield connection
        transaction.rollback()


def config():
    c = copy.deepcopy(ARTIS)
    c["supplier"]["code"] = CODE
    return c


def add(conn, sku, name, pack="24", category="Wine", **specs):
    pid = conn.execute(sa.text(
        "INSERT INTO supplier.supplier_products (supplier_id, supplier_sku, name_raw, name_clean, brand, "
        "supplier_category_raw, specs) SELECT supplier_id, :sku, :name, :name, 'Artis', :cat, CAST(:specs AS jsonb) "
        "FROM supplier.suppliers WHERE code = :c RETURNING supplier_product_id"),
        {"sku": sku, "name": name, "cat": category, "specs": json.dumps({"range": "Perception", **specs}),
         "c": CODE}).scalar_one()
    conn.execute(sa.text(
        "INSERT INTO supplier.supplier_offers (supplier_product_id, base_units_per_pack, cost_ex_vat) "
        "VALUES (:p, :n, 36)"), {"p": pid, "n": pack})
    return pid


def links(conn):
    return conn.execute(sa.text("""
        SELECT sp.supplier_sku, l.status, l.is_preferred, l.confirmed_by, l.match_confidence,
               v.mosferics_sku, v.variant_title, v.pack_size, v.base_units_per_variant, p.title, p.handle, p.base_unit
        FROM link.variant_links l JOIN supplier.supplier_products sp USING (supplier_product_id)
        JOIN catalog.variants v USING (variant_id) JOIN catalog.products p USING (product_id)
        JOIN supplier.suppliers s ON s.supplier_id = sp.supplier_id
        WHERE s.code = :c ORDER BY sp.supplier_sku"""), {"c": CODE}).all()


def test_a_single_sku_gets_a_confirmed_preferred_link_and_a_valid_sku(conn):
    add(conn, "01", "Solo Wine 32cl", capacity_brim=brim(32))
    result = run(conn, config(), dry_run=False)
    assert result["written"]["products"] == 1
    (row,) = links(conn)
    assert (row.status, row.is_preferred, row.confirmed_by, row.match_confidence) == ("confirmed", True, "pipeline", 1)
    assert row.mosferics_sku.startswith("MOS-ZZY-")       # the CHECK has already validated the digit
    # a pack of 24 lands as 24 on both, not as 1 somewhere
    assert (row.pack_size, row.base_units_per_variant, row.base_unit) == (24, Decimal("24.0000"), "each")
    assert row.variant_title is None


def test_a_group_is_one_product_with_proposed_links_and_its_options(conn):
    add(conn, "01", "Perception Wine 32cl", capacity_brim=brim(32))
    add(conn, "02", "Perception Wine 32cl (CE@175ml)", pack="6", capacity_brim=brim(32),
        line_mark=line("(CE@175ml)"))
    add(conn, "03", "Perception Wine 41cl", capacity_brim=brim(41))
    run(conn, config(), dry_run=False)
    rows = links(conn)
    assert {(r.title, r.handle) for r in rows} == {("Perception Wine", "perception-wine")}
    assert [(r.status, r.is_preferred, r.confirmed_by) for r in rows] == [("proposed", False, None)] * 3
    assert [r.variant_title for r in rows] == ["32cl", "32cl (CE@175ml)", "41cl"]
    assert [r.pack_size for r in rows] == [24, 6, 24]
    options = conn.execute(sa.text("""
        SELECT o.axis, o.value, o.canonical_value, o.unit, o.qualifier FROM catalog.variant_options o
        JOIN catalog.variants v USING (variant_id) JOIN link.variant_links l USING (variant_id)
        JOIN supplier.supplier_products sp USING (supplier_product_id) WHERE sp.supplier_sku = '02'""")).all()
    assert options == [("capacity", "32cl", Decimal("320.0"), "ml", "(CE@175ml)")]


def test_a_rerun_creates_nothing_and_a_new_sku_joins_its_group(conn):
    add(conn, "01", "Perception Wine 32cl", capacity_brim=brim(32))
    run(conn, config(), dry_run=False)
    again = run(conn, config(), dry_run=False)
    assert again["plan"]["products"] == 0 and again["plan"]["skus_already_linked"] == 1
    add(conn, "02", "Perception Wine 41cl", capacity_brim=brim(41))
    joined = run(conn, config(), dry_run=False)
    assert joined["plan"]["skus_joining_existing"] == 1
    rows = links(conn)
    assert len({r.handle for r in rows}) == 1
    assert [(r.supplier_sku, r.status) for r in rows] == [("01", "confirmed"), ("02", "proposed")]


def test_a_conflict_is_queued_once(conn):
    add(conn, "01", "Mini Milk Bottle 15cl", capacity_brim=brim(15))
    add(conn, "02", "Mini Milk Bottle 15cl", capacity_brim=brim(15))
    first = run(conn, config(), dry_run=False)
    second = run(conn, config(), dry_run=False)
    assert (first["written"]["review_items"], second["written"]["review_items"]) == (2, 0)
    assert links(conn) == []


def test_handles_and_brands_do_not_collide(conn):
    add(conn, "01", "Solo Wine", capacity_brim=brim(32))
    conn.execute(sa.text("UPDATE supplier.supplier_products SET specs = specs || '{\"range\": \"Other\"}' "
                         "WHERE supplier_sku = '01' AND supplier_id = (SELECT supplier_id FROM supplier.suppliers "
                         "WHERE code = :c)"), {"c": CODE})
    add(conn, "02", "Solo Wine", capacity_brim=brim(41))
    run(conn, config(), dry_run=False)
    assert sorted(r.handle for r in links(conn)) == ["solo-wine", "solo-wine-2"]
    assert conn.execute(sa.text("SELECT count(*) FROM catalog.brands WHERE name = 'Artis'")).scalar() == 1


def test_a_dry_run_writes_nothing(conn):
    add(conn, "01", "Solo Wine 32cl", capacity_brim=brim(32))
    result = run(conn, config(), dry_run=True)
    assert result["plan"]["products"] == 1 and "written" not in result
    assert links(conn) == []
