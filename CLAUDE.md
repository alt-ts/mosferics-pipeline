# Mosferics Pipeline

Full design: docs/mosferics_data_model_v3.md
Build sequence: docs/mosferics_build_instructions.md
Read the relevant section before implementing anything in that area.

## Hard rules — do not violate without asking

1. Supplier links resolve at VARIANT level (`link.variant_links`), never product level.
2. Compliance fields are NEVER AI-populated or inferred. Every compliance row
   requires a linked evidence record. No evidence, no row.
3. Safety-relevant metadata (dishwasher/microwave/oven/freezer safe, food contact,
   materials) lands as `pending_review`. Never auto-verified, never auto-published.
4. Supplier stock NEVER writes to `stock.levels`. It belongs in
   `supplier.supplier_stock`. These are different things.
5. All schema changes go through Alembic migrations. No ad-hoc DDL, ever.
6. Secrets live in .env, loaded from environment. Never committed, never hardcoded,
   never printed in logs.
7. The live gross margin is derived at query time (the `pricing.cost_basis`
   join, exposed as `pricing.live_margins`) and never stored.
   `pricing.variant_prices.margin_pct` is the calc-time snapshot the approval
   gate judged — a stored output, not the stored input this rule forbids.
8. `handle` is frozen after first Shopify publish. Never regenerate it from a title.
9. Two sync paths: catalogue sync is human-gated; availability sync is not.
   Never route availability updates through the review queue.
10. Variant grouping never invents combinations. Sparse matrices are normal and
    must be preserved exactly as the supplier data supports.
11. This is a PRODUCTION database. Never DELETE, TRUNCATE, DROP or bulk-UPDATE
    live data without explicit human approval. Never edit the payload in
    `raw.import_rows` — it is the audit trail, and proving what a supplier
    actually sent depends on it. `raw.import_batches.status` is operational
    state and may be updated as a batch progresses.
12. Test every migration on a scratch database (up, down, up) before running
    `alembic upgrade head` against the live database.
13. Never write to the live Shopify store without the human catalogue-sync gate.
    Test Shopify writes against a development store, never production.
14. Never change the server's security config (ufw, ssh, Tailscale, the firewall)
    or run destructive shell commands. Confirm before any irreversible or
    outward-facing action.

## Conventions

- Python 3.11+, SQLAlchemy 2.x, pandas, Alembic, pytest
- Type hints on all public functions
- Every supplier-specific behaviour goes in config/suppliers/<name>.yaml — never
  in Python. If you find yourself writing `if supplier == "artis"`, stop.
- Extraction rules are per-supplier. Title/description composition is per taxonomy
  node. Do not mix these.
- Fail loudly. A broken column mapping raises; it never silently loads nulls.
