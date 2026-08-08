#!/usr/bin/env python3
# ==============================================================================
#  repair_drivedex_index.py  --  One-off repair for a corrupted/stale
#                                 drivedex_index.json
#
#  What this is for
#  ----------------------------------------------------------------------
#  drivedex_index.json is only ever a *derived summary* of the real data --
#  the actual driver entries live in the per-category manifest files under
#  manifests/ (Audio.manifest.json, Display.manifest.json, etc.), which
#  this script never touches or overwrites. If drivedex_index.json itself
#  ends up corrupted (e.g. a GitHub rate-limit response got written in
#  place of real JSON) or just falls out of sync, none of your driver data
#  is lost -- it just needs to be regenerated from those real files.
#
#  This script does exactly that, and nothing else:
#    1. Lists manifests/ on GitHub directly (ground truth -- does NOT rely
#       on the possibly-broken index.json, and does NOT rely on any
#       hardcoded category list, so it can't miss a category).
#    2. Downloads every category manifest shard it finds via the
#       authenticated Git Data (blobs) API, not the raw CDN that produced
#       the original 429 -- same higher-rate-limit authenticated surface
#       as the Contents API, but (unlike Contents' single-file endpoint)
#       not capped at returning content for files <=1MB. Several real
#       shards here (Audio, Chipset, Display, Input, ...) are multiple MB,
#       so fetching them by path through the Contents API silently comes
#       back with an empty "content" field once a file crosses that 1MB
#       line -- decoded to nothing, which is exactly what a bare
#       json.loads("") reports as "Expecting value: line 1 column 1
#       (char 0)". The blobs endpoint (fetched by sha, which the step-1
#       directory listing already gives us for free) has no such limit
#       up to 100MB, comfortably above MANIFEST_SIZE_LIMIT.
#    3. Rebuilds manifest_shards + category_summary from that real data,
#       exactly the way DriverDex Builder's own _save_index() does.
#    4. Pushes the corrected drivedex_index.json back to GitHub.
#
#  If ANY step fails (listing fails, a shard fails to download, the file
#  can't be parsed as a real manifest) the script aborts and pushes
#  NOTHING -- a partial/guessed index is exactly the kind of silent data
#  loss this exists to prevent. Safe to re-run any time.
#
#  Usage
#  ----------------------------------------------------------------------
#    python3 repair_drivedex_index.py            # rebuild and push
#    python3 repair_drivedex_index.py --dry-run  # show what would happen, don't push
#
#  Token: reads drivedex_config.json (key "github_token") next to this
#  script if present, otherwise the GITHUB_TOKEN environment variable --
#  same convention DriverDex Builder itself uses.
# ==============================================================================

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Must match driverdex-reset.py exactly ────────────────────────────────────
REPO_OWNER          = "driverdex"
REPO_NAME           = "driverdex"
GH_BRANCH           = "main"
MANIFEST_DIR        = "manifests"
INDEX_FILE_NAME     = "drivedex_index.json"
SCHEMA_VER          = "3.0"
MANIFEST_SIZE_LIMIT = 15 * 1024 * 1024

API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

_BASE_RE  = re.compile(r'^([A-Za-z0-9]+)\.manifest\.json$')
_SHARD_RE = re.compile(r'^([A-Za-z0-9]+)\.manifest\.(\d+)\.json$')


# ── Token loading (same convention as driverdex-reset.py) ───────────────────
def _load_token() -> str:
    cfg = Path(__file__).resolve().parent / "drivedex_config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            tok = str(data.get("github_token", "")).strip()
            if tok:
                return tok
        except Exception:
            pass
    return os.environ.get("GITHUB_TOKEN", "").strip()


# ── Minimal authenticated GitHub API caller with 429/5xx retry ──────────────
def _api(method: str, path: str, token: str, data: Optional[Dict] = None,
         timeout: int = 60) -> Tuple[int, object]:
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return exc.code, {"message": raw.decode("utf-8", "replace")[:300]}
    except Exception as exc:
        return 0, {"message": str(exc)}


def _api_with_retry(method: str, path: str, token: str, data: Optional[Dict] = None,
                     max_attempts: int = 5, label: str = "") -> Tuple[int, object]:
    tag = f"[{label}] " if label else ""
    for attempt in range(1, max_attempts + 1):
        st, resp = _api(method, path, token, data=data)
        retryable = st in (429, 500, 502, 503, 504) or st == 0
        if not retryable or attempt == max_attempts:
            return st, resp
        wait = min(4.0 * (2 ** (attempt - 1)), 60.0)
        if isinstance(resp, dict) and resp.get("retry_after"):
            try:
                wait = float(resp["retry_after"]) + 1.0
            except (TypeError, ValueError):
                pass
        print(f"  {tag}HTTP {st} on attempt {attempt}/{max_attempts} -- retrying in {wait:.0f}s ...")
        time.sleep(wait)
    return st, resp  # pragma: no cover


# ── Step 1: ground-truth directory listing ───────────────────────────────────
def list_manifest_files(token: str) -> Optional[List[Dict]]:
    st, resp = _api_with_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{MANIFEST_DIR}?ref={GH_BRANCH}",
        token, label="list manifests/",
    )
    if st == 404:
        return []
    if st != 200 or not isinstance(resp, list):
        msg = resp.get("message", "") if isinstance(resp, dict) else ""
        print(f"ERROR: could not list {MANIFEST_DIR}/ on GitHub (HTTP {st}) {msg}")
        return None
    return [item for item in resp if isinstance(item, dict) and item.get("type") == "file"]


def group_into_categories(files: List[Dict]) -> Dict[str, List[Dict]]:
    """{category_stem: [shard_file_dict, ...]} in shard order (1, 2, 3, ...),
    excluding the installer manifest chain entirely (that's a separate,
    non-category file and was never part of index.json's manifest_shards)."""
    by_stem: Dict[str, Dict[int, Dict]] = {}
    for item in files:
        name = item["name"]
        if name == "installers.manifest.json" or name.startswith("installers.manifest."):
            continue
        m = _BASE_RE.match(name)
        if m:
            by_stem.setdefault(m.group(1), {})[1] = item
            continue
        m = _SHARD_RE.match(name)
        if m:
            idx = int(m.group(2))
            if idx >= 2:
                by_stem.setdefault(m.group(1), {})[idx] = item

    ordered: Dict[str, List[Dict]] = {}
    for stem, shards_by_idx in by_stem.items():
        if 1 not in shards_by_idx:
            print(f"WARNING: {stem} has numbered shard(s) but no base "
                  f"{stem}.manifest.json -- skipping (can't safely order it).")
            continue
        ordered[stem] = [shards_by_idx[i] for i in sorted(shards_by_idx)]
    return ordered


# ── Step 2: download + parse every shard via the Git Data (blobs) API,
#    fetched by sha (which the directory listing in step 1 already gives
#    us) rather than by path through the Contents "get single file"
#    endpoint. That endpoint only returns actual base64 content for files
#    <=1MB -- above that it comes back HTTP 200 with "content": "" and
#    "encoding": "none", which decodes to an empty byte string and makes
#    json.loads() fail with the misleading "Expecting value: line 1
#    column 1 (char 0)". The blobs endpoint has no such cap up to 100MB,
#    still lives under api.github.com (not the raw CDN whose rate limit
#    corrupted the index originally), and needs no separate lookup call
#    since every item from the directory listing already carries its sha.
def fetch_blob_content(sha: str, token: str, label: str = "") -> Optional[bytes]:
    st, resp = _api_with_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs/{sha}",
        token, label=label,
    )
    if st != 200 or not isinstance(resp, dict) or "content" not in resp:
        msg = resp.get("message", "") if isinstance(resp, dict) else ""
        print(f"ERROR: could not fetch blob {sha}{f' ({label})' if label else ''} "
              f"(HTTP {st}) {msg}")
        return None
    try:
        # GitHub wraps blob base64 content with embedded newlines every 60
        # chars; b64decode's default validate=False discards those (and any
        # other non-alphabet characters) before decoding, so no stripping
        # is needed here.
        return base64.b64decode(resp["content"])
    except Exception as exc:
        print(f"ERROR: could not decode blob {sha}: {exc}")
        return None


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    token = _load_token()
    if not token:
        print("ERROR: no GitHub token found (drivedex_config.json or GITHUB_TOKEN env var).")
        return 1

    print(f"Listing {MANIFEST_DIR}/ on {REPO_OWNER}/{REPO_NAME}@{GH_BRANCH} ...")
    files = list_manifest_files(token)
    if files is None:
        print("Aborting -- refusing to rebuild the index from an incomplete listing.")
        return 1
    if not files:
        print(f"No files found under {MANIFEST_DIR}/ -- nothing to rebuild from.")
        return 1

    categories = group_into_categories(files)
    if not categories:
        print("No category manifests recognized -- nothing to rebuild from.")
        return 1

    print(f"Found {len(categories)} categor{'y' if len(categories)==1 else 'ies'}: "
          f"{', '.join(sorted(categories))}")

    manifest_shards: List[Dict] = []
    category_summary: Counter = Counter()

    for stem in sorted(categories):
        shard_items = categories[stem]
        n = len(shard_items)
        for i, item in enumerate(shard_items):
            rel_path = f"{MANIFEST_DIR}/{item['name']}"
            size = item.get("size", 0)
            print(f"  Downloading {rel_path} ({size:,} bytes) ...")
            raw = fetch_blob_content(item["sha"], token, label=f"get {rel_path}")
            if raw is None:
                print("Aborting -- refusing to write a partial index because "
                      f"{rel_path} could not be downloaded.")
                return 1
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                print(f"Aborting -- {rel_path} is not valid JSON ({exc}); "
                      "refusing to guess its contents.")
                return 1

            sealed = (size >= MANIFEST_SIZE_LIMIT) or (i < n - 1)
            manifest_shards.append({
                "filename"  : rel_path,
                "size_bytes": size,
                "active"    : not sealed,
                "note"      : "overflow -> next shard" if sealed else "active",
            })

            # Per-shard visibility: the final "Rebuilt category summary" only
            # ever shows the grand total across every shard of every category,
            # so a shard that's downloaded fine but silently contributes zero
            # (empty/renamed "drivers" key, everything in it disabled, or --
            # the real bug this would catch -- entries landing under some
            # OTHER category than this shard's own stem) is invisible until
            # now. Printed for every shard, not just suspicious ones, so a
            # quiet "0 enabled" is exactly as visible as a healthy count.
            shard_drivers = parsed.get("drivers", [])
            shard_enabled = 0
            shard_cat_hits: Counter = Counter()
            for e in shard_drivers:
                if e.get("enabled", True):
                    cat = e.get("type") or e.get("category_type") or "Other"
                    category_summary[cat] += 1
                    shard_enabled += 1
                    shard_cat_hits[cat] += 1
            off_stem = {c: n for c, n in shard_cat_hits.items() if c != stem}
            line = f"    -> {len(shard_drivers):,} entries, {shard_enabled:,} enabled"
            if off_stem:
                line += ("  [WARNING: enabled under a DIFFERENT category than "
                         f"{stem} -- {', '.join(f'{c}={n:,}' for c, n in sorted(off_stem.items()))}]")
            print(line)

    idx_data = {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : manifest_shards,
        "category_summary": dict(sorted(category_summary.items())),
    }

    print()
    print("Rebuilt category summary:")
    total = 0
    for cat, count in sorted(category_summary.items()):
        print(f"  {cat:<20} {count}")
        total += count
    print(f"  {'TOTAL':<20} {total}")
    print()

    if dry_run:
        print("--dry-run: not pushing. Rebuilt index.json content would be:")
        print(json.dumps(idx_data, indent=2, ensure_ascii=False))
        return 0

    print(f"Pushing corrected {INDEX_FILE_NAME} to GitHub ...")
    st, resp = _api_with_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{INDEX_FILE_NAME}?ref={GH_BRANCH}",
        token, label="get current sha",
    )
    current_sha = resp.get("sha") if st == 200 and isinstance(resp, dict) else None

    body_b64 = base64.b64encode(
        json.dumps(idx_data, indent=2, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    payload: Dict = {
        "message": "chore: repair drivedex_index.json from real manifest data",
        "content": body_b64,
        "branch" : GH_BRANCH,
    }
    if current_sha:
        payload["sha"] = current_sha

    st, resp = _api_with_retry(
        "PUT", f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{INDEX_FILE_NAME}",
        token, data=payload, label=f"push {INDEX_FILE_NAME}",
    )
    if st not in (200, 201):
        msg = resp.get("message", "") if isinstance(resp, dict) else ""
        print(f"ERROR: push failed (HTTP {st}) {msg}")
        return 1

    print(f"Done. {INDEX_FILE_NAME} repaired and pushed:")
    print(f"  https://github.com/{REPO_OWNER}/{REPO_NAME}/blob/{GH_BRANCH}/{INDEX_FILE_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
