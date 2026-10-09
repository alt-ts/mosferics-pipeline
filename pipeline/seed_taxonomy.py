"""Seed catalog.taxonomy from config/taxonomy/taxonomy_v4.json.

The one-off load that unblocks SKU allocation: `catalog.next_mosferics_sku()`
raises until a level-1 node carries the department prefix. It is an insert,
not a migration — the table already exists (0005).

After this runs the database is the source of truth for the tree; the JSON is
the record of what was first loaded (and what a clean rebuild loads), not a
second copy to keep in step. So the seed refuses a table that already has rows.

    python -m pipeline.seed_taxonomy --dry-run     # print the rows, touch nothing
    python -m pipeline.seed_taxonomy               # load into DATABASE_URL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv

SEED_FILE = Path(__file__).resolve().parent.parent / "config" / "taxonomy" / "taxonomy_v4.json"


@dataclass(frozen=True)
class Node:
    """One catalog.taxonomy row, addressed by path until it has a category_id."""

    path: str
    parent_path: str | None
    name: str
    slug: str
    variant_axes: list[str] | None
    sku_prefix: str | None

    @property
    def level(self) -> int:
        return self.path.count("/") + 1


def slugify(name: str) -> str:
    """Shopify-handle style: ASCII-fold, lowercase, runs of anything else -> '-'."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if not slug:
        raise ValueError(f"taxonomy name {name!r} produces an empty slug")
    return slug


def build_nodes(tree: list[dict]) -> list[Node]:
    """Flatten the seed tree into rows, parents before children.

    Departments carry `sku_prefix` and no axes. A category carries its axes in
    option order ([] = explicitly none). A type is either a bare name, which
    inherits its category's axes, or an object that overrides them — so every
    node a product can be mapped to answers "what are my axes?" on its own row.
    """
    nodes: list[Node] = []
    for dept in tree:
        d = Node(slugify(dept["name"]), None, dept["name"], slugify(dept["name"]),
                 None, dept["sku_prefix"])
        nodes.append(d)
        for cat in dept["children"]:
            c = _child(d, cat["name"], list(cat["variant_axes"]))
            nodes.append(c)
            for leaf in cat["children"]:
                if isinstance(leaf, str):
                    nodes.append(_child(c, leaf, list(c.variant_axes)))
                else:
                    nodes.append(_child(c, leaf["name"], list(leaf["variant_axes"])))
    _validate(nodes)
    return nodes


def _child(parent: Node, name: str, axes: list[str]) -> Node:
    slug = slugify(name)
    return Node(f"{parent.path}/{slug}", parent.path, name, slug, axes, None)


def _validate(nodes: list[Node]) -> None:
    """Raise on anything the table's constraints would reject, before any write."""
    paths = [n.path for n in nodes]
    dupes = sorted({p for p in paths if paths.count(p) > 1})
    if dupes:
        raise ValueError(f"duplicate taxonomy paths (sibling slug collision): {dupes}")
    prefixes = [n.sku_prefix for n in nodes if n.level == 1]
    for prefix in prefixes:
        if not re.fullmatch(r"[A-Z]{3}", prefix or ""):
            raise ValueError(f"department sku_prefix {prefix!r} is not three capitals")
    if len(set(prefixes)) != len(prefixes):
        raise ValueError(f"duplicate department sku_prefix in {prefixes}")
    for n in nodes:
        if n.level > 1:
            if len(n.variant_axes) > 3:
                raise ValueError(f"{n.path}: {len(n.variant_axes)} axes exceeds Shopify's 3 options")
            if len(set(n.variant_axes)) != len(n.variant_axes):
                raise ValueError(f"{n.path}: repeated variant axis in {n.variant_axes}")


def load_tree(seed_file: Path = SEED_FILE) -> list[Node]:
    return build_nodes(json.loads(seed_file.read_text(encoding="utf-8")))


def seed(conn: sa.Connection, nodes: list[Node]) -> int:
    """Insert every node. Raises if catalog.taxonomy is not empty."""
    existing = conn.execute(sa.text("SELECT count(*) FROM catalog.taxonomy")).scalar_one()
    if existing:
        raise RuntimeError(
            f"catalog.taxonomy already holds {existing} rows; the seed only loads an "
            "empty table. Restructure the live tree in place instead."
        )
    insert = sa.text(
        "INSERT INTO catalog.taxonomy (parent_id, name, slug, path, variant_axes, sku_prefix) "
        "VALUES (:parent_id, :name, :slug, :path, :variant_axes, :sku_prefix) "
        "RETURNING category_id"
    )
    ids: dict[str, int] = {}
    for n in nodes:
        ids[n.path] = conn.execute(insert, {
            "parent_id": ids[n.parent_path] if n.parent_path else None,
            "name": n.name,
            "slug": n.slug,
            "path": n.path,
            "variant_axes": n.variant_axes,
            "sku_prefix": n.sku_prefix,
        }).scalar_one()
    return len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the rows and exit")
    args = parser.parse_args()

    nodes = load_tree()
    if args.dry_run:
        for n in nodes:
            print(f"{n.level}  {n.sku_prefix or '   '}  {n.path}  {n.variant_axes}  {n.name!r}")
        print(f"{len(nodes)} nodes")
        return

    load_dotenv()
    engine = sa.create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        count = seed(conn, nodes)
    print(f"seeded {count} taxonomy nodes into {engine.url.database}")


if __name__ == "__main__":
    main()
