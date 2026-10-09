# Mosferics Pipeline — Build Instructions

Companion to `mosferics_data_model_v3.md`. That document defines *what* the system is; this one defines *how to stand it up*, in order, with a definition of done at each stage.

Stages 0 and 9 are paper exercises. Everything else is build. Stage 0 is not optional — several stages block on it.

---

## Stage 0 — Decisions to lock before provisioning

| Decision | Needed by | Status |
|---|---|---|
| VM provider, region, size | Stage 1 | Open |
| SKU check digit | Stage 4 | **Done — mod-11, `MOS-<DEPT>-<NNNNNNN>-<C>`** |
| Taxonomy tree v1 | Stage 6 | **Done — `taxonomy_v4.md` loaded 8 Oct 2026**, 256 rows in `catalog.taxonomy` |
| Controlled vocabularies + alias lists | Stage 6 | **Decided, not landed** — `config/vocabularies/` is empty |
| Materiality threshold %, margin floor % | Stage 7 | **Done — 5% threshold, 15% floor** |
| Standard handling time (days) | Stage 8 | **Done — 1 day** |
| Delivery promise engine | Post-pilot | Config decided + seeded at Stage 4; logic deferred — see spec below |
| Stocked SKU list | Deferred — see note | **Deferred** |
| the reviewer's edit permissions | Stage 5 | **Done — the reviewer edits `pricing.override_price`** |
| Role grants applied to live | Stage 5 | **Done — migration 0013, all four roles** |
| Alcohol in range | — | **Done — Mosferics will not sell alcohol** |
| Compliance evidence policy after dropping DoC | Stage 6 | **Done — supplier data sheets are the evidence** |
| Knife age restriction | Stage 6 | **Done — all knives are 18+** |

**The taxonomy has landed; the vocabularies have not, and Stage 7 is therefore still blocked.** The three-level tree (`docs/taxonomy_v4.md`) was loaded into `catalog.taxonomy` on 8 Oct 2026: **256 rows** — 12 departments each carrying its `sku_prefix`, 64 categories, 180 types. Level 1 is fixed for SKU allocation, everything below it restructurable. The four alias-backed vocabulary lists — Colour with hex values — are still only *decisions*: `config/vocabularies/` is **empty**, so `normalise.py` has nothing to read.

**Why the taxonomy was a hard block, not a tidiness problem.** `catalog.next_mosferics_sku()` resolves `<DEPT>` against a level-1 node and raises when there is none — which, on a clean rebuild that has not yet been seeded, it still will:

```text
ERROR:  unknown department prefix GLA: no level-1 taxonomy node carries it
CONTEXT:  PL/pgSQL function catalog.next_mosferics_sku(text) line 11 at RAISE
```

So `link.py` cannot create a single variant on a database without the tree. The `RAISE` precedes the `nextval`, so a failed run burns no SKU and leaves the sequence untouched — the block is safe to hit, just absolute. Check all three before planning any Stage 7 work, rather than trusting the rows above:

```bash
psql -c 'SELECT count(*) FROM catalog.taxonomy'                    # must be > 0
psql -c "SELECT count(*) FROM catalog.taxonomy
          WHERE parent_id IS NULL AND sku_prefix IS NOT NULL"      # must be > 0
ls config/vocabularies/                                             # must list four YAMLs
```

Loading the tree is an **insert, not a migration** — it is data in a table the schema already defines, so it does not go through Alembic. The loader is `python -m pipeline.seed_taxonomy` (`--dry-run` prints the rows), reading `config/taxonomy/taxonomy_v4.json`. It refuses a table that already has rows: once loaded, the database is the source of truth for the tree and the JSON is only what a clean rebuild loads — restructure levels 2–3 in place, not by editing the seed. The vocabulary files are git-tracked YAML and are the source of truth; do not add a vocabulary table to make them editable in NocoDB, or the two copies will drift.

**the reviewer's edit permissions:** the reviewer can edit `pricing.override_price` directly in NocoDB. His grant on `pricing.variant_prices` allows setting the override column; a price refresh never overwrites it (`final_price = COALESCE(override_price, calculated_price)`). Calculated changes above the materiality threshold, or any change breaching the margin floor, still queue for approval regardless.

**Alcohol:** Mosferics will not sell alcohol, now or on the roadmap. No licensing or duty handling is needed, and the `age_restricted` compliance claim is not required for beverage sales (it may still apply to other restricted goods). Glassware and barware stay in scope — the exclusion is the drink, not the vessel.

**Stocked SKU list:** deferred. Write it out by hand when convenient. If it's under about fifty lines, the `stock` schema is a later-phase concern and can be deferred entirely — that single list determines how much of the stock model v1 needs. It does not gate the Artis or BBP pilots.

**Compliance evidence policy — decided:** with Declaration of Conformity dropped, the **supplier data sheet** is the accepted evidence type for a CE/CA/food-contact claim. The `catalog.compliance` rule stands: every row still needs a linked evidence record — now the data sheet — no evidence means no row and the storefront renders the absence as "not stated". Set `stated_by = supplier`, attach the data sheet as the evidence document, and AI still never proposes values here.

**SKU check digit — decided:** mod-11 over the seven-digit body, giving `MOS-<DEPT>-<NNNNNNN>-<C>`. Luhn was rejected because it misses the 09↔90 transposition, which is the error the digit exists to catch. Built in migration 0005 as `catalog.sku_check_digit()` plus `catalog.next_mosferics_sku()`, and enforced by a CHECK constraint on `catalog.variants` — see the SKU section of `mosferics_data_model_v3.md` for the full rule table. Level 1 of the taxonomy must be frozen before allocation begins, as it already is.

**Knife age restriction — decided:** all knives carry an 18+ restriction. Apply `age_restricted` as a blanket claim driven by the knife taxonomy nodes, not per-SKU. This one is a policy rule rather than a manufacturer statement, so it does not require a supplier data sheet to be set — but it does mean the knife nodes must be identifiable in the taxonomy so the rule can be scoped to them.

---

## Stage 1 — VM

**Spec:** 2 vCPU, 4 GB RAM, 40–80 GB SSD. That comfortably runs Postgres, NocoDB and the pipeline at 60k SKUs. Cheap European hosts run this for a few pounds a month; larger providers charge several times that for the same thing. UK or EU region for latency and data residency.

**Deliberately separate from the Joshua Brooks server.** Client work and internal ops should not share a blast radius.

### DigitalOcean specifics

Provider chosen: **DigitalOcean, LON1 (London)** for UK data residency.

**Before you create it — a dedicated SSH key.** Generate one on your laptop scoped to this project, so a compromise here never reaches the Joshua Brooks server or client infra (and vice versa), and the key can be rotated in isolation — the same blast-radius separation applied to the VM itself:

```bash
ssh-keygen -t ed25519 -C "mosferics-ops 2026" -f ~/.ssh/mosferics_ops
# set a passphrase; the -f flag keeps it in its own file, not your default key
```

Add the **public** key (`~/.ssh/mosferics_ops.pub`) to your DO account; the private key never leaves the laptop. This key is only for bootstrap — once Tailscale is up you can hand ongoing SSH auth to **Tailscale SSH** and grant the reviewer access by ACL rather than copying keys around. Each person uses their own key; private keys are never shared.

**In the Create Droplet wizard — the only choices actually made at creation:**

- **Name:** `mosferics-ops-01` — becomes the hostname *and* the Tailscale MagicDNS name (`mosferics-ops-01.<tailnet>.ts.net`), which is your NocoDB HTTPS URL, so keep it clean. Role-based (this box is db + app + pipeline, so `db` is too narrow), numbered for a tidy `-02` later — the same forward-compat instinct as the one-row `stock.locations` and `ops.channels`
- **Image:** Ubuntu 22.04 LTS (its stock Postgres and Python are older than 24.04's — Stages 2 and 3 add the PGDG and deadsnakes repos to reach current versions)
- **Droplet type:** Basic → **Premium AMD/Intel (NVMe SSD)** — a few dollars over the regular tier, and the faster disk genuinely helps Postgres
- **Region:** LON1
- **Authentication:** the `mosferics_ops` SSH key (not password)
- Tick **Backups** (weekly whole-disk snapshots, ~20% of droplet cost — recovers the *machine*, distinct from the Stage 2 pg_dump→S3 that recovers the *data*; keep both) and **Monitoring** (free agent, CPU/RAM/disk alerts)

**Skip** DigitalOcean's Managed PostgreSQL (you self-host on this droplet — cheaper, and it stays reachable purely over Tailscale) and Spaces (you already use AWS S3; don't split object storage).

Everything below — the hardening script, swap, Tailscale, the Cloud Firewall and the SSH tightening — is done **after the droplet boots**, over SSH or in the DO panel where noted. Cloud Firewall in particular is not in the creation wizard.

Provision the droplet, then **SSH in as `root`** — DigitalOcean puts your key on `root`. Run these in order: the `mosferics` user must exist and hold your key *before* you disable root login, or you lock yourself out.

**Create the non-root user and give it your key:**

```bash
apt update && apt upgrade -y

adduser --gecos "" mosferics        # prompts once for a password (used for sudo)
usermod -aG sudo mosferics

# copy the key DO placed on root over to mosferics
mkdir -p /home/mosferics/.ssh
cp /root/.ssh/authorized_keys /home/mosferics/.ssh/authorized_keys
chown -R mosferics:mosferics /home/mosferics/.ssh
chmod 700 /home/mosferics/.ssh
chmod 600 /home/mosferics/.ssh/authorized_keys
```

**Now, in a separate terminal, confirm `ssh mosferics@<droplet-ip>` works** — keep the root session open as a safety net until it does.

**Harden SSH** (key-only, no root) via a drop-in file, which survives package upgrades unlike editing the main config:

```bash
cat > /etc/ssh/sshd_config.d/99-mosferics.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
sshd -t && systemctl restart ssh    # sshd -t validates the config before restart
```

**Firewall, swap, auto-updates, fail2ban:**

```bash
# firewall — SSH only for now (tightened to Tailscale-only below)
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# swap — DO droplets ship with none; cheap OOM insurance on a 4 GB box
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# automatic security updates (non-interactive enable)
apt install -y unattended-upgrades
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades

# brute-force protection (low-value with key-only auth, but cheap)
apt install -y fail2ban
systemctl enable --now fail2ban
```

**Install Tailscale and join the tailnet:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh          # prints a URL — open it on your laptop to authorise the machine
tailscale ip -4             # note this address; Postgres binds to it in Stage 2
```

`--ssh` enables **Tailscale SSH**, so ongoing access is authenticated by your tailnet identity — granting the reviewer later is an ACL, not a key copy. It's the simplest way to give yourself and the reviewer access to Postgres and NocoDB without exposing either to the public internet.

**DigitalOcean Cloud Firewall — a separate step, done in the panel not the wizard.** Create it under **Networking → Firewalls**, then assign it to the droplet. It's a free network-level layer in front of the box, complementing host-level `ufw`: lock inbound to what Tailscale needs, and leave Postgres (5432) and NocoDB off the public internet entirely.

**Tighten SSH to Tailscale-only** — once the step above works and you've confirmed you can reach the box over Tailscale:

```bash
ufw allow in on tailscale0 to any port 22 proto tcp
ufw delete allow OpenSSH
ufw reload
ufw status verbose          # confirm port 22 is allowed only on tailscale0
```

The wizard's `allow OpenSSH` is deliberately open during provisioning so you don't lock yourself out before the tailnet exists; this step closes it afterwards. DO's web-based **recovery console** is your no-lockout fallback if Tailscale ever hiccups, so a fully private SSH surface carries no real risk.

**Done when:** `ssh mosferics@<droplet>` works with the key, password auth is refused, SSH is reachable only over Tailscale, `swapon --show` lists the swap file, `systemctl is-active fail2ban` returns `active`, DO Backups and the Cloud Firewall are both on, and `tailscale status` shows the box on your tailnet.

---

## Stage 2 — PostgreSQL

**You're now `mosferics`** (Stage 1 disabled root SSH). System and database config below uses `sudo`; redirects into `/etc` need `sudo tee` (not `sudo >`, which the shell runs before sudo applies); the backup section drops to `su - mosferics` where a file must be *owned* by that user (`~/.pgpass`, `backup.sh`).

Ubuntu 22.04's default Postgres is **14**, which reaches end-of-life in late 2026 — so install **16** from the official PostgreSQL (PGDG) apt repo instead:

```bash
# add the PGDG repo
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | sudo tee /etc/apt/sources.list.d/pgdg.list

sudo apt update && sudo apt install -y postgresql-16
cd /tmp && sudo -u postgres psql -c 'SELECT version();'      # confirm 16.x
```

(On Ubuntu 24.04, Postgres 16 is the stock default and this repo step isn't needed.)

**Bind to localhost and the tailnet only — never expose 5432 publicly.** Set `listen_addresses`, then allow the Tailscale range in `pg_hba.conf`:

```bash
TSIP=$(tailscale ip -4)
sudo sed -i "s/^#\?listen_addresses.*/listen_addresses = 'localhost,$TSIP'/" /etc/postgresql/16/main/postgresql.conf

# allow tailnet clients (Tailscale CGNAT range), password-authenticated
echo "host    all    all    100.64.0.0/10    scram-sha-256" | sudo tee -a /etc/postgresql/16/main/pg_hba.conf

sudo systemctl restart postgresql
```

**Create four roles, least privilege:**

| Role | Rights |
|---|---|
| `mos_owner` | Owns schemas. Used only by migrations. |
| `mos_pipeline` | SELECT/INSERT/UPDATE on data tables. No DDL. |
| `mos_nocodb` | SELECT/INSERT/UPDATE on review-editable tables. No DDL. No DELETE on `raw`. |
| `mos_trigger` | the staff executable. Narrowest possible — enough to run an import, nothing else. |

The point of `mos_trigger` is that a leaked credential in a distributed `.exe` cannot drop tables.

Create the database and roles (generate each password with `openssl rand -hex 32` — hex avoids the `/ + =` that base64 can emit, which would break the `.env` `DATABASE_URL` at Stage 3/4 — and keep them for the `.env` files):

```bash
sudo -u postgres psql <<'EOF'
CREATE ROLE mos_owner    LOGIN PASSWORD 'CHANGE_ME_OWNER';
CREATE ROLE mos_pipeline LOGIN PASSWORD 'CHANGE_ME_PIPELINE';
CREATE ROLE mos_nocodb   LOGIN PASSWORD 'CHANGE_ME_NOCODB';
CREATE ROLE mos_trigger  LOGIN PASSWORD 'CHANGE_ME_TRIGGER';
CREATE DATABASE mosferics OWNER mos_owner;
EOF
```

**Where each role's credentials live** — deliberately *not* all in one `.env`; each secret sits with the thing that uses it:

| Role | Used by | Credentials live in |
|---|---|---|
| `mos_owner` | Alembic migrations | the repo `.env` (`DATABASE_URL`), Stage 4 |
| `mos_pipeline` | the Python pipeline | the repo `.env` (a second URL), added at Stage 7 |
| `mos_nocodb` | NocoDB | NocoDB's data-source config (Stage 5) — **not** `.env` |
| `mos_trigger` | the staff `.exe` | supplied to that distributed binary (Stage 9) — **not** `.env` |

So `.env` only ever holds `mos_owner` (now) and `mos_pipeline` (later) — which is why you only saw `mos_owner`. The other two connect from elsewhere, keeping each credential scoped to its one consumer.

The **table-level grants live in migration `0013`**, not in a snippet run by hand. This is a change from the original plan, and the reason is hard rule 5: `GRANT` is DDL, and "no ad-hoc DDL, ever" does not carve out privileges. It also makes Stage 4's own exit criterion true — `alembic upgrade head` on a clean instance now builds a database that *works*, rather than one only `mos_owner` can use. Run per schema, since there is no `ALL SCHEMAS` keyword. The shape:

```sql
GRANT USAGE ON SCHEMA <schema> TO mos_pipeline;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA <schema> TO mos_pipeline;
ALTER DEFAULT PRIVILEGES FOR ROLE mos_owner IN SCHEMA <schema>
  GRANT SELECT, INSERT, UPDATE ON TABLES TO mos_pipeline;   -- future tables inherit
```

`ALTER DEFAULT PRIVILEGES` is the part people forget — without it, tables created by later migrations carry no grant and the pipeline breaks after each schema change.

**`FOR ROLE mos_owner` is not optional, and it is the trap inside the trap.** Default privileges attach to *the role that will create the object*, defaulting to whoever runs the statement. Written without `FOR ROLE` and run as `postgres`, it records `postgres` as the creating role — but Alembic connects as `mos_owner`, so every table a later migration creates is still born with no grant. The statement succeeds, `pg_default_acl` gains a row, and the thing it was supposed to prevent happens anyway. `0013` has a test for exactly this: it creates a probe table after the grants and asserts the new table is already readable.

**Three places where `0013` is tighter than the table above**, each because the schema knows something the role table does not:

- **`raw` gets no blanket `UPDATE`** — only `SELECT` and `INSERT`, with `UPDATE (status)` added back on `import_batches` by name. `import_rows` is the audit trail (hard rule 11) and is already immutable by trigger from `0004`; withholding the privilege makes the permission system state the same fact, so the rule survives someone dropping the trigger.
- **`mos_pipeline` needs `USAGE` on `catalog.mosferics_sku_seq`** — the one sequence not owned by an identity column. `catalog.next_mosferics_sku()` calls `nextval` as the *caller*, not as definer, so without this grant the pipeline allocates no SKUs. It would have failed on the first import and nowhere earlier.
- **`mos_nocodb` gets column-level `UPDATE` on `pricing.variant_prices`** — `override_price`, `status`, `approved_by`, and nothing else. The Stage 0 register locks the reviewer's price rights to the override; this expresses that in the database rather than trusting the UI not to offer `calculated_price`.

**Views are covered by the same grants, no extra statement needed.** `ALL TABLES IN SCHEMA` and `ON TABLES` both include views — verified on scratch, where `pricing.live_margins` picked up `mos_pipeline=r` from the plain `ALL TABLES` grant alongside the three real tables. Worth knowing because a view runs with *its owner's* rights, so `SELECT` on `pricing.live_margins` is enough on its own: a role granted the view but not `pricing.cost_basis` can read the margin without being able to read the cost it was computed from. That is a lever if the reviewer should ever see margin without seeing supplier cost — not the current setup, just noted where it will be looked for.

The template above grants `mos_pipeline`. **Repeat it for `mos_nocodb`** — it's the role NocoDB connects as in Stage 5, so without these grants NocoDB opens but shows nothing — and, more narrowly, for `mos_trigger` (only the tables an import touches). Match each role to its rights in the table above; `mos_nocodb` gets no `DELETE` on `raw`.

**Backups on day one, before there is any data to lose:**

First, a dedicated backup bucket and a scoped IAM user (needs an AWS login with permission to create S3 buckets and IAM users).

**Create the bucket** (S3 console): name `mosferics-backups`, region **eu-west-2** (London, matching the droplet), **Block all public access ON**, default encryption left on. A dedicated bucket keeps backups isolated and the policy/retention simple.

**Create the IAM user** `mosferics-backup` (IAM → Users, no console access) and attach this policy, scoped to just this bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "BackupRW",   "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": "arn:aws:s3:::mosferics-backups/*" },
    { "Sid": "BackupList", "Effect": "Allow", "Action": "s3:ListBucket",                   "Resource": "arn:aws:s3:::mosferics-backups" }
  ]
}
```

Create an access key for it ("Application running outside AWS"). Then install the AWS CLI v2 on the droplet and configure it with that key:

```bash
apt install -y unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install
su - mosferics -c 'aws configure'      # IAM key/secret, region e.g. eu-west-2
```

Give `mosferics` password-free local dumps via `~/.pgpass`, then the nightly script and cron (as `mosferics`):

```bash
su - mosferics
echo "localhost:5432:mosferics:mos_owner:CHANGE_ME_OWNER" > ~/.pgpass && chmod 600 ~/.pgpass

cat > ~/backup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
TS=$(date +%F)
DIR=$([ "$(date +%u)" -eq 7 ] && echo weekly || echo daily)   # Sunday -> weekly
DUMP="/tmp/mosferics-$TS.sql.gz"
pg_dump -h localhost -U mos_owner mosferics | gzip > "$DUMP"
aws s3 cp "$DUMP" "s3://mosferics-backups/$DIR/mosferics-$TS.sql.gz"
rm -f "$DUMP"
EOF
chmod +x ~/backup.sh
( crontab -l 2>/dev/null; echo "0 2 * * * /home/mosferics/backup.sh >> /home/mosferics/backup.log 2>&1" ) | crontab -
exit
```

For **retention (14 daily + 4 weekly)**, set two S3 lifecycle rules on the bucket rather than scripting deletes: expire the `daily/` prefix after 14 days, `weekly/` after 28.

**Schedule a restore test one month out — an untested backup is a hypothesis.** The exact test:

```bash
sudo -u postgres createdb scratch_restore
aws s3 cp s3://mosferics-backups/daily/mosferics-<date>.sql.gz /tmp/
gunzip -c /tmp/mosferics-<date>.sql.gz | sudo -u postgres psql scratch_restore
sudo -u postgres psql scratch_restore -c '\dn'     # schemas present?
sudo -u postgres dropdb scratch_restore
```

**Done when:** `psql -h $(tailscale ip -4) -U mos_owner mosferics` connects over Tailscale, the public internet cannot, all four roles exist, and a dump has been taken and restored into `scratch_restore`.

---

## Stage 3 — Repository and Claude Code

```
mosferics-pipeline/
├── CLAUDE.md                  ← hard rules, loaded every session
├── docs/
│   ├── mosferics_data_model_v3.md
│   └── mosferics_build_instructions.md
├── migrations/                ← Alembic
├── config/
│   ├── suppliers/             ← artis.yaml, bbp.yaml, ...
│   ├── templates/             ← title/description by taxonomy node
│   └── vocabularies/          ← colour, material, finish, shape + aliases
├── pipeline/
│   ├── ingest.py
│   ├── normalise.py
│   ├── extract.py
│   ├── link.py
│   ├── price.py
│   └── shopify/
├── tests/
└── .env.example               ← never .env
```

Scaffold the repo, add the two docs, and set up the Python environment before your first Claude Code session.

**Scaffold and init git** (as `mosferics`):

```bash
cd ~
mkdir -p mosferics-pipeline/{docs,config/{suppliers,templates,vocabularies},pipeline/shopify,tests}
cd mosferics-pipeline
git init
printf '.env\n.venv/\n__pycache__/\n*.pyc\n' > .gitignore
printf 'DATABASE_URL=postgresql+psycopg2://mos_owner:CHANGE_ME_OWNER@localhost/mosferics\n' > .env.example
# copy mosferics_data_model_v3.md and mosferics_build_instructions.md into docs/ (scp from your laptop)
```

(`migrations/` is omitted here — `alembic init` creates it in Stage 4.)

**Python environment.** Ubuntu 22.04 ships Python 3.10; install 3.12 from the deadsnakes PPA so you're on a current interpreter (and above the 3.11+ the pipeline expects), then build the venv with it explicitly:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.12 python3.12-venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install "sqlalchemy>=2" alembic psycopg2-binary pandas pytest python-dotenv
pip freeze > requirements.txt
```

(On Ubuntu 24.04, `python3` is already 3.12 — skip the PPA and use `python3 -m venv .venv`.)

**First commit and private remote:**

```bash
git add -A && git commit -m "Initial scaffold"
git remote add origin git@github.com:<you>/mosferics-pipeline.git   # create it private first
git push -u origin main
```

Then create `CLAUDE.md` (below) and install Claude Code.

### Installing and authenticating Claude Code

Install on the droplet as the `mosferics` user — native installer (no Node needed) or npm:

```bash
curl -fsSL https://claude.ai/install.sh | bash   # self-contained binary to ~/.local/bin
# or, with Node 18+:  npm install -g @anthropic-ai/claude-code
claude --version
```

*(If the installer URL has moved, the current command is in the Claude Code install docs — npm is the stable fallback.)*

**Authenticate with your Claude Pro subscription — no API charges.** Run `claude`, `/login`, and pick the **Claude account** option, *not* the API key — that is what routes usage through Pro rather than the API. It prints a login URL; open it in your **laptop's** browser, sign in, approve. Because the droplet is headless, complete the callback one of two ways:

- **Code paste** — if the flow shows a code, paste it back into the terminal; or
- **SSH tunnel** — reconnect with `ssh -L <port>:localhost:<port> mosferics@<droplet>`, matching the callback port the CLI names, so the localhost redirect reaches the droplet.

**One-time only:** the token is stored in `~/.claude` and auto-refreshes, so it persists across sessions and reboots. Pro's limits are per-account (shared with your laptop / claude.ai use) and lower than Max — if long build sessions hit the ceiling, Max is a flat-fee upgrade, still not API pay-as-you-go. This login covers Claude Code as your build tool only; the pipeline's own AI enrichment is a separate, API-billed concern — see Stage 7.

### CLAUDE.md

This is the file that actually governs behaviour. Keep it short enough to be read every time.

```markdown
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
```

**Done when:** repo is initialised, both docs are in `docs/`, `CLAUDE.md` exists, `.env` is gitignored, and a first commit is pushed to a private remote.

---

## Stage 4 — Schema via migrations

Build the schemas in dependency order, one migration per schema:

1. `raw`
2. `supplier`
3. `catalog` (taxonomy, brands, collections, ranges, products, variants, metadata, images, documents, compliance, relations) — `documents` must precede `compliance`; `compliance.evidence_document_id` is a real FK to it
4. `link`
5. `ops` — **swapped ahead of `pricing`**: `pricing.variant_prices.channel_id` is a real FK to `ops.channels`, so the other order cannot be built without deferring a constraint. Nothing in `ops` depends on `pricing`, so the swap costs nothing
6. `pricing` — ships the `pricing.live_margins` view alongside the three tables. It is the canonical answer to "what is the margin now?" (rule 7's derived-at-query-time margin); `variant_prices.margin_pct` is the calc-time snapshot only, and carries a column comment saying so. **The margin must multiply cost by `base_units_per_variant`** — the cost is per base unit and the price is per variant, so omitting the factor overstates margin on every multi-unit line. `0010` shipped that bug and `0011` fixed it
7. `stock` — defer if your stocked list is short

Then, **after all of them**, the reporting views:

8. `review` — three read-only views for NocoDB, at `2b9b2f63d30b`. Last because it
   references tables across `supplier`, `link`, `catalog`, `pricing` and `ops`, so it
   cannot be built before they exist. Views only; no tables, no data. Part A of
   `mosferics_nocodb_views.md` has the SQL

**The migrations as actually built**, since the numbering is not one-per-schema — four of
them are corrections and tightenings applied after the fact, and a clean rebuild needs
them in this order:

| Revision | What |
|---|---|
| `0001` | `raw` schema |
| `0002` | `supplier` schema |
| `0003` | relax `supplier_products.description_status` |
| `0004` | make `raw.import_rows` immutable by trigger (hard rule 11) |
| `0005` | `catalog` schema, `sku_check_digit()`, `next_mosferics_sku()` |
| `0006` | `link` schema |
| `0007` | single-preferred-link trigger; `is_preferred` implies `confirmed` |
| `0008` | auto-sign confirmations; require a confirmer |
| `0009` | `ops` schema |
| `0010` | `pricing` schema + `live_margins` view |
| `0011` | fix `live_margins` to multiply cost by `base_units_per_variant` |
| `0012` | add `notes` to `ops.review_queue` |
| `0013` | apply role grants for all four roles |
| `2b9b2f63d30b` | `review` schema and its three reporting views |

`stock` is not in this list — it is still deferred, and when it lands it must be added to
`DATA_SCHEMAS` in `0013` and granted in the same migration that creates it.

**When building `link.py`, confirm the auto-created links.** Migration 0007 requires `is_preferred` to imply `status = 'confirmed'`, so the auto-created 1:1 link every single-source item gets must be written as `status = 'confirmed'`. It does not have to be signed by hand: migration 0008 added a trigger that fills `confirmed_by` and `confirmed_at` whenever a row is confirmed without a confirmer, so the pipeline can either write `confirmed_by = 'pipeline'` explicitly (which is preserved) or leave both null and sign as `mos_pipeline`. The one remaining trap is writing `confirmed_by` without `confirmed_at` — that fails the pairing constraint, because the signing trigger only fires when `confirmed_by` is null. Leaving `status` at its `proposed` default is the other trap, and the quiet one: it silently drops `is_preferred` on every single-source variant. See the `is_preferred` section of `mosferics_data_model_v3.md` for the full table.

**NocoDB needs no configuration for this.** the reviewer confirms a link by flipping `status` to `confirmed`; the trigger signs it as `mos_nocodb`. Attribution is per role, not per person — if per-person attribution is ever wanted, have NocoDB write the logged-in user into `confirmed_by` and the trigger will leave it alone.

**When building `ops.review_queue`, add the orphaned-preferred-link check.** Migration 0007 made `link.is_preferred` imply `status = 'confirmed'`, and the trigger demotes silently rather than raising — so rejecting or un-confirming the preferred source leaves the variant with no preferred link and nothing says so. The queue must surface **any variant with at least one confirmed link but zero preferred links** as an open item (`entity_type = 'variant_link'`). Sourcing re-picks a winner on its next run, so the item normally clears itself; one that survives across runs means Sourcing found no viable candidate, which is a human problem and otherwise shows up only as a line that quietly cannot be sold. See the `is_preferred` section of `mosferics_data_model_v3.md`.

When building `ops`, create `ops.delivery_config` and seed it with the v1 row — delivery days Tue/Wed/Thu, cutoff 12:00, timezone Europe/London, handling 1 day, empty holiday calendar. Do this now even though the engine that reads it is Stage 8, so a change to delivery days later is a data edit, not a migration — the same up-front pattern as `stock.locations` and `ops.channels`. The table is config only; no logic here.

**Set up Alembic once**, pointing it at the DB through `.env` so no credentials are hardcoded:

```bash
cd ~/mosferics-pipeline && source .venv/bin/activate
alembic init migrations
```

Edit `migrations/env.py` — after the existing `config = context.config` line, load the URL from the environment:

```python
import os
from dotenv import load_dotenv
load_dotenv()
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
```

Leave `sqlalchemy.url` blank in `alembic.ini`. Now create the **real `.env`** — this is the first point it's actually needed (until now only `.env.example` existed). Copy the template and put in the real `mos_owner` password: the *same* value you set on the role in Stage 2 and in `~/.pgpass` — it must be identical in all three places.

```bash
cp .env.example .env
nano .env      # replace CHANGE_ME_OWNER with the real mos_owner password
```

`.env` is gitignored, so the real secret never reaches the repo; `python-dotenv` loads it, so Alembic (and later the pipeline) read `DATABASE_URL` from it. `.env` grows over time — later it also holds a separate connection string for `mos_pipeline` and the Anthropic API key for Stage 7 enrichment.

**Per-schema workflow**, in the dependency order above:

```bash
alembic revision -m "create raw schema"        # generates an empty migration
# author its upgrade()/downgrade() — template below
alembic upgrade head                            # apply
alembic downgrade -1 && alembic upgrade head    # prove down+up before the next schema
```

Template for the first migration (`raw`), which sets the pattern for the rest:

```python
def upgrade():
    op.execute("CREATE SCHEMA raw")
    op.create_table(
        "import_batches",
        sa.Column("batch_id", sa.BigInteger, primary_key=True),
        sa.Column("supplier_id", sa.Integer, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("file_hash", sa.Text, nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer),
        sa.Column("status", sa.Text),
        schema="raw",
    )
    # import_rows likewise, with (batch_id, row_number) as the composite PK

def downgrade():
    op.execute("DROP SCHEMA raw CASCADE")
```

Author the real table definitions with Claude Code, reading the matching `mosferics_data_model_v3.md` section per schema — its *Referential integrity* list gives the FKs, composite PKs, and the `is_preferred` partial index + trigger to include.

Every migration must have a working `downgrade()`. Test each one up, down, and up again on a scratch database before moving on.

**The role grants are migration `0013`** — they are no longer a manual step, so there is nothing to remember to run here. See Stage 2 for the shape and the three places it is deliberately tighter than the role table.

> **Applied to live 17 Aug 2026, at `0013`.** Before that, `relacl` was empty on every table in every schema and `pg_default_acl` had no rows at all: nothing but `mos_owner` could read or write anything, anywhere. It had gone unnoticed because everything so far ran as `mos_owner`, and NocoDB at Stage 5 would have been the first thing to trip over it.
>
> Six schemas are covered — `raw`, `supplier`, `catalog`, `link`, `ops`, `pricing`. **When `stock` lands, add it to `DATA_SCHEMAS` in `0013`** and grant in the same migration that creates it; a new schema does not inherit anything, because default privileges are per-schema.

**Done when:** `alembic upgrade head` builds the whole database from empty on a clean instance — all six schemas plus `review`, ending at `2b9b2f63d30b` — `downgrade base` tears it down without error, and the Stage 2 role grants are applied so `mos_pipeline` can actually read/write the tables.

---

## Stage 5 — NocoDB

Run as `mosferics`. NocoDB is reachable over Tailscale only — never the public internet.

**1. Install Docker + the Compose plugin:**

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker mosferics
newgrp docker                        # activate the group now (or log out/in)
docker --version && docker compose version
```

**2. Run NocoDB via Compose.** Host networking lets it reach Postgres on `localhost:5432` with no extra config; port 8080 stays private behind `ufw` + the Cloud Firewall.

```bash
mkdir -p ~/nocodb && cd ~/nocodb
cat > docker-compose.yml <<'EOF'
services:
  nocodb:
    image: nocodb/nocodb:latest
    restart: unless-stopped
    network_mode: host          # reaches Postgres on localhost:5432
    volumes:
      - ./data:/usr/app/data    # NocoDB's own metadata (views/config) persists here
EOF
docker compose up -d
docker compose logs -f nocodb   # wait for it to report started, then Ctrl-C
```

**3. Put it behind HTTPS over Tailscale.**

**HTTPS here needs the Tailscale approach, not standard Let's Encrypt.** Caddy's zero-config "automatic HTTPS" relies on a public HTTP/TLS challenge, which a tailnet-only service cannot answer — there is no public endpoint to validate against. Instead, enable **HTTPS Certificates + MagicDNS** in the Tailscale admin: that gives the droplet a real, auto-renewing Let's Encrypt certificate on its `<machine>.<tailnet>.ts.net` name via a DNS challenge Tailscale performs for you. Enable that (**HTTPS Certificates + MagicDNS**) in the Tailscale admin console once, then:

```bash
sudo tailscale serve --bg 8080          # HTTPS on the .ts.net name → localhost:8080
tailscale serve status                   # shows the https://mosferics-ops-01.<tailnet>.ts.net URL
```

`tailscale serve` runs inside tailscaled, so it needs no extra `ufw` rule; if your version's flags differ, `tailscale serve --help` shows them. NocoDB is now at `https://mosferics-ops-01.<tailnet>.ts.net`, tailnet-only, with a real auto-renewing certificate.

**Alternative — Caddy**, only worth it if you'll run *multiple* services behind one reverse proxy later: mint a cert with `sudo tailscale cert mosferics-ops-01.<tailnet>.ts.net` (produces a `.crt` and `.key`), add a `caddy` service to the Compose file that mounts them, point its `tls` directive at the pair (no ACME), and proxy to `localhost:8080`. For one service it's needless overhead. Last-resort with no `.ts.net`: Caddy's `tls internal` self-signed cert, but every user must then trust Caddy's root.

**4. Connect NocoDB to Postgres as `mos_nocodb`.** In the NocoDB UI → **New Base → connect an external database** (or Data Sources → New): host `localhost`, port `5432`, database `mosferics`, user `mos_nocodb`, password from Stage 2. This is where `mos_nocodb`'s credentials live — not the repo `.env` (see the role/credential map in Stage 2). Its Stage 4 grants must be applied, or the grid opens empty.

Build these views before the reviewer ever sees it, because an unfiltered 60k-row grid is unusable:

| View | Purpose | Built as | Source |
|---|---|---|---|
| Needs review | `status = pending_review`, grouped by field type | NocoDB filter | `catalog.product_metadata` |
| Proposed links | Variant groupings and multi-source matches awaiting confirmation | NocoDB filter | `link.variant_links` |
| Unmapped categories | Supplier categories with no taxonomy mapping | **Postgres view** | `review.unmapped_categories` |
| Unmapped vocabulary | Colour/material/finish values not in the alias table | NocoDB filter | `ops.review_queue` |
| Missing images | Products with no matched image | **Postgres view** | `review.missing_images` |
| Price changes | Above materiality threshold, awaiting approval | NocoDB filter | `pricing.variant_prices` |
| Ready to publish | Everything verified, not yet synced | **Postgres view** | `review.ready_to_publish` |

**These seven are not seven of the same thing.** A NocoDB view is a saved filter over a
*single table* and cannot join. Three of the seven are defined by an **absence** — a row
with no match in another table — which is an anti-join and has to be SQL; those are the
`review` schema, built in migration `2b9b2f63d30b` (Stage 4, step 8). The other four are
filters in the UI. The dividing rule: **anything the reviewer has to change must be a NocoDB
filter view on the real table**, because a Postgres view is read-only in practice and he
could not approve, confirm or override through one.

*Unmapped vocabulary* is the one that looks like it should be a Postgres view and isn't.
Unmapped *categories* can be computed because `link.category_map` is a table to anti-join
against; the vocabulary alias lists are **YAML files in git**, so there is nothing in the
database to anti-join. The only record of a failed normalisation is the row `normalise.py`
writes to `ops.review_queue` with `entity_type = 'vocabulary'` — so it is a filter on the
queue, and legitimately empty until Stage 7.

Full build: `mosferics_nocodb_views.md` — Part A the migration, Part B the four filter
views, Part C the lockdown.

**NocoDB does not notice new database objects on its own.** After any migration that adds
or changes a view: Base Settings → Data Sources → `review` → Meta Sync → Reload → Sync
Now. A view that exists in Postgres but not in NocoDB is almost always a missing `GRANT`,
which is why the grants live in the migration beside the views.

Lock `raw` tables to read-only in the UI. They are the audit trail. Connect `review` the
same way with **Allow Data Edit off** — one toggle makes every report view read-only, now
and for every view added later.

**Done when:** the reviewer can reach NocoDB over Tailscale, can edit a description and flip a review flag, and cannot touch `raw` or run DDL.

**Not** "sees only the filtered views" — that is not achievable and should not be written as an exit criterion. NocoDB has no per-view access control for internal collaborators, so the reviewer also sees each table's default grid in the sidebar; sharing a view by link does not help, because shared views are read-only and cannot carry the work he has to do. Mitigate ergonomically — trim and **Lock** each table's default view, order the named views above it — and treat the real boundary as the `mos_nocodb` grants plus the per-data-source toggles. See Part C of `mosferics_nocodb_views.md`.

---

## Stage 6 — Taxonomy and vocabularies

**Status: half landed — this stage is not done.** The taxonomy is loaded (8 Oct 2026, 256 rows from `taxonomy_v4.md`; `sku_prefix` on the 12 level-1 nodes, `variant_axes` in option order on every category and type). The four vocabulary lists are agreed but `config/vocabularies/` is empty, so **Stage 7 is still blocked** on `normalise.py` having nothing to read. See the Stage 0 note for the checks.

How the tree was stored, where the docs left it open: `[]` is an **empty array** (explicitly no axis), NULL only on departments; a type carries a **copy** of its category's axes unless the tree overrides it, so any node a product maps to answers for itself; `slug` is Shopify-handle style and `path` is the slugs joined with `/`; descriptive lists after an em dash in `taxonomy_v4.md` are not nodes.

What remains is transcription rather than design — commit the four YAMLs. The method below is the record of how v1 was derived and the guide for extending it: new suppliers add aliases, the tree grows below the frozen Level 1.

Paper work, but it blocks Stage 7. Do it properly.

### Taxonomy

Do not invent a tree top-down. Derive it from what you actually sell:

1. Export distinct `supplier_category_raw` values from Artis, BBP, Beaumont and Nevilles — a pivot on your existing sheets gives you this in minutes
2. Dedupe and cluster into natural groups
3. Draft **three levels**. Deeper becomes a maintenance burden; shallower gives useless filtering
4. For each leaf node record: ordered `variant_axes`, applicable spec fields, title template
5. Sanity test: can a customer reach a 12oz hiball in three clicks or fewer?
6. Freeze v1 and accept it will change

Taxonomy exists for navigation and rule scoping. It is not a complete ontology of hospitality supplies. Resist the urge to perfect it.

Shopify maintains its own standard product taxonomy which drives category-specific metafields. Worth mapping your leaves to it — check the current state of that feature before committing effort.

### Controlled vocabularies

Same extraction method: pull distinct values for Colour, Material, Finish and Shape from your four ready sheets.

For each, produce two lists:

**Canonical values** — the terms you will actually publish.
**Aliases** — every supplier spelling that maps to each canonical value.

```yaml
# config/vocabularies/material.yaml
stainless_steel:
  display: "Stainless Steel"
  aliases: ["S/Steel", "Stainless", "18/10", "18/8", "SS", "Stainless Stl"]
```

Colour additionally needs a hex value per canonical entry, or you cannot render swatches on Shopify.

Glassware colour needs an early decision: "clear", "crystal" and "plain" are the same thing commercially and will arrive as all three.

**The alias table is the deliverable.** It is what lets ingest normalise automatically. Unmapped values go to the review queue, get aliased once, and are never seen again — so the list grows quickly at first and then stops.

**Done when:** taxonomy tree v1 is written with variant axes per leaf, four vocabulary files exist with alias lists covering everything in your four ready sheets, and colours have hex values.

---

## Stage 7 — Artis end to end

First real pilot. Artis is the right choice: capacity variants, and glassware will stress-test unit normalisation harder than anything else in your catalogue.

**The pipeline modules are written here** — one per step below, as a complete vertical slice for one supplier. They are the data model's engines made physical (`pipeline/`):

| Module | Job | Reads → Writes |
|---|---|---|
| `ingest.py` | Land the file verbatim (+ hash) | supplier file + YAML → `raw.*` |
| `extract.py` | Per-supplier column mapping + regex for attributes buried in names | `raw` rows → staged fields |
| `normalise.py` | Vocabulary alias + unit normalisation to canonical | staged + alias tables → `supplier_products`, `supplier_offers` |
| `link.py` | Category mapping, create products/variants + SKUs, 1:1 + multi-source links, grouping | supplier_products + catalog → `catalog.*`, `link.*` |
| `price.py` | Cost-per-base-unit, rules by priority, prices held for approval, preferred source | `cost_basis` + `rules` → `variant_prices`, `is_preferred` |
| `shopify/` | Export verified rows | verified rows → CSV (then API, Stage 10) |

A **runner** sequences them (`ingest → extract → normalise → link → price →` review `→ shopify`) and logs each run to `ops.pipeline_runs`; the staff `.exe` (Stage 9) is a thin trigger as `mos_trigger`. Every module is **idempotent** — a re-run reuses existing variants and SKUs via their links. Per-supplier behaviour lives in the YAML, never in the Python. `enrich.py` (AI descriptions) and the image-matching script come later (step 9 below / Stage 10).

1. `config/suppliers/artis.yaml` — column mapping and extraction rules
2. Ingest to `raw`, verbatim, with file hash
3. Normalise into `supplier_products` and `supplier_offers`
4. Vocabulary normalisation via alias tables; unmapped values to review
5. Category mapping; unmapped to review
6. Create products and variants, 1:1 links auto-confirmed
7. Capacity variant grouping — proposed, then confirmed by you
8. Pricing rules and calculated prices
9. Image matching, ported from your existing script
10. Review in NocoDB
11. Shopify CSV export, verified rows only

### Capacity specifics

Model **`capacity_lined` and `capacity_brim` as separate fields.** They differ materially and suppliers publish both, inconsistently, sometimes in the same column. Lined is the variant axis — it is what the trade buys on.

Expect oz, cl, ml and pint-to-line in the same file. Normalise to ml canonical, retain the raw string.

Capacity is **both an option and a metafield**: the option string carries the selector label, a variant-level `volume` metafield carries the filterable numeric value. This is not duplication — option strings sort alphabetically and cannot be filtered numerically.

**AI cost boundary:** nothing in this pilot invokes the AI enrichment engine — it's deferred behind the review gate (Stage 10). When it does arrive, it calls the Anthropic API directly from Python and needs its **own API key and budget**, separate from and unaffected by the Claude Pro subscription you authenticate Claude Code with (Stage 3).

**Done when:** Artis products exist in Postgres with correct variants, a CSV imports cleanly to Shopify, and capacity filtering works on the storefront.

---

## Stage 8 — BBP, two-axis

Harder than Artis by a meaningful margin. Colour and capacity together.

**Sparse matrices are the rule, not the exception.** Blue in 10oz and 12oz, red in 12oz only. Your grouping logic must reproduce exactly what the supplier data supports and never interpolate. An invented combination becomes a purchasable variant you cannot fulfil.

**Option order is fixed per taxonomy node and never varies.** If Colour is option 1 for one product and option 2 for another, storefront selectors come out inconsistent and it is painful to correct afterwards.

BBP likely buries colour in the product name where Artis gave you a column — so this stage also validates your regex extraction path, which Artis will not have exercised.

**Done when:** a two-axis BBP range renders correctly in Shopify with a sparse matrix intact and no invented combinations.

---

## Delivery promise engine — design spec

Built after the pilot proves out (post Stage 7/8, alongside Shopify storefront work — it needs products, offers and stock signals that do not exist earlier, and it surfaces on the storefront). **Not built during the structural stages.** All parameters are already decided and seeded into `ops.delivery_config` at Stage 4; what follows is the process to build the logic that reads them. Mostly design decisions, a little code at the end — the hard parts are steps 1, 4 and 6.

**Step 1 — Confirm the parameters and their home.** They already live in `ops.delivery_config`: `delivery_days` {Tue, Wed, Thu}, `cutoff_time` 12:00, `timezone` Europe/London, `handling_days` 1, `holiday_calendar` (empty, populate with bank holidays later). A table not a file, so the reviewer can change delivery days in NocoDB without a deploy. `timezone` is explicit — the cutoff and "today" are meaningless without it and BST/GMT shifts produce wrong dates.

**Step 2 — Write the algorithm as a pure function.** Deterministic, no database inside, so it is testable: `promise(now, fulfilment_mode, in_stock, supplier_lead_days, config) -> delivery_date`.

```
1. effective_order_date = date(now); if time(now) >= cutoff → + 1 day
2. goods_at_warehouse:
      stocked & in_stock   → effective_order_date
      on_demand / out      → effective_order_date + supplier_lead_days
3. ready_to_dispatch = goods_at_warehouse + handling_days      (= 1)
4. delivery_date = earliest day >= ready_to_dispatch that is in
                   delivery_days AND not in holiday_calendar
```

Sanity check: stock in hand, ordered Mon 11:00 → ready Tue → Tue is a delivery day → delivered Tue (next-day). Ordered Mon 13:00 → effective order Tue → ready Wed → Wed. Both correct.

**Step 3 — Compute-on-read vs store.** The promised date depends on "now", so it changes daily for the same product. Compute on the fly for storefront display (never store it or it goes stale overnight); **freeze** the promised date onto the order line at checkout (the promise you made that customer at that moment — needed for accountability if missed, the same store-the-event instinct as `stock.movements`).

**Step 4 — Enumerate edge cases as test fixtures first.** Write expected input→output pairs before coding: order exactly at 12:00:00 (is the cutoff inclusive?); **Thursday after cutoff rolling across the weekend to Tuesday**; weekend orders; a bank holiday on a delivery day; supplier goods arriving on a Saturday; stocked-but-out falling through to the on-demand branch. **The one genuine business fork — multi-line orders:** deliver everything together on the slowest line's date, or split shipments? Decide explicitly; most buy-to-order operations pick "one delivery, slowest line sets the date".

**Step 5 — Reconcile with the availability bands.** The engine reads the same `fulfilment_mode` and stock signals the bands use. Do not build a second, divergent copy of "is this in stock" — the bands are the coarse label, the promise date the fine detail from the same inputs. One source.

**Step 6 — Decide the Shopify surface.** Delivery-date messaging on the product page, a shipping profile, or an app. Availability sync is daily and ungated, so whatever is pushed must be safe to auto-update. Simplest v1: publish the raw fields (mode, lead time, in-stock band) and compute the displayed date on the storefront, rather than pushing 60k nightly date recalculations.

**Step 7 — Validate against hand-worked real examples, then freeze.** Take a dozen real product+order scenarios, work the promised date by hand, assert the engine matches, freeze v1, accept the config will change.

**Done when:** the pure function passes its fixture table, the storefront shows a correct delivery date for stocked and on-demand lines including the Thursday→Tuesday weekend roll, and the promised date is frozen onto the order at checkout.

---

## Stage 9 — Remaining work, not code

Running alongside the build:

| Task | Notes |
|---|---|
| Pricing rules per supplier and category | You described these as highly specific. Write them out before Stage 7 |
| Metafield map | Internal key → namespace.key, type, owner level. Blocks Shopify sync |
| Type templates | Title and description composition per taxonomy node |
| Compliance document requests | Longest lead time of anything here. Start now, in parallel |
| Supplier YAMLs | One per supplier, written as you onboard each |
| the staff exe | PyInstaller, after Stage 8 proves the pipeline |

---

## Stage 10 — Later

- Shopify Admin API replacing CSV
- Availability sync from supplier stock feeds, on its own daily schedule, ungated
- Preferred source and cost basis
- AI enrichment behind the review gate, one supplier at a time
- Basket optimisation as a separate tool
- Tiered pricing (Standard/Bronze/Silver/Gold) — additive `pricing.tiers` + `tier_prices` layer, see the data model's forward-design note; build when the commercial model firms up

---

## Sequencing note

Stages 1–5 are a weekend of work for someone with your background. Stage 6 is the one that will take longer than expected and it has no code in it. If you find yourself building Stage 7 before the taxonomy is written, you are building on a foundation that does not exist yet.
