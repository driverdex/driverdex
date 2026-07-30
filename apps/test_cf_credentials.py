#!/usr/bin/env python3
"""
test_cf_credentials.py
----------------------
Validates your Cloudflare Account ID and API Token before you run the
GitHub Actions workflow. Run this locally:

    python test_cf_credentials.py

Then paste your credentials when prompted.
No values are stored or printed beyond what you see on screen.
"""

import json
import urllib.request
import urllib.error
import getpass
import sys

# ── ANSI colours ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}✔{RESET}  {msg}")
def fail(msg): print(f"  {RED}✖{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}!{RESET}  {msg}")
def info(msg): print(f"  {CYAN}→{RESET}  {msg}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")

# ── HTTP helper ──────────────────────────────────────────────────────────────
def cf_get(path, token):
    url = f"https://api.cloudflare.com/client/v4{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code
    except urllib.error.URLError as e:
        print(f"\n{RED}Network error: {e.reason}{RESET}")
        sys.exit(1)

# ── Checks ───────────────────────────────────────────────────────────────────
def check_token(token, account_id):
    header("1 · Verifying API token")
    # /user/tokens/verify requires the token itself to have "API Tokens Read"
    # which custom tokens don't include by default. So we use the account
    # endpoint instead -- if the token can authenticate and read the account,
    # it is valid. A 401 is the only proof the token is bad.
    data, status = cf_get(f"/accounts/{account_id}", token)
    if status == 200 and data.get("success"):
        name = data.get("result", {}).get("name", "unknown")
        ok(f"Token is valid — authenticated as: {BOLD}{name}{RESET}")
        return True
    elif status == 401:
        errors = data.get("errors", [])
        for e in errors:
            fail(f"code {e.get('code')}: {e.get('message')}")
        fail("Token is INVALID or has been revoked.")
        info("Create a new token at: https://dash.cloudflare.com/profile/api-tokens")
        info("Required permissions: D1 Edit · Workers Scripts Edit · Workers KV Storage Edit")
        return False
    elif status == 403:
        fail("Token is valid but has no access to this account.")
        info("Make sure the token is scoped to account: " + account_id)
        return False
    else:
        errors = data.get("errors", [])
        for e in errors:
            fail(f"code {e.get('code')}: {e.get('message')}")
        fail(f"Unexpected status {status} from Cloudflare API.")
        return False


def check_account(account_id):
    header("2 · Verifying Account ID format")
    clean = account_id.strip().replace("-", "")
    if len(clean) != 32 or not all(c in "0123456789abcdefABCDEF" for c in clean):
        fail(f"'{account_id}' does not look like a 32-char hex Account ID.")
        info("Find it at: Cloudflare Dashboard → right sidebar → Account ID")
        return False
    ok(f"Account ID format is valid: {account_id}")
    return True


def check_d1_permission(token, account_id):
    header("3 · Checking D1 access (list databases)")
    data, status = cf_get(f"/accounts/{account_id}/d1/database", token)
    if status == 200 and data.get("success"):
        dbs = data.get("result", [])
        ok(f"D1 access confirmed  ({len(dbs)} database(s) visible)")
        if dbs:
            for db in dbs[:5]:
                info(f"  {db.get('name')}  (id: {db.get('uuid', db.get('id', '?'))})")
            if len(dbs) > 5:
                info(f"  ... and {len(dbs) - 5} more")
        else:
            warn("No D1 databases exist yet — CI will create driverdex-index on first run.")
        return True
    else:
        errors = data.get("errors", [])
        for e in errors:
            fail(f"code {e.get('code')}: {e.get('message')}")
        fail("Token cannot list D1 databases.")
        info("Add 'D1 · Edit' permission scoped to your account when creating the token.")
        return False


def check_workers_permission(token, account_id):
    header("4 · Checking Workers Scripts access")
    data, status = cf_get(f"/accounts/{account_id}/workers/scripts", token)
    if status == 200 and data.get("success"):
        scripts = data.get("result", [])
        ok(f"Workers Scripts access confirmed  ({len(scripts)} script(s) visible)")
        for s in scripts[:5]:
            info(f"  {s.get('id')}")
        if len(scripts) > 5:
            info(f"  ... and {len(scripts) - 5} more")
        return True
    else:
        errors = data.get("errors", [])
        for e in errors:
            fail(f"code {e.get('code')}: {e.get('message')}")
        fail("Token cannot list Workers scripts.")
        info("Add 'Workers Scripts · Edit' permission scoped to your account.")
        return False


def check_kv_permission(token, account_id):
    header("5 · Checking KV Storage access")
    data, status = cf_get(f"/accounts/{account_id}/storage/kv/namespaces", token)
    if status == 200 and data.get("success"):
        ns = data.get("result", [])
        ok(f"KV Storage access confirmed  ({len(ns)} namespace(s) visible)")
        if ns:
            for n in ns[:5]:
                info(f"  {n.get('title')}  (id: {n.get('id', '?')})")
            if len(ns) > 5:
                info(f"  ... and {len(ns) - 5} more")
        else:
            warn("No KV namespaces exist yet.")
            warn("Run:  npx wrangler kv namespace create INSTALL_LINKS")
            warn("Then add the printed id as GitHub secret: KV_INSTALL_LINKS_ID")
        return True
    else:
        errors = data.get("errors", [])
        for e in errors:
            fail(f"code {e.get('code')}: {e.get('message')}")
        fail("Token cannot list KV namespaces.")
        info("Add 'Workers KV Storage · Edit' permission scoped to your account.")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{BOLD}Cloudflare Credentials Tester{RESET}")
    print("─" * 40)
    print("Paste your values below. Token input is hidden.")
    print("Nothing is sent anywhere except api.cloudflare.com.\n")

    account_id = input("Account ID (32-char hex): ").strip()
    token      = getpass.getpass("API Token (hidden):       ").strip()

    if not account_id or not token:
        print(f"\n{RED}Both values are required.{RESET}")
        sys.exit(1)

    results = []
    results.append(check_account(account_id))
    results.append(check_token(token, account_id))
    results.append(check_d1_permission(token, account_id))
    results.append(check_workers_permission(token, account_id))
    results.append(check_kv_permission(token, account_id))

    header("Summary")
    passed = sum(results)
    total  = len(results)

    if passed == total:
        print(f"\n  {GREEN}{BOLD}All {total} checks passed. ✔{RESET}")
        print(f"\n  {BOLD}Paste these into GitHub → Settings → Secrets → Actions:{RESET}")
        print(f"    CLOUDFLARE_ACCOUNT_ID = {account_id}")
        print(f"    CLOUDFLARE_API_TOKEN  = (the token you entered)\n")
    else:
        print(f"\n  {RED}{BOLD}{passed}/{total} checks passed.{RESET}")
        print(f"  Fix the failed checks above before adding secrets to GitHub.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()