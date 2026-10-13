"""Land a supplier's files in raw.*, verbatim, with a hash.

The first pipeline step. Each file named in the supplier YAML's `files:` section
becomes one raw.import_batches row and one raw.import_rows row per data row,
the payload holding every cell as the exact string the supplier sent: nothing
trimmed, nothing typed, a blank cell an empty string and never a null. Making
sense of the rows is extract.py's job; this only proves what arrived.

It refuses a file it cannot land honestly, before writing anything: a missing
required column, a repeated or blank header, a row with the wrong number of
cells, bytes that are not in the declared encoding, or no rows at all.
raw.import_rows is append-only, so a bad file must be stopped here.

Idempotent on the file hash: a file this supplier has already landed is not
landed again, and the existing batch is reported instead.

    python -m pipeline.ingest artis --dry-run     # parse and check, touch nothing
    python -m pipeline.ingest artis               # land data/incoming/artis/
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv

from pipeline.config import REPO_ROOT, load_supplier_config, supplier_config_path

INCOMING_DIR = REPO_ROOT / "data" / "incoming"
LANDED = "landed"


@dataclass(frozen=True)
class FileSpec:
    """One entry of the YAML's `files:` section."""

    role: str
    pattern: str
    encoding: str
    required_columns: tuple[str, ...]


@dataclass(frozen=True)
class ParsedFile:
    """A file read and checked, not yet written anywhere."""

    role: str
    path: Path
    file_hash: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class LandedBatch:
    role: str
    filename: str
    batch_id: int
    row_count: int
    already_landed: bool


def file_specs(config: dict) -> list[FileSpec]:
    """Read the `files:` section. Raises on anything ingest cannot act on."""
    files = config.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("supplier config has no `files:` section")
    specs = []
    for role, entry in files.items():
        missing = [k for k in ("pattern", "format", "encoding", "required_columns") if not entry.get(k)]
        if missing:
            raise ValueError(f"files.{role}: missing {missing}")
        if entry["format"] != "csv":
            raise ValueError(f"files.{role}: format {entry['format']!r} is not supported, only csv")
        specs.append(FileSpec(role, entry["pattern"], entry["encoding"], tuple(entry["required_columns"])))
    return specs


def find_file(directory: Path, spec: FileSpec) -> Path:
    """The one file in `directory` matching the spec's pattern. Raises on none or several."""
    matches = sorted(p for p in directory.glob(spec.pattern) if p.is_file())
    if len(matches) != 1:
        found = [p.name for p in matches] or "nothing"
        raise FileNotFoundError(
            f"files.{spec.role}: expected exactly one file matching {spec.pattern!r} "
            f"in {directory}, found {found}"
        )
    return matches[0]


def parse_file(path: Path, spec: FileSpec) -> ParsedFile:
    """Read a CSV into verbatim string rows. Raises on any structural fault."""
    data = path.read_bytes()
    try:
        text = data.decode(spec.encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name}: not valid {spec.encoding} ({exc})") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        columns = tuple(next(reader))
    except StopIteration:
        raise ValueError(f"{path.name}: file is empty") from None
    if any(not c.strip() for c in columns):
        raise ValueError(f"{path.name}: a header cell is blank, so a column has no name")
    repeated = sorted({c for c in columns if columns.count(c) > 1})
    if repeated:
        raise ValueError(f"{path.name}: repeated column headers {repeated}")
    missing = [c for c in spec.required_columns if c not in columns]
    if missing:
        raise ValueError(f"{path.name}: required columns missing {missing}; file has {list(columns)}")

    rows = []
    for cells in reader:
        if not cells:  # a wholly blank line is not a row
            continue
        if len(cells) != len(columns):
            raise ValueError(
                f"{path.name} line {reader.line_num}: {len(cells)} cells, "
                f"header has {len(columns)}"
            )
        rows.append(dict(zip(columns, cells)))
    if not rows:
        raise ValueError(f"{path.name}: header only, no data rows")
    return ParsedFile(spec.role, path, hashlib.sha256(data).hexdigest(), columns, tuple(rows))


def ensure_supplier(conn: sa.Connection, config: dict, config_path: str) -> int:
    """Return the supplier_id for the config's code, creating the row if absent.

    An existing row is left exactly as it is: once created it is maintained in
    the database, and the YAML's `supplier:` block is only what a first run loads.
    """
    supplier = config["supplier"]
    existing = conn.execute(
        sa.text("SELECT supplier_id FROM supplier.suppliers WHERE code = :code"),
        {"code": supplier["code"]},
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return conn.execute(
        sa.text(
            "INSERT INTO supplier.suppliers (name, code, config_path, currency, availability_source) "
            "VALUES (:name, :code, :config_path, :currency, :availability_source) "
            "RETURNING supplier_id"
        ),
        {
            "name": supplier["name"],
            "code": supplier["code"],
            "config_path": config_path,
            "currency": supplier["currency"],
            "availability_source": supplier["availability_source"],
        },
    ).scalar_one()


def land(conn: sa.Connection, supplier_id: int, parsed: ParsedFile) -> LandedBatch:
    """Write one parsed file as a batch and its rows, unless it is already landed."""
    previous = conn.execute(
        sa.text(
            "SELECT batch_id, row_count FROM raw.import_batches "
            "WHERE supplier_id = :supplier_id AND file_hash = :file_hash AND status = :status "
            "ORDER BY batch_id DESC LIMIT 1"
        ),
        {"supplier_id": supplier_id, "file_hash": parsed.file_hash, "status": LANDED},
    ).one_or_none()
    if previous is not None:
        return LandedBatch(parsed.role, parsed.path.name, previous.batch_id, previous.row_count, True)

    # The batch and its rows commit together, so a batch row marked landed
    # always has every one of its rows behind it.
    batch_id = conn.execute(
        sa.text(
            "INSERT INTO raw.import_batches (supplier_id, filename, file_hash, row_count, status) "
            "VALUES (:supplier_id, :filename, :file_hash, :row_count, :status) "
            "RETURNING batch_id"
        ),
        {
            "supplier_id": supplier_id,
            "filename": parsed.path.name,
            "file_hash": parsed.file_hash,
            "row_count": len(parsed.rows),
            "status": LANDED,
        },
    ).scalar_one()
    conn.execute(
        sa.text(
            "INSERT INTO raw.import_rows (batch_id, row_number, payload) "
            "VALUES (:batch_id, :row_number, CAST(:payload AS jsonb))"
        ),
        [
            {"batch_id": batch_id, "row_number": n, "payload": json.dumps(row, ensure_ascii=False)}
            for n, row in enumerate(parsed.rows, start=1)
        ],
    )
    return LandedBatch(parsed.role, parsed.path.name, batch_id, len(parsed.rows), False)


def parse_all(config: dict, directory: Path) -> list[ParsedFile]:
    """Find and parse every file the config names. All of them, before any write."""
    return [parse_file(find_file(directory, spec), spec) for spec in file_specs(config)]


def ingest(conn: sa.Connection, config: dict, directory: Path, config_path: str) -> list[LandedBatch]:
    """Land every file the config names. The caller owns the transaction."""
    parsed = parse_all(config, directory)
    supplier_id = ensure_supplier(conn, config, config_path)
    return [land(conn, supplier_id, p) for p in parsed]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier", help="supplier code, the name of its YAML in config/suppliers/")
    parser.add_argument("--dir", type=Path, help="directory holding the files (default data/incoming/<supplier>)")
    parser.add_argument("--dry-run", action="store_true", help="parse and check the files, write nothing")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    directory = args.dir or INCOMING_DIR / args.supplier
    if args.dry_run:
        for p in parse_all(config, directory):
            print(f"{p.role}: {p.path.name}  {len(p.rows)} rows x {len(p.columns)} columns  sha256 {p.file_hash[:12]}")
        return

    load_dotenv()
    # The pipeline's own role, not the owner: DATABASE_URL is for migrations.
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    config_path = str(supplier_config_path(args.supplier).relative_to(REPO_ROOT))
    with engine.begin() as conn:
        batches = ingest(conn, config, directory, config_path)
    for b in batches:
        state = "already landed as" if b.already_landed else "landed as"
        print(f"{b.role}: {b.filename}  {state} batch {b.batch_id}  ({b.row_count} rows)")
    print(f"database {engine.url.database}")


if __name__ == "__main__":
    main()
