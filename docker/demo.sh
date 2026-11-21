#!/usr/bin/env bash
# The pipeline end to end on the synthetic demo supplier (sample_data/demo/).
#
#   docker compose run --rm pipeline bash docker/demo.sh
#
# Every step is a real pipeline module, run as it would be on a supplier's
# files. The human review between them is played by docker/demo_review.py and
# signed "demo script". Reports land in ./out on the host.
set -euo pipefail
cd /app

step() { printf '\n== %s\n' "$*"; }

step "schema: alembic upgrade head"
alembic upgrade head

if python - <<'PY'
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["PIPELINE_DATABASE_URL"])
with e.connect() as c:
    raise SystemExit(0 if c.execute(sa.text("SELECT count(*) FROM catalog.taxonomy")).scalar_one() else 1)
PY
then
    echo "This database has been used already. Start fresh with: docker compose down -v" >&2
    exit 1
fi

step "taxonomy: load the category tree"
python -m pipeline.seed_taxonomy | tail -1

step "ingest: land the files verbatim in raw"
python -m pipeline.ingest demo --dir sample_data/demo

step "normalise: canonical values into the supplier tables"
python -m pipeline.normalise demo

step "categories: load the reviewed category map"
python -m pipeline.load_category_map demo sample_data/demo/demo_category_map.csv \
    --confirmed-by "demo script" --apply

step "link: group SKUs into products and variants"
python -m pipeline.link demo --report out/demo_groups.csv

step "review (stand-in): confirm the proposed groupings"
python docker/demo_review.py confirm-groups

step "images: match the files to SKUs (exact names verified, loose ones for review)"
python -m pipeline.images demo --report out/demo_images.csv

step "review (stand-in): set the catch-all pricing rule"
python docker/demo_review.py pricing-rule

step "price: source, cost and price every confirmed variant"
python -m pipeline.price --report out/demo_prices.csv

step "review (stand-in): approve the prices"
python docker/demo_review.py approve-prices

step "publish: mark every product for the trade store (a merchandising decision)"
python -m pipeline.publish intend --all --apply

step "export: write the products that are ready as a Shopify CSV"
python -m pipeline.shopify.export --out out/demo_shopify.csv --report out/demo_export_report.csv

step "result"
python docker/demo_review.py summary

cat <<'EOT'

Reports: out/demo_groups.csv, out/demo_images.csv, out/demo_prices.csv,
         out/demo_shopify.csv (import into a development store), out/demo_export_report.csv
After a human has imported the CSV and checked it, record it as live:
    python -m pipeline.publish published --csv out/demo_shopify.csv --apply
EOT
