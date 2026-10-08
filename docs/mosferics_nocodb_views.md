# Mosferics — NocoDB Views (Stage 5, step 5)

Companion to `mosferics_build_instructions.md` Stage 5. You have NocoDB connected to
Postgres with each schema as its own data source. This is how the seven review views
in that stage's table actually get built.

> **Part A is built and applied to live at migration `2b9b2f63d30b`** (revises `0013`).
> The SQL below is what that migration actually ships, and three statements in it
> differ from this document's first draft — each because a CHECK constraint in an
> earlier migration contradicted the prose. Those three are marked **Corrected**
> where they appear, with the failure each one avoids, because every one of them
> fails *silently*: a wrong view here returns too few rows and looks like an empty
> worklist rather than a bug. Parts B and C are still to do in the NocoDB UI.

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

1. Write and apply the `review` schema migration (Postgres views) — Part A ✅ `2b9b2f63d30b`
2. Grant `SELECT` to `mos_nocodb` — **inside that same migration**, not a separate step.
   `GRANT` is DDL and hard rule 5 admits no exception for privileges; `0013` settled
   this for the data schemas and `review` follows it
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

This migration must come **last**, after every schema migration the views read — they
reference tables across `supplier`, `link`, `catalog`, `pricing` and `ops`, so it cannot
be applied before those exist. It landed on `0013`, which is the role grants rather than a
schema; the six data schemas are `0001`–`0010` and `stock` is still deferred, so "after
all seven schemas" is not the test. The test is simply: last.

### The migration

```python
VIEWS = ("unmapped_categories", "missing_images", "ready_to_publish")

def upgrade():
    op.execute("CREATE SCHEMA review")
    op.execute(UNMAPPED_CATEGORIES)
    op.execute(MISSING_IMAGES)
    op.execute(READY_TO_PUBLISH)
    for comment in COMMENTS:          # one COMMENT ON per schema and view
        op.execute(comment)
    # mos_nocodb needs to read them; ALL TABLES covers views too
    op.execute("GRANT USAGE ON SCHEMA review TO mos_nocodb")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA review TO mos_nocodb")
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA review "
               "GRANT SELECT ON TABLES TO mos_nocodb")

def downgrade():
    # Revoked before the schema goes, so no pg_default_acl row can outlive
    # the schema it names.
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA review "
               "REVOKE SELECT ON TABLES FROM mos_nocodb")
    for view in reversed(VIEWS):
        op.execute(f"DROP VIEW review.{view}")
    op.execute("DROP SCHEMA review CASCADE")
```

**Corrected — `FOR ROLE mos_owner` is load-bearing and the first draft omitted it.**
Default privileges attach to *the role that will create the object*, defaulting to
whoever runs the statement. Without the clause, the statement still succeeds and
`pg_default_acl` still gains a row — but it records the wrong creating role, so every
view a *later* migration adds is born invisible to NocoDB and the symptom looks like a
permissions problem months later. Stage 2 of the build instructions calls this "the
trap inside the trap"; `0013` spells it out everywhere for the same reason. Prove it
the way `0013` does, with a probe:

```sql
CREATE VIEW review._probe AS SELECT 1 AS id;
SELECT has_table_privilege('mos_nocodb', 'review._probe', 'SELECT');  -- must be t
DROP VIEW review._probe;
```

The explicit `DROP VIEW` loop before `DROP SCHEMA ... CASCADE` is not redundant: it
means the `CASCADE` has nothing left to reach, so the downgrade states what it removes
instead of trusting a blanket cascade on a production database.

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
    row_number() OVER (
        ORDER BY count(*) DESC, s.name, sp.supplier_category_raw
    )                            AS id,
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

`supplier_category_raw` is the third `ORDER BY` key purely so the `row_number()` is
deterministic within a single query when two of a supplier's categories tie on count.
It does not change which rows come back or their ranking by weight.

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
          AND i.status IS DISTINCT FROM 'rejected'
      )
  AND EXISTS (
        SELECT 1 FROM catalog.variants v2
        JOIN ops.channel_publish cp ON cp.variant_id = v2.variant_id
        WHERE v2.product_id = p.product_id
          AND cp.publish_status = 'pending'
      )
GROUP BY p.product_id, p.title, t.path, b.name;
```

Not `= 'verified'`, so a product with an image still awaiting review does not show up as
missing one — it isn't missing, it's pending, and that's the *Needs review* view's job.

**Corrected — `IS DISTINCT FROM 'rejected'`, not `<> 'rejected'`.** `catalog.images.status`
is **nullable free text with no CHECK and no default** (`0005`), unlike
`product_metadata.status` which is constrained. So `i.status <> 'rejected'` evaluates to
NULL — not true — for an image whose status has not been set; the `EXISTS` then finds
nothing and a product that *has* an image is reported as missing one. Since nothing
currently sets a status on insert, unset is the state every image the matcher writes
will arrive in, so the plain `<>` would be wrong for essentially every row.
`IS DISTINCT FROM` is NULL-safe and keeps the intent above. Demonstrated on scratch:

```text
 doc's `<> 'rejected'`        mine: `IS DISTINCT FROM 'rejected'`
 ---------------------        ----------------------------------
 p1-no-images                 p1-no-images
 p2-null-status   <-- wrong
 p3-rejected                  p3-rejected
```

**The publish-intent filter is part of the view, not an afterthought.** The data model is
explicit that enrichment and image work are gated on publish intent, not ingest — an
unfiltered list of every active product without an image is most of 60k rows and is not a
worklist. The `EXISTS` on `channel_publish.publish_status = 'pending'` is what turns it
into "things we are actually trying to publish and cannot", so it ships in the view
rather than being left for someone to remember to add.

### `review.ready_to_publish`

```sql
CREATE VIEW review.ready_to_publish AS
SELECT
    cp.variant_id::text || ':' || cp.channel_id::text AS id,
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
  AND pr.status IN ('approved', 'auto_approved')   -- both are live prices
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

Four things about this one.

**It is keyed by variant × channel, not by product.** `publish_status` and price both live
per channel, so "ready to publish" is only answerable for a given storefront. The
synthetic `id` column exists because NocoDB wants one unique column per table and a
composite key won't do — see the gotchas. Both joins are to tables whose primary key is
`(variant_id, channel_id)`, so neither fans the row out.

`l.is_preferred` carries no status test because it does not need one: `0007`'s
`ck_variant_links_preferred_is_confirmed` makes `is_preferred` imply
`status = 'confirmed'`, and its demotion trigger maintains that. Adding
`AND l.status = 'confirmed'` would be harmless but redundant.

**"Everything verified" is expressed as `NOT EXISTS ... pending_review`, not "all rows
verified".** Those differ: the second would also pass a product with *zero* metadata rows,
which is not verified, it's empty. The version above at least won't block on emptiness,
but it will happily publish a product nobody has described. The real fix is a
minimum-required-fields check per taxonomy node, which you do not have yet — note it and
move on, but do not mistake this view for that check.

This is not hypothetical, and the scratch fixtures prove it: a product with a verified
image, a preferred link, an approved price and **no metadata rows at all** appears in
this view. The view comment on the live view says so too, so anyone reading
`\d+ review.ready_to_publish` finds the caveat without having to find this document.

**Nothing guarantees `'verified'` is the literal the image matcher will write.**
`catalog.images.status` has no CHECK to check it against and the table is empty, so the
strict `i.status = 'verified'` here is a convention, not an enforced contract. The
asymmetry with `missing_images` is deliberate — a publish gate *must* require positive
verification, so NULL failing is correct in this view and wrong in that one — but if
`ready_to_publish` comes up unexpectedly empty after Stage 7, this is the first thing to
check, before anything in NocoDB.

**Corrected — the price gate is `IN ('approved', 'auto_approved')`, not `= 'approved'`.**
This was predicted as the most likely error in this stage and it was: the first draft read
`pr.status = 'approved'`. `0010` constrains the column to
`('pending_approval', 'auto_approved', 'approved', 'rejected')` and says in its own
comment that *auto_approved and approved are both live prices; the distinction is how they
got there* — which is what the materiality threshold decides. With a 5% threshold, most
prices never queue at all and land on `auto_approved`, so `= 'approved'` hides the
majority of a working catalogue behind a view that looks merely quiet. Demonstrated on
scratch with four qualifying rows: the single-literal predicate returned two of them.

**The status literals, as the migrations actually created them.** The build doc's first
draft noted that the data model enumerates values for some of these columns and not
others, and said to check the migrations. That advice stands, and this is the result of
doing it — these are now authoritative, and two of them are the reason for the two
corrections above:

| Column | Constrained values | Migration |
|---|---|---|
| `pricing.variant_prices.status` | `pending_approval` (default), `auto_approved`, `approved`, `rejected` | `0010` |
| `catalog.product_metadata.status` | `pending_review` (default), `verified`, `rejected` | `0005` |
| `link.variant_links.status` | `proposed` (default), `confirmed`, `rejected` | `0006` |
| `link.variant_links.match_type` | `identical`, `equivalent`, `substitute` | `0006` |
| `ops.channel_publish.publish_status` | `not_published` (default), `pending`, `published` | `0009` |
| `ops.channel_publish.fulfilment_mode` | `stocked`, `on_demand`, `dropship`, or NULL | `0009` |
| `ops.review_queue.status` | `open` (default), `resolved`, `dismissed` | `0009` |
| `ops.review_queue.entity_type` | `product_metadata`, `variant_link`, `variant_price`, `category_map`, `vocabulary`, `image` | `0009` |
| `ops.channels.type` | `trade_store`, `event_store` | `0009` |
| **`catalog.images.status`** | **none — nullable free text, no CHECK, no default** | `0005` |
| **`catalog.products.status`** | **none — free text, defaults to `active`** | `0005` |
| `ops.review_queue.issue_type` | none — free text | `0009` |

The two bold rows are unconstrained, which is exactly why `missing_images` has to be
NULL-safe about `images.status`. Treat any literal you write against them as a convention
the database will not enforce for you.

`SELECT DISTINCT status FROM ...` cannot settle these yet — every table is empty until
Stage 7 runs, so it returns nothing and reads as agreement. The CHECK constraints are the
authority until there is data.

### Then make NocoDB see them

```bash
alembic upgrade head
alembic downgrade -1 && alembic upgrade head    # prove down+up, as with every migration
```

Run that against the **scratch** database first (hard rule 12), and seed it before you
believe the result. All seven tables these views read are empty until Stage 7, so three
views returning zero rows on an empty database proves only that the SQL parses. The
fixtures that actually test them have to **vary the deciding factor** in each view:

| View | Factor to vary | Rows that must and must not appear |
|---|---|---|
| `unmapped_categories` | category mapped / unmapped, count per category, `discontinued`, NULL category, a supplier sending no category column at all | heavy category ranks above light; mapped, discontinued-only and NULL excluded; BBP absent |
| `missing_images` | image status across **NULL / `rejected` / `verified`**, and publish intent present / absent | no-image and rejected-only appear; **NULL-status does not**; verified and no-intent do not |
| `ready_to_publish` | price status across **all four** legal values, preferred link present / absent, metadata pending / verified / absent, `publish_status` | `approved` and `auto_approved` appear; the other two and every failed gate do not |

A fixture that holds the deciding factor constant passes against a broken view. The
NULL-status image and the `auto_approved` price are the two rows that catch the two
corrections above, and neither exists in a naive happy-path fixture.

Then prove the downgrade leaves nothing behind:

```sql
SELECT count(*) FROM pg_namespace WHERE nspname = 'review';               -- 0
SELECT count(*) FROM pg_default_acl d
  JOIN pg_namespace n ON n.oid = d.defaclnamespace
 WHERE n.nspname = 'review';                                              -- 0
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
7. **Filters** → *Add filter* → `status` `is equal` `pending_approval`. Add a second
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
| Show | `queue_id`, `entity_type`, `issue_type`, `notes`, `status`, `assigned_to`, `resolved_by`, `resolved_at` |
| Editing | Collaborative |

Empty until `normalise.py` exists — see the note at the top. Once it does, the workflow
is: read the unmapped value here, add the alias to the YAML in
`config/vocabularies/<x>.yaml`, commit, re-run, set the row to `resolved`. The alias goes
in git, not in NocoDB. Do not be tempted to add a vocabulary table to make this editable
in place — the YAML is the source of truth and a second copy would drift.

**`resolved_by` and `resolved_at` must be in the Show list, and that is not optional
here.** `0009` puts three paired constraints on this table, and together they mean the
last step of the workflow above *raises* if the reviewer only flips `status`:

```text
UPDATE ops.review_queue SET status = 'resolved' WHERE queue_id = 1;
ERROR:  new row for relation "review_queue" violates check constraint
        "ck_review_queue_closed_has_resolver"
```

`ck_review_queue_closed_has_resolver` requires a resolver on anything not `open`, and
`ck_review_queue_resolution_complete` requires `resolved_by` and `resolved_at` to be set
or null together. So resolving a row is a three-column edit, and a view that hides two of
them gives the reviewer a constraint error with no way to satisfy it.

This is the opposite failure to `product_metadata`, which has **no** such pairing
constraint: flipping that to `verified` succeeds and silently leaves the audit columns
null. One table refuses the incomplete edit, the other accepts it quietly. Both want the
same fix — a `BEFORE UPDATE` trigger that stamps the resolver — and neither has one yet;
see the gotchas.

While you are on this table, build a second view, **All open review items**: filter
`status` is `open`, group by `entity_type`. That is the master queue — the one number that
tells you how much human work is outstanding across all six entity types. The per-table
views above are where the work is *done*; this is where you see it exists.

**Price changes** — on `pricing.variant_prices`

| Setting | Value |
|---|---|
| Filter | `status` is `pending_approval` |
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

**`verified_by` / `verified_at` do not fill themselves, and the trigger is not built
yet.** NocoDB writes the columns you edit, nothing more. If the reviewer flips `status` to
`verified`, those two stay null unless something sets them. Options, in order of
preference: a `BEFORE UPDATE` trigger on `product_metadata` that stamps them when
`status` changes (add it in a migration — it works regardless of which client did the
update, including the pipeline), or NocoDB's own LastModifiedBy field type, which records
the *NocoDB* user and so knows nothing about pipeline writes. Take the trigger.

**As of `2b9b2f63d30b` that trigger does not exist**, on `product_metadata` or on
`review_queue`. The only auto-signing triggers in the database are the two `0008` added
to `link.variant_links` (`trg_variant_links_sign_confirmation`) — which is why confirming
a *link* needs no NocoDB configuration while verifying *metadata* and resolving a *queue
item* both do. Confirm what is actually there before relying on any of it:

```sql
SELECT n.nspname || '.' || c.relname AS tbl, t.tgname
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE NOT t.tgisinternal
 ORDER BY 1, 2;
```

Until those triggers land, the two tables fail differently and both need handling in the
view config: `review_queue` **raises** on an incomplete resolve (so show the columns),
`product_metadata` **accepts** one silently (so the audit trail quietly rots). Modelling
the second on the first would be the better outcome — a pairing constraint plus a signing
trigger — but that is a migration, not a NocoDB setting.

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

- ✅ `alembic upgrade head` creates the `review` schema and its three views from empty, and
  `downgrade -1` removes them cleanly — including the `pg_default_acl` row
- ✅ `mos_nocodb` has `USAGE` on the schema and `SELECT` on all three views, and `INSERT` /
  `UPDATE` on none of them
- ✅ A view created *after* the migration is already readable by `mos_nocodb` — the
  `FOR ROLE mos_owner` probe
- ✅ Every status literal in the three views has been checked against the CHECK
  constraints in `0005`–`0010`, which are authoritative while the tables are empty, and
  each view has been run against fixtures that vary its deciding factor
- All three appear in NocoDB after Meta Sync, read-only
- The four filter views plus `All open review items` and the `link.category_map` grid
  exist, with fields trimmed and grouping set
- `raw` and `review` have Allow Data Edit off; every source has Allow Schema Edit off
- the reviewer, as Editor, can change an `override_price` and flip a `product_metadata` status,
  and cannot edit anything under `raw`
- the reviewer can resolve an `ops.review_queue` row — which needs `resolved_by` and
  `resolved_at` visible, or a signing trigger, or it raises

The first four are done at `2b9b2f63d30b`. The rest are Parts B and C, in the UI.
