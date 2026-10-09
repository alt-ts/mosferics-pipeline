import pytest

from pipeline.seed_taxonomy import build_nodes, load_tree, slugify


def test_slugify_folds_accents_and_punctuation():
    assert slugify("Frying & Sauté Pans") == "frying-saute-pans"
    assert slugify("Clothing & Workwear (PPE)") == "clothing-workwear-ppe"
    assert slugify("Paper/Takeaway Coffee Cups & Lids") == "paper-takeaway-coffee-cups-lids"


def test_v4_seed_shape():
    nodes = load_tree()
    by_level = {lvl: [n for n in nodes if n.level == lvl] for lvl in (1, 2, 3)}
    assert [n.sku_prefix for n in by_level[1]] == [
        "GLA", "TAB", "CUT", "BAR", "KIT", "GAS", "EQP", "DIS", "HYG", "CLO", "FUR", "FNB",
    ]
    assert all(n.variant_axes is None for n in by_level[1])
    assert all(n.sku_prefix is None and n.variant_axes is not None
               for n in by_level[2] + by_level[3])
    assert max(n.level for n in nodes) == 3


def test_leaf_inherits_axes_in_order_unless_it_overrides():
    by_path = {n.path: n for n in load_tree()}
    # two-axis node: order is the Shopify option order and must survive
    assert by_path["food-storage-gastronorm/gastronorm-pans-lids/stainless-gn"].variant_axes == [
        "gn_size", "depth"]
    gloves = "cleaning-hygiene-safety/cloths-wipes-gloves"
    assert by_path[gloves].variant_axes == []
    assert by_path[f"{gloves}/sponges-scourers"].variant_axes == []
    assert by_path[f"{gloves}/washing-up-disposable-gloves"].variant_axes == ["size"]


def test_sibling_slug_collision_raises():
    tree = [{"name": "Dept", "sku_prefix": "DEP", "children": [
        {"name": "Cups & Mugs", "variant_axes": [], "children": []},
        {"name": "Cups, Mugs", "variant_axes": [], "children": []},
    ]}]
    with pytest.raises(ValueError, match="duplicate taxonomy paths"):
        build_nodes(tree)
