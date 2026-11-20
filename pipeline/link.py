"""Place supplier products in the catalog: category, grouping, variants, links.

The fourth pipeline step. For every supplier product without a link it

1. finds the category through link.category_map (an unmapped string is left
   alone: review.unmapped_categories already reports it, heaviest first);
2. proposes a group: SKUs whose brand, category string, range and name stem
   all match are one product, with the stripped-out values as its options
   (`grouping` in the supplier YAML);
3. creates the product, one variant and SKU per supplier product, and the
   variant's option values (catalog.variant_options);
4. links each supplier product to its variant (hard rule 1).

A product of one SKU is the auto-created 1:1 link: written `confirmed` and
`is_preferred`, signed 'pipeline'. A group of several is a proposal: its links
are `proposed`, and a human confirming them confirms the group. Until then no
link in it is preferred, so price.py has nothing to price it from.

Grouping never invents a combination (hard rule 10). A group becomes one
product only if an axis any member has a value on, every member has a value
on, and no two members share all their values. Otherwise none of it is created:
each member goes to ops.review_queue as `grouping_conflict` and the next run
tries again. A SKU matching a product created on an earlier run joins that
product the same way, as a proposed link.

Titles are interim: the name stem, until per-node templates exist. The handle
is slugged from it once, at creation, and never regenerated (hard rule 8).

Idempotent: a supplier product with a link is never placed again, so a re-run
reuses its variant and SKU.

    python -m pipeline.link artis --dry-run [--report groups.csv]   # plan, write nothing
    python -m pipeline.link artis                                   # write
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from dotenv import load_dotenv

from pipeline.config import load_supplier_config
from pipeline.extract import VOCABULARY_DIR, load_aliases, whole_words
from pipeline.seed_taxonomy import slugify

PIPELINE = "pipeline"
ISSUE_GROUPING_CONFLICT = "grouping_conflict"

# Where normalise.py leaves each value a `variant_axes` reference can name.
AXIS_SOURCES = {
    "capacity.brim": "capacity_brim",
    "capacity.line_mark": "line_mark",
    "size": "size",
    "vocabulary.colour": "colour",
}
GROUP_FIELDS = {"brand", "supplier_category_raw", "name_stem"}


class LinkError(Exception):
    """The supplier config or the database is not in a state link.py can work from."""


@dataclass(frozen=True)
class Member:
    """One supplier product, as link.py needs it."""

    supplier_product_id: int
    sku: str
    name: str
    brand: str | None
    category_raw: str | None
    specs: dict[str, Any]
    pack: Decimal | None                 # base_units_per_pack of the current offer
    product_id: int | None = None        # set when already linked


@dataclass(frozen=True)
class Option:
    axis: str
    value: str
    canonical_value: Decimal | None = None
    unit: str | None = None
    qualifier: str | None = None

    @property
    def identity(self) -> tuple:
        """What makes two values the same option: 40cl and 400ml are one capacity."""
        amount = self.canonical_value if self.canonical_value is not None else self.value.casefold()
        return (amount, self.qualifier)

    @property
    def label(self) -> str:
        return f"{self.value} {self.qualifier}" if self.qualifier else self.value


@dataclass(frozen=True)
class Node:
    category_id: int
    path: str
    variant_axes: tuple[str, ...]
    dept_prefix: str


@dataclass
class NewProduct:
    title: str
    node: Node
    brand: str | None
    members: list[tuple[Member, dict[str, Option]]]

    @property
    def grouped(self) -> bool:
        return len(self.members) > 1


@dataclass(frozen=True)
class Joiner:
    product_id: int
    node: Node
    member: Member
    options: dict[str, Option]


@dataclass(frozen=True)
class Conflict:
    members: tuple[Member, ...]
    reason: str


@dataclass
class Plan:
    new_products: list[NewProduct] = field(default_factory=list)
    joiners: list[Joiner] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    unmapped: list[Member] = field(default_factory=list)
    already_linked: int = 0

    def summary(self) -> dict[str, int]:
        sizes = Counter(len(p.members) for p in self.new_products)
        return {
            "products": len(self.new_products),
            "single_sku_products": sizes[1],
            "grouped_products": sum(n for k, n in sizes.items() if k > 1),
            "grouped_skus": sum(k * n for k, n in sizes.items() if k > 1),
            "skus_joining_existing": len(self.joiners),
            "skus_in_conflict": sum(len(c.members) for c in self.conflicts),
            "skus_unmapped_category": len(self.unmapped),
            "skus_already_linked": self.already_linked,
        }


class Grouper:
    """The supplier's grouping and variant-axis rules, compiled."""

    def __init__(self, config: dict, vocabulary_dir: Path = VOCABULARY_DIR) -> None:
        code = config["supplier"]["code"]
        grouping = config.get("grouping")
        if not grouping:
            raise LinkError(f"{code}: no grouping section")
        self.key_fields = list(grouping["key"])
        for f in self.key_fields:
            if f not in GROUP_FIELDS and not f.startswith("specs."):
                raise LinkError(f"{code}: grouping.key {f!r} is not one of {sorted(GROUP_FIELDS)} or specs.*")
        self.strip = [re.compile(p, re.IGNORECASE) for p in grouping.get("strip_from_name", [])]
        words = grouping.get("size_words", [])
        self.size_words = whole_words(words) if words else None

        line_mark = config.get("capacity", {}).get("line_mark")
        self.line_mark_block = re.compile(line_mark["block"]) if line_mark else None

        colour = config.get("vocabulary", {}).get("colour")
        self.colour_pattern = self.colour_ignored = None
        self.colour_display: dict[str, str] = {}
        if colour:
            aliases = load_aliases("colour", colour.get("ignore_aliases", []), vocabulary_dir)
            phrases = colour.get("ignore_phrases", [])
            # One alternation, ignored phrases first, so "Red Wine" is kept whole
            # where "Red" alone would have matched at the same position.
            ordered = sorted(phrases, key=len, reverse=True) + sorted(aliases, key=len, reverse=True)
            self.colour_pattern = re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(p) for p in ordered) + r")(?!\w)", re.IGNORECASE)
            self.colour_ignored = {p.casefold() for p in phrases}
            entries = yaml.safe_load((vocabulary_dir / "colour.yaml").read_text(encoding="utf-8"))
            self.colour_display = {k: v.get("display", k) for k, v in entries.items()}

        self.axes: dict[str, dict[str, str]] = {}
        for axis, rule in (config.get("variant_axes") or {}).items():
            for role in ("value", "qualifier"):
                if role in rule and rule[role] not in AXIS_SOURCES:
                    raise LinkError(f"{code}: variant_axes.{axis}.{role} {rule[role]!r} is not one of "
                                    f"{sorted(AXIS_SOURCES)}")
            self.axes[axis] = rule

        base_unit = config.get("base_unit")
        if not base_unit:
            raise LinkError(f"{code}: no base_unit; catalog.products.base_unit is required")
        self.base_unit = str(base_unit)

    def axes_for(self, node: Node) -> tuple[str, ...]:
        """The node's axes this supplier can give a value on, in the node's order."""
        return tuple(a for a in node.variant_axes if a in self.axes)

    def stem(self, name: str) -> str:
        """The name with every option value taken out: what the group shares."""
        if self.line_mark_block:
            name = self.line_mark_block.sub(" ", name)
        for pattern in self.strip:
            name = pattern.sub(" ", name)
        if self.colour_pattern:
            name = self.colour_pattern.sub(
                lambda m: m.group(0) if m.group(0).casefold() in self.colour_ignored else " ", name)
        if self.size_words:
            name = self.size_words.sub(" ", name)
        name = re.sub(r"\(\s*\)", " ", name)
        return re.sub(r"\s+", " ", name).strip(" -,/+&")

    def key(self, m: Member) -> tuple:
        parts = []
        for f in self.key_fields:
            if f == "brand":
                parts.append(m.brand)
            elif f == "supplier_category_raw":
                parts.append(m.category_raw)
            elif f == "name_stem":
                parts.append(self.stem(m.name))
            else:
                parts.append(m.specs.get(f.removeprefix("specs.")))
        return tuple(parts)

    def options(self, m: Member, axes: tuple[str, ...]) -> dict[str, Option]:
        """The member's value on each of the node's axes that has one."""
        found = {}
        for axis in axes:
            rule = self.axes.get(axis)
            if rule is None:
                continue
            option = self._value(axis, AXIS_SOURCES[rule["value"]], m)
            if option is None:
                continue
            if "qualifier" in rule:
                q = m.specs.get(AXIS_SOURCES[rule["qualifier"]])
                if q:
                    option = Option(option.axis, option.value, option.canonical_value, option.unit, q["raw"])
            found[axis] = option
        return found

    def _value(self, axis: str, key: str, m: Member) -> Option | None:
        v = m.specs.get(key)
        if key == "capacity_brim" and v:
            return Option(axis, f"{v['value']}{v['source_unit']}", Decimal(str(v["canonical_value"])), v["unit"])
        if key == "size":
            if v:
                dims = v.get("dimensions") or []
                if len(dims) == 1:
                    return Option(axis, v["raw"], Decimal(str(dims[0])), v["unit"])
                return Option(axis, v["raw"])
            word = self.size_words.search(m.name) if self.size_words else None
            return Option(axis, word.group(0).title()) if word else None
        if key == "colour" and v:
            candidates = v.get("candidates") or []
            # Two colours in one name is ambiguous, and a guess here would be a
            # variant nobody can fulfil. Treated as no value.
            if len(candidates) == 1:
                return Option(axis, self.colour_display.get(candidates[0], candidates[0]))
        return None


def resolve_group(members: list[Member], options: list[dict[str, Option]], axes: tuple[str, ...]) -> str | None:
    """None if the members can be one product exactly as the data stands, else why not."""
    if len(members) == 1:
        return None
    # Every variant of a Shopify product needs a value on every option, so an
    # axis is all-or-nothing within a group: a blank would have to be invented.
    for axis in axes:
        have = [m.sku for m, o in zip(members, options) if axis in o]
        if have and len(have) != len(members):
            missing = sorted(m.sku for m, o in zip(members, options) if axis not in o)
            return f"no {axis} on {', '.join(missing)}, which other SKUs in the group have"
    seen: dict[tuple, str] = {}
    for m, o in zip(members, options):
        combo = tuple(o[a].identity if a in o else None for a in axes)
        if combo in seen:
            return f"{seen[combo]} and {m.sku} have the same options; nothing tells them apart"
        seen[combo] = m.sku
    return None


def build_plan(grouper: Grouper, members: list[Member], category_of: dict[str, Node],
               product_nodes: dict[int, Node]) -> Plan:
    """Decide everything in memory. No database access, so it is testable whole."""
    plan = Plan()
    existing: dict[tuple, set[int]] = defaultdict(set)
    existing_members: dict[int, list[Member]] = defaultdict(list)
    pending: dict[tuple, list[Member]] = defaultdict(list)
    for m in sorted(members, key=lambda m: m.sku):
        if m.product_id is not None:
            plan.already_linked += 1
            existing[grouper.key(m)].add(m.product_id)
            existing_members[m.product_id].append(m)
        elif m.category_raw is None or m.category_raw not in category_of:
            plan.unmapped.append(m)
        else:
            pending[grouper.key(m)].append(m)

    for key, group in pending.items():
        node = category_of[group[0].category_raw]
        products = existing.get(key, set())
        if len(products) > 1:
            plan.conflicts.append(Conflict(tuple(group), f"matches {len(products)} existing products "
                                                         f"({sorted(products)}); a human has split this group"))
            continue
        if products:
            (product_id,) = products
            pnode = product_nodes[product_id]
            axes = grouper.axes_for(pnode)
            everyone = existing_members[product_id] + group
            options = [grouper.options(m, axes) for m in everyone]
            reason = resolve_group(everyone, options, axes) if axes else "its category has no variant axis"
            if reason:
                plan.conflicts.append(Conflict(tuple(group), f"cannot join product {product_id}: {reason}"))
                continue
            for m, o in zip(group, options[len(existing_members[product_id]):]):
                plan.joiners.append(Joiner(product_id, pnode, m, o))
            continue
        axes = grouper.axes_for(node)
        if not axes:
            # A node with no axis is one variant per product (taxonomy rule), so
            # there is nothing to group on: every SKU stands alone.
            for m in group:
                plan.new_products.append(NewProduct(m.name, node, m.brand, [(m, {})]))
            continue
        options = [grouper.options(m, axes) for m in group]
        reason = resolve_group(group, options, axes)
        if reason:
            plan.conflicts.append(Conflict(tuple(group), reason))
            continue
        if len(group) == 1:
            # One variant, so its options are never shown: the full name keeps them.
            plan.new_products.append(NewProduct(group[0].name, node, group[0].brand, [(group[0], options[0])]))
            continue
        title = grouper.stem(group[0].name)
        if not title:
            plan.conflicts.append(Conflict(tuple(group), "the name stem is empty once options are removed"))
            continue
        plan.new_products.append(NewProduct(title, node, group[0].brand, list(zip(group, options))))
    return plan


# ------------------------------------------------------------------ database

def load_members(conn: sa.Connection, supplier_id: int) -> list[Member]:
    rows = conn.execute(sa.text("""
        SELECT sp.supplier_product_id, sp.supplier_sku, coalesce(sp.name_clean, sp.name_raw) AS name,
               sp.brand, sp.supplier_category_raw, sp.specs, o.base_units_per_pack,
               (SELECT v.product_id FROM link.variant_links l JOIN catalog.variants v USING (variant_id)
                 WHERE l.supplier_product_id = sp.supplier_product_id AND l.status <> 'rejected'
                 ORDER BY l.status = 'confirmed' DESC LIMIT 1) AS product_id,
               EXISTS (SELECT 1 FROM link.variant_links l
                        WHERE l.supplier_product_id = sp.supplier_product_id) AS has_link
        FROM supplier.supplier_products sp
        LEFT JOIN supplier.supplier_offers o ON o.supplier_product_id = sp.supplier_product_id AND o.is_current
        WHERE sp.supplier_id = :s AND NOT sp.discontinued
    """), {"s": supplier_id}).all()
    members = []
    for r in rows:
        if r.has_link and r.product_id is None:
            # Every link on it was rejected. Placing it again would undo that
            # decision, so it waits for a human instead.
            continue
        members.append(Member(r.supplier_product_id, r.supplier_sku, r.name, r.brand, r.supplier_category_raw,
                              r.specs, r.base_units_per_pack, r.product_id))
    return members


def load_nodes(conn: sa.Connection) -> dict[int, Node]:
    rows = {r.category_id: r for r in conn.execute(sa.text(
        "SELECT category_id, parent_id, path, variant_axes, sku_prefix FROM catalog.taxonomy"))}
    nodes = {}
    for cid, r in rows.items():
        root = r
        while root.parent_id is not None:
            root = rows[root.parent_id]
        if not root.sku_prefix:
            raise LinkError(f"department {root.path!r} has no sku_prefix")
        nodes[cid] = Node(cid, r.path, tuple(r.variant_axes or ()), root.sku_prefix)
    return nodes


def load_plan(conn: sa.Connection, config: dict, grouper: Grouper) -> tuple[int, Plan]:
    code = config["supplier"]["code"]
    if config.get("classification_method") != "mapped_column":
        raise LinkError(f"{code}: classification_method {config.get('classification_method')!r} "
                        "is not built yet; only mapped_column is")
    supplier_id = conn.execute(sa.text("SELECT supplier_id FROM supplier.suppliers WHERE code = :c"),
                               {"c": code}).scalar_one_or_none()
    if supplier_id is None:
        raise LinkError(f"no supplier with code {code!r}")
    nodes = load_nodes(conn)
    category_of = {raw: nodes[cid] for raw, cid in conn.execute(sa.text(
        "SELECT supplier_category_raw, category_id FROM link.category_map WHERE supplier_id = :s"),
        {"s": supplier_id})}
    product_nodes = {pid: nodes[cid] for pid, cid in conn.execute(sa.text(
        "SELECT product_id, category_id FROM catalog.products"))}
    return supplier_id, build_plan(grouper, load_members(conn, supplier_id), category_of, product_nodes)


def write_plan(conn: sa.Connection, plan: Plan, base_unit: str) -> dict[str, int]:
    """Write the plan. The caller owns the transaction."""
    handles = set(conn.execute(sa.text("SELECT handle FROM catalog.products")).scalars())
    brands = dict(conn.execute(sa.text("SELECT name, brand_id FROM catalog.brands")).all())
    brand_slugs = set(conn.execute(sa.text("SELECT slug FROM catalog.brands")).scalars())
    counts = Counter()

    def brand_id(name: str | None) -> int | None:
        if name is None:
            return None
        if name not in brands:
            slug = _unique(slugify(name), brand_slugs)
            brands[name] = conn.execute(sa.text(
                "INSERT INTO catalog.brands (name, slug) VALUES (:n, :s) RETURNING brand_id"),
                {"n": name, "s": slug}).scalar_one()
            counts["brands"] += 1
        return brands[name]

    for p in plan.new_products:
        handle = _unique(slugify(p.title), handles)
        product_id = conn.execute(sa.text("""
            INSERT INTO catalog.products (title, handle, category_id, base_unit, brand_id)
            VALUES (:title, :handle, :category_id, :base_unit, :brand_id) RETURNING product_id
        """), {"title": p.title, "handle": handle, "category_id": p.node.category_id,
               "base_unit": base_unit, "brand_id": brand_id(p.brand)}).scalar_one()
        counts["products"] += 1
        for m, options in p.members:
            _write_variant(conn, product_id, p.node, m, options, confirmed=not p.grouped,
                           titled=p.grouped)
            counts["variants"] += 1

    for j in plan.joiners:
        _write_variant(conn, j.product_id, j.node, j.member, j.options, confirmed=False, titled=True)
        counts["variants"] += 1

    counts["review_items"] = _write_conflicts(conn, plan.conflicts)
    return dict(counts)


def _write_variant(conn: sa.Connection, product_id: int, node: Node, m: Member,
                   options: dict[str, Option], *, confirmed: bool, titled: bool) -> None:
    # Option order is the node's axis order (taxonomy.variant_axes), never the supplier's.
    ordered = [options[a] for a in node.variant_axes if a in options]
    pack = int(m.pack) if m.pack is not None and m.pack == int(m.pack) else None
    variant_id = conn.execute(sa.text("""
        INSERT INTO catalog.variants (product_id, variant_title, mosferics_sku, pack_size, base_units_per_variant)
        VALUES (:product_id, :title, catalog.next_mosferics_sku(:dept), :pack, :units) RETURNING variant_id
    """), {"product_id": product_id, "title": " / ".join(o.label for o in ordered) if titled and ordered else None,
           "dept": node.dept_prefix, "pack": pack, "units": m.pack}).scalar_one()
    for o in ordered:
        conn.execute(sa.text("""
            INSERT INTO catalog.variant_options (variant_id, axis, value, canonical_value, unit, qualifier)
            VALUES (:v, :axis, :value, :canonical, :unit, :qualifier)
        """), {"v": variant_id, "axis": o.axis, "value": o.value, "canonical": o.canonical_value,
               "unit": o.unit, "qualifier": o.qualifier})
    if confirmed:
        # The auto-created 1:1 link. Written confirmed, signed by name with
        # both columns (the pairing check), and preferred from birth.
        conn.execute(sa.text("""
            INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, match_confidence,
                                            status, confirmed_by, confirmed_at, is_preferred)
            VALUES (:sp, :v, 'identical', 1.0, 'confirmed', :by, now(), true)
        """), {"sp": m.supplier_product_id, "v": variant_id, "by": PIPELINE})
    else:
        # A grouping proposal. Confidence is left null: the SKU is its variant
        # exactly; what is uncertain is the group, and status says that.
        conn.execute(sa.text("""
            INSERT INTO link.variant_links (supplier_product_id, variant_id, match_type, status)
            VALUES (:sp, :v, 'identical', 'proposed')
        """), {"sp": m.supplier_product_id, "v": variant_id})


def _write_conflicts(conn: sa.Connection, conflicts: list[Conflict]) -> int:
    """One open item per member, not raised again while the same reason stands."""
    existing = {(q.entity_id, q.notes) for q in conn.execute(sa.text(
        "SELECT entity_id, notes FROM ops.review_queue WHERE entity_type = 'supplier_product' "
        "AND issue_type = :t"), {"t": ISSUE_GROUPING_CONFLICT})}
    inserted = 0
    for c in conflicts:
        skus = ", ".join(m.sku for m in c.members)
        for m in c.members:
            notes = f"{m.sku}: grouped with {skus}: {c.reason}"
            if (m.supplier_product_id, notes) in existing:
                continue
            conn.execute(sa.text(
                "INSERT INTO ops.review_queue (entity_type, entity_id, issue_type, notes) "
                "VALUES ('supplier_product', :id, :t, :notes)"),
                {"id": m.supplier_product_id, "t": ISSUE_GROUPING_CONFLICT, "notes": notes})
            inserted += 1
    return inserted


def _unique(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate


def write_report(plan: Plan, path: Path) -> None:
    """Every SKU the plan touches, one row each, for reviewing groups before a live run."""
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["outcome", "product", "category", "sku", "name", "options", "link_status", "reason"])
        for p in sorted(plan.new_products, key=lambda p: (-len(p.members), p.title)):
            for m, o in p.members:
                w.writerow(["grouped" if p.grouped else "single", p.title, p.node.path, m.sku, m.name,
                            " / ".join(x.label for x in o.values()),
                            "proposed" if p.grouped else "confirmed", ""])
        for j in plan.joiners:
            w.writerow(["joins", j.product_id, "", j.member.sku, j.member.name,
                        " / ".join(x.label for x in j.options.values()), "proposed", ""])
        for c in plan.conflicts:
            for m in c.members:
                w.writerow(["conflict", "", "", m.sku, m.name, "", "", c.reason])
        for m in plan.unmapped:
            w.writerow(["unmapped", "", m.category_raw or "", m.sku, m.name, "", "", ""])


def run(conn: sa.Connection, config: dict, *, dry_run: bool, report: Path | None = None) -> dict[str, Any]:
    grouper = Grouper(config)
    _, plan = load_plan(conn, config, grouper)
    if report:
        write_report(plan, report)
    result: dict[str, Any] = {"plan": plan.summary()}
    if not dry_run:
        result["written"] = write_plan(conn, plan, grouper.base_unit)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier")
    parser.add_argument("--dry-run", action="store_true", help="plan and report, write nothing")
    parser.add_argument("--report", type=Path, help="write every planned SKU to this CSV")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    with engine.begin() as conn:
        if args.dry_run:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        result = run(conn, config, dry_run=args.dry_run, report=args.report)
    print(json.dumps(result, indent=2))
    if args.dry_run:
        print("dry run: nothing written")


if __name__ == "__main__":
    main()
