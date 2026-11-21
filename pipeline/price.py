"""Choose each variant's source, cost it, and price it under the approval gate.

The fifth pipeline step, and two engines in one module (data model, "Where the
engines sit"):

Sourcing. For every variant with a confirmed link, the confirmed link whose
current offer is cheapest per base unit becomes `is_preferred`; ties go to the
shorter lead time, then to the link already preferred. Only confirmed links
compete (migration 0007), so a proposed group is not priced until a human has
agreed to it. A variant with confirmed links but no current offer on any of them
has no source: it goes to ops.review_queue as `no_preferred_source`.

Pricing. The preferred offer gives `pricing.cost_basis` (cost of the pack over
its base units). The variant's cost is that times `base_units_per_variant`: the
price is per variant, the cost per base unit, and a margin computed without the
factor is wrong in the flattering direction. One rule prices each variant,
most specific first:

    product > supplier-within-category > category > supplier > global

Category rules cascade down the tree, nearest node first, and a rule naming the
variant's supplier beats a generic one at any depth. `priority` breaks ties
within one scope; a tie it does not break raises, because two rules that both
claim a price are a mistake to fix, not a choice to make silently.

The gate (config/pricing.yaml). A new price always waits for a human. A change
to a live price under the materiality threshold, with margin at or above the
floor, is auto_approved by the pipeline; anything else waits. A live price whose
margin falls under the floor waits again, even if the price did not move. An
override is never touched: final_price = COALESCE(override_price,
calculated_price), and the floor applies to whichever is final.

The live margin is never stored (hard rule 7): margin_pct is the snapshot the
gate judged, and pricing.live_margins is the answer to "what is it now?".

Idempotent: a re-run with nothing changed writes nothing.

    python -m pipeline.price --dry-run [--report prices.csv]   # plan, write nothing
    python -m pipeline.price                                  # write
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from dotenv import load_dotenv

from pipeline.config import REPO_ROOT

PRICING_CONFIG = REPO_ROOT / "config" / "pricing.yaml"
PIPELINE = "pipeline"
LIVE = ("approved", "auto_approved")
PENDING = "pending_approval"
AUTO = "auto_approved"
ISSUE_NO_SOURCE = "no_preferred_source"
ISSUE_UNPRICEABLE = "unpriceable"
HUNDRED = Decimal(100)
COST_STEP = Decimal("0.000001")  # cost per base unit: stable across re-runs, far finer than a penny

# The order a rule's scope wins in. A lower rank beats a higher one.
RANK_PRODUCT, RANK_SUPPLIER_CATEGORY, RANK_CATEGORY, RANK_SUPPLIER, RANK_GLOBAL = range(5)
RANK_NAMES = {RANK_PRODUCT: "product", RANK_SUPPLIER_CATEGORY: "supplier-within-category",
              RANK_CATEGORY: "category", RANK_SUPPLIER: "supplier", RANK_GLOBAL: "global"}


class PriceError(ValueError):
    """The rules or the config cannot price the catalogue as written."""


# ------------------------------------------------------------------- config

@dataclass(frozen=True)
class Settings:
    materiality_pct: Decimal
    margin_floor_pct: Decimal
    vat_rate: Decimal
    rounding: Decimal


def load_settings(path: Path = PRICING_CONFIG) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = sorted({"materiality_pct", "margin_floor_pct", "vat_rate", "rounding", "prices_include_vat"} - set(raw))
    if missing:
        raise PriceError(f"{path.name}: missing {missing}")
    if raw["prices_include_vat"] is not False:
        raise PriceError(f"{path.name}: only prices_include_vat: false is supported")
    settings = Settings(*(Decimal(str(raw[k])) for k in ("materiality_pct", "margin_floor_pct", "vat_rate", "rounding")))
    if not (0 < settings.materiality_pct < 100 and 0 <= settings.margin_floor_pct < 100):
        raise PriceError(f"{path.name}: materiality_pct and margin_floor_pct must be percentages")
    if not (0 <= settings.vat_rate <= 1) or settings.rounding <= 0:
        raise PriceError(f"{path.name}: vat_rate is a fraction and rounding a positive step")
    return settings


# -------------------------------------------------------------------- rules

@dataclass(frozen=True)
class Rule:
    rule_id: int
    scope_type: str
    scope_id: int | None
    supplier_id: int | None
    method: str
    value: Decimal
    priority: int
    valid_from: datetime

    def describe(self) -> str:
        narrowed = f" (supplier {self.supplier_id})" if self.supplier_id else ""
        scope = self.scope_type if self.scope_id is None else f"{self.scope_type} {self.scope_id}"
        return f"rule {self.rule_id}: {scope}{narrowed} {self.method} {self.value}"


class RuleBook:
    """The rules in force, and the one that prices a given variant."""

    def __init__(self, rules: list[Rule], ancestors: dict[int, list[int]], now: datetime) -> None:
        self.ancestors = ancestors
        self.rules = [r for r in rules if r.valid_from <= now]
        if not any(r.scope_type == "global" for r in self.rules):
            raise PriceError("no global rule in force: a variant no other rule covers would have no price")

    def resolve(self, product_id: int, category_id: int, supplier_id: int) -> tuple[Rule, str]:
        """The winning rule and the scope it won at."""
        depth = {node: d for d, node in enumerate(self.ancestors.get(category_id, [category_id]))}
        candidates: list[tuple[int, int, Rule]] = []  # (rank, depth, rule)
        for r in self.rules:
            if r.scope_type == "product" and r.scope_id == product_id:
                candidates.append((RANK_PRODUCT, 0, r))
            elif r.scope_type == "category" and r.scope_id in depth:
                if r.supplier_id is None:
                    candidates.append((RANK_CATEGORY, depth[r.scope_id], r))
                elif r.supplier_id == supplier_id:
                    candidates.append((RANK_SUPPLIER_CATEGORY, depth[r.scope_id], r))
            elif r.scope_type == "supplier" and r.scope_id == supplier_id:
                candidates.append((RANK_SUPPLIER, 0, r))
            elif r.scope_type == "global":
                candidates.append((RANK_GLOBAL, 0, r))
        best_rank, best_depth = min((rank, d) for rank, d, _ in candidates)
        scope = [r for rank, d, r in candidates if (rank, d) == (best_rank, best_depth)]
        top = max(r.priority for r in scope)
        winners = [r for r in scope if r.priority == top]
        if len(winners) > 1:
            raise PriceError("rules tie and priority does not break it: "
                             + "; ".join(r.describe() for r in winners))
        return winners[0], RANK_NAMES[best_rank]


def apply_rule(rule: Rule, variant_cost: Decimal, step: Decimal) -> Decimal:
    if rule.method == "percent_markup":
        price = variant_cost * (1 + rule.value / HUNDRED)
    elif rule.method == "fixed_margin":
        price = variant_cost / (1 - rule.value / HUNDRED)
    elif rule.method == "target_price":
        price = rule.value
    else:  # the CHECK constraint makes this unreachable from the table
        raise PriceError(f"{rule.describe()}: unknown method")
    return price.quantize(step, rounding=ROUND_HALF_UP)


def margin_pct(price: Decimal, cost: Decimal) -> Decimal | None:
    if price <= 0:
        return None
    return ((price - cost) / price * HUNDRED).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------- plan

@dataclass(frozen=True)
class Candidate:
    supplier_product_id: int
    supplier_id: int
    supplier_sku: str
    is_preferred: bool
    offer_id: int | None
    cost_ex_vat: Decimal | None
    base_units_per_pack: Decimal | None
    lead_time_days: int | None

    @property
    def cost_per_base_unit(self) -> Decimal | None:
        if self.offer_id is None or not self.base_units_per_pack:
            return None
        return (self.cost_ex_vat / self.base_units_per_pack).quantize(COST_STEP, rounding=ROUND_HALF_UP)


@dataclass
class Variant:
    variant_id: int
    product_id: int
    category_id: int
    mosferics_sku: str
    title: str
    base_units_per_variant: Decimal | None
    candidates: list[Candidate] = field(default_factory=list)


@dataclass(frozen=True)
class PriceLine:
    variant: Variant
    source: Candidate
    channel_id: int
    rule: Rule
    scope: str
    variant_cost: Decimal
    calculated: Decimal
    override: Decimal | None
    final: Decimal
    margin: Decimal | None
    old_status: str | None
    old_calculated: Decimal | None
    status: str
    reason: str
    write: bool
    below_floor: bool


@dataclass
class Plan:
    preferred_changes: list[tuple[Variant, Candidate]] = field(default_factory=list)
    cost_basis: list[tuple[Variant, Candidate]] = field(default_factory=list)
    lines: list[PriceLine] = field(default_factory=list)
    issues: list[tuple[str, int, str, str]] = field(default_factory=list)  # entity_type, id, issue, notes
    not_confirmed: int = 0

    def summary(self) -> dict[str, int]:
        written = [l for l in self.lines if l.write]
        by_status = defaultdict(int)
        for l in written:
            by_status[l.status] += 1
        return {
            "variants_priced": len(self.lines),
            "preferred_set": len(self.preferred_changes),
            "cost_basis_written": len(self.cost_basis),
            "prices_written": len(written),
            "prices_unchanged": len(self.lines) - len(written),
            **{f"written_{s}": n for s, n in sorted(by_status.items())},
            "written_below_margin_floor": sum(l.below_floor for l in written),
            "variants_without_source": sum(i[2] == ISSUE_NO_SOURCE for i in self.issues),
            "variants_unpriceable": sum(i[2] == ISSUE_UNPRICEABLE for i in self.issues),
            "variants_awaiting_confirmation": self.not_confirmed,
        }


def choose_source(candidates: list[Candidate]) -> Candidate | None:
    """Cheapest per base unit, then shortest lead time, then the one already preferred."""
    costed = [c for c in candidates if c.cost_per_base_unit is not None]
    if not costed:
        return None
    return min(costed, key=lambda c: (c.cost_per_base_unit,
                                      c.lead_time_days if c.lead_time_days is not None else 10**6,
                                      not c.is_preferred, c.supplier_product_id))


def gate(*, old: dict | None, calculated: Decimal, override: Decimal | None, final: Decimal,
         margin: Decimal | None, settings: Settings) -> tuple[str, str, bool]:
    """(status, reason, write) for one variant on one channel."""
    below_floor = margin is None or margin < settings.margin_floor_pct
    floor_note = f"margin {margin}% under the {settings.margin_floor_pct}% floor" if margin is not None \
        else "no margin: price is zero"
    if old is None:
        return PENDING, "new price" + (f"; {floor_note}" if below_floor else ""), True
    moved = old["calculated_price"] != calculated
    live = old["status"] in LIVE
    if not moved:
        if live and below_floor:
            return PENDING, f"{floor_note}; the price did not move, the cost did", True
        return old["status"], "unchanged", False
    if override is not None:
        # A human price stands. The new calculated price is recorded beside it.
        if live and below_floor:
            return PENDING, f"override kept; {floor_note}", True
        return old["status"], "override kept; calculated price recorded", True
    if not live:
        return PENDING, f"recalculated while {old['status']}", True
    change = abs(calculated - old["calculated_price"]) / old["calculated_price"] * HUNDRED
    if below_floor:
        return PENDING, floor_note, True
    if change >= settings.materiality_pct:
        return PENDING, f"moved {change.quantize(Decimal('0.1'))}%, at or over {settings.materiality_pct}%", True
    return AUTO, f"moved {change.quantize(Decimal('0.1'))}%, under {settings.materiality_pct}%", True


def load_plan(conn: sa.Connection, settings: Settings, now: datetime | None = None) -> Plan:
    now = now or datetime.now(timezone.utc)
    rules = [Rule(**r._mapping) for r in conn.execute(sa.text(
        "SELECT rule_id, scope_type, scope_id, supplier_id, method, value, priority, valid_from FROM pricing.rules"))]
    parents = dict(conn.execute(sa.text("SELECT category_id, parent_id FROM catalog.taxonomy")).all())
    ancestors: dict[int, list[int]] = {}
    for node in parents:
        chain, cursor = [], node
        while cursor is not None:
            if cursor in chain:
                raise PriceError(f"catalog.taxonomy has a cycle through {cursor}")
            chain.append(cursor)
            cursor = parents.get(cursor)
        ancestors[node] = chain
    book = RuleBook(rules, ancestors, now)
    channels = conn.execute(sa.text("SELECT channel_id FROM ops.channels WHERE active ORDER BY channel_id")).scalars().all()
    if not channels:
        raise PriceError("no active channel in ops.channels: nothing to price for")

    variants: dict[int, Variant] = {}
    for r in conn.execute(sa.text("""
        SELECT v.variant_id, v.product_id, p.category_id, v.mosferics_sku, p.title, v.base_units_per_variant,
               l.supplier_product_id, sp.supplier_id, sp.supplier_sku, l.is_preferred,
               o.offer_id, o.cost_ex_vat, o.base_units_per_pack, o.lead_time_days
        FROM link.variant_links l
        JOIN catalog.variants v USING (variant_id)
        JOIN catalog.products p USING (product_id)
        JOIN supplier.supplier_products sp USING (supplier_product_id)
        LEFT JOIN supplier.supplier_offers o ON o.supplier_product_id = l.supplier_product_id AND o.is_current
        WHERE l.status = 'confirmed'
        ORDER BY v.variant_id, l.supplier_product_id
    """)):
        v = variants.setdefault(r.variant_id, Variant(r.variant_id, r.product_id, r.category_id, r.mosferics_sku,
                                                      r.title, r.base_units_per_variant))
        v.candidates.append(Candidate(r.supplier_product_id, r.supplier_id, r.supplier_sku, r.is_preferred,
                                      r.offer_id, r.cost_ex_vat, r.base_units_per_pack, r.lead_time_days))

    plan = Plan()
    plan.not_confirmed = conn.execute(sa.text("""
        SELECT count(*) FROM catalog.variants v
        WHERE NOT EXISTS (SELECT 1 FROM link.variant_links l WHERE l.variant_id = v.variant_id AND l.status = 'confirmed')
    """)).scalar_one()
    cost_basis = {r.variant_id: r for r in conn.execute(sa.text(
        "SELECT variant_id, cost_per_base_unit, source_offer_id FROM pricing.cost_basis"))}
    prices = {(r.variant_id, r.channel_id): r._asdict() for r in conn.execute(sa.text(
        "SELECT variant_id, channel_id, calculated_price, override_price, final_price, status FROM pricing.variant_prices"))}

    for v in variants.values():
        source = choose_source(v.candidates)
        if source is None:
            plan.issues.append(("variant_link", v.variant_id, ISSUE_NO_SOURCE,
                                f"{v.mosferics_sku}: no confirmed link has a current offer"))
            continue
        if not source.is_preferred:
            plan.preferred_changes.append((v, source))
        cpbu = source.cost_per_base_unit
        held = cost_basis.get(v.variant_id)
        if held is None or held.cost_per_base_unit != cpbu or held.source_offer_id != source.offer_id:
            plan.cost_basis.append((v, source))
        if not v.base_units_per_variant:
            plan.issues.append(("variant_price", v.variant_id, ISSUE_UNPRICEABLE,
                                f"{v.mosferics_sku}: no base_units_per_variant, so no variant cost"))
            continue
        if cpbu <= 0:
            plan.issues.append(("variant_price", v.variant_id, ISSUE_UNPRICEABLE,
                                f"{v.mosferics_sku}: offer {source.offer_id} costs nothing"))
            continue
        variant_cost = cpbu * v.base_units_per_variant
        rule, scope = book.resolve(v.product_id, v.category_id, source.supplier_id)
        calculated = apply_rule(rule, variant_cost, settings.rounding)
        for channel_id in channels:
            old = prices.get((v.variant_id, channel_id))
            override = old["override_price"] if old else None
            final = override if override is not None else calculated
            margin = margin_pct(final, variant_cost)
            status, reason, write = gate(old=old, calculated=calculated, override=override, final=final,
                                         margin=margin, settings=settings)
            plan.lines.append(PriceLine(v, source, channel_id, rule, scope, variant_cost, calculated, override,
                                        final, margin, old["status"] if old else None,
                                        old["calculated_price"] if old else None, status, reason, write,
                                        margin is None or margin < settings.margin_floor_pct))
    return plan


# -------------------------------------------------------------------- write

def write_plan(conn: sa.Connection, plan: Plan, settings: Settings) -> dict[str, int]:
    for v, source in plan.preferred_changes:
        # The trigger clears every other link on the variant in the same write.
        conn.execute(sa.text("UPDATE link.variant_links SET is_preferred = true "
                             "WHERE variant_id = :v AND supplier_product_id = :sp"),
                     {"v": v.variant_id, "sp": source.supplier_product_id})
    for v, source in plan.cost_basis:
        conn.execute(sa.text("""
            INSERT INTO pricing.cost_basis (variant_id, cost_per_base_unit, source_offer_id, computed_at)
            VALUES (:v, :c, :o, now())
            ON CONFLICT (variant_id) DO UPDATE SET cost_per_base_unit = EXCLUDED.cost_per_base_unit,
                source_offer_id = EXCLUDED.source_offer_id, computed_at = EXCLUDED.computed_at
        """), {"v": v.variant_id, "c": source.cost_per_base_unit, "o": source.offer_id})
    prices = 0
    for line in plan.lines:
        if not line.write:
            continue
        # Only the pipeline's columns. override_price is the reviewer's and never written
        # here; final_price is generated by the table as COALESCE(override, calculated).
        # A status the gate left alone keeps whoever approved it; a new one is
        # signed by the pipeline (auto_approved) or by nobody yet (pending).
        conn.execute(sa.text("""
            INSERT INTO pricing.variant_prices
                (variant_id, channel_id, calculated_price, vat_rate, margin_pct, status, approved_by)
            VALUES (:v, :ch, :calc, :vat, :margin, :status, :by)
            ON CONFLICT (variant_id, channel_id) DO UPDATE SET
                calculated_price = EXCLUDED.calculated_price, vat_rate = EXCLUDED.vat_rate, margin_pct = EXCLUDED.margin_pct, status = EXCLUDED.status,
                approved_by = CASE WHEN :keep THEN pricing.variant_prices.approved_by ELSE EXCLUDED.approved_by END
        """), {"v": line.variant.variant_id, "ch": line.channel_id, "calc": line.calculated,
               "vat": settings.vat_rate, "margin": line.margin, "status": line.status,
               "by": PIPELINE if line.status == AUTO else None,
               "keep": line.status == line.old_status})
        prices += 1
    return {"preferred_set": len(plan.preferred_changes), "cost_basis": len(plan.cost_basis),
            "prices": prices, "review_items": _write_issues(conn, plan.issues)}


def _write_issues(conn: sa.Connection, issues: list[tuple[str, int, str, str]]) -> int:
    """One open item per variant and issue. A resolved item whose problem is back is raised again."""
    open_items = set(conn.execute(sa.text(
        "SELECT entity_type, entity_id, issue_type FROM ops.review_queue WHERE status = 'open' "
        "AND issue_type IN (:a, :b)"), {"a": ISSUE_NO_SOURCE, "b": ISSUE_UNPRICEABLE}).all())
    inserted = 0
    for entity_type, entity_id, issue_type, notes in issues:
        if (entity_type, entity_id, issue_type) in open_items:
            continue
        conn.execute(sa.text("INSERT INTO ops.review_queue (entity_type, entity_id, issue_type, notes) "
                             "VALUES (:e, :i, :t, :n)"), {"e": entity_type, "i": entity_id, "t": issue_type, "n": notes})
        inserted += 1
    return inserted


def write_report(plan: Plan, path: Path) -> None:
    """Every variant priced, one row per channel, for reviewing before a live run."""
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mosferics_sku", "product", "supplier_sku", "cost_per_base_unit", "base_units", "variant_cost",
                    "rule", "won_at", "old_calculated", "calculated", "override", "final", "margin_pct",
                    "old_status", "status", "reason", "written"])
        for l in sorted(plan.lines, key=lambda l: l.variant.mosferics_sku):
            w.writerow([l.variant.mosferics_sku, l.variant.title, l.source.supplier_sku,
                        round(l.source.cost_per_base_unit, 4), l.variant.base_units_per_variant,
                        round(l.variant_cost, 4), l.rule.describe(), l.scope, l.old_calculated, l.calculated,
                        l.override, l.final, l.margin, l.old_status, l.status, l.reason, l.write])


def run(conn: sa.Connection, settings: Settings, *, dry_run: bool, report: Path | None = None) -> dict[str, Any]:
    plan = load_plan(conn, settings)
    if report:
        write_report(plan, report)
    result: dict[str, Any] = {"plan": plan.summary()}
    if not dry_run:
        result["written"] = write_plan(conn, plan, settings)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="plan and report, write nothing")
    parser.add_argument("--report", type=Path, help="write every planned price to this CSV")
    args = parser.parse_args()

    settings = load_settings()
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            if args.dry_run:
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            result = run(conn, settings, dry_run=args.dry_run, report=args.report)
    except PriceError as exc:
        raise SystemExit(f"price failed, nothing written: {exc}") from exc
    print(json.dumps(result, indent=2))
    if args.dry_run:
        print("dry run: nothing written")


if __name__ == "__main__":
    main()
