"""Run a supplier's automated steps in order, and log the run to ops.pipeline_runs.

    ingest -> normalise -> link -> images -> price [-> export]

Each step is the module's own command, run as a subprocess exactly as a person
would run it, so the runner adds sequencing and a record, not a second way of
doing anything. (extract has no step of its own: normalise runs it in memory.)

The human gates are not steps. A group link.py proposes is not priced until
someone confirms it; a new price is not exported until someone approves it. So
a run does all it can and stops short of whatever is still waiting, and the
run's summary says what that is. Running it again after the review carries
everything a stage further. Every step is idempotent, so a re-run with
nothing new writes nothing.

A run is one ops.pipeline_runs row: `running` while it goes, then `succeeded`
or `failed`, with every step's output and the waiting counts in `summary`. A
failed step stops the run; the steps before it have committed, which is safe
because each is idempotent.

--dry-run passes --dry-run to every step and logs nothing. Each step then sees
the database as it is, not as the steps before it would have left it, so a dry
run shows what each step would do now, not the run's combined effect.

    python -m pipeline.run artis [--dir DIR] [--skip images] [--export out/products.csv] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import sqlalchemy as sa
from dotenv import load_dotenv

from pipeline.config import REPO_ROOT, load_supplier_config

STEPS = ("ingest", "normalise", "link", "images", "price", "export")
OUTPUT_LINES = 60  # of a step that prints no JSON, the last lines kept in the summary


@dataclass(frozen=True)
class StepResult:
    step: str
    ok: bool
    seconds: float
    output: dict[str, Any] | list[str]
    error: str = ""


Executor = Callable[[list[str]], tuple[int, str, str]]


def commands(supplier: str, *, files: Path | None, export: Path | None, dry_run: bool,
             skip: set[str]) -> list[tuple[str, list[str]]]:
    """The command line for each step that runs, in order."""
    dry = ["--dry-run"] if dry_run else []
    planned = {
        "ingest": ["pipeline.ingest", supplier, *(["--dir", str(files)] if files else []), *dry],
        "normalise": ["pipeline.normalise", supplier, *dry],
        "link": ["pipeline.link", supplier, *dry],
        "images": ["pipeline.images", supplier, *dry],
        "price": ["pipeline.price", *dry],
    }
    if export is not None:
        planned["export"] = ["pipeline.shopify.export", "--out", str(export)]  # always read-only
    unknown = skip - set(STEPS)
    if unknown:
        raise SystemExit(f"--skip: unknown steps {sorted(unknown)}; steps are {STEPS}")
    return [(s, planned[s]) for s in STEPS if s in planned and s not in skip]


def parse_output(stdout: str) -> dict[str, Any] | list[str]:
    """A step's JSON result if it printed one, otherwise its last lines."""
    start, end = stdout.find("{"), stdout.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stdout[start:end + 1])
        except json.JSONDecodeError:
            pass
    return [l for l in stdout.splitlines() if l.strip()][-OUTPUT_LINES:]


def subprocess_executor(argv: list[str]) -> tuple[int, str, str]:
    done = subprocess.run([sys.executable, "-m", *argv], cwd=REPO_ROOT, capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


def run_steps(planned: list[tuple[str, list[str]]], execute: Executor,
              echo: Callable[[str], None] = print) -> list[StepResult]:
    results: list[StepResult] = []
    for step, argv in planned:
        echo(f"== {step}: python -m {' '.join(argv)}")
        started = time.monotonic()
        code, out, err = execute(argv)
        result = StepResult(step, code == 0, round(time.monotonic() - started, 2), parse_output(out),
                            "" if code == 0 else (err.strip().splitlines() or [f"exit {code}"])[-1])
        echo(out.rstrip() or "(no output)")
        results.append(result)
        if not result.ok:
            echo(f"!! {step} failed: {result.error}")
            break
    return results


def waiting(conn: sa.Connection, supplier: str) -> dict[str, int]:
    """What the run cannot do without a person."""
    return conn.execute(sa.text("""
        WITH mine AS (
            SELECT l.variant_id, l.status, l.is_preferred, sp.supplier_product_id
            FROM link.variant_links l JOIN supplier.supplier_products sp USING (supplier_product_id)
            JOIN supplier.suppliers s USING (supplier_id) WHERE s.code = :c)
        SELECT
          (SELECT count(*) FROM mine WHERE status = 'proposed') AS links_to_confirm,
          (SELECT count(*) FROM pricing.variant_prices vp JOIN mine m ON m.variant_id = vp.variant_id AND m.is_preferred
             WHERE vp.status = 'pending_approval') AS prices_to_approve,
          (SELECT count(*) FROM catalog.images i WHERE i.status IS NULL
             AND i.variant_id IN (SELECT variant_id FROM mine)) AS images_to_review,
          -- Both kinds point at a supplier product: placed or not, so conflicts count too.
          (SELECT count(*) FROM ops.review_queue q WHERE q.status = 'open'
             AND q.entity_type IN ('supplier_product', 'vocabulary')
             AND q.entity_id IN (SELECT sp.supplier_product_id FROM supplier.supplier_products sp
                                 JOIN supplier.suppliers s USING (supplier_id) WHERE s.code = :c))
             AS open_review_items
    """), {"c": supplier}).one()._asdict()


def supplier_id(conn: sa.Connection, supplier: str) -> int | None:
    return conn.execute(sa.text("SELECT supplier_id FROM supplier.suppliers WHERE code = :c"),
                        {"c": supplier}).scalar()


def start_run(conn: sa.Connection, supplier: str) -> int:
    return conn.execute(sa.text("INSERT INTO ops.pipeline_runs (supplier_id) VALUES (:s) RETURNING run_id"),
                        {"s": supplier_id(conn, supplier)}).scalar_one()


def finish_run(conn: sa.Connection, run_id: int, supplier: str, results: list[StepResult],
               planned: list[tuple[str, list[str]]]) -> str:
    ok = len(results) == len(planned) and all(r.ok for r in results)
    summary = {
        "steps": [{"step": r.step, "ok": r.ok, "seconds": r.seconds, "output": r.output,
                   **({"error": r.error} if r.error else {})} for r in results],
        "not_run": [s for s, _ in planned[len(results):]],
        "waiting": waiting(conn, supplier),
    }
    status = "succeeded" if ok else "failed"
    # The supplier may only exist now: ingest creates it on a first run.
    conn.execute(sa.text("""
        UPDATE ops.pipeline_runs SET status = :st, finished_at = now(), summary = CAST(:sm AS jsonb),
               supplier_id = coalesce(supplier_id, (SELECT supplier_id FROM supplier.suppliers WHERE code = :c))
        WHERE run_id = :r
    """), {"st": status, "sm": json.dumps(summary, default=str), "c": supplier, "r": run_id})
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("supplier")
    parser.add_argument("--dir", type=Path, help="the supplier's files (default data/incoming/<supplier>)")
    parser.add_argument("--skip", action="append", default=[], choices=STEPS, help="leave a step out")
    parser.add_argument("--export", type=Path, help="end by writing the ready products to this Shopify CSV")
    parser.add_argument("--dry-run", action="store_true", help="every step as a dry run; nothing logged")
    args = parser.parse_args()

    config = load_supplier_config(args.supplier)
    skip = set(args.skip)
    if "images" not in config:
        skip.add("images")  # a supplier with no image source configured has nothing to match
    planned = commands(args.supplier, files=args.dir, export=args.export, dry_run=args.dry_run, skip=skip)

    if args.dry_run:
        results = run_steps(planned, subprocess_executor)
        raise SystemExit(0 if all(r.ok for r in results) and len(results) == len(planned) else 1)

    load_dotenv()
    engine = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
    with engine.begin() as conn:
        run_id = start_run(conn, args.supplier)
    print(f"run {run_id}: {args.supplier}, steps {[s for s, _ in planned]}")
    results: list[StepResult] = []
    try:
        results = run_steps(planned, subprocess_executor)
    finally:
        # Logged even if the runner itself is interrupted, so no run is left `running`.
        with engine.begin() as conn:
            status = finish_run(conn, run_id, args.supplier, results, planned)
            left = waiting(conn, args.supplier)
    print(f"\nrun {run_id} {status}. Waiting for a person: " + ", ".join(f"{k} {v}" for k, v in left.items()))
    raise SystemExit(0 if status == "succeeded" else 1)


if __name__ == "__main__":
    main()
