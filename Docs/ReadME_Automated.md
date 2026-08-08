# DriverDex D1 Sync &amp; Search

Fully automated pipeline: `*.manifest*.json` driver manifests in this repo &rarr;
Cloudflare D1 (`driverdex-index`) &rarr; two small Cloudflare Workers, all driven by a
single GitHub Actions workflow that runs on every push.

```
push to main (manifest/schema/worker changes)
        │
        ▼
.github/workflows/d1-sync.yml
        │
        ├─ 1. ensure D1 database "driverdex-index" exists (auto-create, idempotent)
        ├─ 2. apply schema.sql                      (idempotent, IF NOT EXISTS)
        ├─ 3. check storage usage vs. 5GB free-plan limit (warns/fails early)
        ├─ 4. migrate_to_d1.py: manifests -> batched upsert SQL chunks + cleanup diff
        ├─ 5. apply import chunks, then cleanup chunks to D1 (--remote)
        ├─ 6. deploy search-worker  ("driverdex-check") -- the API
        └─ 7. deploy site           ("driverdex")       -- the search UI, proxies /api/* to driverdex-check
```

Nothing here needs to be run by hand after the one-time secrets setup below.
Pushing a new/changed `*.manifest*.json` is enough to update the live database
**and** redeploy both Workers.

## What gets created automatically

| Resource | Created by | Name |
|---|---|---|
| D1 database | first CI run (`migrate_to_d1.py --ensure-db`) | `driverdex-index` |
| Search API Worker | first CI run (`wrangler deploy` in `search-worker/`) | `driverdex-check` |
| Search UI Worker | first CI run (`wrangler deploy` in `site/`) | `driverdex` |

You'll get public URLs like `https://driverdex-check.<your-subdomain>.workers.dev`
and `https://driverdex.<your-subdomain>.workers.dev` — the exact URLs are printed
in each `wrangler deploy` step's log, and summarized at the bottom of every
workflow run under the **Summary** tab.

> Note on naming: classic Cloudflare Pages gave you a `*.pages.dev` URL.
> Cloudflare's current guidance (as of 2026) is to use Workers with static
> assets for new static+API sites instead of Pages — same one-deploy
> simplicity, same free static-asset serving, but with D1 bindings, service
> bindings, and observability built in. Functionally this *is* what
> "driverdex.pages.dev" / "driverdex-check.pages.dev" would have been; the URL just ends
> in `.workers.dev` instead of `.pages.dev`. You can attach a custom domain
> (e.g. `driverdex.yourdomain.com`) to either Worker later from the dashboard if
> you'd like a stable, branded URL instead of the `*.workers.dev` one.

## One-time setup

### 1. GitHub repository secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | token described below |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard sidebar, or `wrangler whoami` |

No GitHub-side permission changes are needed. The workflow only checks out
the repo and never commits back, so the default read-only `GITHUB_TOKEN` is
sufficient.

### 2. Cloudflare API token

Create at `https://dash.cloudflare.com/profile/api-tokens → Create Token →
Custom Token`. This setup needs more than the original D1-only scope,
because the workflow now also creates the database itself and deploys two
Workers:

| Scope | Permission | Why |
|---|---|---|
| Account | **D1 → Edit** | Writes to D1 (INSERT/DELETE) and `wrangler d1 create` / `wrangler d1 list` / `wrangler d1 info`. |
| Account | **Workers Scripts → Edit** | `wrangler deploy` for both `search-worker` and `site`. |
| Account | **Account Settings → Read** | Lets Wrangler resolve your account ID and list resources by name. |

Scope it to the specific account (not "All accounts") under **Account
Resources**, and set an expiry if your org requires key rotation — an
expired token just makes the next `wrangler` call fail auth, which the
workflow's `wrangler whoami` preflight step and fail-fast error checking
will catch and surface clearly as a failed Action run, not a silent partial
sync.

**Narrowing the blast radius:** this token is stored as a GitHub Actions
secret, so anyone with write access to the repo (or to the workflow file)
can effectively use it. If you want to scope it tighter, Cloudflare API
tokens can be restricted to specific D1 databases and specific Worker
scripts rather than "all D1" / "all Workers" on the account — worth doing
once the resource names below exist (`driverdex-index`, `driverdex-check`, `driverdex`).

### 3. Nothing else

No `wrangler.toml` to hand-edit, no `wrangler d1 create` to run yourself, no
database_id to copy-paste anywhere. The first push does all of that.

## File placement

```
your-repo/
├── schema.sql                          # idempotent CREATE TABLE/INDEX IF NOT EXISTS
├── migrate_to_d1.py                    # manifests -> SQL chunks, +auto-create, +storage check
├── clean_db.sql                        # manual fallback only, never auto-run
├── wrangler.toml                       # root config anchor; database_id auto-patched by CI
├── .gitignore
├── .github/
│   └── workflows/
│       └── d1-sync.yml                 # the one workflow that does everything
├── search-worker/                      # API: GET /api/search, /api/driver/:id, /api/hwid/:id, /api/stats, /api/facets
│   ├── wrangler.toml
│   └── src/index.js
├── site/                               # UI: search page, proxies /api/* same-origin to search-worker
│   ├── wrangler.toml
│   ├── src/index.js                    # thin proxy Worker
│   └── dist/index.html                 # the actual search page (no build step)
└── manifests/                          # all manifests live here
    ├── Display.manifest.json           # per-category driver manifests
    ├── Network.manifest.json
    └── installers.manifest.json        # intentionally skipped by migrate_to_d1.py (different schema)
```

The hard requirement is `d1-sync.yml` living at exactly
`.github/workflows/d1-sync.yml` — that's the only path GitHub Actions
discovers workflows under. `migrate_to_d1.py` discovers manifests
recursively (`--input-dir .` with `rglob("*.manifest*.json")`), so the
`manifests/` folder is picked up automatically without extra config.
Everything else can move; if you relocate `migrate_to_d1.py` or
`schema.sql`, update the corresponding paths in `d1-sync.yml`.

## How duplicates are prevented (the "no duplicate" guarantee)

This was the main fragility in the original design, and it's fixed at the
SQL level, not just by convention:

- **Drivers** rows use `INSERT ... ON CONFLICT(driver_id) DO UPDATE SET ...`
  — a true upsert. Re-running the exact same manifest sync twice in a row
  (e.g. a retried CI job) updates the existing row in place; it can never
  create a second row for the same `driver_id`, and `created_at` is
  preserved across re-syncs (only `updated_at` refreshes).
- **HWIDs** and **DriverParts** are deleted-then-reinserted per driver on
  every sync (`DELETE FROM HWIDs WHERE driver_id = ?` immediately followed
  by fresh `INSERT`s) — this guarantees the child rows are always an exact
  mirror of the current manifest, including HWIDs that were *removed* from
  a driver entry, not just added. Both tables also carry `UNIQUE` SQLite
  constraints as a backstop.
- **The cleanup pass** (`--clean`) diffs every `driver_id` currently in D1
  against what's currently on disk across all manifests, and deletes
  anything orphaned — including an entire manifest file that was deleted
  from the repo. This runs on every CI push, so deleted/renamed drivers
  don't linger forever.
- **Database/Worker creation** is list-before-create everywhere: the script
  always calls `wrangler d1 list` (or `wrangler deploy`'s own
  create-or-update semantics for Workers) before attempting to create
  anything, so re-running the workflow on an already-provisioned account is
  a no-op for provisioning and only does the data sync + redeploy.

Net effect: you can push the same manifests twice, re-run the workflow
manually, or have a step retry after a transient failure, and the database
ends up in exactly the same state either way.

## The 5GB free-plan limit

`migrate_to_d1.py --check-storage` calls `wrangler d1 info --json` before
every sync and compares the live database size against Cloudflare's 5GB
free-plan ceiling:

- **&lt;80%** — proceeds silently.
- **&ge;80%** — logs a warning but still proceeds (gives you a heads-up
  before it becomes urgent).
- **&ge;95%** — **fails the workflow before any writes happen.** This is
  deliberate: D1 rejecting a write mid-transaction partway through a sync
  is a worse outcome than the sync simply not running this time. If you hit
  this, either clean up stale rows (see below) or upgrade the D1 plan, then
  re-run the workflow (or just push again).

To use the full 5GB automatically over time, you don't need to do anything
differently — the pipeline already writes as much as fits and the storage
check is purely a safety rail, not a throttle. If you ever do need to free
space manually, `clean_db.sql` is the documented manual fallback (see the
comments in that file); it's never run automatically.

## Search API (`search-worker`, deploys as `driverdex-check`)

| Endpoint | Description |
|---|---|
| `GET /api/search?q=&category=&provider=&arch=&page=&pageSize=` | Free-text search, or exact/prefix hardware-ID lookup if `q` looks like a HWID (contains `VEN_`, `DEV_`, `SUBSYS_`, or a backslash). |
| `GET /api/driver/:driver_id` | Full driver detail, including all HWIDs and archive parts (everything needed to download + reassemble). |
| `GET /api/hwid/:hwid` | Direct hardware-ID lookup, ranked exact-match first. This is the endpoint a driver-installer client would actually call. |
| `GET /api/stats` | Aggregate counts + last sync info, for the UI header. |
| `GET /api/facets` | Distinct category/provider/arch values + counts, for filter dropdowns. |

All HWID lookups hit the schema's covering index
(`idx_hwids_lookup_covering`), confirmed via `EXPLAIN QUERY PLAN` during
testing to resolve without a separate table seek.

## Search UI (`site`, deploys as `driverdex`)

A single static page (no build step, no framework, no npm dependencies to
maintain) that calls the API same-origin via the `site` Worker's proxy —
the browser never talks to `driverdex-check` directly, so there's no CORS
configuration to keep in sync. Supports free-text search, hardware-ID exact
lookup, category/provider/architecture filters, expandable per-driver detail
(HWID list + multi-volume archive parts with copy-to-clipboard and direct
download links), and pagination.

## Local development

```bash
# Terminal 1 — the API
cd search-worker
wrangler d1 execute driverdex-index --local --file=../schema.sql
wrangler dev --local --port 8787

# Terminal 2 — the UI (auto-discovers the running driverdex-check dev session
# by name for its service binding)
cd site
wrangler dev --local --port 8788
```

Then open `http://localhost:8788` — the UI, API, and D1 (as a local SQLite
file under `.wrangler/state`) all run entirely offline, no Cloudflare
account needed for local iteration.

## Manual escape hatches

- **Re-run the sync without a new push:** use the **Run workflow** button
  on the `D1 Sync & Deploy` workflow in the Actions tab
  (`workflow_dispatch` is enabled).
- **Manual cleanup:** `clean_db.sql` — see the usage comments at the top of
  that file. Never runs automatically.
- **Inspect generated SQL:** every workflow run uploads the exact chunks it
  applied as a build artifact (`d1-sql-chunks`, kept 7 days) under that
  run's **Artifacts** section, even on failure.

