import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from pipeline.run import commands, finish_run, parse_output, run_steps, start_run, waiting


# ------------------------------------------------------------------ planning

def test_the_steps_run_in_order_and_the_export_only_when_asked():
    planned = commands("demo", files=None, export=None, dry_run=False, skip=set())
    assert [s for s, _ in planned] == ["ingest", "normalise", "link", "images", "price"]
    planned = commands("demo", files=Path("in"), export=Path("out.csv"), dry_run=False, skip={"images"})
    assert [s for s, _ in planned] == ["ingest", "normalise", "link", "price", "export"]
    assert dict(planned)["ingest"] == ["pipeline.ingest", "demo", "--dir", "in"]


def test_a_dry_run_is_a_dry_run_at_every_step_but_the_read_only_export():
    planned = dict(commands("demo", files=None, export=Path("x.csv"), dry_run=True, skip=set()))
    assert all(argv[-1] == "--dry-run" for step, argv in planned.items() if step != "export")
    assert "--dry-run" not in planned["export"]


def test_an_unknown_step_to_skip_is_refused():
    with pytest.raises(SystemExit, match="unknown steps"):
        commands("demo", files=None, export=None, dry_run=False, skip={"enrich"})


def test_output_is_the_json_a_step_printed_or_its_last_lines():
    assert parse_output('{\n  "plan": {"products": 3}\n}\ndry run: nothing written\n') == {"plan": {"products": 3}}
    assert parse_output("rows: 21\nissue: x: 1\n") == ["rows: 21", "issue: x: 1"]


# ----------------------------------------------------------------- sequencing

def fake(outcomes):
    """An executor that answers from a script instead of running anything."""
    calls = []

    def execute(argv):
        calls.append(argv[0])
        return outcomes.get(argv[0], (0, "{}", ""))
    return execute, calls


def test_a_failed_step_stops_the_run():
    planned = commands("demo", files=None, export=None, dry_run=False, skip=set())
    execute, calls = fake({"pipeline.link": (1, "", "Traceback ...\nLinkError: no base_unit")})
    results = run_steps(planned, execute, echo=lambda _: None)
    assert calls == ["pipeline.ingest", "pipeline.normalise", "pipeline.link"]
    assert [r.ok for r in results] == [True, True, False] and results[-1].error == "LinkError: no base_unit"


# ------------------------------------------------------------------ database
# Needs TEST_DATABASE_URL pointing at a scratch database migrated to head.

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
        yield connection
        transaction.rollback()


def run_row(conn, run_id):
    return conn.execute(sa.text("SELECT * FROM ops.pipeline_runs WHERE run_id = :r"), {"r": run_id}).one()


def test_a_run_is_logged_from_running_to_succeeded_with_each_step(conn):
    run_id = start_run(conn, "test_run_new")          # a first run: the supplier does not exist yet
    assert run_row(conn, run_id).status == "running"
    conn.execute(sa.text("INSERT INTO supplier.suppliers (name, code) VALUES ('T', 'test_run_new')"))  # as ingest does
    planned = commands("test_run_new", files=None, export=None, dry_run=False, skip={"images"})
    execute, _ = fake({"pipeline.price": (0, json.dumps({"plan": {"variants_priced": 0}}), "")})
    results = run_steps(planned, execute, echo=lambda _: None)
    assert finish_run(conn, run_id, "test_run_new", results, planned) == "succeeded"
    row = run_row(conn, run_id)
    assert row.finished_at is not None and row.supplier_id is not None
    assert [s["step"] for s in row.summary["steps"]] == ["ingest", "normalise", "link", "price"]
    assert row.summary["steps"][-1]["output"] == {"plan": {"variants_priced": 0}}
    assert set(row.summary["waiting"]) == set(waiting(conn, "test_run_new"))


def test_a_run_that_stopped_early_is_failed_and_says_what_did_not_run(conn):
    run_id = start_run(conn, "test_run_new")
    planned = commands("test_run_new", files=None, export=None, dry_run=False, skip=set())
    execute, _ = fake({"pipeline.normalise": (1, "", "normalise failed, nothing written: bad column")})
    results = run_steps(planned, execute, echo=lambda _: None)
    assert finish_run(conn, run_id, "test_run_new", results, planned) == "failed"
    summary = run_row(conn, run_id).summary
    assert summary["not_run"] == ["link", "images", "price"]
    assert summary["steps"][-1]["error"] == "normalise failed, nothing written: bad column"
