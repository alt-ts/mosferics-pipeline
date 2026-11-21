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


# ------------------------------------------- the name against the column

def ml(value, unit="cl", ml_=None):
    return {"value": str(value), "source_unit": unit, "unit": "ml",
            "canonical_value": float(ml_ if ml_ is not None else Decimal(str(value)) * (10 if unit == "cl" else 1))}


def test_a_capacity_the_name_contradicts_is_left_out_and_queued_alone():
    p = plan_of(member("1", "Atelier Prestige Carafe 15cl [5.25oz]", capacity_brim=brim(15)),
                member("2", "Atelier Prestige Carafe 25cl [8.75oz]", capacity_brim=brim(32)),
                member("3", "Atelier Prestige Carafe 64cl [22.5oz]", capacity_brim=brim(64)))
    (product,) = p.new_products
    assert [m.sku for m, _ in product.members] == ["1", "3"]
    (conflict,) = p.conflicts
    assert conflict.issue_type == "capacity_name_mismatch" and [m.sku for m in conflict.members] == ["2"]
    assert "25cl (250 ml)" in conflict.reason and "32cl (320 ml)" in conflict.reason
    assert p.summary()["skus_capacity_name_mismatch"] == 1 and p.summary()["skus_in_conflict"] == 0


@pytest.mark.parametrize("name, column", [
    ("Maitre Stackable Cup 18cl [6.25oz]", brim(19)),          # 5.6%: rounding
    ("Conical Jug 1.8ltr [62.5oz]", brim("177.4")),            # litres in the name, cl in the column
    ("Beer 500ml", ml(48, "cl")),                              # ml in the name
    ("Munique Beer 0.5pt/ 28cl [9.75oz] (CE Brim@0.5pt)", brim(28)),  # pints are not read; nor is the line mark
    ("Wine Glass (CE@125ml)", brim(32)),                       # the only capacity is the line mark
])
def test_a_capacity_within_tolerance_or_not_in_the_name_is_placed(name, column):
    assert GROUPER.capacity_mismatch(member("1", name, capacity_brim=column)) is None


def test_the_tolerance_is_measured_against_the_name():
    # 18cl named, 20cl in the column: 2cl is 11% of 18, over the 10% tolerance.
    assert GROUPER.capacity_mismatch(member("1", "Mozart Cup 18cl", capacity_brim=brim(20)))


def test_an_accepted_sku_is_not_checked():
    unchecked, accepted = copy.deepcopy(ARTIS), copy.deepcopy(ARTIS)
    unchecked["variant_axes"]["capacity"]["name_check"]["accept"] = {}
    accepted["variant_axes"]["capacity"]["name_check"]["accept"] = {"14-00-017": "checked against the sample"}
    m = member("14-00-017", "Michelangelo Masterpiece Liqueur 7cl [2.5oz]", capacity_brim=brim(23))
    assert Grouper(unchecked).capacity_mismatch(m) and Grouper(accepted).capacity_mismatch(m) is None


def test_the_name_check_ignores_a_node_without_capacity():
    p = plan_of(member("1", "Liqueur 7cl", capacity_brim=brim(23), size=size("8cm", 8.0)), node=PLATE)
    assert len(p.new_products) == 1 and not p.conflicts


# ---------------------------------------------------------- litre labels

@pytest.mark.parametrize("column, label", [
    (brim("118.29", 1182.9), "1.18L"),
    (brim(100, 1000), "1L"),
    (brim(250, 2500), "2.5L"),
    (ml(1775, "ml"), "1.78L"),                  # half up
    (brim("99.5", 995), "99.5cl"),              # under a litre: the supplier's own figure
    (ml(400, "ml"), "400ml"),
])
def test_a_litre_or_more_reads_in_litres(column, label):
    cap = GROUPER.options(member("1", "x", capacity_brim=column), GLASS.variant_axes)["capacity"]
    assert cap.value == label and cap.canonical_value == Decimal(str(column["canonical_value"]))


def test_the_litre_label_keeps_the_line_mark():
    o = GROUPER.options(member("1", "x", capacity_brim=brim("118.29", 1182.9), line_mark=line("(CE@1ltr)")),
                        GLASS.variant_axes)
    assert o["capacity"].label == "1.18L (CE@1ltr)"


def test_two_capacities_that_round_to_one_label_are_a_conflict():
    ms, os_ = options_for(member("1", "x", capacity_brim=brim("118.29", 1182.9)),
                          member("2", "x", capacity_brim=brim(118, 1180)))
    assert resolve_group(ms, os_, GLASS.variant_axes) == "1 and 2 would both show as 1.18L"


# ------------------------------------------------------ a silent colour

GLASS_ = {"raw": "Glass", "status": "pending_review", "canonical": ["glass"]}
STEEL = {"raw": "Steel", "status": "pending_review", "canonical": ["steel"]}
CERAMIC = {"raw": "Ceramic", "status": "pending_review", "canonical": ["ceramic"]}


def test_a_silent_member_of_a_coloured_glass_group_is_clear():
    p = plan_of(member("1", "Ouessant Rocks 26cl", capacity_brim=brim(26), material=GLASS_),
                member("2", "Ouessant Rocks Blue 26cl", capacity_brim=brim(26), material=GLASS_,
                       colour=colour("blue")),
                member("3", "Ouessant Rocks Olive 26cl", capacity_brim=brim(26), material=GLASS_,
                       colour=colour("green")))
    (product,) = p.new_products
    assert [(m.sku, [o.label for o in o_.values()]) for m, o_ in product.members] == [
        ("1", ["26cl", "Clear"]), ("2", ["26cl", "Blue"]), ("3", ["26cl", "Green"])]
    assert [o["colour"].defaulted for _, o in product.members] == [True, False, False]
    assert list(product.members[0][1]) == ["capacity", "colour"]    # the node's axis order


@pytest.mark.parametrize("silent_material, coloured_material, silent_colour", [
    (CERAMIC, CERAMIC, None),                    # Clear is never inferred for a mug
    (GLASS_, STEEL, None),                       # nor across materials
    (GLASS_, GLASS_, colour("natural", "grey")), # two colour words are ambiguous, not silent
    (None, None, None),                          # no material, no default
])
def test_clear_is_not_inferred_elsewhere(silent_material, coloured_material, silent_colour):
    silent = {"material": silent_material} if silent_material else {}
    coloured = {"material": coloured_material} if coloured_material else {}
    if silent_colour:
        silent["colour"] = silent_colour
    p = plan_of(member("1", "Pitcher 1.9ltr", capacity_brim=brim(190, 1900), **silent),
                member("2", "Copper Pitcher 1.9ltr", capacity_brim=brim(190, 1900), colour=colour("copper"),
                       **coloured))
    assert not p.new_products and p.conflicts[0].reason.startswith("no colour on 1")


def test_a_glass_group_with_no_colours_gets_none():
    p = plan_of(member("1", "Wine 32cl", capacity_brim=brim(32), material=GLASS_),
                member("2", "Wine 41cl", capacity_brim=brim(41), material=GLASS_))
    assert all("colour" not in o for _, o in p.new_products[0].members)


def test_a_coloured_joiner_cannot_give_existing_variants_a_colour():
    linked = member("1", "Cidra Double Old Fashioned 39cl", capacity_brim=brim(39), material=GLASS_, product_id=7)
    blue = member("2", "Cidra Double Old Fashioned Blue 39cl", capacity_brim=brim(39), material=GLASS_,
                  colour=colour("blue"))
    p = build_plan(GROUPER, [linked, blue], {linked.category_raw: GLASS}, {7: GLASS})
    assert not p.joiners and "existing variants have no colour" in p.conflicts[0].reason


def test_a_silent_joiner_of_a_coloured_product_is_clear():
    linked = member("1", "Cidra Double Old Fashioned Blue 39cl", capacity_brim=brim(39), material=GLASS_,
                    colour=colour("blue"), product_id=7)
    plain = member("2", "Cidra Double Old Fashioned 39cl", capacity_brim=brim(39), material=GLASS_)
    (joiner,) = build_plan(GROUPER, [linked, plain], {linked.category_raw: GLASS}, {7: GLASS}).joiners
    assert joiner.options["colour"].value == "Clear" and joiner.options["colour"].defaulted


def test_an_unknown_default_colour_raises():
    config = copy.deepcopy(ARTIS)
    config["variant_axes"]["colour"]["absent_in_group"]["value"] = "transparent"
    with pytest.raises(LinkError, match="transparent"):
        Grouper(config)


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


def test_a_capacity_mismatch_is_queued_once_and_its_group_is_still_placed(conn):
    add(conn, "01", "Carafe 15cl", capacity_brim=brim(15))
    add(conn, "02", "Carafe 25cl", capacity_brim=brim(32))
    add(conn, "03", "Carafe 1.2ltr", capacity_brim=brim("118.29", 1182.9))
    first = run(conn, config(), dry_run=False)
    second = run(conn, config(), dry_run=False)
    assert (first["written"]["review_items"], second["written"]["review_items"]) == (1, 0)
    queued = conn.execute(sa.text(
        "SELECT issue_type, notes FROM ops.review_queue q JOIN supplier.supplier_products p "
        "ON p.supplier_product_id = q.entity_id WHERE p.supplier_sku = '02'")).one()
    assert queued.issue_type == "capacity_name_mismatch" and queued.notes.startswith("02: the name says 25cl")
    values = conn.execute(sa.text(
        "SELECT p.supplier_sku, o.value, o.canonical_value FROM catalog.variant_options o "
        "JOIN link.variant_links l USING (variant_id) JOIN supplier.supplier_products p USING (supplier_product_id) "
        "WHERE p.supplier_sku IN ('01', '02', '03') ORDER BY 1")).all()
    assert [tuple(v) for v in values] == [("01", "15cl", Decimal("150.0")), ("03", "1.18L", Decimal("1182.9"))]


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
