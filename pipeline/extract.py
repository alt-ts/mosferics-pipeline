"""Turn a supplier's landed raw rows into staged fields, as the YAML describes.

The second pipeline step. It joins the supplier's files on their key, applies
the column mapping, and pulls out the attributes buried in product names. The
result is held in memory for normalise.py: there is no staging table, and this
module writes nothing to the database.

Extraction finds values and does not judge them. Capacities stay in the unit the
supplier used, and material and finish stay as the supplier spelled them, because
converting units and resolving aliases is normalise.py's job. Colour is the one
vocabulary read here, because it has to be found in the name to exist at all, and
it is only ever a proposal.

A fault in the file or the config raises, and every row is checked first so the
message lists all the bad rows at once. A fault in one product's data becomes an
Issue on that row, for normalise.py to send to review.

    python -m pipeline.extract artis      # report what the landed batches yield
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from dotenv import load_dotenv

from pipeline.config import REPO_ROOT, load_supplier_config
from pipeline.ingest import LANDED

VOCABULARY_DIR = REPO_ROOT / "config" / "vocabularies"
PENDING_REVIEW = "pending_review"
MAX_LISTED_FAULTS = 10
PACK_CHECK_EXPR = re.compile(r"^(?P<total>.+?) == (?P<unit>.+?) \* (?P<count>.+)$")
GTIN_LENGTHS = (8, 12, 13, 14)


class ExtractError(ValueError):
    """The file or the config cannot be read honestly. Nothing is staged."""


@dataclass(frozen=True)
class Issue:
    """Something wrong with one product's data, for a human to look at."""

    code: str
    field: str
    detail: str


@dataclass(frozen=True)
class StagedOffer:
    cost_ex_vat: Decimal  # the cost of the whole pack, never of one piece
    base_units_per_pack: Decimal
    pack_description: str | None


@dataclass(frozen=True)
class Quantity:
    """A number in the unit the supplier wrote it in. Not yet converted."""

    raw: str
    value: Decimal
    unit: str


@dataclass(frozen=True)
class LineMarkSegment:
    kind: str  # 'line' or 'brim'
    kind_raw: str
    values: tuple[str, ...]
    unit: str


@dataclass(frozen=True)
class LineMark:
    raw: str  # always kept, including when it could not be parsed
    segments: tuple[LineMarkSegment, ...]  # empty when unparsed
    # The body a human wrote in place of one the parser could not read
    # (capacity.line_mark.corrections). The segments come from it; raw does not.
    corrected: str | None = None


@dataclass(frozen=True)
class Size:
    raw: str
    dimensions: tuple[Decimal, ...]
    unit: str


@dataclass(frozen=True)
class VocabularyValue:
    raw: str
    status: str | None


@dataclass(frozen=True)
class StagedRow:
    supplier_sku: str
    name_raw: str
    name_clean: str
    brand: str | None
    supplier_category_raw: str | None
    offer: StagedOffer | None  # None only when the SKU has no price row
    brim: Quantity | None
    brim_cross_check: Quantity | None
    line_mark: LineMark | None
    nucleation: bool
    size: Size | None
    vocabulary: dict[str, VocabularyValue]
    colour_candidates: tuple[str, ...]
    colour_status: str | None
    safety: dict[str, str]  # flag -> status; a flag the supplier left blank is absent
    specs: dict[str, Any]
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class SourceBatch:
    role: str
    batch_id: int
    filename: str
    row_count: int
    received_at: datetime


def is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def parse_decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        raise ExtractError(f"{label}: {value!r} is not a number") from None
    if not parsed.is_finite():
        raise ExtractError(f"{label}: {value!r} is not a number")
    return parsed


def parse_flag(value: str, true_value: str, label: str) -> bool:
    """True if the supplier states it, False if blank. Blank means not stated, never 'no'."""
    if is_blank(value):
        return False
    if value.strip() != true_value:
        raise ExtractError(f"{label}: {value!r} is neither {true_value!r} nor blank")
    return True


def valid_gtin(code: str) -> bool:
    if not code.isdigit() or len(code) not in GTIN_LENGTHS:
        return False
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(reversed(code)))
    return total % 10 == 0


def load_aliases(field: str, ignored: list[str], vocabulary_dir: Path = VOCABULARY_DIR) -> dict[str, str]:
    """Lower-cased alias -> canonical, minus the ones the supplier config ignores."""
    path = vocabulary_dir / f"{field}.yaml"
    if not path.is_file():
        raise ExtractError(f"no vocabulary at {path}")
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    aliases = {
        alias.strip().lower(): canonical
        for canonical, entry in entries.items()
        for alias in entry["aliases"]
    }
    unknown = [a for a in ignored if a.strip().lower() not in aliases]
    if unknown:
        raise ExtractError(f"vocabulary.{field}.ignore_aliases: {unknown} are not aliases in {path.name}")
    for alias in ignored:
        del aliases[alias.strip().lower()]
    return aliases


def whole_words(phrases: list[str]) -> re.Pattern[str]:
    """Match any phrase as whole words. Longest first, so 'Off White' beats 'White'."""
    ordered = sorted(phrases, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(p) for p in ordered) + r")(?!\w)", re.IGNORECASE)


def column_references(section: Any, roles: set[str]) -> set[tuple[str, str]]:
    """Every (role, column) a config section points at, however deeply nested."""
    if isinstance(section, dict):
        return set().union(*(column_references(v, roles) for v in section.values()), set())
    if isinstance(section, list):
        return set().union(*(column_references(v, roles) for v in section), set())
    if isinstance(section, str) and "." in section:
        role, column = section.split(".", 1)
        if role in roles:
            return {(role, column)}
    return set()


class Stager:
    """One supplier's config, checked and compiled, applied row by row."""

    def __init__(self, config: dict, vocabulary_dir: Path = VOCABULARY_DIR) -> None:
        self.config = config
        self.files = config["files"]
        self.roles = set(self.files)
        join = config["join"]
        self.primary, self.secondary = join["primary"], join["secondary"]
        for setting, allowed in (
            ("duplicate_key", {"raise"}),
            ("primary_without_secondary", {"load"}),
            ("secondary_without_primary", {"review"}),
        ):
            if join[setting] not in allowed:
                raise ExtractError(f"join.{setting}: {join[setting]!r} is not supported")
        self.key_format = re.compile(config["key_format"])

        self.pack_checks = []
        self.compare_checks = []
        for check in config["offer"].get("checks", []):
            if "expr" in check:
                match = PACK_CHECK_EXPR.match(check["expr"])
                if not match or check["on_fail"] != "raise":
                    raise ExtractError(f"offer.checks.{check['name']}: unsupported expr or on_fail")
                self.pack_checks.append((check, match))
            elif "compare" in check:
                if check["on_fail"] != "review" or check.get("on_blank") != "ignore":
                    raise ExtractError(f"offer.checks.{check['name']}: unsupported on_fail or on_blank")
                # SKUs a human has looked at: the exact pair of values accepted.
                # Different values on a later file are a new disagreement.
                accepted: dict[str, tuple[Decimal, Decimal]] = {}
                for sku, entry in (check.get("accept") or {}).items():
                    values = (entry or {}).get("values")
                    if not entry.get("decided") or not isinstance(values, list) or len(values) != 2:
                        raise ExtractError(f"offer.checks.{check['name']}.accept.{sku}: needs values [left, right] and decided")
                    accepted[str(sku)] = tuple(Decimal(str(v)) for v in values)
                self.compare_checks.append((check, accepted))
            else:
                raise ExtractError(f"offer.checks.{check['name']}: neither expr nor compare")

        self._compile_line_mark(config.get("capacity", {}).get("line_mark"))
        self._compile_vocabulary(config.get("vocabulary", {}), vocabulary_dir)

        self.safety = config.get("safety", {})
        # Hard rule 3. A config edit must not be able to auto-verify a safety claim.
        if self.safety and self.safety.get("status") != PENDING_REVIEW:
            raise ExtractError(f"safety.status must be {PENDING_REVIEW}, not {self.safety.get('status')!r}")
        self.true_value = self.safety.get("true_value", "Y")
        if self.safety.get("blank", "omit") != "omit":
            raise ExtractError(f"safety.blank: {self.safety['blank']!r} is not supported")
        # A human's choice between contradicting flags, per SKU: flag -> SKUs.
        self.safety_keep: list[dict[str, str]] = []
        for contradiction in self.safety.get("contradictions", []):
            chosen: dict[str, str] = {}
            for flag, skus in (contradiction.get("keep") or {}).items():
                if flag not in contradiction["flags"]:
                    raise ExtractError(f"safety.contradictions.keep: {flag!r} is not one of {contradiction['flags']}")
                for sku in skus or []:
                    if sku in chosen:
                        raise ExtractError(f"safety.contradictions.keep: {sku} is listed under two flags")
                    chosen[str(sku)] = flag
            self.safety_keep.append(chosen)

        self.size = config.get("size")
        if self.size:
            if self.size.get("take") != "first":
                raise ExtractError(f"size.take: {self.size.get('take')!r} is not supported")
            self.size_pattern = re.compile(self.size["pattern"])

        barcode = config.get("barcode")
        if barcode:
            if barcode.get("use_as_match_key") is not False:
                raise ExtractError("barcode.use_as_match_key: only false is supported")
            if barcode.get("validate") != "gtin_check_digit" or barcode.get("on_invalid") != "keep_raw":
                raise ExtractError("barcode: only gtin_check_digit with keep_raw is supported")
            if not barcode.get("store_as", "").startswith("specs."):
                raise ExtractError("barcode.store_as must be specs.<key>")

        for key, rule in config.get("specs", {}).items():
            if rule.get("type") not in (None, "decimal", "flag"):
                raise ExtractError(f"specs.{key}: type {rule['type']!r} is not supported")

    def _compile_line_mark(self, rule: dict | None) -> None:
        self.line_mark = rule
        if not rule:
            return
        if rule.get("on_unparsed") != "review" or rule.get("multiple_values") != "keep_all":
            raise ExtractError("capacity.line_mark: only on_unparsed review with keep_all is supported")
        self.lm_block = re.compile(rule["block"])
        self.lm_strip = [re.compile(p) for p in rule.get("strip_from_body", [])]
        self.lm_segment = re.compile(rule["segment"])
        self.lm_corrections: dict[str, str] = {}
        for sku, correction in (rule.get("corrections") or {}).items():
            body = (correction or {}).get("body")
            if not body or not correction.get("decided"):
                raise ExtractError(f"capacity.line_mark.corrections.{sku}: needs body and decided")
            if self._line_mark_segments(body) is None:
                raise ExtractError(f"capacity.line_mark.corrections.{sku}: {body!r} does not parse")
            self.lm_corrections[str(sku)] = body

    def _compile_vocabulary(self, rules: dict, vocabulary_dir: Path) -> None:
        self.vocabulary_rules = rules
        self.colour_rule = None
        for field, rule in rules.items():
            # Hard rule 3: materials are safety-relevant.
            if field == "material" and rule.get("status") != PENDING_REVIEW:
                raise ExtractError(f"vocabulary.material.status must be {PENDING_REVIEW}")
            if rule.get("method") == "alias_in_name":
                if rule.get("multiple_matches") != "review":
                    raise ExtractError(f"vocabulary.{field}: only multiple_matches review is supported")
                self.colour_rule = rule
                self.colour_aliases = load_aliases(field, rule.get("ignore_aliases", []), vocabulary_dir)
                self.colour_pattern = whole_words(list(self.colour_aliases))
                phrases = rule.get("ignore_phrases", [])
                self.colour_ignored = whole_words(phrases) if phrases else None

    def check_columns(self, tables: dict[str, list[dict[str, str]]]) -> None:
        """Raise if any column the config names is not in the landed data."""
        for role in self.roles:
            if not tables.get(role):
                raise ExtractError(f"no rows for file role {role!r}")
        wanted = {(role, c) for role, entry in self.files.items() for c in entry["required_columns"]}
        wanted |= {(role, entry["key"]) for role, entry in self.files.items()}
        for section in ("product", "offer", "capacity", "size", "vocabulary", "safety", "specs", "barcode"):
            wanted |= column_references(self.config.get(section), self.roles)
        for _, match in self.pack_checks:
            wanted |= {(self.primary, c) for c in match.groups()}
        missing = sorted(f"{role}.{c}" for role, c in wanted if c not in tables[role][0])
        if missing:
            raise ExtractError(f"columns in the config are not in the data: {missing}")

    def join(self, tables: dict[str, list[dict[str, str]]]) -> list[tuple[str, dict[str, dict[str, str] | None]]]:
        """Pair up the files on their keys, primary rows first, in file order."""
        keyed = {}
        faults = []
        for role in (self.primary, self.secondary):
            key_column = self.files[role]["key"]
            keyed[role] = {}
            for row in tables[role]:
                key = row[key_column]
                if not self.key_format.fullmatch(key):
                    faults.append(f"{role}: key {key!r} does not match key_format")
                elif key in keyed[role]:
                    faults.append(f"{role}: key {key!r} appears twice")
                else:
                    keyed[role][key] = row
        raise_faults(faults)
        joined = [
            (key, {self.primary: row, self.secondary: keyed[self.secondary].get(key)})
            for key, row in keyed[self.primary].items()
        ]
        joined += [
            (key, {self.primary: None, self.secondary: row})
            for key, row in keyed[self.secondary].items()
            if key not in keyed[self.primary]
        ]
        return joined

    def stage(self, tables: dict[str, list[dict[str, str]]]) -> list[StagedRow]:
        """Stage every row. Raises once, listing every row that cannot be read."""
        self.check_columns(tables)
        staged, faults = [], []
        for key, sources in self.join(tables):
            try:
                staged.append(self.stage_row(key, sources))
            except ExtractError as exc:
                faults.append(f"{key}: {exc}")
        raise_faults(faults)
        return staged

    def stage_row(self, key: str, sources: dict[str, dict[str, str] | None]) -> StagedRow:
        issues: list[Issue] = []

        def cell(reference: str) -> str:
            """The verbatim cell, or '' when this SKU has no row in that file."""
            role, column = reference.split(".", 1)
            row = sources[role]
            return row[column] if row is not None else ""

        def first(references: str | list[str]) -> str | None:
            """The first non-blank of the references, untouched."""
            for reference in [references] if isinstance(references, str) else references:
                value = cell(reference)
                if not is_blank(value):
                    return value
            return None

        mapping = self.config["product"]
        name_raw = first(mapping["name_raw"])
        if name_raw is None:
            raise ExtractError("no name in any source")  # a NOT NULL column downstream

        specs: dict[str, Any] = {}
        vocabulary = self._vocabulary(cell, specs)
        self._specs(cell, specs)
        self._barcode(cell, specs)
        capacity = self.config.get("capacity", {})
        colour_candidates = self._colour(name_raw, issues)

        if sources[self.primary] is None:
            issues.append(Issue("no_price_row", "offer", f"no row in the {self.primary} file, so no offer"))

        sku = first(mapping["supplier_sku"]) or key
        return StagedRow(
            supplier_sku=sku,
            name_raw=name_raw,
            name_clean=self._name_clean(name_raw),
            brand=first(mapping["brand"]),
            supplier_category_raw=self._category(cell, mapping["supplier_category_raw"]),
            offer=self._offer(sku, cell, sources, issues),
            brim=self._brim(cell, capacity.get("brim")),
            brim_cross_check=self._brim_cross_check(cell, capacity.get("brim")),
            line_mark=self._line_mark(sku, name_raw, issues),
            nucleation=self._nucleation(cell, name_raw, capacity.get("nucleation")),
            size=self._size(name_raw),
            vocabulary=vocabulary,
            colour_candidates=colour_candidates,
            colour_status=self.colour_rule["status"] if self.colour_rule else None,
            safety=self._safety(sku, cell, issues),
            specs=specs,
            issues=tuple(issues),
        )

    def _name_clean(self, name_raw: str) -> str:
        if self.config.get("name_clean", {}).get("collapse_whitespace"):
            return " ".join(name_raw.split())
        return name_raw

    def _category(self, cell, rule: dict) -> str | None:
        """All the columns joined, or None when the row has no category at all."""
        parts = [cell(c) for c in rule["columns"]]
        if all(is_blank(p) for p in parts):
            return None
        return rule["separator"].join(rule["blank"] if is_blank(p) else p for p in parts)

    def _offer(self, sku: str, cell, sources, issues: list[Issue]) -> StagedOffer | None:
        if sources[self.primary] is None:
            return None
        rule = self.config["offer"]
        cost = parse_decimal(cell(rule["cost_ex_vat"]), rule["cost_ex_vat"])
        units = parse_decimal(cell(rule["base_units_per_pack"]), rule["base_units_per_pack"])
        if units <= 0:
            raise ExtractError(f"{rule['base_units_per_pack']}: {units} must be above zero")

        for check, match in self.pack_checks:
            values = {
                name: parse_decimal(sources[self.primary][column], column)
                for name, column in match.groupdict().items()
            }
            gap = abs(values["total"] - values["unit"] * values["count"])
            if gap > Decimal(str(check["tolerance"])):
                raise ExtractError(f"check {check['name']} fails: {check['expr']} is out by {gap}")

        for check, accepted in self.compare_checks:
            left, right = check["compare"]
            if is_blank(cell(left)) or is_blank(cell(right)):
                continue
            pair = (parse_decimal(cell(left), left), parse_decimal(cell(right), right))
            if pair[0] != pair[1] and accepted.get(sku) != pair:
                issues.append(Issue(check["name"], "offer", f"{left} {cell(left)!r}, {right} {cell(right)!r}"))

        pack_description = cell(rule["pack_description"]) if rule.get("pack_description") else ""
        return StagedOffer(cost, units, None if is_blank(pack_description) else pack_description)

    def _quantity(self, cell, source: str, unit: str, zero_is_blank: bool) -> Quantity | None:
        raw = cell(source)
        if is_blank(raw):
            return None
        value = parse_decimal(raw, source)
        if zero_is_blank and value == 0:
            return None
        return Quantity(raw, value, unit)

    def _brim(self, cell, rule: dict | None) -> Quantity | None:
        if not rule:
            return None
        return self._quantity(cell, rule["source"], rule["unit"], rule.get("zero_is_blank", False))

    def _brim_cross_check(self, cell, rule: dict | None) -> Quantity | None:
        """Staged, not compared: comparing cl with ml means converting, which is normalise.py's."""
        if not rule or "cross_check" not in rule:
            return None
        cross = rule["cross_check"]
        return self._quantity(cell, cross["source"], cross["unit"], rule.get("zero_is_blank", False))

    def _line_mark(self, sku: str, name_raw: str, issues: list[Issue]) -> LineMark | None:
        if not self.line_mark:
            return None
        blocks = list(self.lm_block.finditer(name_raw))
        if not blocks:
            if sku in self.lm_corrections:
                raise ExtractError(f"capacity.line_mark.corrections.{sku}: the name has no line mark to correct")
            return None
        if sku in self.lm_corrections:
            body = self.lm_corrections[sku]
            return LineMark(" ".join(b.group(0) for b in blocks), self._line_mark_segments(body), corrected=body)
        raw = blocks[0].group(0)
        segments = self._line_mark_segments(blocks[0].group("body")) if len(blocks) == 1 else None
        if segments is None:
            raw = " ".join(b.group(0) for b in blocks)
            issues.append(Issue("line_mark_unparsed", "capacity", raw))
            return LineMark(raw, ())
        return LineMark(raw, segments)

    def _line_mark_segments(self, body: str) -> tuple[LineMarkSegment, ...] | None:
        """Every segment of the body, or None if any part of it cannot be consumed."""
        rule = self.line_mark
        for pattern in self.lm_strip:
            body = pattern.sub("", body)
        body = body.strip()
        separator = rule["segment_separator"]
        segments = []
        position = 0
        while True:
            match = self.lm_segment.match(body, position)
            if not match:
                return None
            kind = rule["kinds"].get(match["kind"])
            unit = rule["units"].get(match["unit"])
            if kind is None or unit is None:
                return None
            aliases = rule.get("value_aliases", {}).get(unit, {})
            values = tuple(aliases.get(v.strip(), v.strip()) for v in match["values"].split(","))
            segments.append(LineMarkSegment(kind, match["kind"], values, unit))
            position = match.end()
            if position == len(body):
                return tuple(segments)
            if not body.startswith(separator, position):
                return None
            position += len(separator)
            while position < len(body) and body[position].isspace():
                position += 1

    def _nucleation(self, cell, name_raw: str, rule: dict | None) -> bool:
        if not rule:
            return False
        for test in rule["any_of"]:
            value = name_raw if test["source"] == "name_raw" else cell(test["source"])
            if "equals" in test and value.strip() == test["equals"]:
                return True
            if "pattern" in test and re.search(test["pattern"], value):
                return True
        return False

    def _size(self, name_raw: str) -> Size | None:
        if not self.size:
            return None
        match = self.size_pattern.search(name_raw)
        if not match:
            return None
        dimensions = tuple(Decimal(v) for v in match.groups() if v is not None)
        return Size(match.group(0), dimensions, self.size["unit"])

    def _vocabulary(self, cell, specs: dict[str, Any]) -> dict[str, VocabularyValue]:
        """Raw material, finish and the like. Resolving them is normalise.py's job."""
        values = {}
        for field, rule in self.vocabulary_rules.items():
            if rule.get("method") == "alias_in_name":
                continue
            raw = cell(rule["source"])
            if is_blank(raw) or raw.strip() in rule.get("treat_as_blank", []):
                continue
            redirect = rule.get("redirect", {}).get(raw.strip())
            if redirect:
                specs[redirect] = raw
                continue
            values[field] = VocabularyValue(raw, rule.get("status"))
        return values

    def _colour(self, name_raw: str, issues: list[Issue]) -> tuple[str, ...]:
        """Canonical colours named in the name. A proposal, whatever it finds."""
        if not self.colour_rule:
            return ()
        name = self.colour_ignored.sub(" ", name_raw) if self.colour_ignored else name_raw
        found = {self.colour_aliases[m.group(0).lower()] for m in self.colour_pattern.finditer(name)}
        if len(found) > 1:
            issues.append(Issue("colour_ambiguous", "colour", f"{sorted(found)} in {name_raw!r}"))
        return tuple(sorted(found))

    def _safety(self, sku: str, cell, issues: list[Issue]) -> dict[str, str]:
        if not self.safety:
            return {}
        stated = {
            flag: PENDING_REVIEW
            for flag, source in self.safety["flags"].items()
            if parse_flag(cell(source), self.true_value, source)
        }
        for contradiction, chosen in zip(self.safety.get("contradictions", []), self.safety_keep):
            if contradiction["on_fail"] != "review":
                raise ExtractError("safety.contradictions: only on_fail review is supported")
            if all(flag in stated for flag in contradiction["flags"]):
                if sku in chosen:
                    # A human chose one claim. The other is dropped; the one kept
                    # is still only a supplier claim, pending_review (hard rule 3).
                    for flag in contradiction["flags"]:
                        if flag != chosen[sku]:
                            del stated[flag]
                    continue
                issues.append(Issue("safety_contradiction", "safety", " and ".join(contradiction["flags"])))
        return stated

    def _specs(self, cell, specs: dict[str, Any]) -> None:
        for key, rule in self.config.get("specs", {}).items():
            raw = cell(rule["source"])
            if is_blank(raw):
                continue
            kind = rule.get("type")
            if kind == "decimal":
                value = parse_decimal(raw, rule["source"])
                if not (rule.get("zero_is_blank") and value == 0):
                    specs[key] = value
            elif kind == "flag":
                specs[key] = parse_flag(raw, self.true_value, rule["source"])
            else:
                specs[key] = raw

    def _barcode(self, cell, specs: dict[str, Any]) -> None:
        rule = self.config.get("barcode")
        if not rule:
            return
        raw = cell(rule["source"])
        if is_blank(raw):
            return
        key = rule["store_as"].removeprefix("specs.")
        specs[key] = raw
        specs[f"{key}_valid"] = valid_gtin(raw.strip())


def raise_faults(faults: list[str]) -> None:
    if faults:
        listed = "; ".join(faults[:MAX_LISTED_FAULTS])
        more = len(faults) - MAX_LISTED_FAULTS
        suffix = f"; and {more} more" if more > 0 else ""
        raise ExtractError(f"{len(faults)} faults: {listed}{suffix}")


def stage_rows(
    config: dict, tables: dict[str, list[dict[str, str]]], vocabulary_dir: Path = VOCABULARY_DIR
) -> list[StagedRow]:
    """Stage rows already in memory, keyed by file role. Touches no database."""
    return Stager(config, vocabulary_dir).stage(tables)


def latest_batches(conn: sa.Connection, config: dict) -> dict[str, SourceBatch]:
    """The newest landed batch for each file role, recognised by the file's name."""
    code = config["supplier"]["code"]
    supplier_id = conn.execute(
        sa.text("SELECT supplier_id FROM supplier.suppliers WHERE code = :code"), {"code": code}
    ).scalar_one_or_none()
    if supplier_id is None:
        raise ExtractError(f"no supplier row for {code!r}; has it been ingested?")
    rows = conn.execute(
        sa.text(
            "SELECT batch_id, filename, row_count, received_at FROM raw.import_batches "
            "WHERE supplier_id = :supplier_id AND status = :status ORDER BY batch_id"
        ),
        {"supplier_id": supplier_id, "status": LANDED},
    ).all()
    batches: dict[str, SourceBatch] = {}
    for batch in rows:
        roles = [r for r, e in config["files"].items() if fnmatch.fnmatchcase(batch.filename, e["pattern"])]
        if len(roles) != 1:
            raise ExtractError(f"batch {batch.batch_id} {batch.filename!r} matches file roles {roles}")
        batches[roles[0]] = SourceBatch(
            roles[0], batch.batch_id, batch.filename, batch.row_count, batch.received_at
        )
    missing = sorted(set(config["files"]) - set(batches))
    if missing:
        raise ExtractError(f"no landed batch for file roles {missing}")
    return batches


def load_rows(conn: sa.Connection, batch: SourceBatch) -> list[dict[str, str]]:
    """A batch's payloads in file order. Raises if the batch is not all there."""
    rows = conn.execute(
        sa.text("SELECT payload FROM raw.import_rows WHERE batch_id = :batch_id ORDER BY row_number"),
        {"batch_id": batch.batch_id},
    ).scalars().all()
    if len(rows) != batch.row_count:
        raise ExtractError(f"batch {batch.batch_id} says {batch.row_count} rows, found {len(rows)}")
    return list(rows)


def extract(conn: sa.Connection, config: dict) -> tuple[dict[str, SourceBatch], list[StagedRow]]:
    """Stage the supplier's latest landed batches. Reads only."""
    batches = latest_batches(conn, config)
    return batches, stage_rows(config, {role: load_rows(conn, b) for role, b in batches.items()})


def summarise(rows: list[StagedRow]) -> dict[str, int]:
    """How many rows yielded each field, and how many carry each kind of issue."""
    counts = {
        "rows": len(rows),
        "with offer": sum(r.offer is not None for r in rows),
        "with category": sum(r.supplier_category_raw is not None for r in rows),
        "with brim capacity": sum(r.brim is not None for r in rows),
        "with line mark": sum(r.line_mark is not None for r in rows),
        "with several line values": sum(
            r.line_mark is not None and sum(len(s.values) for s in r.line_mark.segments) > 1 for r in rows
        ),
        "nucleated": sum(r.nucleation for r in rows),
        "with size": sum(r.size is not None for r in rows),
        "with material": sum("material" in r.vocabulary for r in rows),
        "with finish": sum("finish" in r.vocabulary for r in rows),
        "with a colour proposal": sum(bool(r.colour_candidates) for r in rows),
        "with a safety claim": sum(bool(r.safety) for r in rows),
    }
    issues = Counter(i.code for r in rows for i in r.issues)
    counts.update({f"issue: {code}": n for code, n in sorted(issues.items())})
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier", help="supplier code, the name of its YAML in config/suppliers/")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    with engine.connect() as conn:
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        batches, rows = extract(conn, config)
    for b in batches.values():
        print(f"{b.role}: batch {b.batch_id}  {b.filename}  ({b.row_count} rows)")
    for label, count in summarise(rows).items():
        print(f"{label}: {count}")
    print(f"database {engine.url.database}  (read only, nothing written)")


if __name__ == "__main__":
    main()
