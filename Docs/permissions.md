## Required permissions (Account-level)

Create a **Custom Token** at *Cloudflare Dashboard → My Profile → API Tokens → Create Token → Custom token* with these scopes:

| Permission | Type | Why it's needed
|-----|-----|-----
| **Account → D1** | **Edit** | `wrangler d1 list`, auto-create the `ldc-index` DB (`--ensure-db`), and `d1 execute --remote` to write your synced rows.
| **Account → Workers Scripts** | **Edit** | Deploys both Workers (`ldc-check` and `ldc`), including the static `./dist` assets bound to the `site` Worker.
| **Account → Workers KV Storage** | **Edit** | The `INSTALL_LINKS` KV namespace both Workers bind (one writes install tokens, the other reads them).
| **Account → Account Settings** | **Read** | Lets `wrangler whoami` (the credential-verification step) resolve your account.


## Token settings

- **Account Resources:** Include → *your specific account* (the one matching `CLOUDFLARE_ACCOUNT_ID`).
- **Zone Resources:** none needed — everything here uses `*.workers.dev`, not a custom domain. (If you later route the Workers through a custom domain, add **Zone → Workers Routes: Edit** and **Zone → DNS: Edit** for that zone.)


## A simpler alternative

If you don't want to hand-pick scopes, use the prebuilt **"Edit Cloudflare Workers"** template when creating the token — it bundles Workers Scripts, KV, D1, and Account Settings read in one go. It's slightly broader than the minimal set above but covers this entire workflow.

## The two non-permission secrets

These are identifiers, not scopes, but the workflow fails without them: `CLOUDFLARE_ACCOUNT_ID` (your account ID) and the repo secret `KV_INSTALL_LINKS_ID` (the `INSTALL_LINKS` namespace id from `wrangler kv namespace create INSTALL_LINKS`). `D1_DB_NAME` is already hardcoded to `ldc-index` in the workflow.
