# Mosferics — NocoDB Views (Stage 5, step 5)

Companion to `mosferics_build_instructions.md` Stage 5. You have NocoDB connected to
Postgres with each schema as its own data source. This is how the seven review views
in that stage's table actually get built.

---

## The one decision that shapes everything

A NocoDB view is a saved filter/sort/group/hidden-fields configuration **over a single
table**. It cannot join. So the seven views in the build doc are not seven of the same
thing — they split cleanly in two:

| Kind | When | Built as | Editable? |
|---|---|---|---|
| **Filter view** | Everything you need is already on one table | NocoDB grid view | Yes — this is where the reviewer works |
| **Report view** | The list is defined by an *absence* — a row with no match in another table | A **Postgres view**, surfaced in NocoDB | No — read-only triage list |

The rule: **if the reviewer has to change the row, it must be a NocoDB filter view on the real
table.** Postgres views are read-only in practice, so anything he approves, confirms or
overrides has to sit on a live table or he cannot do his job.

Mapping the build doc's seven:

| Build doc view | Kind | Source |
|---|---|---|
| Needs review | Filter | `catalog.product_metadata` |
| Proposed links | Filter | `link.variant_links` |
| Unmapped categories | **Report** | `review.unmapped_categories` |
| Unmapped vocabulary | Filter | `ops.review_queue` |
| Missing images | **Report** | `review.missing_images` |
| Price changes | Filter | `pricing.variant_prices` |
| Ready to publish | **Report** | `review.ready_to_publish` |

**Unmapped vocabulary is the odd one out and worth understanding before you start.**
Unmapped *categories* can be computed, because `link.category_map` is a table — you
anti-join against it. The vocabulary alias lists are **YAML files in git**
(`config/vocabularies/`), not Postgres tables, so there is nothing in the database to
anti-join against. The only record that a supplier's colour value failed to normalise is
the row `normalise.py` writes to `ops.review_queue` with `entity_type = 'vocabulary'`.
That view is therefore a filter on the queue, and it only works once the pipeline is
writing those rows — until Stage 7 it will legitimately be empty.

---

## Order of work

1. Write and apply the `review` schema migration (Postgres views) — Part A
2. Grant `SELECT` to `mos_nocodb`
3. Connect `review` as a NocoDB data source, data-edit **off**
4. Build the four filter views — Part B
5. Lock down what the reviewer can reach — Part C

---

## Part A — the Postgres views

### Where they live

A new **`review` schema**, one view per report, no `v_` prefix needed because the schema
name already says what they are.

Why its own schema rather than `ops.v_*`: you connect it to NocoDB as a separate data
source with **Allow Data Edit off**, and every report view is read-only in one toggle
instead of per-view locking you have to remember for each new one. It also keeps a
read-only reporting surface visibly distinct from the real tables, which matters when
the reviewer is looking at the sidebar.

### Rule 5 applies — these go through Alembic

A view is DDL. `CLAUDE.md` rule 5 says all schema changes go through Alembic migrations,
no ad-hoc DDL, ever. That includes these. Do not paste them into `psql` even though it
would work: the next person to run `alembic upgrade head` on a clean instance gets a
database with no review views and no clue they were supposed to exist.

```bash
cd ~/mosferics-pipeline && source .venv/bin/activate
alembic revision -m "create review schema and reporting views"
```

This migration must come **after** all seven schema migrations, since the views reference
tables across `supplier`, `link`, `catalog`, `pricing` and `ops`.

### The migration
``
```python
def upgrade():
    op.execute("CREATE SCHEMA review")
    op.execute(UNMAPPED_CATEGORIES)
    op.execute(MISSING_IMAGES)
    op.execute(READY_TO_PUBLISH)
    # mos_nocodb needs to read them; ALL TABLES covers views too
    op.execute("GRANT USAGE ON SCHEMA review TO mos_nocodb")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA review TO mos_nocodb")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA review "
               "GRANT SELECT ON TABLES TO mos_nocodb")

def downgrade():
    op.execute("DROP SCHEMA review CASCADE")
```

`GRANT ... ON ALL TABLES IN SCHEMA` does include views — Postgres treats them as
relations here, so you do not need a separate grant statement. The
`ALTER DEFAULT PRIVILEGES` line is the same one you already needed for `mos_pipeline`
in Stage 2, for the same reason: without it, every view you add in a later migration
is invisible to NocoDB until someone re-runs the grant by hand.

**Views run with their owner's privileges**, so `mos_owner` (which owns the schemas, and
which Alembic connects as) must be able to read every underlying table. It can. This is
also why `mos_nocodb` needs no grants on the *underlying* tables to read a view —
handy if you ever want to expose a narrowed slice of something without granting the table.

### `review.unmapped_categories`

```sql
CREATE VIEW review.unmapped_categories AS
SELECT
    row_number() OVER (ORDER BY count(*) DESC, s.name) AS id,
    sp.supplier_id,
    s.name                       AS supplier,
    sp.supplier_category_raw,
    count(*)                     AS product_count,
    min(sp.name_raw)             AS example_product
FROM supplier.supplier_products sp
JOIN supplier.suppliers s  ON s.supplier_id = sp.supplier_id
LEFT JOIN link.category_map cm
       ON cm.supplier_id           = sp.supplier_id
      AND cm.supplier_category_raw = sp.supplier_category_raw
WHERE sp.supplier_category_raw IS NOT NULL
  AND cm.category_id IS NULL
  AND sp.discontinued IS NOT TRUE
GROUP BY sp.supplier_id, s.name, sp.supplier_category_raw;
```

`product_count DESC` is the point of the view, not decoration — it puts the raw category
covering 400 SKUs above the one covering 2, so an hour of mapping clears the most
catalogue. `example_product` saves you guessing what "GLASS-HB-MISC" means.

**This view only applies to suppliers whose YAML declares
`classification_method: mapped_column`.** BBP sends no category column at all, so it will
never appear here and its absence is correct, not a bug — BBP's unmapped products surface
through `derived_from_name` proposals in `ops.review_queue` instead. If you want the view
to say so explicitly, join `supplier.suppliers` to the YAML-declared method once you have
somewhere to store it.

The pair to this view is an **editable NocoDB grid on `link.category_map`** — the report
tells you what is missing, and you add the mapping rows there. Keep both open side by side.

### `review.missing_images`

```sql
CREATE VIEW review.missing_images AS
SELECT
    p.product_id            AS id,
    p.product_id,
    p.title,
    t.path                  AS category_path,
    b.name                  AS brand,
    count(v.variant_id)     AS variant_count
FROM catalog.products p
LEFT JOIN catalog.taxonomy t ON t.category_id = p.category_id
LEFT JOIN catalog.brands   b ON b.brand_id    = p.brand_id
LEFT JOIN catalog.variants v ON v.product_id  = p.product_id
WHERE p.status = 'active'
  AND NOT EXISTS (
        SELECT 1 FROM catalog.images i
        WHERE i.product_id = p.product_id
          AND i.status <> 'rejected'
      )
GROUP BY p.product_id, p.title, t.path, b.name;
```

`i.status <> 'rejected'` rather than `= 'verified'`, so a product with an image still
awaiting review does not show up as missing one — it isn't missing, it's pending, and
that's the *Needs review* view's job.

**Scope this to publish intent before you show it to anyone.** The data model is explicit
that enrichment and image work are gated on publish intent, not ingest — an unfiltered
list of every active product without an image is most of 60k rows and is not a worklist.
Add:

```sql
  AND EXISTS (
        SELECT 1 FROM catalog.variants v2
        JOIN ops.channel_publish cp ON cp.variant_id = v2.variant_id
        WHERE v2.product_id = p.product_id
          AND cp.publish_status = 'pending'
      )
```

That turns it from a 60k-row inventory into "things we are actually trying to publish and
cannot".

### `review.ready_to_publish`

```sql
CREATE VIEW review.ready_to_publish AS
SELECT
    cp.variant_id || ':' || cp.channel_id AS id,
    cp.variant_id,
    cp.channel_id,
    v.mosferics_sku,
    p.title,
    pr.final_price,
    pr.margin_pct,
    cp.fulfilment_mode
FROM ops.channel_publish cp
JOIN catalog.variants v ON v.variant_id = cp.variant_id
JOIN catalog.products p ON p.product_id = v.product_id
JOIN pricing.variant_prices pr
     ON pr.variant_id = cp.variant_id
    AND pr.channel_id = cp.channel_id
WHERE cp.publish_status <> 'published'
  AND p.status = 'active'
  AND pr.status = 'approved'
  -- has a preferred source
  AND EXISTS (SELECT 1 FROM link.variant_links l
              WHERE l.variant_id = cp.variant_id AND l.is_preferred)
  -- at least one verified image
  AND EXISTS (SELECT 1 FROM catalog.images i
              WHERE i.product_id = p.product_id AND i.status = 'verified')
  -- nothing still awaiting review
  AND NOT EXISTS (SELECT 1 FROM catalog.product_metadata m
                  WHERE m.product_id = p.product_id AND m.status = 'pending_review');
```

Three things about this one.

**It is keyed by variant × channel, not by product.** `publish_status` and price both live
per channel, so "ready to publish" is only answerable for a given storefront. The
synthetic `id` column exists because NocoDB wants one unique column per table and a
composite key won't do — see the gotchas.

**"Everything verified" is expressed as `NOT EXISTS ... pending_review`, not "all rows
verified".** Those differ: the second would also pass a product with *zero* metadata rows,
which is not verified, it's empty. The version above at least won't block on emptiness,
but it will happily publish a product nobody has described. The real fix is a
minimum-required-fields check per taxonomy node, which you do not have yet — note it and
move on, but do not mistake this view for that check.

**The status string literals are the thing most likely to be wrong.** `'approved'`,
`'verified'`, `'pending_review'`, `'pending'` — the data model enumerates values for
`link.variant_links.status`, `catalog.product_metadata.status`,
`ops.review_queue.status` and `ops.channel_publish.publish_status`, but **not** for
`pricing.variant_prices.status` or `catalog.images.status`. Whatever your Alembic
migrations actually created is authoritative. Check before you trust any of these views:

```sql
SELECT DISTINCT status FROM pricing.variant_prices;
SELECT DISTINCT status FROM catalog.images;
-- or, if you used CHECK constraints / enums:
\d+ pricing.variant_prices
```

A silently-empty view because the literal is `pending_approval` and you wrote `'pending'`
is the single most likely way this stage wastes an afternoon.

### Then make NocoDB see them

```bash
alembic upgrade head
alembic downgrade -1 && alembic upgrade head    # prove down+up, as with every migration
```

NocoDB does **not** notice new database objects on its own. In the NocoDB UI:

**Base Settings → Data Sources → `review` → Meta Sync tab → Reload → Sync Now.**

New objects appear flagged in the Sync State column; `Sync Now` pulls them in. You will
repeat this every time you add or change a review view. If a view you just created does
not show up here, it is almost always a missing `GRANT` — `mos_nocodb` cannot see what it
has no `SELECT` on.

Connect `review` as a data source the same way you connected the other schemas (host
`localhost`, port `5432`, database `mosferics`, user `mos_nocodb`), and set
**Allow Data Edit off** and **Allow Schema Edit off** on it.

---

## Part B — the NocoDB filter views

### How to build one (do this once, slowly)

Take *Price changes* as the worked example.

1. In the sidebar, expand the `pricing` data source and click `variant_prices`.
2. Click **+ New View** below the table name → **Grid**.
3. Name it `Price changes`. Names are what the reviewer navigates by — keep them exactly as the
   build doc's table, not "price changes v2 FINAL".
4. Set **Who can edit** to **Collaborative**. Not Locked — the reviewer must be able to edit
   these rows. (Locked blocks edits to the view *and* its records until someone unlocks
   it, which is right for report views and wrong here.)
5. **Create View.**
6. **Fields** in the toolbar → hide everything the reviewer does not need. This matters more
   than the filter. A 20-column grid where 5 columns are relevant is nearly as unusable
   as an unfiltered one.
7. **Filters** → *Add filter* → `status` `is equal` `<your pending value>`. Add a second
   filter where useful; the nested-group control gives you AND/OR if you need
   `margin_pct < 15 OR status = pending`.
8. **Sort** → the column that decides what he does first.
9. **Group By** → up to three levels of nesting, which is what the build doc means by
   "grouped by field type".

Then repeat for the other three. Everything below is that same sequence with different
values.

### The four filter views

**Needs review** — on `catalog.product_metadata`

| Setting | Value |
|---|---|
| Filter | `status` is `pending_review` |
| Group by | `key` — this is the "grouped by field type" the build doc asks for |
| Sort | `product_id` asc, so all fields for one product sit together within a group |
| Show | `product_id`, `key`, `value`, `canonical_value`, `unit`, `source`, `status` |
| Hide | `verified_by`, `verified_at` — NocoDB will not fill these for you; see gotchas |
| Editing | Collaborative |

Grouping by `key` is what makes this view work: reviewing 300 `dishwasher_safe` values in
one pass is a different and far faster job than reviewing 300 mixed fields one product at
a time. The data model is deliberately key/value here precisely so oven-safe can be
verified while dimensions are still pending.

**Proposed links** — on `link.variant_links`

| Setting | Value |
|---|---|
| Filter | `status` is `proposed` |
| Sort | `match_confidence` **desc** |
| Group by | `match_type` |
| Show | `supplier_product_id`, `variant_id`, `match_type`, `match_confidence`, `status`, `is_preferred` |
| Editing | Collaborative |

Confidence-descending is deliberate: the top of the list is near-certain EAN matches he
can confirm in bulk, and the judgement calls collect at the bottom. Grouping by
`match_type` separates "same item, two suppliers" (`identical`) from "different sellable
things under one product" (`equivalent` / `substitute`) — two different questions that are
slow to answer if they're interleaved.

`is_preferred` is safe for him to tick directly. The sibling-clearing trigger on
`variant_links` unsets the previous winner in the same statement, so he will not hit the
partial unique index — that trigger exists specifically so this view is a one-click job.

**Unmapped vocabulary** — on `ops.review_queue`

| Setting | Value |
|---|---|
| Filter | `entity_type` is `vocabulary` **AND** `status` is `open` |
| Group by | `issue_type` |
| Show | `queue_id`, `entity_type`, `issue_type`, `notes`, `status`, `assigned_to` |
| Editing | Collaborative |

Empty until `normalise.py` exists — see the note at the top. Once it does, the workflow
is: read the unmapped value here, add the alias to the YAML in
`config/vocabularies/<x>.yaml`, commit, re-run, set the row to `resolved`. The alias goes
in git, not in NocoDB. Do not be tempted to add a vocabulary table to make this editable
in place — the YAML is the source of truth and a second copy would drift.

While you are on this table, build a second view, **All open review items**: filter
`status` is `open`, group by `entity_type`. That is the master queue — the one number that
tells you how much human work is outstanding across all six entity types. The per-table
views above are where the work is *done*; this is where you see it exists.

**Price changes** — on `pricing.variant_prices`

| Setting | Value |
|---|---|
| Filter | `status` is your pending-approval value |
| Sort | `margin_pct` asc — margin-floor breaches first |
| Show | `variant_id`, `channel_id`, `calculated_price`, `override_price`, `final_price`, `margin_pct`, `status` |
| Editing | Collaborative |

This is the one view where the reviewer's Stage 0 permission actually bites: he edits
`override_price`, and `final_price` is `COALESCE(override_price, calculated_price)`, so a
price refresh never wipes his number. Make sure `calculated_price` and `override_price` are
**both visible and adjacent** — the whole judgement is "what did the rules say, and what
am I overriding it to". Sorting on margin ascending puts the sub-15% floor breaches at the
top, which are the ones that queued regardless of size.

### The category-map pair

Not in the build doc's seven, but you need it for *Unmapped categories* to be actionable:
a Collaborative grid view on `link.category_map`, showing `supplier_id`,
`supplier_category_raw`, `category_id`, `confirmed_by`, `confirmed_at`. If NocoDB has
picked up the foreign key to `catalog.taxonomy`, `category_id` renders as a record picker
rather than an integer field, which turns mapping from "look up the ID" into a dropdown.
Worth checking; if it hasn't, Meta Sync usually fixes it.

---

## Part C — locking it down

**Set per-data-source permissions.** This is the cleanest control you have, and it is the
reason connecting each schema separately was a good decision. Base Settings → Data
Sources → each source:

| Source | Allow Data Edit | Allow Schema Edit |
|---|---|---|
| `raw` | **Off** | Off |
| `review` | **Off** | Off |
| `supplier` | On | Off |
| `catalog` | On | Off |
| `link` | On | Off |
| `pricing` | On | Off |
| `ops` | On | Off |
| `stock` | On | Off |

`raw` off is the build doc's "lock `raw` tables to read-only in the UI" — and it is
stronger than locking each view, because it survives someone adding a new view later.
`mos_nocodb` already has no `DELETE` on `raw` at the database level; this makes the UI
agree rather than showing the reviewer buttons that will fail.

**Schema Edit off everywhere** is not optional. It is `CLAUDE.md` rule 5 enforced at the
UI layer: schema changes go through Alembic, and a NocoDB "add column" button that
silently works would put the database out of step with the migration history. `mos_nocodb`
has no DDL rights anyway, so this is belt and braces — but it stops the confusing failure
rather than letting someone hit it.

**Give the reviewer the Editor role** on the base. Editor can read and write records but cannot
create or drop tables.

**Be honest with yourself about one limit:** NocoDB has no per-view access control for
internal collaborators — you cannot grant the reviewer *only* the seven views. He will see each
table's default grid view in the sidebar too, unfiltered. Sharing a view by link does not
solve it either: shared grid views are read-only, so they cannot carry the work he has to
do. Two mitigations, both worth doing:

- On each table's **default** view, trim the fields down and set it to **Locked**, so it
  stays a deliberate, narrow thing rather than a 60k-row 20-column wall.
- Order the sidebar so the named views sit above the default, and name them as the build
  doc does. In practice the reviewer clicks the thing with his job's name on it.

The security boundary is the `mos_nocodb` grants and the data-source toggles, not view
visibility. Treat "sees only the filtered views" as an ergonomic goal rather than an
access control.

---

## Gotchas

**A Postgres view with no single unique column confuses NocoDB.** It expects one primary
key per relation. `review.ready_to_publish` is keyed by `(variant_id, channel_id)`, which
is why it synthesises `cp.variant_id || ':' || cp.channel_id AS id`. Give every review
view exactly one such column, first in the select list. `row_number() OVER (...)` is fine
for aggregate views where no natural key exists — it is not stable between refreshes, but
nothing joins to it.

**NocoDB brings in views from schemas you did not select.** There is a long-standing bug
where the schema filter is applied to tables but not to views, so views from other schemas
can appear under a data source. If you see `review.*` turning up under `ops`, that is why —
it is cosmetic, not a leak, but it explains a confusing sidebar.

**Meta Sync after every migration that touches a view.** Covered above, easy to forget,
and the symptom (view exists in Postgres, invisible in NocoDB) looks like a permissions
problem.

**`verified_by` / `verified_at` do not fill themselves.** NocoDB writes the columns you
edit, nothing more. If the reviewer flips `status` to `verified`, those two stay null unless
something sets them. Options, in order of preference: a `BEFORE UPDATE` trigger on
`product_metadata` that stamps them when `status` changes (add it in a migration — it
works regardless of which client did the update, including the pipeline), or NocoDB's own
LastModifiedBy field type, which records the *NocoDB* user and so knows nothing about
pipeline writes. Take the trigger.

**Filters are saved config, not a security boundary.** A Collaborative view's filter can be
changed by anyone who can edit the view. That is fine — it is what makes the views usable
— but it means a filter is never what stops someone reaching a row. The grants are.

**Don't build views against tables the pipeline hasn't populated yet and then conclude
they're broken.** *Unmapped vocabulary*, *Missing images* and *Ready to publish* are all
legitimately empty until Stage 7 runs. Verify each view's SQL with a hand-written
`SELECT` against real data before deciding a NocoDB view is misconfigured — the query is
almost always the problem, and `psql` tells you faster than the UI does.

---

## Done when

- `alembic upgrade head` creates the `review` schema and its three views from empty, and
  `downgrade -1` removes them cleanly
- `psql -U mos_nocodb` can `SELECT` from all three review views
- All three appear in NocoDB after Meta Sync, read-only
- The four filter views plus `All open review items` and the `link.category_map` grid
  exist, with fields trimmed and grouping set
- `raw` and `review` have Allow Data Edit off; every source has Allow Schema Edit off
- the reviewer, as Editor, can change a `override_price` and flip a `product_metadata` status,
  and cannot edit anything under `raw`
- Every status literal in the three views has been checked against
  `SELECT DISTINCT status FROM ...` rather than assumed
