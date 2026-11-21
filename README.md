# Mosferics catalogue pipeline

[![CI](https://github.com/alt-ts/mosferics-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/alt-ts/mosferics-pipeline/actions/workflows/ci.yml)

A PostgreSQL and Python pipeline that turns wholesale suppliers' price and product
files into a Shopify catalogue. It was built for Mosferics, a UK supplier of
glassware, tableware and bar equipment to the hospitality trade. The business
closed in late 2026 before the shop launched, so this repository is the finished
state of the project rather than a running service.

It was taken as far as one real supplier (Artis, about 3,500 SKUs) end to end on
the production database: files landed, normalised, grouped into products,
priced and approved. The Shopify export is written and tested but was never
imported into a store. The status section below says exactly what ran where.

## The problem

Suppliers send spreadsheets with one row per SKU, and almost nothing a shop
needs is in a clean column:

- Variants arrive as unrelated rows. "Perception Wine 32cl" and "Perception
  Wine 41cl" are one product with a capacity selector, but nothing in the file
  says so.
- Attributes hide in names. `Nonic 57cl (CE@285ml)` carries a brim capacity and
  a line mark, and the brim column sometimes disagrees with the name.
- Units, spellings and pack sizes vary from row to row and between suppliers.
- Prices come per pack, the shop sells per variant, and the margin has to be
  right for both.
- Some fields are safety claims (dishwasher safe, food contact). Those cannot be
  guessed, and a wrong one is a liability.

The pipeline does the mechanical work and stops wherever a human has to decide:
grouping proposals, unmapped values, price changes and safety claims all wait in
a review queue rather than being filled in.

## How it works

```mermaid
flowchart LR
    F[Supplier files] --> I[ingest]
    I --> R[(raw)]
    R --> E[extract] --> N[normalise]
    N --> S[(supplier)]
    S --> L[link] --> C[(catalog + link)]
    C --> M[promote] --> P[price] --> PR[(pricing)]
    PR --> Q{{review in NocoDB}}
    Q --> U[publish intent] --> X[shopify/export] --> CSV[Shopify CSV]
```

| Step | Module | What it does |
|---|---|---|
| Ingest | `pipeline/ingest.py` | Lands each file verbatim in `raw`, with a hash, so any later value can be traced to what the supplier actually sent |
| Extract | `pipeline/extract.py` | Maps columns and pulls attributes out of names, as the supplier's YAML describes |
| Normalise | `pipeline/normalise.py` | Resolves values against controlled vocabularies and converts units to canonical (ml for capacity) |
| Link | `pipeline/link.py` | Maps categories, groups SKUs into products and variants, allocates SKUs, links each supplier SKU to its variant |
| Promote | `pipeline/promote.py` | Carries supplier barcodes onto variants, and safety and material claims into product metadata, held for review |
| Price | `pipeline/price.py` | Picks each variant's source, costs it, applies pricing rules and holds changes for approval |
| Images | `pipeline/images.py` | Matches image files to SKUs by name and records each against its variant |
| Publish | `pipeline/publish.py` | Records which products are meant for a channel, and which are live there |
| Export | `pipeline/shopify/export.py` | Writes the products that are ready to publish as a Shopify product CSV |

Every step has a `--dry-run` that writes nothing, and every step is idempotent: a
re-run with nothing changed writes nothing. `pipeline/run.py` runs the automated
steps in order and logs each run, with every step's result and what is left for
a person, to `ops.pipeline_runs`. The human review is not a step: a run does
everything that does not need a decision, and the next run after the review
carries the work a stage further.

Each supplier is described by one YAML file (`config/suppliers/artis.yaml`):
column mappings, extraction patterns, grouping rules, and the decisions a human
has made about individual SKUs. There is no `if supplier == ...` anywhere in
the Python, so adding a supplier means writing a config file, not changing code.

## The data model

Seven schemas, each with one job. The full design is in
[`docs/mosferics_data_model_v3.md`](docs/mosferics_data_model_v3.md).

| Schema | Holds |
|---|---|
| `raw` | The supplier files, row by row, exactly as received. Immutable |
| `supplier` | Their products and offers, standardised but still theirs |
| `link` | Which supplier SKU is which of our variants, and who confirmed it |
| `catalog` | What we sell: taxonomy, products, variants, options, images |
| `pricing` | Rules, cost basis and prices, with the approval state |
| `ops` | Channels, the review queue, publishing state, delivery config |
| `review` | Read-only views for the reviewer: unmapped categories, missing images, ready to publish |

The schema is built entirely by Alembic migrations (`migrations/versions/`,
0001 to 0020), including role grants and triggers. `alembic upgrade head` on an
empty database produces a working one.

## Design decisions

The choices most worth a closer look. Each has its reasoning written up in the
docs.

- **The raw layer cannot be edited.** A trigger rejects updates to
  `raw.import_rows`, and the pipeline's database role has no UPDATE privilege on
  it either, so the rule survives someone dropping the trigger. Proving what a
  supplier sent depends on it.
- **Links resolve at variant level.** The same table expresses both "this Artis
  plate and that BBP plate are the same thing" (two links to one variant) and
  "the white and blue plates are one product" (two variants of one product).
- **Grouping never invents combinations.** If a range comes in 25cl Clear, 35cl
  Clear and 35cl Blue, the product has exactly those three variants. A group the
  rules cannot separate cleanly goes to review whole, rather than being guessed
  into shape.
- **Safety claims are never verified by the pipeline.** Dishwasher-safe and
  similar flags land as `pending_review`, and the code refuses a config that
  tries to change that. When a supplier claims both "dishwasher safe" and
  "handwash only", a human picks one, and the choice is recorded per SKU in the
  YAML.
- **The live margin is computed, never stored.** `pricing.live_margins` derives
  it from today's cost. The stored `margin_pct` is only the snapshot the approval
  gate judged. Cost is per base unit and price is per variant, so the margin has
  to multiply by the pack size; the first version of the view forgot, and
  migration 0011 is the fix.
- **Price approval has a materiality threshold.** A new price always waits for
  a human. A change under 5% to an approved price, with margin still above the
  15% floor, is approved automatically and logged; anything else waits. Without
  that, a full supplier price update queues thousands of rows and the gate turns
  into rubber-stamping.
- **Decisions are signed by role.** Triggers record who confirmed a link or
  approved a price (`mos_nocodb` for the reviewer's interface, `pipeline` for
  automatic decisions), so the record does not rely on whoever wrote the row
  remembering to fill it in.
- **Closing a review item is not a mute.** An item resolved while its problem is
  still in the data comes back on the next run. A decision meant to last goes in
  the supplier's YAML, with who made it, when and why.
- **SKUs carry a check digit.** `MOS-GLA-0000123-4` uses mod 11 over the body
  because it catches adjacent transpositions (09 and 90), which Luhn misses.
  A CHECK constraint enforces it.

## Results on real data

The Artis pilot ran on the production database in November 2026:

- 3,529 supplier SKUs landed and normalised.
- 2,192 products and 2,957 variants created. 384 products have more than one
  variant; their groupings were confirmed by a human in NocoDB before pricing.
- Every variant has a preferred source and a price. All but 19 were approved
  through the gate; those 19 were held back because the supplier's file gives
  them an identical, implausible unit cost.
- Supplier data problems surfaced as review items rather than wrong products:
  capacities in the name contradicting the column, line marks the parser could
  not read, pack sizes that differ between the supplier's two files, and colour
  words that turned out to be range names.

The supplier files are not in this repository because they contain trade prices.

## Status

| | |
|---|---|
| **Ran on real data, production database** | Migrations 0001 to 0017; ingest, extract, normalise, link and price for the Artis files; the human review of groupings and prices |
| **Tested against a scratch database and in Docker only** | Migrations 0018 to 0020. Promoting barcodes and claims to the catalog (`pipeline/promote.py`). Image matching (`pipeline/images.py`): the pilot's image bucket was never connected, so it has not run on real files. Publish intent (`pipeline/publish.py`) and the runner (`pipeline/run.py`). The Shopify CSV export: it has never been imported into a store, and the column format for variant metafields is unverified until it is |
| **Designed, not built** | A second supplier with two variant axes (Stage 8), a desktop trigger for non-technical staff (Stage 9), the Shopify API sync and AI-written descriptions (Stage 10), the delivery-date engine |

The build sequence and the reasoning behind each stage are in
[`docs/mosferics_build_instructions.md`](docs/mosferics_build_instructions.md).

## Running it

### With Docker

The quickest way to see it work. Docker Compose starts PostgreSQL 16, creates
the four roles and two databases on first start, and builds the pipeline image.

```bash
git clone https://github.com/alt-ts/mosferics-pipeline.git && cd mosferics-pipeline
docker compose up -d db
docker compose run --rm pipeline                        # the test suite: 335 tests
docker compose run --rm pipeline bash docker/demo.sh    # the pipeline end to end
docker compose down -v                                  # stop and delete the data
```

The demo runs every module, in order, on a small **synthetic** supplier: an
invented brand, names, SKUs and prices in `sample_data/demo/`, generated by
`sample_data/make_demo_files.py` and described by `config/suppliers/demo.yaml`.
Each row is chosen to exercise one case: a capacity range with a lined twin, a
size and colour matrix with a gap that must stay a gap, a two-tone colour, a
capacity the name contradicts, two SKUs nothing tells apart, a safety
contradiction, a pack size the two files disagree on, an unknown material and a
category left for manual review. The 21 SKUs become 10 products and 17 variants,
and the six problems end up in the review queue rather than in the catalogue.
Twenty image keys cover the matching cases: numbered and unnumbered exact
names (recorded verified), a loose name (left for review), an orphan and a file
type the supplier config does not allow. The demo then marks every product for
the trade store and writes `out/demo_shopify.csv`: 9 products and 16 variants.
The tenth is held back because its only picture is the loose one still waiting
for review, and the demo says so. Before that, valid barcodes reach the
variants (one with a bad check digit does not), and the safety and material
claims wait as product metadata until the stand-in reviewer verifies them; a
bowl pair where only one claims microwave-safe gets no microwave claim at all,
and goes to the review queue instead. Last, the runner repeats the whole sequence
and finds nothing to do, which is the idempotency claim tested end to end.

The human review between steps is played by `docker/demo_review.py`, which
signs everything it confirms or approves as "demo script" and refuses to run
against a database that holds any other supplier. Reports land in `./out`.
`docker compose --profile review up -d nocodb` adds the NocoDB review interface
on port 8080 (connect it to the `db` service as `mos_nocodb`); that service is
included but was not exercised as part of the demo.

### By hand

You need Python 3.12 and PostgreSQL 16.

1. Create the database and the four roles. The migrations check that the roles
   exist before granting to them:

   ```sql
   CREATE ROLE mos_owner    LOGIN PASSWORD '...';  -- runs migrations
   CREATE ROLE mos_pipeline LOGIN PASSWORD '...';  -- runs the pipeline
   CREATE ROLE mos_nocodb   LOGIN PASSWORD '...';  -- the review interface
   CREATE ROLE mos_trigger  LOGIN PASSWORD '...';  -- a narrow role for a distributed trigger
   CREATE DATABASE mosferics OWNER mos_owner;
   ```

2. Install and configure:

   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env      # set DATABASE_URL (mos_owner) and PIPELINE_DATABASE_URL (mos_pipeline)
   ```

3. Build the schema and load the taxonomy:

   ```bash
   .venv/bin/alembic upgrade head
   .venv/bin/python -m pipeline.seed_taxonomy
   ```

4. Run the tests. The database tests need a separate scratch database migrated
   to head; without `TEST_DATABASE_URL` they are skipped, not passed.

   ```bash
   TEST_DATABASE_URL=postgresql+psycopg2://mos_pipeline:...@localhost/mosferics_scratch \
       .venv/bin/python -m pytest
   ```

   335 tests, 78 of which need the database. Every database test runs inside a
   transaction that is rolled back, and the fixtures refuse to run against the
   production database name.

CI (`.github/workflows/ci.yml`) does the same on every push against a fresh
PostgreSQL 16 container: it creates the roles, runs the migrations up, all the
way down and up again, then runs the tests, and fails if any test was skipped.
The role setup is `docker/postgres/init-roles.sh`, shared with the Docker setup.

## Repository layout

```
config/          supplier YAML, vocabularies, taxonomy, pricing and Shopify settings
docker/          role setup, test and demo scripts for Docker Compose
docs/            data model, build instructions, NocoDB review views, schema reviews
migrations/      Alembic migrations, 0001 to 0020
pipeline/        one module per step, plus shopify/
sample_data/     the synthetic demo supplier and its generator
tests/           pytest, unit and database tests
```

## How it was built

Built by Tom Storey with [Claude Code](https://claude.com/claude-code). The
design documents, the business decisions and the review of each migration
against the live schema were Tom's; much of the code was written with Claude, as
the `Co-Authored-By` trailers on the commits show.
