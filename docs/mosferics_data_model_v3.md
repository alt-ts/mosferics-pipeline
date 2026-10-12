# Mosferics Data Model — Concrete Specification

One PostgreSQL database. Seven schemas. Tables store; engines run. NocoDB reads/writes the tables; the pipeline runs the engines.

---

## Core principle: three product concepts, not two

You identified two. There are three, and conflating the second and third is what makes unit handling messy.

| Concept | Example | Lives in |
|---|---|---|
| **Supplier product** | Artis `ART-1234`, "Blue Roll 2ply", their catalogue entry | `supplier.supplier_products` |
| **Mosferics product** | "Blue roll 2ply" — the abstract thing we sell | `catalog.products` |
| **Mosferics variant** | "Blue roll 2ply — box of 6" — the sellable unit | `catalog.variants` |

The variant is what becomes a Shopify variant, carries a price, and holds stock. The product is what carries the description, images and metadata.

**Pack size is not the variant axis.** We sell in boxes; a customer ordering quantity 2 gets two boxes. So most products have exactly one variant. Variants still earn their own table because genuine variant axes do exist in this catalogue — glass capacity, colour, size. Pack size is a *field*, not an axis.

**Base unit is mandatory, and internal only.** Every Mosferics product declares one (`roll`, `glass`, `litre`, `each`). Every supplier offer declares `base_units_per_pack`. Cost-per-base-unit is what makes Artis's case of 6 comparable to BBP's case of 10. It never surfaces on the storefront.

**Pack size generates the title.** `pack_size` is a real field used to build the display name — never hand-typed — or the catalogue ends up with "Box of 12", "Case of 12" and "12pk" all meaning the same thing. If a supplier changes their pack from 6 to 10, that is effectively a different product to the customer: import flags it for review rather than silently updating.

---

## `raw` — audit trail, never edited

| Table | Columns |
|---|---|
| `raw.import_batches` | `batch_id`, `supplier_id`, `filename`, `file_hash`, `received_at`, `row_count`, `status` |
| `raw.import_rows` | `batch_id`, `row_number`, `payload` (JSONB — the row exactly as received) |

Why: when a price looks wrong six months from now, you can prove what the supplier actually sent. Costs almost nothing to keep.

---

## `supplier` — their world, standardised

**`supplier.suppliers`**
`supplier_id`, `name`, `code`, `config_path` (the YAML in git), `account_ref`, `min_order_value`, `carriage_paid_threshold`, `default_lead_time_days`, `currency`, `availability_source` (live_feed / static_lead_time / unknown), `active`

**`supplier.supplier_products`** — identity and specs, one row per supplier SKU
`supplier_product_id`, `supplier_id`, `supplier_sku`, `name_raw`, `name_clean`, `brand`, `supplier_category_raw`, `description_raw`, `description_enriched`, `description_source` (supplier/ai/manual), `description_status`, `specs` (JSONB), `first_seen`, `last_seen`, `discontinued`

**`supplier.supplier_offers`** — commercial terms, changes over time
`offer_id`, `supplier_product_id`, `pack_description`, `base_units_per_pack`, `cost_ex_vat`, `moq`, `lead_time_days`, `valid_from`, `valid_to`, `is_current`

**`supplier.supplier_stock`** — where a live feed exists
`supplier_product_id`, `qty_available`, `next_delivery_date`, `fetched_at`, `feed_status`

**Supplier stock is not our stock** and never enters `stock.levels` — mixing them destroys the ability to answer "what do we actually own". A feed does not change `fulfilment_mode`; goods still route through our warehouse. It changes confidence only, tracked as `availability_source` on the supplier: `live_feed` / `static_lead_time` / `unknown`.

Rules: publish bands (In stock / Low / Made to order / Unavailable), never raw quantities — 24-hour-old numbers are a promise we can't keep, and they leak supplier commercial data. Expire rows past a staleness window and fall back to static lead time, because feeds fail quietly.

**`specs` as JSONB:** you don't yet know the full metadata field list and it varies per supplier. Land everything in JSONB, then promote fields to real columns once they prove stable and need indexing (dimensions, oven-safe, material). This is the right call given the field list is still coming.

---

## `link` — the join between their world and yours

**`link.category_map`** — deterministic, maintained by you
`supplier_id`, `supplier_category_raw`, `category_id`, `confirmed_by`, `confirmed_at`

`category_id` is a foreign key to `catalog.taxonomy.category_id`. The map translates; `catalog.products.category_id` is where the category actually lives. Unmapped raw strings go to the review queue rather than producing uncategorised products.

**Not every supplier sends a category column.** BBP sends none at all, so there is nothing to key the map on — yet every product still needs a `category_id`. Each supplier declares a `classification_method` in its YAML:

| Method | Use |
|---|---|
| `mapped_column` | Map `supplier_category_raw` through `category_map` |
| `derived_from_name` | Keyword rules against the product name, proposed then confirmed |
| `manual` | One-time assignment per SKU, persisted; correct for small or irregular ranges |

Manual is not a failure state. For a few hundred SKUs it is faster and more accurate than building rules, and it never needs revisiting.

**`link.variant_links`** — fuzzy, proposed by pipeline, confirmed by human
`supplier_product_id`, `variant_id`, `match_type` (identical / equivalent / substitute), `match_confidence`, `status` (proposed / confirmed / rejected), `confirmed_by`, `confirmed_at`, `is_preferred`

**Links resolve at variant level, not product level.** Two different operations happen here and only variant-level linking expresses both:

- *Multi-source matching* — Artis white plate and BBP white plate are the same sellable thing from different suppliers → both link to one variant
- *Variant grouping* — white plate and blue plate are different sellable things under one product → different variants, same product

Suppliers send colour variants as separate SKUs on separate rows, so grouping them is a pipeline job, not something the supplier file does for you.

Every supplier product gets a link — single-source items get an auto-created 1:1 link at confidence 1.0. Only genuine multi-source candidates and proposed variant groupings enter the review queue.

**`is_preferred` is where preferred source lives.** The Sourcing engine weighs cost and lead time across a variant's links and marks the winner. A single-source variant's sole 1:1 link is preferred by default. It is deliberately the link, not a pointer column on the product or variant, so it can never name a supplier the variant isn't actually linked to — integrity is automatic, not something the builder has to remember to enforce.

**Preferred implies confirmed.** Tightened in migration 0007: `is_preferred` requires `status = 'confirmed'`, not merely "not rejected". Sourcing can therefore never be pointed at a match no human has agreed to. Two consequences follow, and both are real work rather than bookkeeping:

- **The auto-created 1:1 link must be written as `confirmed`.** Single-source items are preferred by default, so `link.py` has to insert them with `status = 'confirmed'`. Recording such a link as confirmed is consistent with the rule above — it is never something a human is asked about, so confirmed is the honest state rather than a shortcut. But it is the pipeline asserting a confirmation, which is why every confirmation is signed.

  **Confirmations sign themselves.** Migration 0008 added `link.sign_confirmation()`, a `BEFORE INSERT OR UPDATE` trigger that fires when a row is confirmed with no `confirmed_by`, and fills in `current_user` and `now()`. So confirming is a single write for everyone: the reviewer ticks the status in NocoDB and the row signs as `mos_nocodb`; the pipeline may write `confirmed_by = 'pipeline'` explicitly and the trigger leaves it alone. `ck_variant_links_confirmed_has_confirmer` states the rule and is left as a backstop the trigger prevents anyone reaching.

  What `current_user` records is the **role, not the individual** — a NocoDB edit is `mos_nocodb`, the pipeline is `mos_pipeline` unless it names itself. That preserves the distinction that matters here, human-via-NocoDB versus machine, without needing a database role per person. If per-person attribution is ever wanted, have NocoDB write the logged-in user into `confirmed_by`; the trigger will then not touch it.

  One way to get it wrong survives, and it is loud: writing `confirmed_by` without `confirmed_at` fails the `(confirmed_by IS NULL) = (confirmed_at IS NULL)` pairing check, because the signing trigger only fires when `confirmed_by` is null. A caller that knows to set one should set both.

  | What `link.py` writes | What happens |
  |---|---|
  | `status = 'confirmed'`, both columns null | Trigger signs it as the connecting role. The intended path |
  | `status = 'confirmed'`, `confirmed_by = 'pipeline'`, `confirmed_at` set | Kept verbatim; the trigger does not fire |
  | `confirmed_by` alone | **Fails** the pairing check. Loud, and correct |
  | Neither column, `status` left at its `proposed` default | Succeeds, and the preferred-link trigger silently drops `is_preferred`. Every single-source variant ends up with no preferred source |
- **A confirmed variant can now hold zero preferred links.** Rejecting or un-confirming the winner demotes it in the same write, and nothing promotes a replacement. See the review-queue note below.

Two mechanisms keep it consistent:

- **Partial unique index** `UNIQUE (variant_id) WHERE is_preferred` — enforces *at most one* preferred link per variant. ("At least one" is a business rule the Sourcing engine and the single-source default guarantee, not a schema constraint — a variant exists before its first link does, so the schema could never assert it.)
- **A single `BEFORE INSERT OR UPDATE` trigger**, `link.enforce_preferred_link()`, which runs only on a row trying to be preferred and sequences two steps explicitly. *Demote*: if the row is not `confirmed`, drop `is_preferred` rather than raising — so rejecting or un-confirming the winner is one write. *Clear*: otherwise set every other link for the same `variant_id` back to `false`. Together this makes changing the preferred source a single write ("mark this one") instead of an unset-old-then-set-new dance, and it means the reviewer can just tick one link in NocoDB without hitting the unique-index error. It also sidesteps the deferred-constraint timing wrinkle the raw two-row update would otherwise need.

  Migration 0006 split these across two trigger functions and relied on Postgres firing `BEFORE` row triggers in name order. That worked, but the correctness lived in a naming coincidence; 0007 folded them into one function so the order is stated.

**Review-queue note — for whoever builds `ops.review_queue`.** Because the demote step is silent, a variant whose links are all confirmed can end up with no preferred link at all, and nothing currently reports it. When `ops.review_queue` is built, add a check that surfaces **any variant holding at least one confirmed link but zero preferred links** as an open queue item (`entity_type = 'variant_link'`). The Sourcing engine re-picks a winner on its next run, so in the normal case the queue item resolves itself — but a variant that sits in that state across runs means Sourcing has no viable candidate, and that is exactly the case a human needs to see rather than a silently unsellable line.

If sourcing ever needs to differ **per channel** (dropship a supplier for events, stock a different one for trade — mirroring `fulfilment_mode` and pricing, which are already per-channel), neither the flag nor a variant pointer survives: replace it with a dedicated `sourcing_preference (variant_id, channel_id, supplier_product_id)` table. Not in scope now — flagged as the one change that would force a rethink.

**Variant axes are a property of the taxonomy node**, not the supplier: `taxonomy.variant_axes` = `['colour']` for tableware, `['capacity']` for glassware. This drives Shopify option positions deterministically.

---

## `catalog` — what Mosferics sells

**`catalog.taxonomy`** — `category_id`, `parent_id`, `name`, `slug`, `path`, `variant_axes` (ordered array — applicable nodes only), `sku_prefix` (immutable, level-1 department nodes only)

`variant_axes` is the property read at line _Variant axes are a property of the taxonomy node_ below — it drives Shopify option positions. `sku_prefix` is the immutable department-level column the Mosferics SKU derives its `<DEPT>` from; it is never derived from the name at runtime. Title templates and the applicable spec-field list per node live in `config/` keyed by `category_id`, not as columns here.

**`catalog.products`** — `product_id`, `title`, `handle`, `category_id`, `base_unit`, `brand_id`, `collection_id`, `range_id`, `description`, `description_source`, `description_status`, `seo_title`, `seo_description`, `is_multi_source`, `status`

`brand_id`, `collection_id`, `range_id` are the foreign keys promised by the brand hierarchy below — the free-text supplier brand string stays in `supplier.supplier_products.brand` and is resolved to `brand_id` by the pipeline, never stored as text here. `seo_title` / `seo_description` are the routing targets for the SEO fields (template-first, AI only where the template is weak).

`status` is lifecycle only (active / discontinued). Fulfilment mode and publish state are per channel — see `ops.channel_publish`.

**`catalog.variants`** — `variant_id`, `product_id`, `variant_title`, `mosferics_sku`, `ean`, `pack_size`, `base_units_per_variant`, `weight_g`, `dims_mm`

`ean` replaces the earlier generic `barcode`: it is the core matching column named in the field routing, and it populates Shopify's barcode field on export. `weight_g` / `dims_mm` are shipping-carton logistics, distinct from the spec dimensions that live in `product_metadata`.

### Mosferics SKU

Format `MOS-<DEPT>-<NNNNNNN>-<C>`, e.g. `MOS-GLA-0000123-6`. Variant-level, not product-level — a plate in three colours is three SKUs.

| Rule | Reason |
|---|---|
| Supplier-independent, never contains a supplier code | The supplier link is `link.variant_links`, deliberately many-to-one. A resourced product would make an embedded supplier code a lie |
| Prefix taken from taxonomy **level 1 only** | Leaf-level prefixes produce hundreds of codes nobody can recognise |
| Prefix stored as an immutable column on the department node | Never derived from the name at runtime — renaming a department must not change the derivation |
| **Single global sequence**, not per prefix | The numeric part must be unique on its own, because the prefix will get stripped somewhere eventually |
| Allocated once at variant creation, immutable thereafter | A re-run of an import finds the existing variant via the link and reuses its SKU |
| Retired SKUs are never reused | Reuse repoints historic orders at a different product |
| Prefix is frozen at birth, not authoritative | Reclassifying a product leaves a stale prefix. This is acceptable and must not be "fixed" |

**Check digit — decided, mod-11.** Taken in migration 0005. Luhn was rejected because it misses the 09↔90 transposition, which is the error the digit exists to catch. Weights 8..2 left to right over the 7-digit body, check = (11 − (sum mod 11)) mod 11; every single-digit substitution and every transposition, adjacent or not, is detected.

| Decision | Reason |
|---|---|
| Covers the 7-digit body only, never the `<DEPT>` prefix | The body has to stay self-checking after the prefix is stripped, which is already assumed above |
| Bodies whose check digit would be 10 are skipped, not rendered `X` | Keeps the SKU numeric after the prefix — numeric keypad entry, spreadsheets, scanners. Costs 1 sequence value in 11, and gaps are already normal because retired SKUs are never reused |
| Allocated by `catalog.next_mosferics_sku(dept)`, not in the pipeline | The skip rule and the format then exist in exactly one place. Raises on a department prefix no level-1 node carries |
| Enforced by a CHECK calling `catalog.sku_check_digit()` | A SKU that fails the digit cannot be inserted at all, rather than being caught by convention |

**Level 1 of the taxonomy is frozen once SKU allocation begins.** Everything below it stays freely restructurable.

**Use EAN as the primary multi-source matching signal.** It is manufacturer-assigned and global, so a shared EAN identifies the same item with near-certainty — far more reliable than fuzzy name matching, and it cuts the review queue substantially. Fall back to name matching only where EAN is absent. Stored as `catalog.variants.ean`.

**`catalog.product_metadata`** — the review gate lives here, per field
`product_id`, `key`, `value` (raw, as received), `canonical_value` (normalised number), `unit` (canonical), `source` (supplier / ai / manual), `status` (pending_review / verified / rejected), `verified_by`, `verified_at`

`canonical_value` and `unit` are the two columns the _Dimensions need three columns, not one_ note requires — they let "285ml", "28.5cl" and "10oz" sort and filter together. They are null for non-dimensional keys (e.g. country of origin, features).

Key/value rather than wide columns because the metadata set is open-ended and each field needs its own verification state. Oven-safe can be `verified` while dimensions are still `pending_review`.

**`verified_by` / `verified_at` are not signed for you, and nothing makes them mandatory.** Unlike `link.variant_links`, this table has neither the `(verified_by IS NULL) = (verified_at IS NULL)` pairing constraint nor a signing trigger. Flipping `status` to `verified` therefore **succeeds with both columns null** — no error, and the audit trail for a safety-relevant field quietly has no author. Hard rule 3 makes these the fields that most need one. The fix is the same shape as `0008`'s: a pairing constraint plus a `BEFORE UPDATE` trigger stamping `current_user` and `now()`, in a migration, so it holds regardless of which client wrote the row. Not built as of `2b9b2f63d30b` — see the signing-trigger note under `ops`.

**`catalog.images`** — `image_id`, `product_id`, `s3_key`, `url`, `position`, `source`, `status`

**`images.status` is deliberately unconstrained — and that has a cost worth stating.** Unlike `product_metadata.status`, it carries no CHECK, no default and is nullable, because the image pipeline's review states were not settled when `0005` was written. The convention is the same three values (`pending_review` / `verified` / `rejected`), but the database does not enforce it and an image inserted without one is **NULL**, not pending.

Any query asking "does this product have a usable image?" must therefore be NULL-safe: `status <> 'rejected'` evaluates to NULL for an unset status, so the row vanishes from an `EXISTS` and the product reads as having *no* image at all. Use `status IS DISTINCT FROM 'rejected'`. This is not hypothetical — it is the bug `review.missing_images` was corrected for before it ever ran. A publish *gate* is the one place strict `status = 'verified'` is right, because there NULL failing is the safe direction.

### Brand hierarchy — entities, not text

**`catalog.brands`** — `brand_id`, `name`, `slug`, `logo_s3_key`, `description`
**`catalog.collections`** — `collection_id`, `brand_id`, `name`, `slug`, `banner_s3_key`, `description`
**`catalog.ranges`** — `range_id`, `collection_id`, `name`, `slug`

`catalog.products` carries `brand_id`, `collection_id`, `range_id` as foreign keys. Free text here produces "Steelite", "Steelite International" and "steelite" as three brands, which breaks filtering and makes brand pages impossible. It also puts brand logos and collection banners where they belong — once per brand, not 60,000 times per product.

### `catalog.compliance` — stricter rules than metadata

`product_id`, `claim` (ce_mark / ca_mark / reach / rohs / food_contact / coshh_required / age_restricted), `value`, `evidence_document_id`, `stated_by` (supplier / manufacturer), `recorded_by`, `recorded_at`

**Separate from `product_metadata` because the rules are different.** Every row requires a linked evidence document. No evidence, no row — and the storefront renders the absence as "not stated", never as "no". AI never proposes values here, not even into a review queue. A wrong dishwasher-safe flag costs a return; a wrong REACH claim is a regulatory exposure.

### `catalog.product_relations`

`from_product_id`, `to_product_id`, `relation_type` (matching_cup / matching_saucer / matching_plate / matching_bowl / matching_lid / matching_teapot / spare_part / accessory), `auto_derived`

One table, not eight columns. Extensible to new relation types, supports multiple accessories per product, and the inverse relation is created automatically so the two directions can't drift apart. Where `range_id` is populated, most matching relations can be derived rather than entered — flagged with `auto_derived`.

### `catalog.documents`

`document_id`, `attach_level` (product / range / collection / brand), `attach_id`, `doc_type` (care_guide / warranty / brochure / spec_sheet / coshh / declaration_of_conformity), `s3_key`, `version`, `issued_date`, `expires_date`, `superseded_by`

Attachment level matters: one care guide usually covers an entire range. Version and expiry matter because a Declaration of Conformity is a dated artefact that gets superseded — a plain URL column loses all of that.

---

## `pricing` — rules in, prices out

**`pricing.rules`** — `rule_id`, `scope_type` (global / supplier / category / product), `scope_id`, `supplier_id` (category rules only), `method` (percent_markup / fixed_margin / target_price), `value`, `priority`, `valid_from`

Scope resolves conflicts, most specific first: **product > supplier-within-category > category > supplier > global**. `priority` only breaks ties between rules on the same scope. This is how you get "highly specific per supplier and per category within supplier" without hardcoding anything.

**Supplier-within-category is a category rule with `supplier_id` set** (migration 0014). "Artis glassware at 45%" is `scope_type = 'category'`, `scope_id` = the Glassware node, `supplier_id` = Artis. Null `supplier_id` means the category whoever supplies it. The supplier a rule is matched against is the variant's **preferred source** — whose cost is in `cost_basis` — so a multi-source variant re-prices if its preferred source changes. `supplier_id` is a real FK; `scope_id` is polymorphic and is not, so a mistyped category id is accepted and matches nothing.

**Category rules cascade down the tree.** A rule on a node covers every node beneath it, so a department rule prices the whole department until something more specific says otherwise. Two rules of the same kind: the nearest node wins. A supplier-specific rule against a generic one: **the supplier-specific rule wins at any depth** — "Artis + Glassware" on the department beats "Beer Glasses, any supplier" one level nearer the product. That keeps a supplier's terms intact across a branch however many generic rules are added beneath; to vary them deeper, add a supplier-specific rule deeper. None of this is enforced by the table: it is resolution logic `price.py` must implement, recorded in the column comments until then.

| For an Artis pint glass, these rules exist | Winner |
|---|---|
| Artis + Glassware (dept) 45% · Beer Glasses (any supplier) 50% | 45% — supplier-specific beats generic |
| Artis + Glassware (dept) 45% · Artis + Beer Glasses 48% | 48% — same kind, nearest node |
| Glassware (dept) 40% · Beer Glasses 50% | 50% — same kind, nearest node |
| Artis supplier rule 35% · Glassware (dept) 40% | 40% — category beats supplier-wide |

The catch-all is one global rule, `fixed_margin` 30 (added 12 Oct 2026): a 30% gross margin, i.e. price = cost ÷ 0.70 — not a 30% markup, which would be a 23% margin.

**`pricing.cost_basis`** — `variant_id`, `cost_per_base_unit`, `source_offer_id`, `computed_at`

**`pricing.variant_prices`** — `variant_id`, `channel_id`, `calculated_price`, `override_price`, `final_price`, `vat_rate`, `margin_pct`, `status` (pending_approval / auto_approved / approved / rejected), `approved_by`

**`auto_approved` and `approved` are both live prices**; the distinction is only *how* they got there, which is what the materiality threshold below decides. Anything asking "is this price usable?" must accept both — `status = 'approved'` alone silently excludes every price that cleared the 5% threshold without a human, which at that threshold is the majority of the catalogue. The two to exclude are `pending_approval` and `rejected`.

Keyed by channel. Event and trade pricing will not match — different volumes, different margin expectations, different fulfilment economics.

`calculated_price` and `override_price` are separate columns for a reason: a manual price is never wiped by a refresh. `final_price` is `COALESCE(override_price, calculated_price)`.

**Two margins, and they are not the same number.** Hard rule 7 reads as though it forbids `margin_pct`; it does not, because the rule and this column are talking about different quantities:

- **The live margin** — `final_price` against *today's* cost. Derived at query time from the `pricing.cost_basis` join and never stored, because a stored copy goes stale the moment cost moves and then lies about the current price/cost relationship. It has a first-class home: the view **`pricing.live_margins`**. Anyone asking "what is the margin now?" reads the view, never the column.

  **The cost is per base unit; the price is per variant.** `cost_basis.cost_per_base_unit` is the cost of *one* base unit, while `final_price` is the price of the whole thing you sell — and we sell in boxes. The variant's cost is therefore `cost_per_base_unit × base_units_per_variant`, and any margin calculated without that factor is wrong in the flattering direction, by more the bigger the box. A box of 6 costing £2 a unit and selling at £18 has a 33% margin, not 89%. The view was shipped with this bug at 0010 and corrected at 0011; it is the single easiest mistake to make against these three tables, so make the multiplication explicit anywhere the view is not used.

  A variant with no `base_units_per_variant` gets a **NULL** margin, not an assumed factor of 1 — an unknown pack size means an unknown margin, and defaulting it would reintroduce the bug precisely where nobody has checked. The view exposes `base_units_per_variant` and `variant_cost` alongside the margin so a NULL is traceable to its cause: missing cost, or missing pack factor.
- **`margin_pct` — the snapshot** — the margin at the moment the price was calculated, which is precisely what the approval gate judged. A stored *output*, not the stored input rule 7 forbids: you never type a margin and have the system back-solve a price from it.

The snapshot is kept because it cannot be reconstructed. `cost_basis` holds a current cost with no history, so once cost moves, the calc-time cost is gone and `margin_pct` is the only record of what the gate saw. A snapshot of `-8.500` beside a live `38.909` is a real audit story — flagged as a loss-maker when set, healthy against today's lower cost. `pricing.live_margins` returns both side by side (`gross_margin` and `margin_pct_at_calc`) so that comparison is one read.

**Prices are calculated automatically but held for approval — with a materiality threshold.** A full supplier price list can touch thousands of rows. If every change queues for review, the queue gets rubber-stamped and the gate becomes theatre. Rules:

- Change below the threshold (**5%**) → auto-approved, logged
- Change at or above the threshold (**5%**) → queued for review
- Any change pushing margin below the floor (**15%**) → queued regardless of size
- New product with no prior price → always queued

### Tiered pricing — forward design (not v1)

Customer pricing tiers (Standard / Bronze / Silver / Gold) are a **purely additive** extension — two new tables plus one more `COALESCE` in price resolution. Nothing restructures `variant_prices`, so it is safe to defer until the commercial model firms up; the design is captured here so the eventual build is guided.

**`pricing.tiers`** — `tier_id`, `name`, `rank`, `channel_id`, `default_discount_pct`, `active`
**`pricing.tier_prices`** — sparse per-variant overrides, only where a tier needs a *specific* price rather than the default discount: `tier_id`, `variant_id`, `channel_id`, `tier_price`, `source`, `status`, `approved_by` — composite PK `(tier_id, variant_id, channel_id)`

The price for a customer in tier T is one more layer of the COALESCE pattern the base price already uses:

```
tier_price = COALESCE(
    tier_prices.tier_price,                                        -- explicit per-product tier price
    variant_prices.final_price * (1 - tiers.default_discount_pct)  -- else: standard price, discounted
)
```

Standard *is* the existing `variant_prices` base; Bronze/Silver/Gold layer on top, sparse and mostly derived — a tier-wide discount is one row, not 60k. The two mechanisms map directly: **percentage discounts** → `tiers.default_discount_pct`; **specific per-product prices** → a `tier_prices` row.

Three rules the build must honour:

- **The margin floor applies to tier prices.** A tier discount lowers margin — "Gold 15% off" on a thin line can breach the 15% floor and sell near cost. Any tier price breaching the floor queues for review, never auto-publishes: the same gate, one more place it fires.
- **Shopify B2B price lists are the delivery mechanism** — one price list per tier (percentage-adjustment for the discount tiers, fixed prices for the overrides). Requires the B2B-capable Shopify plan.
- **Customer→tier assignment is not this model's job.** The model defines tier *prices*; who sits in which tier lives in Shopify B2B (companies / catalogs) or a thin mapping — never pull customer records into the catalog database.

Two sub-decisions to settle before building: **trade-only or both channels**, and **percentage-off-list vs cost-based tier margins** (the design above assumes percentage-off-list; cost-based would instead add `tier` as a scope on `pricing.rules`).

---

## `stock` — a minority of the catalogue

**The model is buy-to-order, not stock-holding.** A small range of common lines is held; the majority is ordered in when a customer orders, received into the warehouse, then shipped out. Goods pass through us, so this is not drop-ship — but it is also not inventory in any meaningful sense for most SKUs.

Consequences:

- `stock.levels` is populated only where `ops.channel_publish.fulfilment_mode = stocked`
- For `on_demand` products, quantity is permanently zero and must never drive availability
- **Shopify trap:** 60k SKUs imported with inventory tracking on and quantity 0 will display as sold out across the entire catalogue. For `on_demand` products, either disable inventory tracking or set the policy to continue selling when out of stock
- `lead_time_days` on the supplier offer becomes a **customer-facing promise**, not an internal note
- Preferred source therefore weighs cost *and* lead time. Cheapest at 10 days can be worse than second-cheapest at 2



**`stock.locations`** — `location_id`, `name`, `type` (warehouse / 3pl / supplier_dropship), `active`

Create this table now even with one warehouse. Adding a second location then becomes a data change, not a schema migration.

**`stock.levels`** — `variant_id`, `location_id`, `qty_on_hand`, `qty_allocated`, `qty_available`, `updated_at`

**`stock.movements`** — append-only: `movement_id`, `variant_id`, `location_id`, `qty_delta`, `reason`, `reference`, `created_at`

Store movements, derive levels. If you only store a current number, the first time it's wrong nobody can work out why. The ledger is the difference between "stock is 14" and "stock is 14 because of these nine events".

**Availability is not stock.** What Shopify shows is derived, and the source depends on fulfilment mode:

| Product | Source of truth |
|---|---|
| `stocked`, qty available > 0 | In stock, ships today |
| `stocked`, qty available = 0 | Falls back to supplier lead time |
| `on_demand` | Supplier lead time + our handling time, always |

Supplier lead time lives on the offer, not in the stock schema.

---

## `ops` — operational plumbing

### Channels

**`ops.channels`** — `channel_id`, `name`, `type` (trade_store / event_store), `shopify_domain`, `active`

Create this with one row now, exactly as `stock.locations` is created with one warehouse. A second storefront then becomes data rather than a migration across four tables and every query that touches them.

**`ops.channel_publish`** — `variant_id`, `channel_id`, `publish_status` (not_published / pending / published), `fulfilment_mode` (stocked / on_demand / dropship)

`fulfilment_mode` moves here from `catalog.products`. It is a **product × channel** property, not a product one: a pallet of cups is drop-shipped direct for an event and routed through the warehouse for a trade order. Same product, two modes.

Note the third mode. Trade orders are `stocked` or `on_demand` (both transit our warehouse); event orders add true `dropship`, where goods never touch us.

**Products exist in `catalog` whether or not they are published anywhere.** `status` is lifecycle (active / discontinued); publish state is per channel. Active-but-unpublished is a normal, expected state — do not collapse the two columns.

**Gate expensive enrichment on publish intent, not on ingest.** Ingesting, pricing and categorising is cheap. AI enrichment, image matching and human review are not. Pull everything in; enrich on demand, or "just in case" quietly means paying to enrich tens of thousands of products nobody will ever see.

### Delivery configuration

**`ops.delivery_config`** — `delivery_days` (set of weekdays), `cutoff_time`, `timezone`, `handling_days`, `holiday_calendar` (dates with no delivery)

v1 values: `delivery_days` = {Tue, Wed, Thu}, `cutoff_time` = 12:00, `timezone` = Europe/London, `handling_days` = 1, `holiday_calendar` empty for now.

Create this table when the `ops` schema is built, populated with the v1 row, exactly as `stock.locations` and `ops.channels` are created up front. These parameters *will* change as the business grows — adding a delivery day, moving the cutoff, adding bank holidays must be a data change, not a migration. `timezone` is explicit and non-negotiable: the cutoff and "today" are meaningless without it, and BST/GMT shifts will produce wrong dates otherwise.

The table only holds config. The **delivery promise engine** that reads it is a Stage 8 build — see the build instructions. It is deliberately not built now: it consumes products, offers and stock signals that do not exist until the pilot, and it changes no table, so it is downstream of the whole structural build.

### Plumbing tables

| Table | Purpose |
|---|---|
| `ops.review_queue` | `queue_id`, `entity_type` (product_metadata / variant_link / variant_price / category_map / vocabulary / image), `entity_id`, `issue_type`, `status` (open / resolved / dismissed), `assigned_to`, `created_at`, `resolved_by`, `resolved_at`, `notes` — anything needing human sign-off |
| `ops.pipeline_runs` | `run_id`, `supplier_id`, `started_at`, `finished_at`, `status`, `summary` (JSONB) — what the staff exe reports back |
| `ops.shopify_sync` | `channel_id`, `entity`, `entity_id`, `shopify_gid`, `shopify_handle`, `last_synced`, `payload_hash`, `status` |

**Resolving a `review_queue` row is a three-column write, not a status flip.** `0009` puts three constraints on this table that interlock: `resolved_by` and `resolved_at` must be set or null *together*; anything not `open` must have a resolver; and an `open` row must have none. So `UPDATE ... SET status = 'resolved'` on its own **raises** `ck_review_queue_closed_has_resolver`. Any UI that lets someone close a queue item must expose all three columns — or the table needs the signing trigger below.

**The signing triggers are inconsistent across the three tables that have audit columns, and this is a known gap.** Each fails differently on an incomplete edit:

| Table | Pairing constraint | Signing trigger | An incomplete edit |
|---|---|---|---|
| `link.variant_links` | yes | **yes** (`0008`) | signs itself; one write is enough |
| `ops.review_queue` | yes | no | **raises** — loud, and recoverable by supplying all three columns |
| `catalog.product_metadata` | no | no | **succeeds silently** with a null author — the bad one |

The first is the pattern the other two should follow. The middle one is at least honest. The last is the one to fix first, because it is the only one that loses information without telling anyone, and it governs the safety-relevant fields of hard rule 3.

`channel_id` on the sync table is required, not optional — without it the same variant cannot hold a Shopify GID for two storefronts.

`payload_hash` means you only push changed records to the Shopify API, not the whole catalogue every time.

### Referential integrity — before you build

Three things need to be explicit for the schema to build cleanly:

- **Standard foreign keys** (single parent PK — declare as SQL FKs): `products.category_id → taxonomy`, `products.brand_id → brands`, `products.collection_id → collections`, `products.range_id → ranges`, `variants.product_id → products`, `variant_links.variant_id → variants`, `variant_links.supplier_product_id → supplier_products`, `category_map.category_id → taxonomy`, `category_map.supplier_id → suppliers`, `supplier_products.supplier_id → suppliers`, `supplier_offers.supplier_product_id → supplier_products`, `supplier_stock.supplier_product_id → supplier_products`, `compliance.product_id → products`, `compliance.evidence_document_id → documents`, `product_metadata.product_id → products`, `images.product_id → products`, `product_relations.from_product_id / to_product_id → products`, `collections.brand_id → brands`, `ranges.collection_id → collections`, `cost_basis.variant_id → variants`, `cost_basis.source_offer_id → supplier_offers`, `variant_prices.variant_id → variants`, `variant_prices.channel_id → channels`, `channel_publish.variant_id → variants`, `channel_publish.channel_id → channels`, `stock.levels / stock.movements .variant_id → variants` and `.location_id → locations`, `shopify_sync.channel_id → channels`, plus the self-references `taxonomy.parent_id → taxonomy` and `documents.superseded_by → documents`.
- **Polymorphic references** point at different tables depending on a sibling type column, so they cannot be single SQL foreign keys — enforce them in the pipeline and with a CHECK on the type column: `pricing.rules.scope_id` (by `scope_type`), `catalog.documents.attach_id` (by `attach_level`), `ops.shopify_sync.entity_id` (by `entity`), `ops.review_queue.entity_id` (by `entity_type`).
- **Composite primary keys** on the junction / attribute tables: `variant_links` (supplier_product_id, variant_id), `channel_publish` (variant_id, channel_id), `stock.levels` (variant_id, location_id), `product_metadata` (product_id, key), `compliance` (product_id, claim), `variant_prices` (variant_id, channel_id), `category_map` (supplier_id, supplier_category_raw), `metafield_map` (internal_key, owner_type), `raw.import_rows` (batch_id, row_number). `product_relations` keys on (from_product_id, to_product_id, relation_type).

---

## `review` — read-only reporting surface

A schema of nothing but views, added at migration `2b9b2f63d30b` to serve NocoDB. It holds no tables, no data and no state, and nothing joins to it.

| View | Answers |
|---|---|
| `review.unmapped_categories` | supplier categories with no `link.category_map` row, heaviest first |
| `review.missing_images` | active products with publish intent and no usable image |
| `review.ready_to_publish` | variant × channel rows cleared to sync |

**Why a schema rather than `ops.v_*`.** NocoDB connects it as a separate data source with *Allow Data Edit* off, so every report view is read-only in one toggle instead of per-view locking someone must remember for each new view. It also keeps a read-only reporting surface visibly distinct from the real tables in the reviewer's sidebar.

**Why views at all, when NocoDB has filter views.** A NocoDB view is a saved filter over a *single table* — it cannot join. Each of these three is defined by an **absence**: a row with no match in another table. That is an anti-join, so it has to be SQL. The corollary is the rule that splits the Stage 5 review views in two: anything the reviewer must *change* has to be a NocoDB filter view on the real table, because a Postgres view is read-only in practice and he could not do his job through one.

**Views run with their owner's privileges.** `mos_owner` owns these and can read every underlying table, so `mos_nocodb` needs `SELECT` on the views alone and no grant on the tables beneath them. That is a lever worth remembering: a role can be given a view without the cost basis it was computed from — how the reviewer could see margin without seeing supplier cost, should that ever be wanted.

These views are reports, not gates. `ready_to_publish` in particular is **not** a minimum-required-fields check: it asks that nothing is still `pending_review`, which a product with *zero* metadata rows also satisfies. Empty is not verified. The real check is per taxonomy node and does not exist yet.

Full SQL, the status literals each view depends on, and the NocoDB setup around them are in `mosferics_nocodb_views.md`.

---

## Where the engines sit

Not tables. Python modules in `pipeline/` that read and write the above — written during the Artis pilot (build instructions Stage 7), not up front.

| Engine | Module(s) | Reads | Writes |
|---|---|---|---|
| Ingest / normalise | `ingest.py` → `extract.py` → `normalise.py` | raw rows + supplier YAML | `supplier_products`, `supplier_offers` |
| Enrich | `enrich.py` (added later) | supplier products with gaps | descriptions, `product_metadata` (as `pending_review`) |
| Match | `link.py` | supplier products + catalog | `link.variant_links` (as `proposed`) |
| Pricing | `price.py` | `cost_basis` + `rules` | `variant_prices` |
| Sourcing | `price.py` | current offers | `is_preferred` on `link.variant_links` |
| Shopify sync | `shopify/` | verified rows only | `ops.shopify_sync`, Shopify API |

---

## Field routing — the full target schema

Where every field from the target list actually lives. Note that roughly a third of it is not `product_metadata` at all.

| Field | Destination | Notes |
|---|---|---|
| Brand / Collection / Range | `catalog.brands` / `collections` / `ranges` | FK, not text |
| Product Type | `catalog.taxonomy` | Already modelled |
| Material, Colour, Finish, Shape | `product_metadata` | Controlled vocabulary, not free text |
| Country of Origin | `product_metadata` | ISO country code |
| SKU, EAN | `catalog.variants` | Core columns |
| Pack Quantity | `catalog.variants.pack_size` | What we sell |
| Case Quantity | `supplier.supplier_offers` | What we buy |
| Unit Cost | `pricing.cost_basis` | Derived from current offer |
| Selling Price, VAT Rate | `pricing.variant_prices` | Calculated, held for approval |
| Gross Margin | **derived only** | Never a stored input |
| Capacity, Diameter, Height, Width, Length, Weight | `product_metadata` | Value + unit + canonical value |
| Dishwasher / Microwave / Oven / Freezer Safe | `product_metadata` | Review-gated, AI may propose |
| Stackable, Edge Chip Guarantee, Warranty, Care Instructions | `product_metadata` | Review-gated |
| Age Restricted, Food Safe, CE, CA, COSHH, REACH, RoHS | `catalog.compliance` | Evidence required, AI never proposes |
| All Matching / Spare Parts / Accessories | `catalog.product_relations` | One table, typed, bidirectional |
| Lifestyle Image | `catalog.images` | Positioned |
| Brand Logo | `catalog.brands.logo_s3_key` | Once per brand |
| Collection Banner | `catalog.collections.banner_s3_key` | Once per collection |
| Features, Benefits | `product_metadata` | Lists, AI-generatable, low risk |
| SEO Title, SEO Description | `catalog.products` | Template first, AI only where template output is weak |
| All PDFs | `catalog.documents` | Versioned, expiring, attached at the right level |

### Dimensions need three columns, not one

Suppliers send "285ml", "28.5cl" and "10oz" for the same glass. Store `value` (raw, as received), `canonical_value` (normalised number), `unit` (canonical) — the three `product_metadata` columns above. Without this you cannot filter, sort, or compare across suppliers — and cross-supplier comparison is the entire point of the model.

### Not every field applies to every product

A glass has capacity and diameter. A chopping board has length, width and height and no capacity. This is exactly why metadata is sparse key/value rather than sixty mostly-NULL columns.

### Realistic sequencing

This list is roughly sixty fields across sixty thousand SKUs. Suppliers provide a fraction of it. Treat it as the target state, not a v1 deliverable — populate in tiers:

1. Anything the supplier file already contains
2. Dimensions and material (usually on supplier websites, scrapable or bulk-requestable)
3. Compliance — request documents from suppliers directly, it is the slowest and cannot be shortcut
4. Marketing and SEO — generatable, do last, cheapest to redo

---

## Shopify mapping layer

**`ops.metafield_map`** — `internal_key`, `namespace`, `shopify_key`, `shopify_type`, `owner_type` (product / variant), `active`

Internal storage is key/value text plus canonical value and unit; Shopify metafields are strongly typed. This table does the casting. `owner_type` is the column that gets forgotten — dimensions shared across colours belong on the product, anything differing per variant must be variant-level.

**Colour is an option, not a metafield.** Where colour is a variant axis it must not also exist as a metafield, or the product page carries two sources of truth that will eventually disagree.

**Metafield definitions must be created via API before values are pushed.** Values without definitions land unstructured and can't power admin or storefront filtering.

### Metaobjects vs collections

A Shopify collection is a browsable grouping — listing page, URL, filters. A metaobject is a structured content record referenced by metafield. Use both, for different jobs:

| Ours | Shopify |
|---|---|
| `catalog.brands` | `brand` metaobject (logo, story) + optional collection |
| `catalog.collections` | `collection_range` metaobject (banner, description) + collection |
| `catalog.ranges` | metaobject, referencing its collection |
| `catalog.documents` | `document` metaobject — a bare file reference loses version and expiry |

Metaobjects can reference other metaobjects, so brand → collection → range survives intact. Content lives once and is referenced by hundreds of products.

**Sync order: definitions → metaobjects → products → variants → metafield values.** References fail if the target doesn't exist. Store the returned Shopify GID and handle for every metaobject in `ops.shopify_sync`, or re-syncs create duplicates instead of updating.

### Two sync paths, not one

| Path | Cadence | Gate |
|---|---|---|
| Catalogue sync | Infrequent, on change | Human review required |
| Availability sync | Daily, automated | No review |

Routing stock updates through human approval either drowns the queue or leaves availability permanently stale.

---

## Build order

Do not build this in one pass. Slices, each one useful on its own:

1. `raw` + `supplier` schemas, one supplier (Artis), ingest only
2. `catalog` + `link` with auto 1:1 links — single-source path working end to end
3. NocoDB on top, review states live — plus the `review` schema's report views, which are the anti-joins a NocoDB filter view cannot express
4. `pricing` rules and variant prices
5. Variant grouping and multi-source: `variant_links` with human confirmation, preferred source (`is_preferred`)
6. `stock` schema, plus `supplier_stock` feeds where available
7. AI enrichment behind the review gate
8. Shopify CSV, then API
9. Basket optimisation as a separate tool, last

---

## Decisions locked

- **Taxonomy is decided and loaded.** Three-level tree (`taxonomy_v4.md`) derived from the ready supplier sheets, with variant axes per node; Level 1 fixed, everything below restructurable. `catalog.taxonomy` holds **256 rows** as of 8 Oct 2026 — 12 departments carrying the frozen `sku_prefix` values, 64 categories, 180 types — so `catalog.next_mosferics_sku()` resolves every department. Loading the tree was an insert, not a migration (`pipeline/seed_taxonomy.py`); the database is now the source of truth for it.
- **Controlled vocabularies v1 are decided and the files exist.** Colour, Material, Finish and Shape — canonical values plus alias lists, colours carrying hex values — landed in `config/vocabularies/` on 8 Oct 2026. Alias coverage of the Artis, BBP and Beaumont sheets is unchecked. The YAMLs are git-tracked and are the source of truth: do not add a vocabulary table to make them editable in NocoDB, because the second copy would drift. Unmapped supplier values route to `ops.review_queue` with `entity_type = 'vocabulary'`, get aliased once, and are not seen again.
- **the reviewer can edit `pricing.override_price`.** He sets manual prices directly in NocoDB; a refresh never wipes an override (`final_price = COALESCE(override_price, calculated_price)`). Calculated prices above the materiality threshold, or any change breaching the margin floor, still queue for approval.
- **Standard handling time = 1 day.** The internal buffer added to the supplier lead time — goods arrive at the warehouse, are received/checked/picked/packed, and are ready to dispatch the next day. Applies to stocked lines too (supplier leg zero, handling leg still one day). It is an input to the delivery promise engine, which snaps `supplier lead_time_days + 1 day` to the next delivery day; a starting value, tunable once real warehouse turnaround is visible.
- **Materiality threshold = 5%, margin floor = 15%.** A recalculated price that moves less than 5% is auto-approved and logged; a move of 5% or more queues for review. Any change that would drop a variant's margin below 15% queues regardless of how small the move is, and new products with no prior price always queue. Both are starting values, tunable once real supplier price-list churn is visible.
- **Mosferics will not sell alcohol.** No alcohol lines now or in the roadmap, so the catalogue carries no licensing or duty concerns and the `age_restricted` compliance claim is not needed for a beverage-sales reason. It still applies to other restricted goods — notably knives (see below). Glassware and barware remain in scope — the exclusion is the drink, not the vessel.
- **Compliance evidence = supplier data sheets.** The accepted evidence type for a compliance claim (CE / CA / food_contact etc.) is the supplier's data sheet, replacing Declaration of Conformity as the strongest source. The `catalog.compliance` rule is unchanged: every row still requires a linked evidence record — now the data sheet — no evidence, no row, and AI never proposes values here. Record `stated_by` as `supplier` and attach the data sheet as the evidence document.
- **All knives carry an 18+ age restriction.** A blanket `age_restricted` claim applies to every SKU under the knife taxonomy nodes — driven by the node, not a per-SKU judgement. Unlike other compliance claims this is a policy rule rather than an evidence-backed manufacturer statement, so it does not depend on a supplier data sheet to be set.

## Open items

- Field list per supplier (you're supplying this — lands in `specs` JSONB, promoted to columns where it earns it)
- **Delivery promise engine — a Stage 8 build, not an open decision.** Every parameter is now decided and lives in `ops.delivery_config` (delivery days Tue/Wed/Thu, midday cutoff, Europe/London, handling 1 day). What remains is the *logic* that reads that config: the mainline promise is order before the **midday cutoff** → next-day delivery, provided the day is a **delivery day** and the goods are on hand or can reach the warehouse in time. For `on_demand`/out-of-stock lines the test is `supplier lead_time_days + handling time` landing before that delivery day, else roll to the next delivery day. This leg is distinct from `supplier_offers.lead_time_days`, which covers supplier→warehouse only, and it touches the customer-facing delivery date alone: not stock levels, pricing, taxonomy, or the availability bands. Deliberately deferred to Stage 8 — it consumes products, offers and stock signals that do not exist until the pilot. Full design process in the build instructions, Stage 8.
- **Stocked SKU list — deferred.** It's a short list; the `stock` schema is a later concern and does not gate the pilot. Write the list out by hand when convenient and build `stock` against it then.
- **Tiered pricing — deferred, design captured.** Standard/Bronze/Silver/Gold customer tiers, as an additive `pricing.tiers` + `pricing.tier_prices` layer (see the forward-design note in the `pricing` section). No retrofit, so build when the commercial model firms up. Sub-decisions: trade-only vs both channels, and percentage-off-list vs cost-based tier margins.
