#!/usr/bin/env python3
# ==============================================================================
#  restore_installers_manifest.py  --  one-time repair utility
#
#  Incident:
#    manifests/installers.manifest.json on rhshourav/driverdex@main was
#    overwritten on 2026-07-09 09:19:45 +06 by commit e5514d68655698bbb09
#    134fc4c239065bb9f1691 ("Add files via upload" -- GitHub's own default
#    message for the website's drag-and-drop upload button, not a message
#    this tool ever generates itself). The content that landed there is a
#    literal GitHub rate-limit error page ("429: Too Many Requests ..."),
#    not real manifest data -- almost certainly a locally-cached copy of a
#    failed fetch that got manually re-uploaded via the GitHub web UI.
#
#    The commit immediately before it, e953e1466e841b05bf378ec2a46f108a
#    600a1bb5 (2026-07-09 07:39:07 +06), still holds the real file: valid
#    JSON, schema 3.0, 4,432 installer entries.
#
#  What this script does:
#    1. Fetches that known-good historical version by commit sha (does not
#       depend on the corrupted tip at all).
#    2. Re-validates it really is well-formed JSON with an "installers" list.
#    3. Reads the CURRENT file on main (sha + content) and refuses to touch
#       anything unless the current content still matches the exact known
#       corrupted text -- so this can never clobber a fix someone else
#       already made, or an unrelated legitimate update.
#    4. Shows a summary and asks for an explicit y/N before writing anything.
#    5. PUTs the restored content back to manifests/installers.manifest.json
#       on main via the Contents API, using the current file's sha so the
#       update is a proper, conflict-safe replace (not a blind overwrite).
#
#  Requires: GITHUB_TOKEN env var with 'repo' push access (same token your
#  driverdex-reset.py already uses).
# ==============================================================================

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO_OWNER  = "rhshourav"
REPO_NAME   = "driverdex"
BRANCH      = "main"
FILE_PATH   = "manifests/installers.manifest.json"
GOOD_SHA    = "e953e1466e841b05bf378ec2a46f108a600a1bb5"
BAD_COMMIT  = "e5514d68655698bbb09134fc4c239065bb9f1691"

_CORRUPTED_SIGNATURE = "429: Too Many Requests"


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivedex_config.json")
    if os.path.exists(cfg):
        try:
            return json.loads(open(cfg, encoding="utf-8").read()).get("github_token", "").strip()
        except Exception:
            pass
    return ""


def _api(method: str, url: str, token: str, data: bytes = None, accept: str = "application/vnd.github+json"):
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "driverdex-restore-script",
        "Authorization": f"Bearer {token}",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    token = _token()
    if not token:
        print("ERROR: No GitHub token found. Set GITHUB_TOKEN env var first.")
        return 1

    print(f"Repo   : {REPO_OWNER}/{REPO_NAME}@{BRANCH}")
    print(f"File   : {FILE_PATH}")
    print(f"Restoring content from known-good commit: {GOOD_SHA}")
    print()

    # ---- Step 1: fetch the known-good historical content -------------------
    good_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{GOOD_SHA}/{FILE_PATH}"
    st, body = _api("GET", good_url, token, accept="*/*")
    if st != 200:
        print(f"ERROR: Could not fetch known-good version (HTTP {st}). Aborting -- nothing changed.")
        return 1
    try:
        good_data = json.loads(body.decode("utf-8"))
        installer_count = len(good_data.get("installers", []))
        assert installer_count > 0
    except Exception as exc:
        print(f"ERROR: Known-good content did not parse as expected ({exc}). Aborting -- nothing changed.")
        return 1
    print(f"✓ Known-good version fetched and validated: {installer_count} installer(s), "
          f"schema {good_data.get('schema')}, updated {good_data.get('updated')}.")

    # ---- Step 2: read the CURRENT live file + its sha -----------------------
    contents_url = (
        f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}?ref={BRANCH}"
    )
    st, body = _api("GET", contents_url, token)
    if st != 200:
        print(f"ERROR: Could not read current file metadata (HTTP {st}): {body[:300]}")
        return 1
    meta = json.loads(body.decode("utf-8"))
    current_sha = meta["sha"]

    # Fetch the current raw content (not just base64 from meta, in case
    # GitHub omitted it for a large file) to confirm it's really the
    # corrupted text before touching anything.
    st, current_body = _api(
        "GET",
        f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{FILE_PATH}",
        token, accept="*/*",
    )
    current_text_preview = current_body[:200].decode("utf-8", errors="replace")
    if _CORRUPTED_SIGNATURE not in current_text_preview:
        print("SAFETY STOP: the file currently on GitHub does NOT match the known corrupted")
        print("text this script expects to replace. Someone may have already fixed it, or")
        print("something else has changed. Refusing to overwrite -- nothing changed.")
        print(f"Current content preview: {current_text_preview!r}")
        return 1

    print(f"✓ Confirmed current file (sha {current_sha[:10]}…) is still the corrupted version.")
    print()
    print("About to REPLACE the current (corrupted) file on GitHub with the restored,")
    print(f"known-good version ({installer_count} installer(s)). This pushes a real commit")
    print(f"to {REPO_OWNER}/{REPO_NAME}@{BRANCH}.")
    answer = input("Proceed? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted by user. Nothing changed.")
        return 0

    # ---- Step 3: PUT the restored content back ------------------------------
    restored_bytes = json.dumps(good_data, indent=2, ensure_ascii=False).encode("utf-8")
    put_payload = {
        "message": (
            f"Restore {FILE_PATH} — fix corruption introduced by commit "
            f"{BAD_COMMIT[:10]} ('Add files via upload'); restored from "
            f"{GOOD_SHA[:10]}"
        ),
        "content": base64.b64encode(restored_bytes).decode("ascii"),
        "sha": current_sha,
        "branch": BRANCH,
    }

    st, resp_body = _api(
        "PUT", contents_url.split("?")[0], token,
        data=json.dumps(put_payload).encode("utf-8"),
    )
    if st not in (200, 201):
        print(f"ERROR: Restore commit failed (HTTP {st}): {resp_body[:500]}")
        return 1

    resp = json.loads(resp_body.decode("utf-8"))
    new_sha = resp.get("commit", {}).get("sha", "?")
    print(f"✓ Restored successfully. New commit: {new_sha}")
    print(f"  https://github.com/{REPO_OWNER}/{REPO_NAME}/commit/{new_sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
