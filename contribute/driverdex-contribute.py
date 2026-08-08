#!/usr/bin/env python3
# ==============================================================================
#  DriverDex Contribute  --  automatic single-PC driver contribution tool
#  Author  : rhshourav
#  Repo    : https://github.com/rhshourav/driverdex
#  Part of : Windows-Scripts  --  github.com/rhshourav/Windows-Scripts
#
#  For general/end users: run this file with no arguments (double-click, or
#  `python driverdex-contribute.py`) and it does one thing automatically,
#  with zero prompts and zero configuration:
#
#    1. Exports every third-party driver installed on THIS PC (DISM + pnputil).
#    2. Checks each one against DriverDex by Hardware ID.
#    3. Classifies it PRESENT / UPDATED / MISSING.
#    4. Pushes only the MISSING and locally-newer UPDATED drivers to GitHub.
#
#  Authentication is entirely self-contained: the GitHub token is decrypted
#  in memory from an encrypted key file fetched from GitHub (see
#  _dd_bootstrap_token() below) using a salt + passphrase compiled into this
#  build. There is no personal token to create, no environment variable, and
#  no config file to edit — that's deliberate, this build is meant for
#  people who've never touched a GitHub token and shouldn't need to.
#
#  This is a standalone, self-contained file — it has no runtime dependency
#  on any other script and can be built into its own single .exe. It is the
#  general-user counterpart to driverdex-reset.py (the "Bulk Builder"), a
#  separate tool for maintainers who process many pre-organized driver-pack
#  folders using their own personal GitHub token. The two tools intentionally
#  share no code at runtime, though this file's upload/manifest/push
#  machinery mirrors the Bulk Builder's more advanced dual-repo /
#  quota-fallback design rather than reinventing it independently.
#
#  Before shipping a real build: EMBED_SALT_B64 / EMBED_PASSPHRASE /
#  EMBED_SCRYPT_N below are placeholders. Generate real values with
#  tools/make_token_blob.py and rebuild.
# ==============================================================================

from __future__ import annotations

import base64
import contextlib
import copy
import math
import os
import sys
import re
import json
import shutil
import zipfile
import hashlib
import subprocess
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import concurrent.futures
import getpass
import platform
import socket
from collections  import Counter, defaultdict
from pathlib      import Path
from datetime     import date, datetime
from typing       import Callable, Dict, List, Optional, Set, Tuple

# ── auto-install required packages ───────────────────────────────────────────
def _ensure(pkg: str, import_name: str = "") -> None:
    name = import_name or pkg
    try:
        __import__(name)
    except ImportError:
        print(f"  Installing '{pkg}' ...", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "-q"],
            check=True,
        )

_ensure("rich")
_ensure("py7zr")
_ensure("multivolumefile")

from rich.console  import Console, Group as RichGroup
from rich.panel    import Panel
from rich.table    import Table
from rich.live     import Live
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    TimeRemainingColumn, TaskProgressColumn, DownloadColumn,
    TransferSpeedColumn, MofNCompleteColumn, ProgressColumn,
)
from rich.text     import Text
from rich.rule     import Rule
from rich.align    import Align
from rich.markup   import escape


class DimMofNColumn(ProgressColumn):
    """Drop-in replacement for rich's MofNCompleteColumn with a dim separator.

    Stock MofNCompleteColumn.render() builds its output with plain
    Text(f"...{self.separator}...") -- and Text() takes its argument as
    literal characters, it does NOT parse console markup (that only happens
    via Console.print()/Text.from_markup()). So passing
    separator="[dim white]/[/dim white]" never dims anything; it just prints
    those literal tag characters around the slash, e.g. "15[dim white]/[/dim
    white]15". This subclass applies the dim style properly by appending it
    as a real Text style span instead of unparsed markup text.
    """

    def __init__(self, separator: str = "/", table_column: Optional["Column"] = None):
        self.separator = separator
        super().__init__(table_column=table_column)

    def render(self, task: "Task") -> Text:
        completed = int(task.completed)
        total = int(task.total) if task.total is not None else "?"
        total_width = len(str(total))
        text = Text(style="progress.download")
        text.append(f"{completed:{total_width}d}")
        text.append(self.separator, style="dim white")
        text.append(f"{total}")
        return text


# ── PyInstaller-safe base directory ──────────────────────────────────────────
def _get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

_APP_DIR = _get_app_dir()

# ── constants ─────────────────────────────────────────────────────────────────
# Manifests (+ README / index) live in the "driverdex" repo under manifests/.
REPO_OWNER          = "rhshourav"
REPO_NAME           = "driverdex"

# Driver archives now live in a SEPARATE dedicated repo, at its ROOT — i.e.
# github.com/rhshourav/drivers/<Type>/DP_<Pack>/…  (NOT under a nested
# "drivers/" sub-folder, and NOT inside the driverdex repo anymore).
DRIVERS_REPO_OWNER  = "rhshourav"
DRIVERS_REPO_NAME   = "drivers"

GH_BRANCH           = "main"
MANIFEST_DIR        = "manifests"                                   # all manifests live here
INSTALLER_MANIFEST_REL = f"{MANIFEST_DIR}/installers.manifest.json"
DRIVERS_DIR         = "drivers"       # local workspace sub-folder for staged archives
SPLIT_BYTES         = 15 * 1024 * 1024
SCHEMA_VER          = "3.0"
APP_VER             = "6.1.1"
# GitHub's API docs require a User-Agent on every request, and a descriptive
# one (vs. urllib's silent default of "Python-urllib/3.x") is also just less
# likely to get auto-flagged as a generic scraping bot by GitHub's own edge
# or by a network-level content filter sitting in front of it — both of
# which have been observed serving a "looks like scraping" block page for
# bare-urllib traffic hitting raw.githubusercontent.com in bulk.
HTTP_USER_AGENT     = f"DriverDexBuilder/{APP_VER} (+https://github.com/rhshourav/driverdex)"
COMMIT_EMAIL        = "driverdex-builder@noreply.local"
COMMIT_NAME         = "DriverDex-Builder"
LFS_BATCH_URL = (
    f"https://github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}.git/info/lfs/objects/batch"
)

WORKSPACE_DIR       = _APP_DIR / "drivedex_workspace"

# Top-level workspace sub-folders that are local scratch space ONLY and must
# never be swept into a commit by github_commit_push()'s workspace walk:
#   - "extracted_drivers" is the raw pnputil/DISM export used purely for
#     local HWID scanning/comparison (extract_local_drivers()) -- it holds
#     the untouched driver payloads straight off the user's PC (installers,
#     .dll/.sys companions, etc.), often tens of MB each, and is NEVER meant
#     to be pushed anywhere. The zip/split archives built from it (via
#     zip_all_drivers()) land under WORKSPACE_DIR/DRIVERS_DIR instead, which
#     IS meant to be committed.
#   - "_staging_DP_" is a name *prefix*, not a fixed dir -- _dd_upload_candidates()
#     stages each pack's in-progress archives at
#     f"{DRIVERS_DIR}/_staging_DP_{pack_name}" and removes it in a `finally`
#     block once done. It's excluded here too as a defensive backstop in
#     case that cleanup is ever interrupted (e.g. a hard crash) and a stale
#     staging folder is left behind on the next run.
WORKSPACE_SCRATCH_DIRS         = {"extracted_drivers"}
WORKSPACE_SCRATCH_NAME_PREFIXES = ("_staging_DP_",)

# Absolute ceiling a manifest shard may ever reach on disk.
MANIFEST_SIZE_LIMIT = 15 * 1024 * 1024
# Conservative split trigger: once a shard would exceed this we roll the
# overflow into a fresh part (…manifest.2.json, …3, …) so no shard ever gets
# close to the 15 MB ceiling and no driver/installer entry is ever dropped.
MANIFEST_SPLIT_THRESHOLD = 13 * 1024 * 1024
INDEX_FILE_NAME     = "drivedex_index.json"

BADGE_MARKER_START  = "<!-- DRIVERDEX_DRIVER_BADGE_START -->"
BADGE_MARKER_END    = "<!-- DRIVERDEX_DRIVER_BADGE_END -->"


# Raw base for driver archives — points at the ROOT of the drivers repo.
DRIVERS_RAW_BASE = (
    f"https://raw.githubusercontent.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}/{GH_BRANCH}"
)
# base_url advertised inside each manifest so consumers know where archives live.
BASE_RAW_URL = DRIVERS_RAW_BASE


def _driver_raw_url(repo_rel: str) -> str:
    """
    Build the public raw URL for a driver/installer archive that lives in the
    dedicated drivers repo. Accepts a workspace-relative path (which may still
    carry the leading "drivers/" staging prefix) and strips that prefix so the
    URL resolves to the archive at the drivers-repo ROOT.
    """
    rel = repo_rel.replace("\\", "/").lstrip("/")
    prefix = f"{DRIVERS_DIR}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    return f"{DRIVERS_RAW_BASE}/{rel}"


# ── Drivers-repo automatic quota fallback ─────────────────────────────────────
# When the active drivers repo (DRIVERS_REPO_NAME) hits GitHub's repository
# size quota, the tool automatically switches to the next repo below (same
# owner) and retries — no manual re-run required. Repos are named by index
# off DRIVERS_REPO_BASE_NAME: index 0 is the base name itself ("drivers"),
# index N>=1 is "drivers_N". This is computed on the fly (see
# _drivers_repo_name_for_index / _drivers_repo_index_for_name) instead of a
# hardcoded list, so growing past drivers_1, drivers_2, ... never requires
# a source edit — the next number in sequence is always just "current + 1".
# Once the next repo in sequence doesn't exist yet, the tool falls back to
# asking the user (via Telegram) to create it — see
# _notify_telegram_new_repo_needed() further down.
DRIVERS_REPO_BASE_NAME = "drivers"


def _drivers_repo_name_for_index(idx: int) -> str:
    """index 0 -> the base repo name ('drivers'); index N>=1 -> 'drivers_N'."""
    return DRIVERS_REPO_BASE_NAME if idx == 0 else f"{DRIVERS_REPO_BASE_NAME}_{idx}"


def _drivers_repo_index_for_name(name: str) -> Optional[int]:
    """
    Inverse of _drivers_repo_name_for_index(). Returns None if `name` doesn't
    match the "<base>" / "<base>_<N>" pattern at all (e.g. a repo that
    predates this naming convention, or was renamed to something custom) —
    callers treat that the same as "not part of the sequence yet, start
    probing from the top".
    """
    if name == DRIVERS_REPO_BASE_NAME:
        return 0
    m = re.fullmatch(rf"{re.escape(DRIVERS_REPO_BASE_NAME)}_(\d+)", name)
    return int(m.group(1)) if m else None

# Persists which repo in the sequence is currently active, so a later run
# picks up where the last one left off instead of re-discovering the same
# quota error against a repo we already know is full. Also persists the
# full set of repos already found to be over quota ("spent") — this is the
# actual efficiency win: any index in this set is skipped with zero GitHub
# API calls and zero doomed push attempts, on this run or any future one,
# instead of being rediscovered the expensive way (partial blob upload,
# then a 403 partway through).
#
# The source of truth lives on GitHub now — committed to REPO_OWNER/REPO_NAME
# at the root, right alongside README.md/index.json — NOT just on the local
# disk next to the script. That's what makes the fallback chain efficient
# across *machines*, not just across runs on one machine: two different
# clones of this tool (or the same one, wiped and re-run later) converge on
# the same active repo and the same spent-set without either one ever having
# to rediscover a 403 the expensive way. The local file is kept too, purely
# as a fast/offline cache — every load prefers GitHub and every save updates
# both, so a run with no network yet (e.g. before the token is even checked)
# still has the last-known-good answer instead of none at all.
_REPO_STATE_FILE        = _APP_DIR / "driverdex_repo_state.json"   # local cache / offline fallback
_REPO_STATE_REMOTE_PATH = "driverdex_repo_state.json"              # root of REPO_OWNER/REPO_NAME — the real source of truth
_SPENT_DRIVERS_REPOS: Set[str] = set()
_repo_state_remote_sha: Optional[str] = None   # blob sha of the last-seen remote copy;
                                                # Contents API needs this to update rather
                                                # than blindly overwrite/create the file
_repo_state_etag: Optional[str] = None         # ETag of the last-seen remote copy; sent as
                                                # If-None-Match so an unchanged file comes back
                                                # as a bodyless 304 instead of the full blob


def _recompute_drivers_repo_constants() -> None:
    """
    Recompute every constant derived from DRIVERS_REPO_OWNER/DRIVERS_REPO_NAME.
    Must be called any time those two globals change (e.g. after an automatic
    repo-quota fallback switch) so LFS_BATCH_URL / DRIVERS_RAW_BASE /
    BASE_RAW_URL stay in sync with the newly-active repo for the rest of the
    run — every later call site reads these as plain module globals, so as
    long as they're refreshed here, the switch is picked up everywhere
    automatically.
    """
    global LFS_BATCH_URL, DRIVERS_RAW_BASE, BASE_RAW_URL
    LFS_BATCH_URL = (
        f"https://github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}.git/info/lfs/objects/batch"
    )
    DRIVERS_RAW_BASE = (
        f"https://raw.githubusercontent.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}/{GH_BRANCH}"
    )
    BASE_RAW_URL = DRIVERS_RAW_BASE


def _apply_repo_state(data: Dict) -> None:
    """
    Adopt a parsed repo-state dict — from GitHub or from the local cache —
    into the live globals. Both callers funnel through here so the two
    sources are interpreted identically.
    """
    global DRIVERS_REPO_NAME, _SPENT_DRIVERS_REPOS
    active = (data.get("active_drivers_repo") or "").strip()
    if active and active != DRIVERS_REPO_NAME:
        DRIVERS_REPO_NAME = active
        _recompute_drivers_repo_constants()
    _SPENT_DRIVERS_REPOS = set(data.get("spent_drivers_repos") or [])


def _load_repo_state() -> None:
    """
    Restore the last-known-active drivers repo AND the full spent-repos set.
    GitHub (REPO_OWNER/REPO_NAME @ _REPO_STATE_REMOTE_PATH) is checked first
    — it's the source of truth, so every run on every machine converges on
    the same picture. Falls back to the local on-disk cache when GitHub
    can't answer right now (no token verified yet, offline, rate-limited,
    or the remote file simply doesn't exist yet on a brand-new setup). Safe
    to call multiple times — a total no-op if neither source has anything.

    Sends the ETag from the *previous* successful read (persisted in the
    local cache, so this survives across process restarts / machines using
    a shared cache) as If-None-Match. If nothing changed remotely, GitHub
    answers with a bodyless 304 instead of re-sending the full file — and
    per GitHub's docs, a 304 doesn't count against the rate limit either.
    A local cache miss just means no conditional header is sent, which is
    exactly the old unconditional-GET behavior — never worse than before.
    """
    global _repo_state_remote_sha, _repo_state_etag

    cached_local: Optional[Dict] = None
    try:
        if _REPO_STATE_FILE.exists():
            cached_local = json.loads(_REPO_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cached_local = None
    cached_etag = (cached_local or {}).get("_cache_etag")

    remote_data: Optional[Dict] = None
    try:
        st, resp = _api_with_retry(
            "GET",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{_REPO_STATE_REMOTE_PATH}?ref={GH_BRANCH}",
            max_attempts=2, backoff_base=2.0, label="repo-state GET",
            extra_headers={"If-None-Match": cached_etag} if cached_etag else None,
        )
        if st == 304:
            # Unchanged since our last read — adopt the local cache (which
            # is, by definition, what that ETag was issued for) and skip
            # the GitHub round-trip's body entirely.
            _repo_state_etag = cached_etag
            _repo_state_remote_sha = (cached_local or {}).get("_cache_sha") or _repo_state_remote_sha
            if cached_local is not None:
                _apply_repo_state(cached_local)
            return
        if st == 200 and isinstance(resp, dict) and "content" in resp:
            raw = base64.b64decode(resp["content"]).decode("utf-8")
            remote_data = json.loads(raw)
            _repo_state_remote_sha = resp.get("sha")
            _repo_state_etag = resp.get("_etag")
        elif st == 404:
            _repo_state_remote_sha = None  # nothing pushed yet — first run ever, anywhere
            _repo_state_etag = None
    except Exception:
        pass  # network down / token not ready yet — local cache below covers us

    if remote_data is not None:
        try:
            _apply_repo_state(remote_data)
            # Mirror to local disk so a later offline run still has this —
            # plus the sha/etag (under reserved "_cache_*" keys, stripped
            # before anything is ever pushed back to GitHub) so the *next*
            # process, on this machine or another sharing the cache, can
            # send If-None-Match on its very first GET.
            cache_payload = dict(remote_data)
            cache_payload["_cache_sha"]  = _repo_state_remote_sha
            cache_payload["_cache_etag"] = _repo_state_etag
            _REPO_STATE_FILE.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
        except Exception:
            pass
        return

    # GitHub unreachable or empty this time — fall back to the local cache.
    if cached_local is not None:
        try:
            _apply_repo_state(cached_local)
        except Exception:
            pass  # corrupted/unreadable local cache — fall back to the compiled-in default


def _save_repo_state() -> None:
    """
    Persist the currently-active drivers repo and the spent-repos set — to
    the local disk cache immediately (cheap, always succeeds, and is what
    protects _mark_repo_spent() even if the process dies right after), then
    to GitHub, which is what other runs/machines actually read from. A
    failed GitHub push only logs a warning: the local cache already has the
    update, and the very next save (or the Telegram-notify retry loop)
    reconciles it — nothing is lost, this run just keeps going.
    """
    data: Dict = {}
    if _REPO_STATE_FILE.exists():
        try:
            data = json.loads(_REPO_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["active_drivers_repo"] = DRIVERS_REPO_NAME
    data["spent_drivers_repos"] = sorted(_SPENT_DRIVERS_REPOS)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")

    try:
        _REPO_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        warn(f"Could not save repo state to {_REPO_STATE_FILE.name}: {exc}")

    _push_repo_state_to_github(data)


def _push_repo_state_to_github(data: Dict) -> None:
    """
    Commit `data` to REPO_OWNER/REPO_NAME @ _REPO_STATE_REMOTE_PATH via the
    single-file Contents API (create-or-update in one call) — deliberately
    NOT the multi-blob/tree/commit Git Data API used elsewhere in this file
    for driver archives; that machinery exists for bulk/large-file commits
    and would be pure overhead for this tiny, rarely-changing file.

    On a stale-sha conflict (409/422 — another machine/run pushed a state
    update in the gap between our last read and this write) this re-fetches
    the current remote copy and MERGES rather than blindly overwriting:
    spent_drivers_repos becomes the union of ours and theirs, so the set of
    known-full repos only ever grows, regardless of which machine's push
    lands last. (active_drivers_repo keeps our own value — this process is
    actively using it right now, and there's no timestamp to arbitrate
    which machine's choice is "newer".) Retries once on conflict, then
    falls back to "local cache has it, next save reconciles."
    """
    global _repo_state_remote_sha, _repo_state_etag, _SPENT_DRIVERS_REPOS

    for attempt in (1, 2):
        # Local-only bookkeeping (_cache_sha / _cache_etag, used to seed
        # If-None-Match on the next _load_repo_state()) must never leak
        # into the committed file content — strip anything underscore-
        # prefixed right before encoding, every attempt (post-merge too).
        pushable = {k: v for k, v in data.items() if not k.startswith("_cache_")}
        body_b64 = base64.b64encode(json.dumps(pushable, indent=2).encode("utf-8")).decode("ascii")

        payload: Dict = {
            "message": f"chore: update drivers-repo state ({DRIVERS_REPO_NAME})",
            "content": body_b64,
            "branch": GH_BRANCH,
        }
        if _repo_state_remote_sha:
            payload["sha"] = _repo_state_remote_sha

        st, resp = _api_with_retry(
            "PUT",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{_REPO_STATE_REMOTE_PATH}",
            data=payload, max_attempts=3, backoff_base=3.0, label="repo-state PUT",
        )
        if st in (200, 201):
            _repo_state_remote_sha = (resp.get("content") or {}).get("sha")
            # The sha just changed (this push authored it), so the ETag we
            # had cached is now stale by definition — drop it rather than
            # risk a future If-None-Match matching against the old content.
            _repo_state_etag = None
            return

        if st in (409, 422) and attempt == 1:
            # Someone else updated it since we last read the sha — refetch
            # and merge instead of stomping their write.
            st2, resp2 = _api_with_retry(
                "GET",
                f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{_REPO_STATE_REMOTE_PATH}?ref={GH_BRANCH}",
                max_attempts=2, backoff_base=2.0, label="repo-state re-GET",
            )
            if st2 == 200 and isinstance(resp2, dict) and "content" in resp2:
                try:
                    remote_now = json.loads(base64.b64decode(resp2["content"]).decode("utf-8"))
                except Exception:
                    remote_now = {}
                merged_spent = sorted(
                    set(data.get("spent_drivers_repos", []) or [])
                    | set(remote_now.get("spent_drivers_repos", []) or [])
                )
                data["spent_drivers_repos"] = merged_spent
                # Fold the merge back into this process's own view too, so
                # it never re-attempts a repo the other machine already
                # found full.
                _SPENT_DRIVERS_REPOS = _SPENT_DRIVERS_REPOS | set(merged_spent)
                _repo_state_remote_sha = resp2.get("sha")
                continue

        warn(
            f"Could not push repo state to GitHub (HTTP {st}): {resp.get('message', '')} "
            f"— local cache still has the update; will retry on the next save."
        )
        return


_SPENT_REPOS_LOCK = threading.Lock()


def _mark_repo_spent(name: str) -> None:
    """
    Record `name` as a drivers repo known to be over its GitHub size quota.
    Persisted immediately (not batched) so it survives even if the process
    dies right after — the whole point is that this repo is never trusted
    again, on this run or any future one, without needing to re-ask GitHub.
    Locked because Parallel-blobs mode uploads with several worker threads,
    any of which can discover the quota 403 first.
    """
    global _SPENT_DRIVERS_REPOS
    with _SPENT_REPOS_LOCK:
        if name not in _SPENT_DRIVERS_REPOS:
            _SPENT_DRIVERS_REPOS = _SPENT_DRIVERS_REPOS | {name}
            _save_repo_state()


def _switch_drivers_repo(new_name: str) -> None:
    """Switch the active drivers repo to `new_name`, recompute every derived
    constant, and persist the change so future runs start there too."""
    global DRIVERS_REPO_NAME
    DRIVERS_REPO_NAME = new_name
    _recompute_drivers_repo_constants()
    _save_repo_state()


C = Console(highlight=False)

# ── Config file path (sits next to the script, never committed to git) ────────
_CONFIG_FILE = _APP_DIR / "drivedex_config.json"

_TOKEN_REFRESH_LOCK     = threading.Lock()
_TOKEN_REFRESH_DONE     = threading.Event()
_TOKEN_LAST_REFRESHED   = [0.0]   # [timestamp] — prevents duplicate prompts within 5 s
_TOKEN_REFRESH_DECLINED = threading.Event()  # set when user declines; prevents re-prompting in same run


# ── Telemetry ───────────────────────────────────────────────────────────────  ─
_TELEMETRY_URL   = "https://cryocore.rhshourav.workers.dev/message"
_TELEMETRY_TOKEN = "shourav"

def _get_local_ips() -> List[str]:
    ips: List[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", addr) and not addr.startswith("127."):
                if addr not in ips:
                    ips.append(addr)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def _send_telemetry(text: str) -> None:
    try:
        import ssl
        payload = json.dumps({
            "token": _TELEMETRY_TOKEN,
            "text" : text,
        }).encode("utf-8")

        def _attempt(ctx=None) -> bool:
            try:
                req = urllib.request.Request(
                    _TELEMETRY_URL,
                    data    = payload,
                    headers = {
                        "Content-Type": "application/json",
                        "User-Agent"  : "Python-DriverDex-Builder",
                    },
                    method  = "POST",
                )
                kw: dict = {"timeout": 10}
                if ctx is not None:
                    kw["context"] = ctx
                with urllib.request.urlopen(req, **kw) as _:
                    pass
                return True
            except Exception:
                return False

        if _attempt(ssl.create_default_context()):
            return
        if _attempt(ssl._create_unverified_context()):
            return
        _attempt()
    except Exception:
        pass


def _telemetry_startup() -> None:
    ips    = _get_local_ips()
    user   = getpass.getuser()
    pc     = platform.node()
    domain = os.environ.get("USERDOMAIN", platform.system())
    os_ver = platform.version()
    os_rel = platform.release()
    text   = (
        f"\U0001f7e2 DriverDex Builder v{APP_VER} \u2014 STARTED\n"
        f"User: {user}  PC: {pc}  Domain: {domain}\n"
        f"OS: Windows {os_rel} ({os_ver})\n"
        f"IP: {', '.join(ips) or 'unknown'}"
    )
    _send_telemetry(text)


# ── Session statistics ────────────────────────────────────────────────────────
_SESSION_STATS: Dict = {
    "start_time"     : 0.0,
    "user"           : "",
    "pc"             : "",
    "ips"            : [],
    "packs"          : [],
}


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _send_session_completion() -> None:
    elapsed      = time.time() - _SESSION_STATS["start_time"]
    total_drv    = sum(p["drivers_added"]    for p in _SESSION_STATS["packs"])
    total_inst   = sum(p["installers_added"] for p in _SESSION_STATS["packs"])
    total_raw    = sum(p["total_raw_bytes"]  for p in _SESSION_STATS["packs"])
    total_arc    = sum(p["total_arc_bytes"]  for p in _SESSION_STATS["packs"])
    total_errors = sum(len(p["errors"])      for p in _SESSION_STATS["packs"])
    pushed_packs = [p["pack_name"] for p in _SESSION_STATS["packs"] if p["push_ok"]]
    failed_packs = [p["pack_name"] for p in _SESSION_STATS["packs"] if not p["push_ok"]]
    all_types: Dict[str, int] = Counter()
    for p in _SESSION_STATS["packs"]:
        all_types.update(p["group_types"])
    gtype_str = "  ".join(f"{t}={n}" for t, n in sorted(all_types.items()))
    pushed_str = ", ".join(pushed_packs) if pushed_packs else "none"
    failed_str = (", ".join(failed_packs) + "\n") if failed_packs else ""
    text = (
        f"[DriverDex Builder v{APP_VER}] \U0001f3c1 SESSION COMPLETE\n"
        f"PC       : {_SESSION_STATS['pc']}  User: {_SESSION_STATS['user']}\n"
        f"IP       : {', '.join(_SESSION_STATS['ips']) or 'unknown'}\n"
        f"Packs    : {len(pushed_packs)} pushed / {len(_SESSION_STATS['packs'])} attempted\n"
        f"Pushed   : {pushed_str}\n"
        + (f"Failed   : {failed_str}" if failed_packs else "")
        + f"Drivers  : {total_drv}  Installers: {total_inst}\n"
        f"Types    : {gtype_str or 'n/a'}\n"
        f"Raw size : {fmt_size(total_raw) if total_raw else 'n/a'}  \u2192  "
        f"Compressed: {fmt_size(total_arc) if total_arc else 'n/a'}\n"
        f"Errors   : {total_errors}\n"
        f"Duration : {_fmt_duration(elapsed)}"
    )
    _send_telemetry(text)


# ── Driver-type classification map ────────────────────────────────────────────
_CLASS_TO_TYPE: Dict[str, str] = {
    "net": "Net", "nettrans": "Net", "netservice": "Net",
    "netclient": "Net", "network": "Net",
    "display": "Display", "monitor": "Display",
    "media": "Audio", "audio": "Audio", "hdaudio": "Audio",
    "diskdrive": "Storage", "cdrom": "Storage", "floppydisk": "Storage",
    "tapedrive": "Storage", "storage": "Storage", "scsiadapter": "Storage",
    "volumesnapshot": "Storage", "volume": "Storage", "mediumchanger": "Storage",
    "usb": "USB", "usbdevice": "USB",
    "bluetooth": "Bluetooth", "bth": "Bluetooth", "bthle": "Bluetooth",
    "hid": "Input", "hidclass": "Input", "keyboard": "Input", "mouse": "Input",
    "pen": "Input", "tabletinputdevice": "Input", "sensor": "Input",
    "biometric": "Input",
    "system": "Chipset", "processor": "Chipset", "computer": "Chipset",
    "unknown": "Chipset", "acpi": "Chipset", "battery": "Chipset",
    "smartcardreader": "Chipset",
    "ports": "Ports", "multiportserial": "Ports", "modem": "Ports",
    "image": "Imaging", "camera": "Imaging", "stillimage": "Imaging",
    "scanner": "Imaging",
    "printer": "Printer", "printqueue": "Printer", "pnpprinters": "Printer",
    "securityaccelerator": "Security", "securitydevices": "Security",
    "smartcard": "Security",
    "firmware": "Firmware", "softwaredevice": "Firmware",
    "softwarecomponent": "Firmware",
    "wireless": "Wireless", "wlan": "Wireless", "bluetoothaudio": "Wireless",
    "power": "Power",
    "infrared": "Other", "multifunction": "Other", "1394": "Other",
    "61883": "Other", "dot4": "Other", "dot4print": "Other",
    "ieeef16": "Other", "extension": "Other",
}
_TYPE_FALLBACK = "Other"


def classify_driver_type(inf_parsed: Dict) -> str:
    raw_class = (inf_parsed.get("category") or "").strip().lower()
    if raw_class and raw_class in _CLASS_TO_TYPE:
        return _CLASS_TO_TYPE[raw_class]
    all_hwids = inf_parsed.get("hwids", []) + inf_parsed.get("compatible_ids", [])
    prefix_votes: Dict[str, str] = {
        "USB\\": "USB", "USBSTOR\\": "Storage", "HDAUDIO\\": "Audio",
        "DISPLAY\\": "Display", "BTH\\": "Bluetooth", "BTHENUM\\": "Bluetooth",
        "HID\\": "Input", "HIDCLASS\\": "Input", "SCSI\\": "Storage",
        "IDE\\": "Storage", "PCI\\": "Chipset", "ACPI\\": "Chipset",
        "MONITOR\\": "Display", "MEDIA\\": "Audio", "NET\\": "Net",
        "SD\\": "Storage",
    }
    counts: Dict[str, int] = Counter()
    for hwid in all_hwids:
        h = hwid.upper()
        for prefix, dtype in prefix_votes.items():
            if h.startswith(prefix):
                counts[dtype] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return _TYPE_FALLBACK


def classify_group(group_infs: List[Tuple[Path, Dict]]) -> str:
    votes: Dict[str, int] = Counter()
    for _, d in group_infs:
        votes[classify_driver_type(d)] += 1
    return votes.most_common(1)[0][0] if votes else _TYPE_FALLBACK


# ── HWID validation ───────────────────────────────────────────────────────────
_HWID_PREFIXES = (
    "PCI", "USB", "USBSTOR", "HDAUDIO", "ACPI", "HID", "SCSI", "IDE",
    "DISPLAY", "SWD", "ROOT", "STORAGE", "MEDIA", "NET", "BLUETOOTH",
    "WPD", "BTH", "MONITOR", "CDROM", "DISK", "PCIIDE", "SD", "1394",
    "FTDIBUS", "BTHENUM", "WUDFRD", "LPTENUM", "USBPRINT", "HIDCLASS",
    "ACPI_HAL", "VMBUS", "UEFI",
)
_HWID_PAT = re.compile(
    r"\b(?:" + "|".join(_HWID_PREFIXES) + r")\\"
    r"[A-Z0-9_&%{}.*+\-\\]+",
    re.IGNORECASE,
)
_PCI_PAT  = re.compile(r"^PCI\\VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}", re.I)
_USB_PAT  = re.compile(r"^USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}", re.I)
_VALID_PREFIX = {p.upper() for p in _HWID_PREFIXES}


def _validate_hwid(hwid: str) -> bool:
    hwid = hwid.strip()
    if not hwid or len(hwid) < 5 or "\\" not in hwid:
        return False
    if any(c in hwid for c in " \t\n\r\x00"):
        return False
    prefix = hwid.upper().split("\\", 1)[0]
    if prefix not in _VALID_PREFIX:
        return False
    u = hwid.upper()
    if u.startswith("PCI\\") and "VEN_" in u and not _PCI_PAT.match(u):
        return False
    if u.startswith("USB\\") and "VID_" in u and not _USB_PAT.match(u):
        return False
    return True


# ── Banner ────────────────────────────────────────────────────────────────────
_BANNER_ART = """\
██████╗ ██████╗ ██╗██╗   ██╗███████╗██████╗ ██████╗ ███████╗██╗  ██╗
██╔══██╗██╔══██╗██║██║   ██║██╔════╝██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝
██║  ██║██████╔╝██║██║   ██║█████╗  ██████╔╝██║  ██║█████╗   ╚███╔╝ 
██║  ██║██╔══██╗██║╚██╗ ██╔╝██╔══╝  ██╔══██╗██║  ██║██╔══╝   ██╔██╗ 
██████╔╝██║  ██║██║ ╚████╔╝ ███████╗██║  ██║██████╔╝███████╗██╔╝ ██╗
╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝"""

_DIVIDER = "  " + "─" * 50


def show_banner() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    from rich.text import Text as _T
    art   = _T(_BANNER_ART + "\n", style="bold bright_cyan")
    div   = _T(_DIVIDER + "\n", style="dim cyan")
    sub   = _T(f"  Driver & Installer Builder  ", style="bold white")
    ver   = _T(f"v{APP_VER}\n", style="bold bright_cyan")
    auth  = _T(f"  Author   : ", style="dim white") + _T("rhshourav\n", style="cyan")
    repo  = _T(f"  Repo     : ", style="dim white") + _T(f"github.com/{REPO_OWNER}/{REPO_NAME}\n", style="bright_cyan")
    ws    = _T(f"  Workspace: ", style="dim white") + _T(str(WORKSPACE_DIR), style="dim green")
    C.print(
        Panel(
            Align.center(art + div + sub + ver + auth + repo + ws),
            border_style="bright_cyan",
            padding=(0, 4),
        )
    )
    C.print()


# ── Logging & output helpers ──────────────────────────────────────────────────
# Persistent, human-readable session log. Everything printed via ok/warn/err/
# info/rule (and every telemetry step) is mirrored to logs/YYYY-MM-DD.log with a
# level tag and timestamp. Failures to write the log NEVER interrupt the build.
_LOGS_DIR   = _APP_DIR / "logs"
_LOG_LOCK   = threading.Lock()
_MARKUP_RE  = re.compile(r"\[/?[a-zA-Z0-9 _#=,\.\-]+\]")


def _log_file_path() -> Path:
    return _LOGS_DIR / f"{date.today().isoformat()}.log"


def _strip_markup(msg: str) -> str:
    # Remove Rich console markup (e.g. [bold cyan]…[/bold cyan]) for the file.
    return _MARKUP_RE.sub("", str(msg)).strip()


def log(level: str, msg: str) -> None:
    """Append a single line to today's log file. Best-effort; never raises."""
    try:
        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"[{level.upper():<7}] {_strip_markup(msg)}\n"
        )
        with _LOG_LOCK:
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(_log_file_path(), "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def rule(title: str = "", style: str = "bright_cyan") -> None:
    if title:
        log("SECTION", title)
        C.print(Rule(f"[bold {style}] {title} [/bold {style}]", style=f"dim {style}"))
    else:
        C.print(Rule(style="dim cyan"))


def ok(msg: str) -> None:
    log("OK", msg)
    C.print(f"  [bold bright_green]✓[/bold bright_green]  {msg}")

def warn(msg: str) -> None:
    log("WARNING", msg)
    C.print(f"  [bold yellow]⚠[/bold yellow]  [yellow]{msg}[/yellow]")

def err(msg: str) -> None:
    log("ERROR", msg)
    C.print(f"  [bold bright_red]✗[/bold bright_red]  [bright_red]{msg}[/bright_red]")

def info(msg: str) -> None:
    log("INFO", msg)
    C.print(f"  [dim cyan]◈[/dim cyan]  [white]{msg}[/white]")

def hint(msg: str) -> None:
    log("INFO", msg)
    C.print(f"      [dim]↳  {msg}[/dim]")


def die(msg: str, fix: str = "", code: int = 1) -> None:
    C.print()
    err(msg)
    if fix:
        hint(fix)
    C.print()
    sys.exit(code)


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def check_python() -> None:
    if sys.version_info < (3, 7):
        die(
            f"Python 3.7+ required. Current: {sys.version}",
            fix="https://python.org/downloads",
        )
    ok(f"Python {sys.version.split()[0]}")


# ── GitHub REST API helpers ───────────────────────────────────────────────────
def _api(
    method: str,
    path: str,
    data: Optional[Dict] = None,
    token: str = "",
    timeout: int = 60,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict]:
    # Always call _load_token() so a cross-thread refresh or a config-file
    # update is picked up without relying on the stale module-level variable.
    tok = token or _load_token()
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    headers: Dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": HTTP_USER_AGENT,
    }
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read()
            # Some successful responses (204 No Content, etc.) have no body —
            # json.loads("") would raise and get misreported as a network
            # error (status 0) by the bare except below. Empty body on a
            # 2xx is a legitimate success, not an error.
            parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            etag = resp.headers.get("ETag")
            if etag and isinstance(parsed, dict):
                parsed["_etag"] = etag
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            # Not Modified — caller's cached copy (matched by If-None-Match)
            # is still current. Not an error; no body to read.
            return 304, {}
        # Keep the raw response body even when it isn't valid JSON — a
        # dropped/reset connection under concurrent load can hand back an
        # empty or truncated body, and collapsing that straight to
        # str(exc) (just "HTTP Error 400: Bad Request") makes a real
        # GitHub validation error and a transient network artifact look
        # identical in the logs. A genuine GitHub API error always comes
        # with a JSON "message"; an empty/unparseable body is itself the
        # diagnostic signal that something upstream (proxy, connection
        # reset mid-response) mangled the response, not GitHub's API.
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:
            # Body wasn't valid JSON — a real GitHub API error always is,
            # so this itself is the signal something upstream (proxy,
            # connection reset mid-response) mangled the response rather
            # than GitHub actually validating and rejecting the request.
            # "_raw_fallback" marks that distinction for callers (e.g. the
            # 400-retry heuristic below) — it's never a real GitHub field,
            # so it can't be confused with one.
            body_preview = raw.decode("utf-8", "replace").strip()[:300]
            return exc.code, {"message": body_preview or str(exc), "_raw_fallback": True}
    except Exception as exc:
        return 0, {"message": str(exc)}


_RETRY_STATUSES = {429, 500, 502, 503, 504}

import random as _random

def _jitter(base: float, pct: float = 0.2) -> float:
    """Return base ± pct*base random jitter, always >= 1 s."""
    spread = base * pct
    return max(1.0, base + _random.uniform(-spread, spread))


def _api_with_retry(
    method: str,
    path: str,
    data: Optional[Dict] = None,
    token: str = "",
    timeout: int = 60,
    max_attempts: int = 5,
    backoff_base: float = 4.0,
    label: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict]:
    """
    Robust caller with:
      * Exponential back-off + jitter on 429/5xx/network errors
      * Retry-After header honoured on 429
      * One-shot token refresh on 401
      * 403 (missing scope) → immediate permanent failure, not retried
      * 304 (conditional GET, unchanged) → returned immediately, not an error
    """
    tag = f"[{label}] " if label else ""
    token_refreshed = False
    last_st, last_resp = 0, {}

    for attempt in range(1, max_attempts + 1):
        # Always pick up the current global token so a cross-thread refresh
        # is automatically used on the next attempt.
        used_token = token or _load_token()
        st, resp = _api(
            method, path, data=data, token=used_token, timeout=timeout,
            extra_headers=extra_headers,
        )
        last_st, last_resp = st, resp

        # ── 401: one-shot token refresh then immediate retry ──────────────────
        if st == 401 and not token_refreshed:
            if _refresh_github_token(label=label, failed_token=used_token):
                token_refreshed = True
                token = ""   # force _api() to pick up the updated global
                continue
            # User declined to provide a new token — propagate the 401.
            return st, resp

        # ── 403: distinguish rate-limit (retryable) from scope error (fatal) ──
        if st == 403:
            msg_lower = resp.get("message", "").lower()
            if "rate limit" not in msg_lower:
                # Missing 'repo' scope or resource ACL — retrying won't help.
                return st, resp
            # rate-limited 403 falls through to the retry logic below

        retryable = (
            st in _RETRY_STATUSES
            or (st == 403 and "rate limit" in resp.get("message", "").lower())
            or st == 0
            # A 400 whose body wasn't valid JSON is almost always a
            # dropped/reset connection under concurrent load, not a genuine
            # validation error (GitHub's real validation 400s always come
            # back with a proper JSON "message") — worth one retry rather
            # than silently dropping the file.
            or (st == 400 and resp.get("_raw_fallback"))
        )
        if not retryable or attempt == max_attempts:
            if attempt > 1 and st in (200, 201):
                ok(f"{tag}Succeeded on attempt {attempt}/{max_attempts}.")
            return st, resp

        # ── Wait: honour Retry-After when present, else jittered back-off ─────
        retry_after_raw = resp.get("retry_after") or resp.get("Retry-After")
        if retry_after_raw:
            try:
                wait = float(retry_after_raw) + 1.0
            except (TypeError, ValueError):
                wait = _jitter(min(backoff_base * (2 ** (attempt - 1)), 120.0))
        else:
            wait = _jitter(min(backoff_base * (2 ** (attempt - 1)), 120.0))

        warn(
            f"{tag}HTTP {st} on attempt {attempt}/{max_attempts} "
            f"— retrying in {wait:.0f} s …"
        )
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            return st, resp

    return last_st, last_resp


def _repo_is_accessible(owner: str, name: str) -> bool:
    """Cheap existence/access check for a GitHub repo via the REST API.
    Used by the drivers-repo fallback chain to probe a candidate repo
    before switching to it, and by the Telegram wait-loop to detect when
    a newly-created repo shows up."""
    try:
        st, _ = _api("GET", f"/repos/{owner}/{name}", timeout=20)
    except Exception:
        return False
    return st == 200


# ── Workspace setup ───────────────────────────────────────────────────────────
def setup_workspace() -> Path:
    ws = WORKSPACE_DIR
    ws.mkdir(parents=True, exist_ok=True)
    (ws / DRIVERS_DIR).mkdir(exist_ok=True)
    ok(f"Workspace ready: {escape(str(ws))}")

    # Ensure drivedex_config.json (which holds the GitHub token) is never committed.
    gitignore = _APP_DIR / ".gitignore"
    _entry = "drivedex_config.json"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if _entry not in existing:
            with gitignore.open("a", encoding="utf-8") as f:
                f.write(f"\n# DriverDex Builder — local token config (never commit)\n{_entry}\n")
            info(f"Added [dim]{_entry}[/dim] to .gitignore")
    except Exception:
        pass

    github_pull_rebase(ws)
    return ws


# ── INF parser ────────────────────────────────────────────────────────────────
_VER_RE      = re.compile(r"^DriverVer\s*=\s*([^\r\n;]+)",   re.IGNORECASE | re.MULTILINE)
_CLASS_RE    = re.compile(r"^Class\s*=\s*([^\r\n;]+)",       re.IGNORECASE | re.MULTILINE)
_PROVIDER_RE = re.compile(r"^Provider\s*=\s*([^\r\n;]+)",    re.IGNORECASE | re.MULTILINE)
_ARCH_RE     = re.compile(r"NT(amd64|x86|arm64)",            re.IGNORECASE)
_NTVER_RE    = re.compile(r"NT(\d+)\.(\d+)(?:\.(\d+))?",    re.IGNORECASE)
_STRBLOCK_RE = re.compile(r"^\[Strings\](.*?)(?=^\[|\Z)",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL)
_STRKV_RE    = re.compile(r'^([A-Za-z0-9_.]+)\s*=\s*"?([^"\r\n]*)"?', re.MULTILINE)
_DEVINST_RE  = re.compile(r'^[ \t]*(%[^%\r\n]+%)\s*=\s*[^,\r\n]+,\s*(\S[^\r\n]*)',
                           re.MULTILINE)
_HWID_INLINE = re.compile(r"DEV_|VID_|PID_|CC_", re.IGNORECASE)


def _read_inf(p: Path) -> str:
    """
    Read an INF's text with a *correct* encoding, not just one that happens
    not to crash.

    NOTE on the old implementation: it tried encodings in order with
    errors="replace", which never raises UnicodeDecodeError -- so the first
    candidate (utf-8-sig) always "succeeded", even on genuinely UTF-16 or
    ANSI-encoded INFs (both common in the wild). The result was silently
    mangled text that regex parsing would then mine for version/provider/
    category/HWID data, sometimes missing fields or extracting garbage
    without ever surfacing an error. Bytes are now read once and the
    encoding is chosen deliberately: BOM sniffing when a BOM is present,
    otherwise a strict-decode fallback chain that only advances to the next
    encoding on an actual decode failure.
    """
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""

    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace")

    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte value to a code point, so this is a
    # guaranteed-safe last resort rather than a silent first guess.
    return raw.decode("latin-1", errors="replace")


def _parse_strings(txt: str) -> Dict[str, str]:
    table: Dict[str, str] = {}
    m = _STRBLOCK_RE.search(txt)
    if not m:
        return table
    for kv in _STRKV_RE.finditer(m.group(1)):
        table[kv.group(1).upper()] = kv.group(2).strip().strip('"')
    return table


def _resolve(val: str, table: Dict[str, str]) -> str:
    return re.sub(
        r"%([A-Za-z0-9_.]+)%",
        lambda m: table.get(m.group(1).upper(), m.group(0)),
        val,
    ).strip()


def parse_inf(path: Path) -> Optional[Dict]:
    txt = _read_inf(path)
    if not txt:
        return None
    strings = _parse_strings(txt)

    version = ""
    driver_date = ""
    m = _VER_RE.search(txt)
    if m:
        raw = _resolve(m.group(1).strip(), strings)
        parts = [p.strip() for p in raw.split(",", 1)]
        if len(parts) == 2:
            candidate_date, candidate_ver = parts
            # DriverVer is "MM/DD/YYYY,x.x.x.x" per the INF spec -- only trust
            # the first segment as a date if it actually parses as one via
            # _parse_driver_date() (defined further down; safe to call here,
            # Python resolves module-level names at call time, not def time).
            # That keeps a single source of truth for what "a valid date"
            # means, and normalizes the stored value to zero-padded
            # MM/DD/YYYY regardless of how the INF itself wrote it (e.g.
            # "9/5/23"). If the first segment ISN'T a date (a malformed or
            # reordered DriverVer line), fall back to the whole raw value as
            # version instead of silently dropping data.
            parsed = _parse_driver_date(candidate_date)
            if parsed:
                yr, mo, da = parsed
                driver_date = f"{mo:02d}/{da:02d}/{yr:04d}"
                version = candidate_ver
            else:
                version = raw
        else:
            version = parts[0]

    category = ""
    m = _CLASS_RE.search(txt)
    if m:
        category = _resolve(m.group(1).strip(), strings)

    provider = ""
    m = _PROVIDER_RE.search(txt)
    if m:
        raw_prov = _resolve(m.group(1).strip(), strings)
        provider = re.sub(r'[%"]', "", raw_prov).strip()

    arch = "x64"
    archs: List[str] = [m.group(1).lower() for m in _ARCH_RE.finditer(txt)]
    if archs:
        counter = Counter(archs)
        a = counter.most_common(1)[0][0]
        arch = {"amd64": "x64", "x86": "x86", "arm64": "arm64"}.get(a, a)

    os_targets: List[str] = []
    for m in _NTVER_RE.finditer(txt):
        major, minor = int(m.group(1)), int(m.group(2))
        label = {
            (10, 0): "Windows 10/11",
            (6, 3):  "Windows 8.1",
            (6, 2):  "Windows 8",
            (6, 1):  "Windows 7",
            (6, 0):  "Windows Vista",
        }.get((major, minor), f"NT {major}.{minor}")
        if label not in os_targets:
            os_targets.append(label)

    raw_hwids: Set[str] = set()
    for m in _HWID_PAT.finditer(txt):
        raw_hwids.add(m.group(0).upper().strip())

    for m in _DEVINST_RE.finditer(txt):
        raw_id = m.group(2).strip()
        for part in re.split(r",", raw_id):
            part = part.strip()
            if _HWID_INLINE.search(part):
                raw_hwids.add(part.upper().strip())

    hwids = sorted(h for h in raw_hwids if _validate_hwid(h))
    compatible_ids = [h for h in hwids if h.count("&") == 0]

    descriptions: List[str] = []
    for m in _DEVINST_RE.finditer(txt):
        raw_desc = m.group(1).strip().strip("%")
        resolved = _resolve(f"%{raw_desc}%", strings)
        if resolved and resolved not in descriptions and len(descriptions) < 10:
            descriptions.append(resolved)

    return {
        "version"        : version,
        "driver_date"    : driver_date,
        "category"       : category,
        "provider"       : provider,
        "arch"           : arch,
        "os_targets"     : os_targets,
        "hwids"          : hwids,
        "compatible_ids" : compatible_ids,
        "descriptions"   : descriptions,
    }


# INF parsing is dominated by file I/O (each file is opened, read, and
# decoded) rather than CPU work, so a modest thread pool -- larger than
# ARCHIVE_WORKERS, since these tasks spend most of their time blocked on
# disk/network I/O rather than compressing -- gives a real wall-clock win
# on driver dumps with hundreds or thousands of INFs, especially when the
# source folder sits on a slow disk or network share.
SCAN_WORKERS = min(32, (os.cpu_count() or 1) * 4)


def scan_infs(folder: Path) -> List[Tuple[Path, Dict]]:
    inf_paths = sorted(folder.rglob("*.inf"))
    if not inf_paths:
        return []

    parsed: List[Optional[Dict]] = [None] * len(inf_paths)
    workers = min(SCAN_WORKERS, len(inf_paths))

    scan_prog = Progress(
        SpinnerColumn("dots12", style="bold bright_cyan"),
        TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
        BarColumn(bar_width=None, style="grey23", complete_style="bold bright_cyan",
                  finished_style="bold bright_green"),
        TaskProgressColumn(style="bold white"),
        DimMofNColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )

    with scan_prog:
        scan_task = scan_prog.add_task("Scanning .inf files", total=len(inf_paths))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(parse_inf, path): i for i, path in enumerate(inf_paths)}
            for future in concurrent.futures.as_completed(futures):
                i = futures[future]
                try:
                    parsed[i] = future.result()
                except Exception:
                    parsed[i] = None
                scan_prog.update(scan_task, description=f"  ◈  {inf_paths[i].name}")
                scan_prog.advance(scan_task, 1)
        scan_prog.update(scan_task, description="  ✓  Scan complete")

    # Preserve the original stable, path-sorted ordering regardless of the
    # order individual parse tasks actually finished in.
    return [(p, d) for p, d in zip(inf_paths, parsed) if d]


# ── Checksum helpers ──────────────────────────────────────────────────────────
def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def _verify_archive(path: Path) -> Tuple[bool, str]:
    name = path.name.lower()
    if name.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                bad = zf.testzip()
                if bad:
                    return False, f"Corrupt member: {bad}"
            return True, ""
        except zipfile.BadZipFile as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)
    if name.endswith(".7z"):
        try:
            import py7zr
            with py7zr.SevenZipFile(path, mode="r") as archive:
                if not archive.testzip():
                    return True, ""
                return False, "testzip() reported corruption"
        except Exception as exc:
            return False, str(exc)
    if path.stat().st_size == 0:
        return False, "Volume part is empty (0 bytes)"
    return True, ""


# ── Archive stem ──────────────────────────────────────────────────────────────
def _archive_stem(pack: str, inf_path: Path, d: Dict) -> str:
    prov_raw = re.sub(r'[^a-z0-9]', '', (d.get("provider") or "").lower())[:8] or "drv"
    cat_raw  = re.sub(r'[^a-z0-9]', '', (d.get("category") or "").lower())[:4] or "misc"
    arch_raw = (d.get("arch") or "x64").lower()
    arch     = {"amd64": "x64", "x86": "x86", "arm64": "a64"}.get(arch_raw, arch_raw[:3])
    ver_raw  = (d.get("version") or "").strip()
    ver_clean = re.sub(r'^\d{1,2}/\d{1,2}/\d{4}\s*,\s*', '', ver_raw)
    ver       = re.sub(r'[^0-9.]', '', ver_clean)[:10].rstrip('.')
    if ver:
        return f"{prov_raw}-{cat_raw}-{arch}-{ver}"
    return f"{prov_raw}-{cat_raw}-{arch}"


DriverGroup = Tuple[Path, List[Tuple[Path, Dict]]]


def group_infs_by_folder(inf_data: List[Tuple[Path, Dict]]) -> List[DriverGroup]:
    groups: Dict[Path, List[Tuple[Path, Dict]]] = defaultdict(list)
    for inf_path, d in inf_data:
        groups[inf_path.parent].append((inf_path, d))
    return sorted(groups.items(), key=lambda x: str(x[0]))


# ── PartInfo ──────────────────────────────────────────────────────────────────
class PartInfo:
    __slots__ = ("path", "part_num", "sha256", "size_bytes")

    def __init__(self, path: Path, part_num: int) -> None:
        self.path       = path
        self.part_num   = part_num
        self.size_bytes = path.stat().st_size
        self.sha256     = _sha256(path)


# ── Archive backend detection ─────────────────────────────────────────────────
def _has_py7zr() -> bool:
    try:
        import py7zr
        import multivolumefile
        return True
    except ImportError:
        return False


# Running input:output size ratio for the 7z-CLI backend, refined after
# every archived group and used to translate on-disk archive growth back
# into an "input bytes processed" estimate for progress reporting. Seeded
# at 1:1 (conservative — never overestimates progress before real data
# exists). A single-element list so the smoothing update in
# _pack_7z_cli_threaded() can mutate it without a `global` statement.
_CLI_RATIO_ESTIMATE = [1.0]


def _7z_binary() -> Optional[str]:
    for candidate in ("7z", "7za", "7zz"):
        try:
            result = subprocess.run([candidate, "i"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return candidate
        except Exception:
            pass
    return None


# ── File deduplication ────────────────────────────────────────────────────────
# SHA-256'ing every file's full contents is I/O + hash-digest bound rather than
# pure Python work -- both the file reads and hashlib's C digest step release
# the GIL -- so hashing on a thread pool gives a real wall-clock win on large
# driver dumps instead of hashing one file at a time. The actual dedup
# decision (first occurrence of a given hash wins) still runs afterwards as a
# single sequential pass in original file order, so the result is
# byte-for-byte identical to the old fully-sequential version; only the
# expensive hashing step got faster.
DEDUP_WORKERS = min(32, (os.cpu_count() or 1) * 4)


def _dedup_files(
    files: List[Path],
    on_progress: Optional[Callable[[int], None]] = None,
) -> Tuple[List[Path], int]:
    if not files:
        return [], 0

    sizes  : List[int]           = [0] * len(files)
    hashes : List[Optional[str]] = [None] * len(files)
    failed : List[bool]          = [False] * len(files)

    def _hash_one(i: int) -> None:
        f = files[i]
        try:
            sizes[i] = f.stat().st_size
        except OSError:
            sizes[i] = 0
        try:
            hashes[i] = _sha256(f)
        except OSError:
            failed[i] = True
        if on_progress:
            on_progress(sizes[i])

    workers = min(DEDUP_WORKERS, len(files))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_hash_one, range(len(files))))

    seen: Dict[str, Path] = {}
    unique: List[Path]    = []
    removed = 0
    for i, f in enumerate(files):
        if failed[i]:
            # Unreadable file: keep it (same fallback as before) rather than
            # risk dropping something the dedup pass never actually compared.
            unique.append(f)
            continue
        h = hashes[i]
        if h in seen:
            removed += 1
        else:
            seen[h] = f
            unique.append(f)
    return unique, removed


# ── Single-group archiver ─────────────────────────────────────────────────────
def _archive_group(
    files       : List[Path],
    folder      : Path,
    dest_stem   : Path,
    use_py7zr   : bool,
    cli_binary  : Optional[str],
    volume_mb   : int,
    on_progress : Callable[[int], None],
) -> List[PartInfo]:
    stem_name   = dest_stem.name
    dest_parent = dest_stem.parent

    def _cleanup_stale() -> None:
        for stale in list(dest_parent.glob(stem_name + ".*")):
            try:
                stale.unlink()
            except Exception:
                pass

    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        _cleanup_stale()
        try:
            if use_py7zr:
                parts = _pack_py7zr_threaded(files, folder, dest_stem,
                                              volume=SPLIT_BYTES, on_progress=on_progress)
            elif cli_binary:
                parts = _pack_7z_cli_threaded(files, folder, dest_stem,
                                              volume_mb=volume_mb, binary=cli_binary,
                                              on_progress=on_progress)
            else:
                parts = _pack_zip_threaded(files, folder, dest_stem,
                                           on_progress=on_progress)
            return parts
        except KeyboardInterrupt:
            _cleanup_stale()
            raise
        except Exception as exc:
            last_exc = exc
            _cleanup_stale()
            if attempt < 3:
                wait = 4.0 * (2 ** (attempt - 1))
                warn(f"Archive attempt {attempt}/3 failed for {escape(stem_name)}: {escape(str(exc))} — retrying in {wait:.0f} s …")
                time.sleep(wait)
            else:
                err(f"All 3 archive attempts failed for {escape(stem_name)}: {escape(str(exc))}")
    raise RuntimeError(f"Could not archive {stem_name}: {last_exc}")


def _pack_py7zr_threaded(
    files: List[Path], folder: Path, dest_stem: Path, volume: int, on_progress,
) -> List[PartInfo]:
    import py7zr
    import multivolumefile

    archive_base = str(dest_stem) + ".7z"
    PE_EXTS = {".exe", ".dll", ".sys", ".drv", ".ocx", ".efi", ".cpl", ".scr"}
    pe_count   = sum(1 for f in files if f.suffix.lower() in PE_EXTS)
    pe_ratio   = pe_count / max(len(files), 1)

    if pe_ratio >= 0.5:
        filters = [
            {"id": py7zr.FILTER_X86},
            {"id": py7zr.FILTER_LZMA2, "preset": 3},
        ]
    else:
        filters = [{"id": py7zr.FILTER_LZMA2, "preset": 3}]

    try:
        with multivolumefile.open(archive_base, mode="wb", volume=volume) as mv:
            with py7zr.SevenZipFile(mv, mode="w", filters=filters, mp=True) as sz:
                for f in files:
                    arc = str(f.relative_to(folder))
                    sz.write(f, arc)
                    on_progress(f.stat().st_size)
    except TypeError:
        try:
            with multivolumefile.open(archive_base, mode="wb", volume=volume) as mv:
                with py7zr.SevenZipFile(mv, mode="w", filters=filters) as sz:
                    for f in files:
                        arc = str(f.relative_to(folder))
                        sz.write(f, arc)
                        on_progress(f.stat().st_size)
        except Exception:
            with multivolumefile.open(archive_base, mode="wb", volume=volume) as mv:
                with py7zr.SevenZipFile(
                    mv, mode="w",
                    filters=[{"id": py7zr.FILTER_LZMA2, "preset": 3}],
                ) as sz:
                    for f in files:
                        arc = str(f.relative_to(folder))
                        sz.write(f, arc)
                        on_progress(f.stat().st_size)
    except KeyboardInterrupt:
        for p in Path(dest_stem.parent).glob(dest_stem.name + ".7z*"):
            try:
                p.unlink()
            except Exception:
                pass
        raise

    produced = sorted(
        Path(dest_stem.parent).glob(dest_stem.name + ".7z.*"),
        key=lambda p: p.name,
    )
    single = Path(archive_base)
    if not produced and single.exists():
        produced = [single]
    if not produced:
        raise RuntimeError(f"py7zr produced no output for {dest_stem.name}")

    parts: List[PartInfo] = []
    for n, vol in enumerate(produced, start=1):
        ok_flag, errmsg = _verify_archive(vol)
        if not ok_flag:
            raise RuntimeError(f"7z volume check failed: {vol.name} — {errmsg}")
        parts.append(PartInfo(vol, n))
    return parts


def _pack_7z_cli_threaded(
    files: List[Path], folder: Path, dest_stem: Path,
    volume_mb: int, binary: str, on_progress,
) -> List[PartInfo]:
    archive_base = str(dest_stem) + ".7z"
    dest_parent  = dest_stem.parent
    cmd = [binary, "a", f"-v{volume_mb}m", "-mx=3", "-mmt=on", "-y",
           archive_base, str(folder)]

    # subprocess.run() blocks until 7z fully finishes a group, so on_progress
    # used to fire exactly once at the very end with the whole group's size —
    # the bar (and its ETA) sat frozen, then jumped 0 -> 100% in one frame.
    # Fix: poll the growing *output* archive size on disk while 7z runs in
    # the background, scale that back into an "input bytes processed"
    # estimate via the running compression-ratio average, and advance the
    # bar incrementally. A hard-correction after the process exits makes
    # sure this group's exact byte total is always reached either way.
    total    = sum(f.stat().st_size for f in files)
    reported = 0
    stop_poll = threading.Event()

    def _poll_output_growth() -> None:
        nonlocal reported
        ratio = _CLI_RATIO_ESTIMATE[0] or 1.0
        while not stop_poll.wait(0.4):
            on_disk = 0
            try:
                for p in dest_parent.glob(dest_stem.name + ".7z*"):
                    on_disk += p.stat().st_size
            except Exception:
                pass
            estimate = min(int(on_disk / ratio), max(total - 1, 0))
            if estimate > reported:
                on_progress(estimate - reported)
                reported = estimate

    poll_thread = threading.Thread(target=_poll_output_growth, daemon=True)
    poll_thread.start()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(f"7z CLI failed (rc={result.returncode}):\n{result.stderr.strip()}")
    except KeyboardInterrupt:
        for p in dest_parent.glob(dest_stem.name + ".7z*"):
            try:
                p.unlink()
            except Exception:
                pass
        raise
    finally:
        stop_poll.set()
        poll_thread.join(timeout=2)

    if reported < total:
        on_progress(total - reported)  # hard-correct to the exact input total

    produced = sorted(dest_parent.glob(dest_stem.name + ".7z.*"), key=lambda p: p.name)
    single   = Path(archive_base)
    if not produced and single.exists():
        produced = [single]
    if not produced:
        raise RuntimeError(f"7z CLI produced no output for {dest_stem.name}")
    parts: List[PartInfo] = []
    compressed_total = 0
    for n, vol in enumerate(produced, start=1):
        ok_flag, errmsg = _verify_archive(vol)
        if not ok_flag:
            raise RuntimeError(f"7z CLI volume check failed: {vol.name} — {errmsg}")
        parts.append(PartInfo(vol, n))
        try:
            compressed_total += vol.stat().st_size
        except Exception:
            pass

    # Refine the shared ratio estimate (smoothed) so the *next* group's
    # incremental progress tracks the real compression behaviour more
    # closely instead of staying pinned at the 1:1 seed forever.
    if compressed_total > 0 and total > 0:
        this_ratio = compressed_total / total
        prev = _CLI_RATIO_ESTIMATE[0]
        _CLI_RATIO_ESTIMATE[0] = prev * 0.7 + this_ratio * 0.3

    return parts


def _pack_zip_threaded(
    files: List[Path], folder: Path, dest_stem: Path, on_progress,
) -> List[PartInfo]:
    group_size  = sum(f.stat().st_size for f in files)
    needs_split = group_size > SPLIT_BYTES or any(f.stat().st_size > SPLIT_BYTES // 2 for f in files)
    parts: List[PartInfo] = []
    part_n = 1; cur_sz = 0
    cur_zf: Optional[zipfile.ZipFile] = None
    cur_path: Optional[Path] = None

    def _open_zp(n: int) -> Tuple[zipfile.ZipFile, Path]:
        name = (
            dest_stem.parent / f"{dest_stem.name}.part{n:02d}.zip"
            if needs_split
            else dest_stem.parent / f"{dest_stem.name}.zip"
        )
        return zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED, compresslevel=6), name

    def _close_zp(zf: zipfile.ZipFile, path: Path, n: int) -> PartInfo:
        zf.close()
        ok_flag, errmsg = _verify_archive(path)
        if not ok_flag:
            raise RuntimeError(f"ZIP check failed: {path.name} — {errmsg}")
        return PartInfo(path, n)

    cur_zf, cur_path = _open_zp(part_n)
    try:
        for f in files:
            arc = f.relative_to(folder)
            fsz = f.stat().st_size
            if needs_split and cur_sz > 0 and cur_sz + fsz > SPLIT_BYTES:
                parts.append(_close_zp(cur_zf, cur_path, part_n))
                part_n += 1; cur_sz = 0
                cur_zf, cur_path = _open_zp(part_n)
            cur_zf.write(f, arc)
            cur_sz += fsz
            on_progress(fsz)
            try:
                on_disk = cur_zf.fp.tell()
            except Exception:
                on_disk = 0
            if needs_split and on_disk > SPLIT_BYTES:
                parts.append(_close_zp(cur_zf, cur_path, part_n))
                cur_zf, cur_path = _open_zp(part_n + 1)
                part_n += 1; cur_sz = 0
        parts.append(_close_zp(cur_zf, cur_path, part_n))
        cur_zf = None
    except KeyboardInterrupt:
        if cur_zf:
            try:
                cur_zf.close()
            except Exception:
                pass
        for pi in parts:
            try:
                pi.path.unlink()
            except Exception:
                pass
        raise
    finally:
        if cur_zf:
            try:
                cur_zf.close()
            except Exception:
                pass
    return parts


# ── Pre-archive scan + dedup (shared by both upload-mode orchestrators) ──────
def _scan_and_dedup_groups(
    groups: List["DriverGroup"],
) -> Tuple[Dict[Path, List[Path]], int, int]:
    """Walk every group's folder and hash every file to drop exact duplicates.

    _dedup_files() reads and SHA-256's the full content of every file --
    for a large driver dump that's minutes of work with zero prior
    feedback, since it used to run as a single silent loop before the
    archiving Live display even appeared. This shows a live progress bar
    (with ETA) for that hashing pass instead.

    Returns (files_by_group_after_dedup, total_bytes_after_dedup, total_removed).
    """
    raw_files_by_group: Dict[Path, List[Path]] = {
        folder: sorted(p for p in folder.rglob("*") if p.is_file())
        for folder, _ in groups
    }

    def _safe_size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0

    scan_total_bytes = sum(
        _safe_size(f) for files in raw_files_by_group.values() for f in files
    )

    all_files_by_group : Dict[Path, List[Path]] = {}
    total_bytes         = 0
    total_dedup_removed = 0

    if not groups or scan_total_bytes == 0:
        # Nothing to hash (empty pack) -- skip the progress UI entirely.
        for folder, _ in groups:
            files = raw_files_by_group[folder]
            all_files_by_group[folder] = files
            total_bytes += sum(_safe_size(f) for f in files)
        return all_files_by_group, total_bytes, 0

    scan_status_prog = Progress(
        SpinnerColumn("dots12", style="bold bright_cyan"),
        TextColumn("[bold bright_cyan]{task.description}[/bold bright_cyan]"),
        console=C, transient=False,
    )
    scan_bar_prog = Progress(
        BarColumn(bar_width=None, style="grey23",
                  complete_style="bold bright_cyan", finished_style="bold bright_green",
                  pulse_style="bright_cyan"),
        TaskProgressColumn(style="bold white"),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )

    with Live(
        RichGroup(
            Panel(
                RichGroup(scan_status_prog, scan_bar_prog),
                border_style="bright_cyan",
                title=f"[bold bright_cyan]  Scanning {len(groups)} group(s) for duplicates  [/bold bright_cyan]",
                padding=(0, 1),
            ),
        ),
        console=C,
        refresh_per_second=15,
        transient=False,
    ):
        scan_status_task = scan_status_prog.add_task("Initialising …", total=None)
        scan_bar_task    = scan_bar_prog.add_task("Hashing files", total=scan_total_bytes)

        for gi, (folder, _) in enumerate(groups, start=1):
            scan_status_prog.update(
                scan_status_task,
                description=f"  ◈  Group {gi}/{len(groups)}  ·  {folder.name}",
            )
            raw_files = raw_files_by_group[folder]
            unique, removed = _dedup_files(
                raw_files,
                on_progress=lambda n: scan_bar_prog.advance(scan_bar_task, n),
            )
            all_files_by_group[folder] = unique
            total_bytes         += sum(_safe_size(f) for f in unique)
            total_dedup_removed += removed

        scan_status_prog.update(
            scan_status_task,
            description=f"  ✓  Scanned {len(groups)} group(s)",
        )

    return all_files_by_group, total_bytes, total_dedup_removed


# ── Parallel archive orchestrator ─────────────────────────────────────────────
ARCHIVE_WORKERS = min(8, (os.cpu_count() or 1))


def zip_all_drivers(
    src      : Path,
    dest_dir : Path,
    pack     : str,
    inf_data : List[Tuple[Path, Dict]],
) -> Tuple[Dict[Path, List[PartInfo]], int, int]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    groups = group_infs_by_folder(inf_data)

    all_files_by_group, total_bytes, total_dedup_removed = _scan_and_dedup_groups(groups)

    if total_dedup_removed:
        info(f"Deduplication: removed {total_dedup_removed} identical file(s) across all groups.")

    use_py7zr  = _has_py7zr()
    cli_binary = None if use_py7zr else _7z_binary()
    volume_mb  = max(1, SPLIT_BYTES // (1024 * 1024))

    if use_py7zr:
        backend_label = "py7zr  LZMA2 level-3  multi-volume"
    elif cli_binary:
        backend_label = f"7z CLI [{cli_binary}]  multi-volume"
    else:
        backend_label = "ZIP fallback  multi-part"
        warn("Neither py7zr nor 7z CLI found — falling back to ZIP.\n  pip install py7zr multivolumefile")

    workers = min(ARCHIVE_WORKERS, len(groups)) if groups else 1
    group_idx: Dict[Path, int] = {folder: i+1 for i, (folder, _) in enumerate(groups)}

    result      : Dict[Path, List[PartInfo]] = {}
    result_lock = threading.Lock()
    abort_event = threading.Event()
    status_lock = threading.Lock()
    completed_count = [0]

    status_prog = Progress(
        SpinnerColumn("dots12", style="bold bright_cyan"),
        TextColumn("[bold bright_cyan]{task.description}[/bold bright_cyan]"),
        console=C, transient=False,
    )
    main_prog = Progress(
        BarColumn(bar_width=None, style="grey23",
                  complete_style="bold bright_cyan", finished_style="bold bright_green",
                  pulse_style="bright_cyan"),
        TaskProgressColumn(style="bold white"),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )

    info(
        f"Back-end : [bold bright_cyan]{backend_label}[/bold bright_cyan]  |  "
        f"Volume: {fmt_size(SPLIT_BYTES)}  |  "
        f"Workers: [bold]{workers}[/bold] / {len(groups)} group(s)"
    )
    C.print()

    try:
        with Live(
            RichGroup(
                Panel(
                    RichGroup(status_prog, main_prog),
                    border_style="bright_cyan",
                    title=f"[bold bright_cyan]  Archiving {len(groups)} groups  [/bold bright_cyan]",
                    padding=(0, 1),
                ),
            ),
            console=C,
            refresh_per_second=15,
            transient=False,
        ):
            status_task = status_prog.add_task("Initialising …", total=None)
            main_task   = main_prog.add_task("Overall progress", total=total_bytes)

            def _advance(n_bytes: int) -> None:
                main_prog.advance(main_task, n_bytes)

            def _worker(folder: Path, infs) -> None:
                if abort_event.is_set():
                    return
                rep_inf, rep_d = max(infs, key=lambda x: len(x[1].get("hwids", [])))
                stem  = _archive_stem(pack, rep_inf, rep_d)
                idx   = group_idx[folder]
                group_dest = dest_dir / f"g{idx:04d}"
                group_dest.mkdir(parents=True, exist_ok=True)
                dest_stem  = group_dest / stem

                files = all_files_by_group[folder]
                if not files:
                    with result_lock:
                        result[folder] = []
                    return

                status_prog.update(status_task, description=f"  ◈  Group {idx}/{len(groups)}  ·  {stem}")

                try:
                    parts = _archive_group(
                        files=files, folder=folder, dest_stem=dest_stem,
                        use_py7zr=use_py7zr, cli_binary=cli_binary,
                        volume_mb=volume_mb, on_progress=_advance,
                    )
                    with result_lock:
                        result[folder] = parts
                    with status_lock:
                        completed_count[0] += 1
                except KeyboardInterrupt:
                    abort_event.set()
                    raise
                except Exception as exc:
                    abort_event.set()
                    err(f"Fatal archive error for group {idx}: {exc}")
                    raise

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_worker, folder, infs): folder
                    for folder, infs in groups
                }
                try:
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except KeyboardInterrupt:
                            abort_event.set()
                            raise
                        except Exception:
                            abort_event.set()
                            for f in futures:
                                f.cancel()
                            raise
                except KeyboardInterrupt:
                    abort_event.set()
                    for f in futures:
                        f.cancel()
                    raise

            status_prog.update(
                status_task,
                description=f"  ✓  All {len(groups)} groups archived",
            )

    except KeyboardInterrupt:
        with result_lock:
            for pis in result.values():
                for pi in pis:
                    try:
                        if pi.path.exists():
                            pi.path.unlink()
                    except Exception:
                        pass
        raise

    total_verified = sum(len(v) for v in result.values())
    C.print()
    return result, total_verified, total_bytes


# ── REST API error diagnosis ──────────────────────────────────────────────────
def _is_repo_quota_error(status: int, resp: Dict) -> bool:
    """
    True when a 403 is specifically GitHub's repository-size-quota error
    (e.g. "Repository is above its size quota"), as opposed to a missing
    'repo' scope or a rate-limited 403 — those three need very different
    recovery (switch repo / fix token / back off & retry), so they must
    never be collapsed into one generic "permissions" message.
    """
    if status != 403:
        return False
    msg = (resp.get("message") or "").lower()
    return "quota" in msg


def diagnose_api_error(status: int, resp: Dict) -> str:
    msg = resp.get("message", "")
    if status == 401:
        return "GitHub token is invalid or expired. Regenerate at https://github.com/settings/tokens"
    if status == 403:
        msg_lower = msg.lower()
        if "rate limit" in msg_lower:
            return "GitHub API rate limit exceeded. Wait ~1 hour or use an authenticated token."
        if _is_repo_quota_error(status, resp):
            return f"Repository is above its size quota: {msg or 'no further detail from GitHub'}."
        return "Token lacks required permissions (needs 'repo' scope with push access)."
    if status == 404:
        return f"Resource not found (HTTP 404). Repo: {REPO_OWNER}/{REPO_NAME}"
    if status == 409:
        return "Merge conflict on push (HTTP 409). Re-run the script to re-sync."
    if status == 422:
        errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
        return f"Validation error (HTTP 422): {errs or msg}"
    if status == 0:
        return f"Network error: {msg}"
    return f"Unexpected API error (HTTP {status}): {msg}"



# ── GitHub REST — manifest sync ───────────────────────────────────────────────
def _load_remote_index(workspace: Path) -> Optional[Dict]:
    result = _download_file_from_github(INDEX_FILE_NAME, workspace / INDEX_FILE_NAME)
    if result != "ok":
        return None
    try:
        return json.loads((workspace / INDEX_FILE_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_remote_manifest_dir() -> Optional[List[str]]:
    """Ground-truth listing of every manifest file that actually exists in
    MANIFEST_DIR/ on GitHub right now, straight from the Contents API.

    This is deliberately independent of two things that can each go stale
    or unavailable on their own:
      * INDEX_FILE_NAME (drivedex_index.json) — may fail to download (e.g.
        rate-limited: exactly the "429: Too Many Requests" body that has
        been seen written in place of real index content), may fail to
        parse, or may simply be out of date with what's really committed.
      * _CLASS_TO_TYPE — a hardcoded map of *known* categories baked into
        this script at the time it was written. Any category that only
        exists because of repo history (renamed/retired hardware classes,
        categories introduced by an older script version, etc.) is real
        data sitting in the repo but has NO entry in that map.

    Falling back to "just re-derive the category list from _CLASS_TO_TYPE"
    (the old behavior) silently drops any category not in that hardcoded
    set whenever the index can't be read. A later save_manifest() for that
    category then finds zero local shards, assumes it's brand new, and
    writes a fresh shard 1 — which overwrites/deletes every real entry
    that category had on GitHub. Asking GitHub directly what's actually in
    manifests/ closes that hole: it can't miss a category, known or not,
    because it isn't derived from any list at all, only from what's really
    there.

    Returns None if the listing itself could not be retrieved after every
    retry (e.g. still rate-limited) — callers must NOT treat that the same
    as "no files exist"; that's the exact mistake this function exists to
    avoid. Returns [] if MANIFEST_DIR/ genuinely doesn't exist yet (a
    brand-new repo), which is a legitimate empty result, not a failure.
    """
    st, resp = _api_with_retry(
        "GET",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{MANIFEST_DIR}?ref={GH_BRANCH}",
        max_attempts=5, backoff_base=4.0, label="list manifests/",
    )
    if st == 404:
        return []
    if st != 200 or not isinstance(resp, list):
        warn(f"Could not list {MANIFEST_DIR}/ on GitHub (HTTP {st}) "
             f"— {resp.get('message', '') if isinstance(resp, dict) else ''}".rstrip())
        return None
    return sorted(
        f"{MANIFEST_DIR}/{item['name']}"
        for item in resp
        if isinstance(item, dict) and item.get("type") == "file"
        and item.get("name", "").endswith(".json")
    )


# Remote branch SHA as of our last fully-successful github_pull_manifest().
# github_pull_rebase() is called twice in a normal run — once from
# setup_workspace() before any local scanning has happened, and again from
# _dd_upload_candidates() right before the commit, purely so the upload is
# built on top of the freshest manifest content rather than a copy that
# might be stale (e.g. another contributor pushed in between). But nothing
# about the local driver scan / HWID check in between those two calls can
# possibly change what's sitting on GitHub, so the *second* call is only
# ever doing real work if something else pushed meanwhile. Recording the
# SHA we synced to lets the next call check that cheaply (one small GET)
# instead of unconditionally re-downloading the index + every category
# shard + installer chain + README again. See github_pull_manifest() below.
_LAST_MANIFEST_PULL_SHA: Optional[str] = None


def _get_branch_head_sha() -> Optional[str]:
    """Cheapest possible check of whether REPO_OWNER/REPO_NAME has moved at
    all: a single small GET of the branch ref, no tree/blob traffic.

    Returns None (not "unchanged") on any failure — callers must treat
    "couldn't confirm" as "assume it changed, pull for real", never as
    "safe to skip". Skipping should only ever be a proven, not assumed, safe.
    """
    st, resp = _api_with_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/ref/heads/{GH_BRANCH}",
        max_attempts=3, backoff_base=2.0, label="HEAD ref (freshness check)",
    )
    if st == 200 and isinstance(resp, dict):
        sha = resp.get("object", {}).get("sha")
        return sha if isinstance(sha, str) else None
    return None


# Matches ul_workers (blob upload concurrency) — pulls are pure network I/O
# so threads give full overlap despite the GIL.
# Lowered from 12 -> 6: a burst of a dozen simultaneous requests to the same
# raw.githubusercontent.com host is exactly the kind of traffic shape that
# gets a session flagged as "scraping" by GitHub's own edge or by a
# network-level content filter in front of it (both have been observed
# serving a look-alike 429 block page with a 200 status for this reason).
# Still plenty of overlap for pure network I/O; just less bursty.
PULL_WORKERS = 6


def github_pull_manifest(workspace: Path) -> None:
    global _LAST_MANIFEST_PULL_SHA

    # ── freshness short-circuit ──────────────────────────────────────────────
    # One cheap GET to prove (not assume) nothing changed on GitHub since our
    # last successful full pull in this run. If it's unchanged, the local
    # copy on disk is already exactly what a re-download would produce, so
    # skip straight to returning instead of re-fetching the index + every
    # category shard + installer chain + README all over again.
    head_sha = _get_branch_head_sha()
    if (
        head_sha is not None
        and _LAST_MANIFEST_PULL_SHA is not None
        and head_sha == _LAST_MANIFEST_PULL_SHA
        and _all_category_manifest_rels(workspace)
    ):
        ok(f"{REPO_NAME}@{GH_BRANCH} unchanged since last sync "
           f"([bold]{head_sha[:7]}[/bold]) — reusing local manifest, skipping re-download.")
        return
    # head_sha is None (couldn't confirm), or this is the first pull of the
    # run, or the remote genuinely moved -> fall through to a real full pull.

    # Shard names that failed to download after every retry — these are
    # filenames GitHub told us (via the index, or via a prior successful
    # probe) actually exist, so silently proceeding without them would give
    # save_manifest() / save_installer_manifest() a false picture of what's
    # already been split, which is exactly what produces corrupted re-splits
    # (oversized shards, orphaned remote shards, broken next_shard chains).
    #
    # Every shard is re-downloaded on every pull, including older/"sealed"
    # ones — a shard can still be amended by a later commit (manual fix,
    # correction, history rewrite, etc.), not just appended to, so treating
    # it as immutable and skipping it would risk silently working off a
    # stale local copy.
    #
    # Category manifest chains, the installer manifest chain, and README.md
    # are all independent of each other — nothing about one affects what
    # gets fetched for another — so each runs as its own job on a thread
    # pool instead of the whole thing running one HTTP request at a time.
    # The sequential *probing* within a single chain (shard 2, then 3, then
    # 4… until not_found) is unchanged and still happens in order, since
    # each probe's existence genuinely depends on the previous one having
    # been found; only independent chains were made to overlap.
    failed: List[str] = []
    _prog_lock = threading.Lock()

    dl_prog = Progress(
        SpinnerColumn("dots12", style="bold bright_cyan"),
        TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
        BarColumn(bar_width=None, style="grey23", complete_style="bold bright_cyan",
                  finished_style="bold bright_green"),
        TaskProgressColumn(style="bold white"),
        DimMofNColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )

    with dl_prog:
        # Total is only an estimate until every shard is actually discovered
        # (shard counts beyond the first per category/installer are only
        # known once we probe and hit "not_found"), so it's bumped upward
        # as more work is discovered — the ETA self-corrects as it goes,
        # the same way a download manager refines "time remaining" once it
        # knows the true file count. Bumped from multiple threads now, so
        # both the counter and the progress-bar call are lock-protected.
        total_units = 1  # the remote index file itself
        dl_task = dl_prog.add_task("Downloading manifests from GitHub", total=total_units)

        def _bump_total(extra: int = 1) -> None:
            nonlocal total_units
            with _prog_lock:
                total_units += extra
                dl_prog.update(dl_task, total=total_units)

        def _pull(repo_rel: str) -> str:
            with _prog_lock:
                dl_prog.update(dl_task, description=f"  ◈  {repo_rel}")
            result = _download_file_from_github(repo_rel, workspace / repo_rel)
            with _prog_lock:
                dl_prog.advance(dl_task, 1)
            return result

        dl_prog.update(dl_task, description=f"  ◈  {INDEX_FILE_NAME}")
        remote_index = _load_remote_index(workspace)
        dl_prog.advance(dl_task, 1)

        # Each job is self-contained and returns the list of shard names
        # (if any) it failed to fetch — results are merged from these
        # return values rather than a shared mutable list, so no locking
        # is needed for correctness there.
        jobs: List[Callable[[], List[str]]] = []

        if remote_index and remote_index.get("manifest_shards"):
            shard_list = remote_index["manifest_shards"]
            _bump_total(len(shard_list))

            def _make_shard_job(sname: str) -> Callable[[], List[str]]:
                def _job() -> List[str]:
                    return [sname] if _pull(sname) == "error" else []
                return _job

            for s in shard_list:
                jobs.append(_make_shard_job(s["filename"]))
        else:
            # index.json couldn't be downloaded or parsed (rate-limited,
            # missing, or corrupted — e.g. a 429 response body landing
            # where JSON should be). The OLD behavior here re-derived the
            # category list from the hardcoded _CLASS_TO_TYPE map, which
            # silently drops any category that only exists in repo history
            # and isn't in that map — the exact hole that lets a later
            # save_manifest() start a fresh empty shard 1 for that category
            # and overwrite/delete its real data on push. Ask GitHub what's
            # actually in manifests/ instead: that's ground truth and can't
            # miss a category the way a hardcoded list can.
            warn(f"{INDEX_FILE_NAME} unavailable — listing {MANIFEST_DIR}/ on "
                 f"GitHub directly instead of guessing from the known-category list.")
            remote_files = _list_remote_manifest_dir()
            if remote_files is None:
                # The directory listing failed too (still rate-limited /
                # unreachable after every retry). Continuing here is exactly
                # what risks silently working from an incomplete category
                # set — refuse instead, same philosophy as the "failed"
                # check below for individual shards.
                raise RuntimeError(
                    f"Could not read {INDEX_FILE_NAME} or list {MANIFEST_DIR}/ on GitHub "
                    "(rate-limited or unreachable) — refusing to guess which category "
                    "manifests exist, since that risks silently overwriting or deleting "
                    "real data. Wait a bit for the rate limit to clear and re-run."
                )

            # The installer manifest + its numbered shards are handled
            # separately by _installer_job() below — exclude them here so
            # they're queued (and counted) exactly once, not twice.
            remote_files = [
                f for f in remote_files
                if not f.startswith(f"{_INSTALLER_MANIFEST_STEM}.manifest")
            ]

            # Union with the hardcoded list too, purely as a safety net: a
            # category that's known in code but has no file yet just comes
            # back "not_found" from _pull(), which is harmless. The
            # directory listing is what guarantees nothing real is missed.
            known_cats = sorted(set(_CLASS_TO_TYPE.values()) | {"Other"})
            all_targets = sorted(set(remote_files) | {_category_manifest_rel(c) for c in known_cats})
            _bump_total(len(all_targets))

            def _make_listed_file_job(fname: str) -> Callable[[], List[str]]:
                def _job() -> List[str]:
                    return [fname] if _pull(fname) == "error" else []
                return _job

            for fname in all_targets:
                jobs.append(_make_listed_file_job(fname))

        def _installer_job() -> List[str]:
            local_failed: List[str] = []
            r = _pull(INSTALLER_MANIFEST_REL)
            if r == "error":
                local_failed.append(INSTALLER_MANIFEST_REL)
            if r == "ok":
                for idx in range(2, 50):
                    shard_r = _installer_shard_name(idx)
                    _bump_total(1)
                    sr = _pull(shard_r)
                    if sr == "not_found":
                        break
                    if sr == "error":
                        local_failed.append(shard_r)
            return local_failed

        _bump_total(1)
        jobs.append(_installer_job)

        def _readme_job() -> List[str]:
            _pull("README.md")   # failure here was never tracked; unchanged
            return []

        _bump_total(1)
        jobs.append(_readme_job)

        with concurrent.futures.ThreadPoolExecutor(max_workers=PULL_WORKERS) as pool:
            futs = [pool.submit(job) for job in jobs]
            try:
                for fut in concurrent.futures.as_completed(futs):
                    failed.extend(fut.result())
            except KeyboardInterrupt:
                for fut in futs:
                    fut.cancel()
                raise

        # Validation only touches locally-downloaded category manifests, so
        # it's unaffected by running after the installer/README jobs too —
        # it just needs every category chain above to have finished, which
        # the pool wait already guarantees.
        for cat_rel in _all_category_manifest_rels(workspace):
            local = workspace / cat_rel
            if not local.exists():
                continue
            raw_bytes = local.read_bytes()
            if not raw_bytes or raw_bytes[:1] in (b"<", b"\n", b"\r\n"):
                local.unlink(missing_ok=True)
                warn(f"{escape(cat_rel)} contained non-JSON content — removed.")
                continue
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
                if "drivers" not in data:
                    raise ValueError("Missing 'drivers' key")
            except (json.JSONDecodeError, ValueError) as exc:
                local.unlink(missing_ok=True)
                warn(f"{escape(cat_rel)} is not valid JSON: {escape(str(exc))} — removed.")

        dl_prog.update(dl_task, description="  ✓  Manifest sync complete")

    if failed:
        raise RuntimeError(
            "Could not download the following manifest shard(s) after retries — "
            "refusing to continue with an incomplete local mirror, since that "
            "would corrupt future splits: " + ", ".join(escape(f) for f in failed)
        )

    # Only remember this as "known-fresh" once every shard genuinely landed
    # on disk with no failures. Re-check the HEAD SHA now, right after the
    # download finished, rather than reusing the value read at the top of
    # this function -- if a push landed on GitHub while the download was in
    # progress, the pre-download SHA would no longer describe what's really
    # on disk, and a later same-run call could wrongly trust a skip against
    # a mix of old and new state. A None result (freshness check failed) is
    # fine: it just means the next call does a real pull again instead of
    # wrongly trusting a skip.
    _LAST_MANIFEST_PULL_SHA = _get_branch_head_sha()

    n_cats = len(_all_category_manifest_rels(workspace))
    if n_cats:
        n_shards = len(_manifest_shard_paths(workspace))
        ok(f"Synced {n_cats} category manifest(s) from GitHub  ({n_shards} total shard(s)).")
    else:
        ok("No category manifests on GitHub yet — starting fresh.")


_LFS_POINTER_SIG = b"version https://git-lfs.github.com/spec/"


def _parse_lfs_pointer(data: bytes) -> Optional[Dict[str, str]]:
    if not data.lstrip().startswith(_LFS_POINTER_SIG):
        return None
    info_d: Dict[str, str] = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("oid sha256:"):
            info_d["oid"] = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            info_d["size"] = line.split(" ", 1)[1].strip()
    return info_d if "oid" in info_d and "size" in info_d else None


def _download_via_lfs_batch(repo_rel_path: str, oid: str, size: int, local_dest: Path) -> str:
    lfs_url = LFS_BATCH_URL
    payload = json.dumps({
        "operation": "download", "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
    }).encode("utf-8")
    headers: Dict[str, str] = {
        "Content-Type": "application/vnd.git-lfs+json",
        "Accept"      : "application/vnd.git-lfs+json",
    }
    _tok = _load_token()
    if _tok:
        headers["Authorization"] = f"Bearer {_tok}"
    req = urllib.request.Request(lfs_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        warn(f"LFS batch request failed for {repo_rel_path}: {exc}")
        return "error"
    objects = batch.get("objects", [])
    if not objects:
        return "error"
    obj = objects[0]
    if "error" in obj:
        return "not_found" if obj["error"].get("code") == 404 else "error"
    download_href = obj.get("actions", {}).get("download", {}).get("href", "")
    if not download_href:
        return "error"
    dl_headers = dict(obj.get("actions", {}).get("download", {}).get("header", {}))
    dl_req = urllib.request.Request(download_href, headers=dl_headers)
    try:
        with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
            content = dl_resp.read()
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(content)
        return "ok"
    except Exception as exc:
        warn(f"LFS object download failed for {repo_rel_path}: {exc}")
        return "error"


# Phrases that show up in a "soft" rate-limit / block response — one that
# arrives with a normal 2xx status instead of a real HTTP 429/503, so
# urllib never raises for it. Seen in the wild from two different sources:
# GitHub's own Fastly edge occasionally serving an abuse-detection page for
# raw.githubusercontent.com with a 200, and (more often) a network-level
# intermediary — corporate proxy, campus firewall, antivirus web-shield —
# that terminates the request itself and hands back its own "looks like
# scraping" block page while reporting 200 regardless of what GitHub would
# have said. Either way the fix is the same: treat it as transient and
# retry, not as corrupt content to give up on.
_SOFT_RATE_LIMIT_SIGNATURES = (
    "too many requests",
    "rate limit exceeded",
    "secondary rate limit",
    "abuse detection",
    "please wait a few minutes",
    "you have exceeded a rate limit",
)


def _looks_like_rate_limit_body(snippet_lower: str) -> bool:
    return any(sig in snippet_lower for sig in _SOFT_RATE_LIMIT_SIGNATURES)


def _fetch_via_git_blob_api(
    repo_rel_path: str, max_attempts: int = 4,
) -> Optional[bytes]:
    """
    Last-resort fetch of a single file's bytes through the Git Data (Blob)
    API instead of raw.githubusercontent.com.

    NOTE / history: the first version of this fallback requested the
    Contents API with the "raw" media type
    (Accept: application/vnd.github.raw+json) on the theory that
    api.github.com is a separate backend from raw.githubusercontent.com.
    In practice it isn't, for raw content specifically -- that request
    still resolves to the same raw-content serving path under the hood, so
    it reproduced the exact same block-page body instead of real content.
    The Git Blob API is the one part of the GitHub REST surface that is
    genuinely different: it returns base64-encoded blob content inline in
    a normal JSON response from api.github.com's git-data backend, with no
    redirect to (or dependency on) raw-content serving at all, and it
    supports blobs up to 100 MB -- comfortably above this project's
    MANIFEST_SIZE_LIMIT.

    Two-step process, both legs going through the already-proven
    _api_with_retry (same backoff/Retry-After handling used everywhere
    else in this script):
      1. GET /contents/{path} to resolve the current blob `sha` for this
         path (works regardless of file size; only the base64 `content`
         field is capped at 1 MB, `sha` always comes back).
      2. GET /git/blobs/{sha} to fetch the actual base64 content by sha.

    Only called after the normal raw-CDN path in
    _download_file_from_github has already exhausted every retry.
    """
    st, resp = _api_with_retry(
        "GET",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_rel_path}?ref={GH_BRANCH}",
        max_attempts=max_attempts, backoff_base=4.0,
        label=f"blob-sha {repo_rel_path}",
    )
    if st == 404:
        return None
    if st != 200 or not isinstance(resp, dict) or not resp.get("sha"):
        warn(f"Could not resolve a blob sha for {repo_rel_path} via the "
             f"Contents API (HTTP {st}) — Git Blob API fallback unavailable.")
        return None
    sha = resp["sha"]

    st2, resp2 = _api_with_retry(
        "GET",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs/{sha}",
        max_attempts=max_attempts, backoff_base=4.0,
        label=f"blob-fetch {repo_rel_path}",
    )
    if st2 != 200 or not isinstance(resp2, dict) or "content" not in resp2:
        warn(f"Git Blob API fetch failed for {repo_rel_path} (sha {sha[:8]}…, HTTP {st2}).")
        return None
    try:
        # GitHub base64-encodes blob content with embedded newlines; b64decode
        # tolerates those fine, but strip defensively in case of odd wrapping.
        return base64.b64decode(resp2["content"].replace("\n", ""))
    except Exception as exc:
        warn(f"Could not decode blob content for {repo_rel_path}: {exc}")
        return None


def _download_file_from_github(
    repo_rel_path: str, local_dest: Path,
    max_attempts: int = 6, timeout: int = 90,
) -> str:
    raw_url = (
        f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
        f"/{GH_BRANCH}/{repo_rel_path}"
    )
    headers: Dict[str, str] = {"User-Agent": HTTP_USER_AGENT}
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    # Retry with backoff on transient network errors/timeouts AND on a
    # "soft" rate limit (see _looks_like_rate_limit_body above) — large
    # manifest shards can be several MB and a single flaky connection or
    # one blocked attempt shouldn't cause them to be silently skipped — a
    # skipped-but-existing shard is what corrupts the local workspace's
    # view of what's already been split on GitHub, which is what produces
    # bad re-splits later.
    data: Optional[bytes] = None
    last_exc: Optional[Exception] = None
    last_soft_block: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        wait: Optional[float] = None
        try:
            req = urllib.request.Request(raw_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                candidate = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return "not_found"
            last_exc = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                wait = float(retry_after) + 1.0 if retry_after else None
            except (TypeError, ValueError):
                wait = None
        except Exception as exc:
            last_exc = exc
        else:
            # Request "succeeded" (2xx) — but a 2xx status doesn't guarantee
            # the body is actually the file we asked for. Check for a
            # soft-blocked / rate-limited body before trusting it; anything
            # else is handed on to the existing content checks below
            # unchanged.
            probe = candidate.lstrip()
            snippet = probe[:120].decode("utf-8", errors="replace")
            if probe and _looks_like_rate_limit_body(snippet.lower()):
                last_soft_block = snippet.strip()
                last_exc = None
            else:
                data = candidate
                break

        if attempt < max_attempts:
            time.sleep(wait if wait is not None else min(2 ** attempt, 20))   # 2s, 4s, 8s, ...

    if data is None:
        if last_soft_block is not None:
            warn(
                f"{repo_rel_path} kept getting a rate-limit/block response instead of "
                f"real content after {max_attempts} attempt(s) ({last_soft_block!r}) "
                f"— trying the Git Blob API before giving up …"
            )
        else:
            warn(
                f"Could not download {repo_rel_path} after {max_attempts} attempt(s) via "
                f"raw.githubusercontent.com ({last_exc}) — trying the Git Blob API "
                f"before giving up …"
            )
        data = _fetch_via_git_blob_api(repo_rel_path)
        if data is None:
            return "error"
        ok(f"{repo_rel_path} recovered via the Git Blob API fallback.")

    lfs_info = _parse_lfs_pointer(data)
    if lfs_info is not None:
        return _download_via_lfs_batch(
            repo_rel_path, oid=lfs_info["oid"], size=int(lfs_info["size"]), local_dest=local_dest
        )
    probe = data.lstrip()
    if not probe:
        warn(f"Empty response for {repo_rel_path} — skipping.")
        return "error"
    if repo_rel_path.endswith(".json") and probe[:1] not in (b"{", b"["):
        snippet = probe[:80].decode("utf-8", errors="replace").rstrip()
        warn(f"Non-JSON response for {repo_rel_path}: {snippet!r} — skipping.")
        return "error"
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    local_dest.write_bytes(data)
    return "ok"


def github_pull_rebase(workspace: Path) -> bool:
    try:
        github_pull_manifest(workspace)
        return True
    except RuntimeError as exc:
        C.print()
        err(str(exc))
        C.print()
        die("Aborting to protect your existing manifest on GitHub.",
            fix="Fix the connection/token issue above and re-run.")
        return False
    except KeyboardInterrupt:
        C.print()
        warn("Sync interrupted.")
        return False
    except Exception as exc:
        err(f"GitHub sync failed: {exc}")
        return False


# ── Drivers-repo quota detection ──────────────────────────────────────────────
# Set by _upload_as_blob / _upload_lfs_object / _create_lfs_pointer_blob the
# moment a repo-quota 403 is seen, so the push-level caller
# (_push_driver_files_with_fallback) can tell "the repo is full" apart from
# any other failure and switch repos instead of just giving up.
_QUOTA_ERROR_DETECTED = threading.Event()
_QUOTA_ERROR_REPO: List[Optional[str]] = [None, None]   # [owner, name]
_QUOTA_ERROR_LOCK = threading.Lock()


def _mark_quota_error(owner: str, name: str) -> None:
    with _QUOTA_ERROR_LOCK:
        _QUOTA_ERROR_REPO[0], _QUOTA_ERROR_REPO[1] = owner, name
    _QUOTA_ERROR_DETECTED.set()
    if owner == DRIVERS_REPO_OWNER:
        _mark_repo_spent(name)


def _clear_quota_error() -> None:
    _QUOTA_ERROR_DETECTED.clear()
    with _QUOTA_ERROR_LOCK:
        _QUOTA_ERROR_REPO[0], _QUOTA_ERROR_REPO[1] = None, None


# ── Telegram notifications ────────────────────────────────────────────────────
# Used only as a last resort, when the next repo in the drivers/drivers_1/
# drivers_2/... sequence doesn't exist yet and a human needs to create it.
# No credentials are bundled — telegram_bot_token / telegram_chat_id are
# user-supplied, following the exact same config-file-then-env-var pattern
# as github_token (see _load_token()).
def _load_telegram_config() -> Tuple[str, str]:
    """Load Telegram bot credentials: drivedex_config.json first, then the
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables. Either or
    both may come back empty if not configured."""
    bot_token = ""
    chat_id   = ""
    try:
        if _CONFIG_FILE.exists():
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            bot_token = (data.get("telegram_bot_token") or "").strip()
            chat_id   = (data.get("telegram_chat_id") or "").strip()
    except Exception:
        pass
    if not bot_token:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bot_token, chat_id


def _print_telegram_setup_guide() -> None:
    lines = [
        "[bold yellow]How to set up Telegram notifications[/bold yellow]\n",
        "[dim]1.[/dim]  Message [bold]@BotFather[/bold] on Telegram → [white]/newbot[/white] → copy the bot token.",
        "[dim]2.[/dim]  Send your new bot any message once, so it's allowed to reply to you.",
        "[dim]3.[/dim]  Get your chat ID: open  "
        "[cyan]https://api.telegram.org/bot<TOKEN>/getUpdates[/cyan]  "
        "in a browser after step 2 and read the numeric \"chat\":{\"id\": …} value.",
        "[dim]4.[/dim]  Add both values to [dim]drivedex_config.json[/dim] next to the script:\n",
        "     [bold yellow]{ \"telegram_bot_token\": \"123456:ABC-YourTokenHere\", "
        "\"telegram_chat_id\": \"123456789\" }[/bold yellow]",
        "     [dim]Or set env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID instead.[/dim]\n",
        "[dim]5.[/dim]  Re-run the script.",
    ]
    C.print(Panel("\n".join(lines), border_style="yellow",
                  title="[bold yellow]  Telegram Notification Setup  [/bold yellow]", padding=(1, 3)))


def _send_telegram_message(text: str) -> bool:
    """Send a single message via the Telegram Bot API. Returns True on success,
    False on any failure (including missing/unconfigured credentials)."""
    bot_token, chat_id = _load_telegram_config()
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("ok"))
    except Exception as exc:
        warn(f"Telegram notification failed: {exc}")
        return False


def _notify_telegram_new_repo_needed(new_repo_name: str) -> bool:
    """
    Escalation of last resort: the next repo in sequence isn't accessible
    yet. Sends up to 10 Telegram reminders, 30 s apart,
    asking the user to create `new_repo_name` — checking GitHub after every
    reminder so it doesn't wait longer than necessary once the repo shows
    up (10 reminders x 30 s = 5 min ceiling). Switches to the new repo and
    returns True the moment it's accessible; returns False (after reporting
    the failure, including via Telegram) if it's still missing at the end.
    """
    bot_token, chat_id = _load_telegram_config()
    if not bot_token or not chat_id:
        err("Telegram is not configured — cannot send the repo-creation reminder.")
        _print_telegram_setup_guide()
        C.print()
        hint(
            f"Manual step: create an empty repo named "
            f"[bold]{DRIVERS_REPO_OWNER}/{new_repo_name}[/bold] "
            f"(with an initial commit on '{GH_BRANCH}'), then re-run the script."
        )
        return False

    C.print()
    warn(
        f"All known drivers repos are full/inaccessible. Notifying you via "
        f"Telegram to create [bold]{DRIVERS_REPO_OWNER}/{new_repo_name}[/bold] …"
    )
    reminder_text = (
        f"DriverDex Builder: {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} is full.\n"
        f"Please create a new empty GitHub repo named '{new_repo_name}' "
        f"(owner: {DRIVERS_REPO_OWNER}, with an initial commit on branch "
        f"'{GH_BRANCH}') so the upload can continue.\n"
        f"Checking automatically every 30s…"
    )

    N_REMINDERS   = 10
    REMINDER_WAIT = 30.0

    for i in range(1, N_REMINDERS + 1):
        if _send_telegram_message(f"[{i}/{N_REMINDERS}] {reminder_text}"):
            info(f"Telegram reminder {i}/{N_REMINDERS} sent.")
        else:
            warn(f"Telegram reminder {i}/{N_REMINDERS} failed to send.")

        if _repo_is_accessible(DRIVERS_REPO_OWNER, new_repo_name):
            ok(f"{DRIVERS_REPO_OWNER}/{new_repo_name} is now accessible!")
            _send_telegram_message(
                f"DriverDex Builder: {new_repo_name} detected — resuming upload."
            )
            _switch_drivers_repo(new_repo_name)
            return True

        if i < N_REMINDERS:
            try:
                time.sleep(REMINDER_WAIT)
            except KeyboardInterrupt:
                raise

    # Final check in case it appeared right after the last reminder.
    if _repo_is_accessible(DRIVERS_REPO_OWNER, new_repo_name):
        ok(f"{DRIVERS_REPO_OWNER}/{new_repo_name} is now accessible!")
        _send_telegram_message(f"DriverDex Builder: {new_repo_name} detected — resuming upload.")
        _switch_drivers_repo(new_repo_name)
        return True

    _send_telegram_message(
        f"DriverDex Builder: {new_repo_name} still doesn't exist after "
        f"{N_REMINDERS} reminders — giving up. Re-run the script once it's created."
    )
    err(
        f"{DRIVERS_REPO_OWNER}/{new_repo_name} still isn't accessible after "
        f"{N_REMINDERS} reminders ({N_REMINDERS * REMINDER_WAIT / 60:.0f} min). Giving up."
    )
    return False


def _advance_drivers_repo_or_notify() -> bool:
    """
    Called after the ACTIVE drivers repo hits its size quota. Probes the
    next repo in sequence (drivers -> drivers_1 -> drivers_2 -> ...) by
    index rather than a hardcoded list. Any index already recorded in
    _SPENT_DRIVERS_REPOS (this run or a prior one) is skipped with zero
    GitHub calls — that repo is already known full, so re-checking it would
    just burn an API call for an answer we already have. If you pre-created
    several repos ahead of time, each quota hit just advances one step and
    switches with no Telegram involved. The first time the next un-spent
    index in sequence doesn't exist yet, it escalates to the user via
    Telegram to create exactly that one repo, then resumes automatically
    once it appears.
    Returns True with the new repo already switched-to + persisted if the
    caller should retry the push; False if it should give up entirely.
    """
    cur_idx = _drivers_repo_index_for_name(DRIVERS_REPO_NAME)
    if cur_idx is None:
        cur_idx = -1   # active repo doesn't match the naming convention — probe from the top

    nxt_idx = cur_idx + 1
    nxt = _drivers_repo_name_for_index(nxt_idx)
    while nxt in _SPENT_DRIVERS_REPOS:
        info(f"[dim]{DRIVERS_REPO_OWNER}/{nxt} already known to be full — skipping (no API call needed).[/dim]")
        nxt_idx += 1
        nxt = _drivers_repo_name_for_index(nxt_idx)

    info(f"Checking next repo in sequence [bold]{DRIVERS_REPO_OWNER}/{nxt}[/bold] …")
    if _repo_is_accessible(DRIVERS_REPO_OWNER, nxt):
        _switch_drivers_repo(nxt)
        ok(f"Switched to fallback repo: [bold]{DRIVERS_REPO_OWNER}/{nxt}[/bold]")
        return True
    warn(f"{DRIVERS_REPO_OWNER}/{nxt} isn't accessible yet.")

    return _notify_telegram_new_repo_needed(nxt)


# ── Upload mode selector ──────────────────────────────────────────────────────
UPLOAD_MODE_PARALLEL  = "parallel"
UPLOAD_MODE_PIPELINE  = "pipeline"


# ── GitHub commit + push (two-repo dispatcher) ────────────────────────────────
def github_commit_push(
    workspace            : Path,
    commit_msg           : str,
    upload_mode          : str = UPLOAD_MODE_PARALLEL,
    skip_archive_upload  : bool = False,
) -> bool:
    """
    Split the workspace across the two repos and commit each independently:

      • drivers/**              → github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}
                                  committed at the repo ROOT (the leading
                                  "drivers/" staging prefix is stripped, so an
                                  archive lands at  …/<Type>/DP_<Pack>/…  and NOT
                                  at  …/drivers/<Type>/… ).
      • manifests/**, README.md → github.com/{REPO_OWNER}/{REPO_NAME}  (driverdex)
        drivedex_index.json,
        everything else

    Returns True only if every repo that actually had files to push succeeded.
    """
    all_files: List[Tuple[str, Path]] = []
    for fpath in sorted(workspace.rglob("*")):
        if not fpath.is_file():
            continue
        try:
            rel = fpath.relative_to(workspace)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        top = parts[0]
        if top in WORKSPACE_SCRATCH_DIRS or top.startswith(WORKSPACE_SCRATCH_NAME_PREFIXES):
            continue
        # Same check one level down, in case a scratch dir sits inside a
        # committed parent (e.g. DRIVERS_DIR/_staging_DP_<pack> left behind
        # by an interrupted run) rather than directly under the workspace.
        if any(p in WORKSPACE_SCRATCH_DIRS or p.startswith(WORKSPACE_SCRATCH_NAME_PREFIXES)
               for p in parts[1:-1]):
            continue
        repo_rel = str(rel).replace("\\", "/")
        all_files.append((repo_rel, fpath))

    if not all_files:
        warn("Workspace is empty — nothing to upload.")
        return True

    driver_files: List[Tuple[str, Path]] = []
    dex_files   : List[Tuple[str, Path]] = []
    _drv_prefix = f"{DRIVERS_DIR}/"
    for repo_rel, fpath in all_files:
        if repo_rel.startswith(_drv_prefix):
            # Strip "drivers/" so the archive lands at the drivers-repo ROOT.
            driver_files.append((repo_rel[len(_drv_prefix):], fpath))
        else:
            dex_files.append((repo_rel, fpath))

    overall_ok = True

    if driver_files:
        info(
            f"→ [bold]{len(driver_files)}[/bold] driver archive(s) → "
            f"[cyan]github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}[/cyan] [dim](repo root)[/dim]"
        )
        overall_ok &= _push_driver_files_with_fallback(
            driver_files, commit_msg, skip_archive_upload,
        )

    if dex_files:
        info(
            f"→ [bold]{len(dex_files)}[/bold] manifest/meta file(s) → "
            f"[cyan]github.com/{REPO_OWNER}/{REPO_NAME}[/cyan]"
        )
        overall_ok &= _commit_files_to_repo(
            REPO_OWNER, REPO_NAME,
            dex_files, commit_msg, upload_mode, skip_archive_upload,
        )

    return bool(overall_ok)


def _commit_files_to_repo(
    repo_owner           : str,
    repo_name            : str,
    files                : List[Tuple[str, Path]],
    commit_msg           : str,
    upload_mode          : str = UPLOAD_MODE_PARALLEL,
    skip_archive_upload  : bool = False,
) -> bool:
    """
    Commit an already-collected, repo-relative file list to a single GitHub
    repo (owner/name) in one commit via the Git data API.
    """
    if not files:
        info(f"No files to push to {repo_owner}/{repo_name} — skipping.")
        return True

    # ── 1. HEAD ref ───────────────────────────────────────────────────────────
    with C.status(f"[bold bright_cyan]  Fetching HEAD ref ({repo_owner}/{repo_name}) …[/bold bright_cyan]", spinner="dots12"):
        st, ref_data = _api_with_retry(
            "GET", f"/repos/{repo_owner}/{repo_name}/git/ref/heads/{GH_BRANCH}", label="HEAD ref"
        )
    if st != 200:
        err(f"Cannot read HEAD ref for {repo_owner}/{repo_name} (HTTP {st}): {ref_data.get('message', '')}")
        hint(diagnose_api_error(st, ref_data))
        return False
    head_sha = ref_data["object"]["sha"]

    # ── 2. Base tree SHA ──────────────────────────────────────────────────────
    st, commit_data = _api_with_retry(
        "GET", f"/repos/{repo_owner}/{repo_name}/git/commits/{head_sha}", label="HEAD commit"
    )
    if st != 200:
        err(f"Cannot read HEAD commit for {repo_owner}/{repo_name} (HTTP {st})")
        return False
    base_tree_sha = commit_data["tree"]["sha"]

    # Use the same ceiling archives are actually split to (SPLIT_BYTES,
    # 15 MB) rather than a separate hardcoded number -- a properly-split
    # archive part or manifest shard should never trip this in the first
    # place; if something does, it's a real bug upstream (e.g. a raw file
    # that skipped the split pipeline entirely) and belongs in this
    # skip-list, not silently pushed as an oversized blob.
    _BLOB_RAW_LIMIT = SPLIT_BYTES
    oversized = [(rr, fp) for rr, fp in files if fp.stat().st_size > _BLOB_RAW_LIMIT]
    if oversized:
        err(f"{len(oversized)} file(s) still exceed the GitHub blob API limit ({fmt_size(_BLOB_RAW_LIMIT)}) "
            f"— these were not split correctly and will be skipped:")
        for rr, fp in oversized:
            hint(f"{rr}  ({fmt_size(fp.stat().st_size)})")
        oversized_set = {rr for rr, _ in oversized}
        files = [(rr, fp) for rr, fp in files if rr not in oversized_set]
        if not files:
            err("No uploadable files remain after removing oversized entries.")
            return False

    def _is_lfs_file(fp: Path) -> bool:
        name = fp.name.lower()
        if name.endswith(".zip") or name.endswith(".7z"):
            return True
        if re.search(r"\.7z\.\d{4}$", name):
            return True
        return False

    zip_files  = [(rr, fp) for rr, fp in files if _is_lfs_file(fp)]
    meta_files = [(rr, fp) for rr, fp in files if not _is_lfs_file(fp)]

    already_uploaded_rrs: set = set()
    if skip_archive_upload:
        # In the drivers repo every archive already lives at the repo root
        # (no "drivers/" prefix anymore), so key purely off LFS-file detection.
        already_uploaded_rrs = {
            rr for rr, fp in zip_files if _is_lfs_file(fp)
        }
        if already_uploaded_rrs:
            info(
                f"Pipeline: skipping re-upload of {len(already_uploaded_rrs)} "
                f"archive(s) already in LFS — writing pointer blobs only."
            )

    mode_label = {
        UPLOAD_MODE_PARALLEL : "Parallel blobs (12 threads)",
        UPLOAD_MODE_PIPELINE : "Pipeline REST (archive→upload interleaved)",
    }.get(upload_mode, upload_mode)

    archive_via = "git-lfs batch upload" if upload_mode == UPLOAD_MODE_PIPELINE else "blob API"
    info(
        f"Upload mode: [bold bright_cyan]{mode_label}[/bold bright_cyan]  |  "
        f"{len(zip_files)} archive(s) via {archive_via}  ·  {len(meta_files)} meta file(s) via blob API"
    )
    C.print()

    # ── 4a. Upload archives via blob API ──────────────────────────────────────
    lfs_tree_entries: List[Dict] = []

    # Per-call upload-abort flag — lives here (not inside the zip_files block
    # below) so it's always defined, whether this call has archives, meta
    # files, or both. Set the moment a repo-quota 403 is detected (or the
    # user declines a token refresh) so every other concurrent upload stops
    # immediately instead of continuing to hammer a repo we already know is
    # full.
    _upload_abort = threading.Event()

    def _upload_as_blob(repo_rel: str, fpath: Path) -> Optional[Dict]:
        """
        Upload file content as a plain Git blob via the GitHub REST API.
        Key improvements over v5.3.2:
          * 403 (missing repo scope) → immediate skip with clear diagnosis; no retry loop
          * 403 (repo size quota) → flags _QUOTA_ERROR_DETECTED so the caller can
            switch to a fallback repo instead of just reporting a permissions error
          * 401 → one-shot token refresh via _api_with_retry, then inline retry
          * Memory guard: rejects files > 95 MB before reading into RAM
          * Returns None immediately if user already declined token refresh
        """
        # Fast-fail: user already declined a token refresh, or a repo-quota
        # 403 was already detected by another concurrent upload this call.
        if _TOKEN_REFRESH_DECLINED.is_set() or _upload_abort.is_set():
            return None

        fsize = fpath.stat().st_size
        if fsize > 95 * 1024 * 1024:
            err(
                f"File too large for blob API: {repo_rel} "
                f"({fmt_size(fsize)} > 95 MB). Reduce SPLIT_BYTES."
            )
            return None

        raw_bytes = _read_bytes_tracked(fpath)
        encoded   = base64.b64encode(raw_bytes).decode("ascii")

        st, blob = _api_with_retry(
            "POST", f"/repos/{repo_owner}/{repo_name}/git/blobs",
            {"content": encoded, "encoding": "base64"},
            timeout=300, max_attempts=5,
            label=f"blob {Path(repo_rel).name}",
        )

        # ── 403: repo-quota exhaustion vs missing 'repo' scope — both are
        # permanent failures for THIS request, but need very different
        # recovery, so they're diagnosed and flagged separately.
        if st == 403:
            msg403 = blob.get('message', 'Resource not accessible by personal access token')
            err(f"Blob upload failed for {repo_rel} (HTTP 403): {msg403}")
            if _is_repo_quota_error(st, blob):
                hint(f"Repository {repo_owner}/{repo_name} is above its size quota.")
                _mark_quota_error(repo_owner, repo_name)
                _upload_abort.set()
            else:
                hint("Token lacks required permissions (needs 'repo' scope with push access).")
            return None

        # ── 401: _api_with_retry already attempted a one-shot refresh.
        # If it still returns 401 here, the user declined — propagate as abort.
        if st == 401:
            if _refresh_github_token(label=f"blob {Path(repo_rel).name}", failed_token=_load_token()):
                st, blob = _api_with_retry(
                    "POST", f"/repos/{repo_owner}/{repo_name}/git/blobs",
                    {"content": encoded, "encoding": "base64"},
                    timeout=300, max_attempts=3,
                    label=f"blob-retry {Path(repo_rel).name}",
                )
            if st not in (200, 201):
                err(f"Blob upload failed after token refresh for {repo_rel} (HTTP {st})")
                return None

        if st not in (200, 201):
            err(
                f"Blob upload failed for {repo_rel} "
                f"(HTTP {st}): {blob.get('message', '')}"
            )
            hint(diagnose_api_error(st, blob))
            return None

        return {"path": repo_rel, "mode": "100644", "type": "blob", "sha": blob["sha"]}

    if zip_files:
        if upload_mode == UPLOAD_MODE_PIPELINE:
            # Genuine git-lfs batch-protocol upload -- objects land in LFS
            # storage (a separate quota from the regular repo), and the git
            # tree only ever gets a small (~130 byte) pointer blob per
            # archive. This is what makes Pipeline REST a real fallback for
            # a drivers repo that hit its regular size quota: a genuinely
            # different transfer mechanism, not just a relabeled retry.
            lfs_ok, new_lfs_entries = _upload_archives_via_lfs(
                repo_owner, repo_name, zip_files, already_uploaded_rrs,
            )
            if not lfs_ok:
                return False
            lfs_tree_entries.extend(new_lfs_entries)
            ok(f"LFS: uploaded/pointed {len(lfs_tree_entries)} archive(s).")
        else:
            lfs_total_bytes = sum(fp.stat().st_size for _, fp in zip_files)
            lfs_prog = Progress(
                SpinnerColumn("dots12", style="bold bright_cyan"),
                TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
                BarColumn(bar_width=None, style="grey23", complete_style="bold bright_cyan",
                          finished_style="bold bright_green"),
                TaskProgressColumn(style="bold white"),
                DimMofNColumn(),
                DownloadColumn(binary_units=True),
                TransferSpeedColumn(),
                TimeRemainingColumn(compact=True, elapsed_when_finished=True),
                console=C, transient=False, expand=True,
            )
            with lfs_prog:
                blob_task      = lfs_prog.add_task("Blob upload (archives)", total=len(zip_files))
                blob_byte_task = lfs_prog.add_task("bytes", total=lfs_total_bytes, visible=False)

                ul_workers = 12
                results_blob: Dict[str, Optional[Dict]] = {}
                res_lock = threading.Lock()

                # ── Per-run blob dedup cache (sha256 → tree entry) ────────────────
                # Prevents re-uploading identical archive volumes that appear more
                # than once in the file list (rare but possible with certain packs).
                # Also means a re-run after a partial 401 failure reuses already-
                # committed blobs instead of re-uploading them.
                _blob_cache      : Dict[str, Dict] = {}
                _blob_cache_lock = threading.Lock()

                # ── Per-run 403 bookkeeping ──────────────────────────────────────
                # _upload_abort itself now lives at the top of
                # _commit_files_to_repo (shared with the metadata blob path
                # below, and set on sight of a repo-quota 403 as well as a
                # declined token refresh) — see its definition above.
                _failed_403   : List[str] = []          # paths that got 403
                _failed_403_lock = threading.Lock()

                def _ul_worker(rr: str, fp: Path) -> None:
                    # Respect a previous decline or 401 failure — stop immediately.
                    if _upload_abort.is_set() or _TOKEN_REFRESH_DECLINED.is_set():
                        with res_lock:
                            results_blob[rr] = None
                        lfs_prog.advance(blob_task, 1)
                        lfs_prog.advance(blob_byte_task, fp.stat().st_size)
                        return

                    file_sha = _sha256(fp)
                    # Check dedup cache first
                    with _blob_cache_lock:
                        cached = _blob_cache.get(file_sha)

                    if cached:
                        # Reuse the existing blob SHA — just point the tree path at it
                        entry = dict(cached)
                        entry["path"] = rr
                    else:
                        entry = _upload_as_blob(rr, fp)
                        if entry:
                            with _blob_cache_lock:
                                _blob_cache[file_sha] = entry
                        elif _TOKEN_REFRESH_DECLINED.is_set():
                            # User just declined — signal all other threads to stop.
                            _upload_abort.set()
                        # 403 missing-scope failures: record path but let other
                        # uploads continue (the root cause is the token, not the file).
                        elif entry is None:
                            # Distinguish 403 (already printed by _upload_as_blob)
                            # from other transient failures — nothing extra needed here.
                            pass

                    with res_lock:
                        results_blob[rr] = entry
                    lfs_prog.advance(blob_task, 1)
                    lfs_prog.advance(blob_byte_task, fp.stat().st_size)
                    lfs_prog.update(blob_task, description=f"  ◈  blob  {Path(rr).name}")

                with concurrent.futures.ThreadPoolExecutor(max_workers=ul_workers) as pool:
                    futs = [pool.submit(_ul_worker, rr, fp) for rr, fp in zip_files]
                    try:
                        for f in concurrent.futures.as_completed(futs):
                            f.result()
                    except KeyboardInterrupt:
                        for f in futs:
                            f.cancel()
                        raise

                for rr, _ in zip_files:
                    entry = results_blob.get(rr)
                    if entry is None:
                        err(f"Blob upload failed for {rr}")

                failed_blobs = [rr for rr, _ in zip_files if results_blob.get(rr) is None]
                if failed_blobs:
                    # If every failure is a 403, surface a single actionable
                    # summary instead of a wall of per-file noise — but the
                    # cause differs (full repo vs. bad token), so check which.
                    n_fail = len(failed_blobs)
                    n_total = len(zip_files)
                    if n_fail == n_total:
                        if _QUOTA_ERROR_DETECTED.is_set():
                            err(
                                f"All {n_total} blob upload(s) failed. "
                                f"Cause: {repo_owner}/{repo_name} is above its GitHub size quota."
                            )
                        else:
                            err(
                                f"All {n_total} blob upload(s) failed. "
                                f"Most likely cause: the GitHub token is missing the 'repo' scope "
                                f"(push access). Generate a new token at "
                                f"https://github.com/settings/tokens/new with scope: repo"
                            )
                    else:
                        warn(
                            f"{n_fail}/{n_total} blob(s) failed to upload — "
                            f"the successful {n_total - n_fail} will still be committed."
                        )
                        # Remove failed entries from lfs_tree_entries so we still commit what succeeded
                        succeeded_rrs = {rr for rr, _ in zip_files if results_blob.get(rr) is not None}
                        lfs_tree_entries[:] = [e for e in lfs_tree_entries if e.get("path") in succeeded_rrs]
                        if not lfs_tree_entries and not meta_files:
                            err("No successful uploads remain — aborting commit.")
                            return False
                    return False if n_fail == n_total else True

                for rr, _ in zip_files:
                    lfs_tree_entries.append(results_blob[rr])

            ok(f"Blobs: uploaded {len(lfs_tree_entries)} archive(s).")

    # ── 4b. Upload metadata via blob API ─────────────────────────────────────
    blob_tree_entries: List[Dict] = []
    if meta_files:
        meta_prog = Progress(
            SpinnerColumn("dots12", style="bold bright_green"),
            TextColumn("  [bold bright_green]{task.description}[/bold bright_green]"),
            BarColumn(bar_width=None, style="grey30", complete_style="bold bright_green",
                      finished_style="bold bright_green"),
            TaskProgressColumn(style="bold white"),
            MofNCompleteColumn(),
            TimeRemainingColumn(compact=True, elapsed_when_finished=True),
            console=C, transient=False, expand=True,
        )
        with meta_prog:
            meta_task = meta_prog.add_task("Metadata blobs", total=len(meta_files))

            # These used to upload one at a time — each a separate network
            # round trip — even though the archive blobs just above already
            # use a thread pool. Metadata files (manifests/index/README) are
            # typically small in size but not in count, so the sequential
            # per-file latency added up; upload them concurrently the same
            # way, via the same _upload_as_blob() helper, then check results
            # afterward (same "gather, then check for failures" pattern the
            # archive-blob section above already uses).
            results_meta: Dict[str, Optional[Dict]] = {}
            meta_prog_lock = threading.Lock()

            def _meta_worker(rr: str, fp: Path) -> None:
                entry = _upload_as_blob(rr, fp)
                with meta_prog_lock:
                    results_meta[rr] = entry
                    meta_prog.update(meta_task, description=f"  ◈  blob  {Path(rr).name}")
                    meta_prog.advance(meta_task, 1)

            META_WORKERS = min(12, len(meta_files))
            with concurrent.futures.ThreadPoolExecutor(max_workers=META_WORKERS) as pool:
                futs = [pool.submit(_meta_worker, rr, fp) for rr, fp in meta_files]
                try:
                    for f in concurrent.futures.as_completed(futs):
                        f.result()
                except KeyboardInterrupt:
                    for f in futs:
                        f.cancel()
                    raise

            for repo_rel, _fp in meta_files:
                entry = results_meta.get(repo_rel)
                if entry is None:
                    err(f"Failed to create blob for {repo_rel}")
                    return False
                blob_tree_entries.append(entry)

    tree_entries = lfs_tree_entries + blob_tree_entries
    ok(f"Blobs ready: {len(lfs_tree_entries)} archive(s) + {len(blob_tree_entries)} meta file(s).")

    # ── 5. Create tree (chunked) ──────────────────────────────────────────────
    TREE_CHUNK_SIZE = 100
    current_base = base_tree_sha
    tree_sha: Optional[str] = None
    total_entries = len(tree_entries)

    if total_entries == 0:
        warn("No tree entries to commit.")
        return False

    n_chunks = math.ceil(total_entries / TREE_CHUNK_SIZE)
    info(
        f"Creating tree in {n_chunks} chunk(s)  "
        f"({total_entries} entries, {TREE_CHUNK_SIZE} per chunk)"
    )

    tree_prog = Progress(
        SpinnerColumn("dots12", style="bold bright_cyan"),
        TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
        BarColumn(bar_width=None, style="grey23", complete_style="bold bright_cyan",
                  finished_style="bold bright_green"),
        TaskProgressColumn(style="bold white"),
        DimMofNColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )
    with tree_prog:
        tree_task = tree_prog.add_task("Creating tree", total=n_chunks)
        for chunk_i in range(n_chunks):
            chunk = tree_entries[chunk_i * TREE_CHUNK_SIZE : (chunk_i + 1) * TREE_CHUNK_SIZE]
            chunk_label = f"tree chunk {chunk_i + 1}/{n_chunks}"
            tree_prog.update(tree_task, description=f"  ◈  {chunk_label}")
            st, tree = _api_with_retry(
                "POST", f"/repos/{repo_owner}/{repo_name}/git/trees",
                {"base_tree": current_base, "tree": chunk},
                timeout=300, max_attempts=7, backoff_base=8.0,
                label=chunk_label,
            )
            if st not in (200, 201):
                err(f"Failed to create tree (HTTP {st}): {tree.get('message', '')}")
                hint(diagnose_api_error(st, tree))
                return False
            current_base = tree["sha"]
            tree_sha = tree["sha"]
            info(f"  ✓  {chunk_label} — tree SHA {tree_sha[:8]}…")
            tree_prog.advance(tree_task, 1)
        tree_prog.update(tree_task, description="  ✓  Tree created")

    # ── 6. Create commit ──────────────────────────────────────────────────────
    with C.status("[bold bright_cyan]  Creating commit …[/bold bright_cyan]", spinner="dots12"):
        st, new_commit = _api_with_retry(
            "POST", f"/repos/{repo_owner}/{repo_name}/git/commits",
            {
                "message": commit_msg,
                "tree"   : tree_sha,
                "parents": [head_sha],
                "author" : {"name": COMMIT_NAME, "email": COMMIT_EMAIL},
            },
            timeout=120, max_attempts=5, label="create commit",
        )
    if st not in (200, 201):
        err(f"Failed to create commit (HTTP {st}): {new_commit.get('message', '')}")
        hint(diagnose_api_error(st, new_commit))
        return False

    # ── 7. Update ref ─────────────────────────────────────────────────────────
    with C.status("[bold bright_cyan]  Updating ref …[/bold bright_cyan]", spinner="dots12"):
        st, ref_resp = _api_with_retry(
            "PATCH",
            f"/repos/{repo_owner}/{repo_name}/git/refs/heads/{GH_BRANCH}",
            {"sha": new_commit["sha"], "force": False},
            timeout=60, max_attempts=5, label="update ref",
        )
    if st in (200, 201):
        ok(f"Pushed to {repo_owner}/{repo_name} — commit {new_commit['sha'][:8]}")
        return True
    err(f"Ref update failed (HTTP {st}): {ref_resp.get('message', '')}")
    hint(diagnose_api_error(st, ref_resp))
    return False


# ── Genuine git-lfs batch upload (push-level Pipeline REST mechanism) ─────────
def _upload_lfs_object(repo_owner: str, repo_name: str, repo_rel: str, fpath: Path) -> Optional[Tuple[str, int]]:
    """
    Upload one file's content into repo_owner/repo_name's LFS storage via the
    real git-lfs batch protocol (verify → upload → verify). Standalone,
    repo-parameterized version of the transfer logic — needed here (rather
    than reusing the private closure inside zip_and_upload_pipeline) so the
    automatic push-level fallback can target ANY repo in the fallback chain,
    not just whichever repo was active when the module loaded.
    Returns (oid, size) on success (including "already present"), None on
    failure.
    """
    lfs_batch_url = f"https://github.com/{repo_owner}/{repo_name}.git/info/lfs/objects/batch"
    raw_bytes = _read_bytes_tracked(fpath)
    oid  = hashlib.sha256(raw_bytes).hexdigest()
    size = len(raw_bytes)

    lfs_headers: Dict[str, str] = {
        "Content-Type": "application/vnd.git-lfs+json",
        "Accept"      : "application/vnd.git-lfs+json",
    }
    _tok = _load_token()
    if _tok:
        lfs_headers["Authorization"] = f"Bearer {_tok}"

    def _lfs_batch_download_ok() -> bool:
        dl_payload = json.dumps({
            "operation": "download", "transfers": ["basic"],
            "ref": {"name": f"refs/heads/{GH_BRANCH}"},
            "objects": [{"oid": oid, "size": size}],
        }).encode("utf-8")
        try:
            vreq = urllib.request.Request(lfs_batch_url, data=dl_payload,
                                           headers=lfs_headers, method="POST")
            with urllib.request.urlopen(vreq, timeout=60) as vresp:
                vb = json.loads(vresp.read().decode("utf-8"))
            vobj = (vb.get("objects") or [{}])[0]
            return (
                not vobj.get("error")
                and isinstance(vobj.get("actions"), dict)
                and bool(vobj["actions"].get("download"))
            )
        except Exception:
            return False

    # Already in LFS storage (e.g. a previous, partially-successful attempt)?
    if _lfs_batch_download_ok():
        return oid, size

    ul_payload = json.dumps({
        "operation": "upload", "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
    }).encode("utf-8")

    batch = None
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(lfs_batch_url, data=ul_payload,
                                          headers=lfs_headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            if attempt < 5:
                time.sleep(min(4.0 * (2 ** (attempt - 1)), 60.0))
            else:
                err(f"LFS batch request failed for {repo_rel}: {exc}")
                return None

    if not batch:
        return None
    objects = batch.get("objects", [])
    if not objects or "error" in objects[0]:
        obj_err = objects[0].get("error") if objects else "empty response"
        err(f"LFS batch error for {repo_rel}: {obj_err}")
        if objects and isinstance(objects[0].get("error"), dict):
            emsg = str(objects[0]["error"].get("message", "")).lower()
            if "quota" in emsg:
                _mark_quota_error(repo_owner, repo_name)
        return None

    obj            = objects[0]
    upload_actions = obj.get("actions", {})
    if "upload" in upload_actions:
        up_href    = upload_actions["upload"]["href"]
        up_headers = dict(upload_actions["upload"].get("header", {}))
        up_headers.setdefault("Content-Type", "application/octet-stream")
        for attempt in range(1, 6):
            try:
                ul_req = urllib.request.Request(up_href, data=raw_bytes,
                                                 headers=up_headers, method="PUT")
                with urllib.request.urlopen(ul_req, timeout=300) as ul_resp:
                    ul_resp.read()
                break
            except Exception as exc:
                if attempt < 5:
                    time.sleep(min(4.0 * (2 ** (attempt - 1)), 60.0))
                else:
                    err(f"LFS upload PUT failed for {repo_rel}: {exc}")
                    return None

    for pv in range(1, 7):
        if _lfs_batch_download_ok():
            return oid, size
        if pv < 6:
            time.sleep(10)
    err(f"Post-upload verification failed for {repo_rel} — OID {oid[:12]}… not downloadable.")
    return None


def _create_lfs_pointer_blob(repo_owner: str, repo_name: str, repo_rel: str, oid: str, size: int) -> Optional[Dict]:
    """
    Create the small git-lfs pointer-file blob (~130 bytes: version/oid/size)
    that the git tree entry for an LFS-tracked path actually points at — NOT
    the raw archive bytes, which already live in LFS storage. This is the
    piece that was previously missing: without it, an "LFS upload" just
    silently re-committed the full binary as a normal blob, defeating the
    point of using LFS (and its separate, much larger storage quota) at all.
    """
    pointer_text = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {size}\n"
    )
    encoded = base64.b64encode(pointer_text.encode("utf-8")).decode("ascii")
    st, blob = _api_with_retry(
        "POST", f"/repos/{repo_owner}/{repo_name}/git/blobs",
        {"content": encoded, "encoding": "base64"},
        timeout=60, max_attempts=5,
        label=f"lfs-pointer {Path(repo_rel).name}",
    )
    if st == 403 and _is_repo_quota_error(st, blob):
        _mark_quota_error(repo_owner, repo_name)
    if st not in (200, 201):
        err(f"Failed to create LFS pointer blob for {repo_rel} (HTTP {st}): {blob.get('message', '')}")
        return None
    return {"path": repo_rel, "mode": "100644", "type": "blob", "sha": blob["sha"]}


def _upload_archives_via_lfs(
    repo_owner: str,
    repo_name: str,
    zip_files: List[Tuple[str, Path]],
    already_uploaded_rrs: set = frozenset(),
) -> Tuple[bool, List[Dict]]:
    """
    Push-level Pipeline REST transfer: uploads every archive in zip_files
    into repo_owner/repo_name's LFS storage (skipping any already confirmed
    uploaded earlier in the same run) and returns git-tree entries pointing
    at their LFS pointer blobs. This is the mechanism automatically tried
    when a Parallel-blobs push fails for a non-quota reason, and it's also
    what makes it a genuine fallback: LFS storage is billed/quota'd
    separately from the regular repo, so it can succeed where a
    quota-exhausted repo's blob API would not.
    """
    entries: List[Dict] = []
    failed : List[str]  = []
    lock = threading.Lock()

    lfs_prog = Progress(
        SpinnerColumn("dots12", style="bold bright_green"),
        TextColumn("  [bold bright_green]{task.description}[/bold bright_green]"),
        BarColumn(bar_width=None, style="grey23", complete_style="bold bright_green",
                  finished_style="bold bright_green"),
        TaskProgressColumn(style="bold white"),
        DimMofNColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )
    with lfs_prog:
        task = lfs_prog.add_task("LFS upload (archives)", total=len(zip_files))

        def _worker(rr: str, fp: Path) -> None:
            try:
                if _QUOTA_ERROR_DETECTED.is_set():
                    with lock:
                        failed.append(rr)
                    return
                if rr in already_uploaded_rrs:
                    oid  = _sha256(fp)
                    size = fp.stat().st_size
                else:
                    result = _upload_lfs_object(repo_owner, repo_name, rr, fp)
                    if result is None:
                        with lock:
                            failed.append(rr)
                        return
                    oid, size = result
                entry = _create_lfs_pointer_blob(repo_owner, repo_name, rr, oid, size)
                with lock:
                    if entry is None:
                        failed.append(rr)
                    else:
                        entries.append(entry)
            finally:
                lfs_prog.advance(task, 1)
                lfs_prog.update(task, description=f"  ◈  lfs  {Path(rr).name}")

        LFS_WORKERS = 6
        with concurrent.futures.ThreadPoolExecutor(max_workers=LFS_WORKERS) as pool:
            futs = [pool.submit(_worker, rr, fp) for rr, fp in zip_files]
            try:
                for f in concurrent.futures.as_completed(futs):
                    f.result()
            except KeyboardInterrupt:
                for f in futs:
                    f.cancel()
                raise

    if failed:
        err(f"{len(failed)}/{len(zip_files)} archive(s) failed to upload via LFS.")
        return False, entries
    return True, entries


# ── Automatic push: Parallel blobs → Pipeline REST → repo fallback ───────────
def _push_driver_files_with_fallback(
    driver_files: List[Tuple[str, Path]],
    commit_msg: str,
    skip_archive_upload: bool,
) -> bool:
    """
    Push driver archives to the active drivers repo with two layers of fully
    automatic recovery — no manual mode selection required:

      1. Try the fast Parallel-blobs transfer (raw Git Blob API).
      2. If that fails for a reason OTHER than the repo being full, retry
         the SAME already-archived files via the Pipeline REST mechanism
         (a real git-lfs batch upload) — a genuinely different transfer
         path, not just a relabeled retry.
      3. If either attempt fails because the active repo is above its size
         quota, switch to the next repo in sequence (drivers_1, drivers_2,
         ...; persisted to disk) and start back at step 1 on the new repo.
         Once the next one in sequence doesn't exist yet, the user is
         notified via Telegram to create it, and the tool waits (with
         reminders) for it.
    """
    _load_repo_state()

    while True:
        if DRIVERS_REPO_NAME in _SPENT_DRIVERS_REPOS:
            # Known full already (e.g. discovered mid-run against a
            # different pack, or by the startup check) — don't waste a
            # push attempt re-confirming it, just advance straight away.
            warn(f"{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} is already known to be full — "
                 f"skipping straight to the next repo.")
            if _advance_drivers_repo_or_notify():
                continue
            return False

        _clear_quota_error()
        info(f"Attempt: [bold]Parallel blobs[/bold] → {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}")
        if _commit_files_to_repo(
            DRIVERS_REPO_OWNER, DRIVERS_REPO_NAME, driver_files, commit_msg,
            upload_mode=UPLOAD_MODE_PARALLEL, skip_archive_upload=skip_archive_upload,
        ):
            return True

        if _QUOTA_ERROR_DETECTED.is_set():
            if _advance_drivers_repo_or_notify():
                continue
            return False

        warn(
            "Parallel blobs push failed for a non-quota reason — "
            "automatically retrying via [bold]Pipeline REST[/bold] (git-lfs) …"
        )
        _clear_quota_error()
        if _commit_files_to_repo(
            DRIVERS_REPO_OWNER, DRIVERS_REPO_NAME, driver_files, commit_msg,
            upload_mode=UPLOAD_MODE_PIPELINE, skip_archive_upload=skip_archive_upload,
        ):
            return True

        if _QUOTA_ERROR_DETECTED.is_set():
            if _advance_drivers_repo_or_notify():
                continue
            return False

        err(
            "Both Parallel blobs and Pipeline REST push attempts failed for "
            f"{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}."
        )
        return False


# ── Version comparison ────────────────────────────────────────────────────────
def _parse_ver(ver_str: str) -> Tuple[int, ...]:
    clean = re.sub(r'^\d{1,2}/\d{1,2}/\d{4}\s*,\s*', '', ver_str.strip())
    parts = re.split(r'[.,\-]', clean)
    ints: List[int] = [int(p.strip()) for p in parts if p.strip().isdigit()]
    return tuple(ints) if ints else (0,)


def version_is_newer(new_ver: str, old_ver: str) -> bool:
    return _parse_ver(new_ver) > _parse_ver(old_ver)


# ── Write-then-read byte cache ────────────────────────────────────────────────
# Every metadata file (manifest/installer shards, index.json, README.md) gets
# written locally and then read back moments later — either re-parsed for a
# repo-wide recalculation or re-read as raw bytes to upload as a git blob
# during commit. Since this process is the only writer, we can remember the
# exact bytes we just wrote (keyed by the file's mtime+size at write time)
# and hand those back instead of touching the disk again. Every read still
# re-verifies the file's current mtime/size against what we cached before
# trusting it — if anything external changed the file since, the cache is
# bypassed and a real read happens, so this can never serve stale content.
_WRITTEN_BYTES_CACHE: Dict[Path, Tuple[bytes, int, int]] = {}
_WRITTEN_BYTES_LOCK  = threading.Lock()


def _write_bytes_tracked(path: Path, data: bytes) -> None:
    """Write bytes to disk and remember them for a later same-run read."""
    path.write_bytes(data)
    try:
        st = path.stat()
        with _WRITTEN_BYTES_LOCK:
            _WRITTEN_BYTES_CACHE[path.resolve()] = (data, st.st_mtime_ns, st.st_size)
    except OSError:
        pass  # caching is a pure optimization — never let it break the write


def _read_bytes_tracked(path: Path) -> bytes:
    """Read a file's bytes, reusing a cached copy from a write earlier in
    this run if — and only if — the file's mtime/size on disk still match
    exactly what we wrote (i.e. nothing touched it since)."""
    rp = path.resolve()
    with _WRITTEN_BYTES_LOCK:
        cached = _WRITTEN_BYTES_CACHE.get(rp)
    if cached is not None:
        data, mtime_ns, size = cached
        try:
            st = path.stat()
            if st.st_mtime_ns == mtime_ns and st.st_size == size:
                return data
        except OSError:
            pass
    return path.read_bytes()


def _write_shards_parallel(writes: List[Tuple[Path, bytes]], max_workers: int = 8) -> None:
    """Write several independent shard files concurrently instead of one at
    a time. Safe because each write targets its own distinct path with no
    shared state between them — only wall-clock order changes, every byte
    that used to be written sequentially is still written exactly once.
    Any failure is re-raised (via future.result()) rather than swallowed,
    so a failed shard write can never be silently skipped."""
    if not writes:
        return
    if len(writes) == 1:
        path, data = writes[0]
        _write_bytes_tracked(path, data)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(writes))) as pool:
        futs = [pool.submit(_write_bytes_tracked, path, data) for path, data in writes]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()


# ── Session-level memoization for manifest discovery ─────────────────────────
# When processing multiple packs in one session, the manifests/ directory
# contents don't change between packs (nothing writes to it outside this
# script). Memoize category listing and shard-path discovery so Pack 2+ skip
# the filesystem glob that Pack 1 already did. Keyed by (repo_dir, category).
_DISCOVERY_CACHE: Dict[Tuple[Path, Optional[str]], Any] = {}
_DISCOVERY_CACHE_LOCK = threading.Lock()

def _get_cached_discovery(repo: Path, key: Optional[str] = None) -> Optional[Any]:
    """Retrieve a memoized discovery result if it exists."""
    with _DISCOVERY_CACHE_LOCK:
        return _DISCOVERY_CACHE.get((repo.resolve(), key))

def _set_cached_discovery(repo: Path, key: Optional[str], value: Any) -> None:
    """Store a discovery result in the session cache."""
    with _DISCOVERY_CACHE_LOCK:
        _DISCOVERY_CACHE[(repo.resolve(), key)] = value

def _clear_discovery_cache(repo: Path) -> None:
    """Clear the discovery cache for a repo (e.g., after a pack writes manifests)."""
    repo_key = repo.resolve()
    with _DISCOVERY_CACHE_LOCK:
        to_delete = [k for k in _DISCOVERY_CACHE if k[0] == repo_key]
        for k in to_delete:
            del _DISCOVERY_CACHE[k]


# ── Category-manifest naming ──────────────────────────────────────────────────
def _category_manifest_rel(cat: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9]', '', cat.strip()) or "Other"
    # Repo-relative path, e.g. "manifests/Audio.manifest.json".
    return f"{MANIFEST_DIR}/{safe}.manifest.json"


def _manifest_shard_paths_for(repo: Path, base_name: str) -> List[Path]:
    """Discover all shards for a category manifest in one listdir pass.
    
    Instead of looping with repeated .exists() checks (which cost one FS
    roundtrip per shard probed), discovers all numbered shards via a single
    listdir or glob, filtering by pattern. The base shard (no numeral) is
    checked separately and prepended if found.
    """
    paths: List[Path] = []
    base = repo / base_name
    
    # Check base shard in one stat (we'll use it below anyway).
    try:
        base.stat()  # single FS roundtrip to confirm existence + grab size
        paths.append(base)
    except OSError:
        pass
    
    # All remaining shards are numbered <stem>.manifest.{2,3,...}.json.
    # Batch-discover them via glob instead of a while True loop with
    # per-shard .exists() checks.
    stem = base_name.replace(".manifest.json", "")
    manifest_dir = repo / MANIFEST_DIR
    try:
        # One glob call discovers all shards matching the pattern in one FS pass.
        pattern = f"{stem}.manifest.[0-9]*.json"
        candidates: List[Path] = sorted(manifest_dir.glob(pattern))
        
        # Filter to numbered ones >= 2 (shard 1 is the base, already handled above).
        for p in candidates:
            fname = p.name
            # Extract the number: "Audio.manifest.2.json" -> 2
            try:
                mid = fname.replace(stem + ".manifest.", "").replace(".json", "")
                idx = int(mid)
                if idx >= 2:
                    paths.append(p)
            except (ValueError, AttributeError):
                pass
    except OSError:
        pass
    
    return paths


def _all_category_manifest_rels(repo: Path) -> List[str]:
    """Find all category manifest base shards in one listdir pass.
    
    Results are memoized per session so Pack 2+ don't re-glob manifests/
    for a result that hasn't changed since Pack 1 (nothing else writes to
    it between packs).
    """
    # Check session cache first.
    cached = _get_cached_discovery(repo, key=None)
    if cached is not None:
        return cached
    
    found: List[str] = []
    seen: set = set()
    
    # Get all known categories and sort them for stable discovery order.
    known_cats = sorted(set(_CLASS_TO_TYPE.values()) | {"Other"})
    
    # Batch-discover all .manifest.json files in manifests/ via glob in one
    # FS pass. Extract the category name from each filename and cross-check
    # against known categories to establish definitive list.
    manifest_dir = repo / MANIFEST_DIR
    try:
        all_manifests: List[Path] = sorted(manifest_dir.glob("*.manifest.json"))
        discovered_rels = {p.relative_to(repo).as_posix() for p in all_manifests}
    except OSError:
        discovered_rels = set()
    
    # Emit known categories that exist (in stable sort order).
    for cat in known_cats:
        rel = _category_manifest_rel(cat)
        if rel in discovered_rels:
            found.append(rel)
            seen.add(rel)
    
    # Emit any extra unknown categories found on disk that aren't in the
    # known list (e.g. historical ones no longer in _CLASS_TO_TYPE).
    for rel in sorted(discovered_rels):
        if rel not in seen and rel != INSTALLER_MANIFEST_REL:
            found.append(rel)
            seen.add(rel)
    
    # Store in session cache for Pack 2+.
    _set_cached_discovery(repo, key=None, value=found)
    return found


def load_all_manifests_combined(repo: Path, shard_cache: Optional[Dict[Path, Dict]] = None) -> Dict:
    """Read every category manifest shard and merge them into one combined
    view (used for repo-wide dedup / version lookups).

    `shard_cache` is a read-through cache: a shard already present there is
    reused as-is (no disk read), and any shard read fresh here is stashed
    into it — so a subsequent load_manifest() / load_all_manifests_combined()
    call for the same shard, in either order, never re-reads the same file
    twice. Purely additive: passing None (the default) reproduces the
    previous behavior exactly.
    """
    combined: Dict = {
        "schema"         : SCHEMA_VER,
        "drivers"        : [],
        "version_history": {},
    }
    for cat_rel in _all_category_manifest_rels(repo):
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            cache_key = sp.resolve()
            data = shard_cache.get(cache_key) if shard_cache is not None else None
            try:
                if data is None:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    if shard_cache is not None:
                        shard_cache[cache_key] = data
                combined["drivers"].extend(data.get("drivers", []))
                combined["version_history"].update(data.get("version_history", {}))
            except Exception:
                pass
    return combined


# ── Manifest helpers ──────────────────────────────────────────────────────────
def _manifest_shard_paths(repo: Path, cat: str = "") -> List[Path]:
    if cat:
        return _manifest_shard_paths_for(repo, _category_manifest_rel(cat))
    all_paths: List[Path] = []
    for rel in _all_category_manifest_rels(repo):
        all_paths.extend(_manifest_shard_paths_for(repo, rel))
    return all_paths


def load_manifest(repo: Path, cat: str = "Other", shard_cache: Optional[Dict[Path, Dict]] = None) -> Dict:
    """Load the active (last) shard for one category, for appending to.

    If `shard_cache` is given and already holds this shard's parsed data
    (e.g. from a load_all_manifests_combined(..., shard_cache=...) call made
    moments earlier in the same repo state), that cached parse is reused
    instead of reading + re-parsing the file again — the two calls were
    reading the exact same bytes back-to-back. On a cache miss the freshly
    parsed data is stored back into `shard_cache` (read-through), so it's
    available to any other loader sharing the same cache. Either way, the
    caller always gets back a deep copy, so mutating it (appending entries,
    etc.) can never corrupt the cache or anything built from it (e.g. the
    combined dict's "drivers" list, which shares entry objects with the
    cached shard).
    """
    shards = _manifest_shard_paths_for(repo, _category_manifest_rel(cat))
    if shards:
        p = shards[-1]
        cache_key = p.resolve()
        cached = shard_cache.get(cache_key) if shard_cache is not None else None
        try:
            if cached is None:
                cached = json.loads(p.read_text(encoding="utf-8"))
                if shard_cache is not None:
                    shard_cache[cache_key] = cached
            data = copy.deepcopy(cached)
            data.setdefault("version_history", {})
            data.setdefault("shard_index",  len(shards))
            data.setdefault("total_shards", len(shards))
            data["category"] = cat
            return data
        except Exception as exc:
            warn(f"Could not parse {escape(p.name)}: {escape(str(exc))} — starting fresh shard.")
    return {
        "schema"         : SCHEMA_VER,
        "category"       : cat,
        "shard_index"    : 1,
        "total_shards"   : 1,
        "updated"        : str(date.today()),
        "base_url"       : BASE_RAW_URL,
        "lfs_batch_url"  : LFS_BATCH_URL,
        "drivers"        : [],
        "version_history": {},
    }


def save_manifest(m: Dict, repo: Path, cat: str = "", update_index: bool = True) -> Path:
    """Write a category manifest shard (splitting into new shards as needed).

    update_index controls whether the (expensive, full-repo-scan) README
    badge + index.json refresh runs after this write. Callers that invoke
    save_manifest() repeatedly in a loop (once per driver type in a pack)
    should pass update_index=False and call _refresh_index_and_badge(repo)
    once after the loop — the on-disk result is identical either way since
    only the state after the final call in such a loop is ever read, but
    doing it once instead of N times avoids N-1 redundant full-repo rescans.
    """
    cat = cat or m.get("category", "Other")
    base_name = _category_manifest_rel(cat)
    m["updated"]       = str(date.today())
    m["schema"]        = SCHEMA_VER
    m["category"]      = cat
    m["lfs_batch_url"] = LFS_BATCH_URL
    existing_shards = _manifest_shard_paths_for(repo, base_name)
    active_path = existing_shards[-1] if existing_shards else (repo / base_name)
    active_path.parent.mkdir(parents=True, exist_ok=True)   # ensure manifests/ exists
    probe = json.dumps(m, indent=2, ensure_ascii=False).encode("utf-8")

    if len(probe) <= MANIFEST_SPLIT_THRESHOLD:
        _write_bytes_tracked(active_path, probe)
        # This write may have created a brand-new category shard file (or
        # resurrected one the discovery cache doesn't know about yet). The
        # discovery cache is never otherwise invalidated, so without this
        # clear, a stale cached listing can cause a subsequent session's
        # index.json to omit this category entirely — which is exactly the
        # bug that lets a later save_manifest() start a fresh shard 1 and
        # overwrite/delete every real entry this category had. Cheap: it
        # only forces a re-glob on the next _all_category_manifest_rels()
        # call, not an eager rescan now.
        _clear_discovery_cache(repo)
        if update_index:
            _refresh_index_and_badge(repo)
        return active_path

    warn(f"Manifest shard for [bold]{escape(cat)}[/bold] would exceed {fmt_size(MANIFEST_SPLIT_THRESHOLD)} — splitting into new shard(s).")
    try:
        sealed_data    = json.loads(active_path.read_text(encoding="utf-8"))
        sealed_drivers = {e["id"] for e in sealed_data.get("drivers", [])}
    except Exception:
        sealed_data = {}; sealed_drivers = set()

    new_drivers  = [e for e in m.get("drivers", []) if e["id"] not in sealed_drivers]
    version_hist = m.get("version_history", {})
    stem         = base_name.replace(".manifest.json", "")
    start_idx    = len(existing_shards) + 1

    def _shard_name(idx: int) -> str:
        # shard 1 is always the bare "<cat>.manifest.json" (no numeral),
        # matching _manifest_shard_paths_for()'s discovery scheme.
        return f"{stem}.manifest.json" if idx == 1 else f"{stem}.manifest.{idx}.json"

    def _skeleton(idx: int) -> Dict:
        return {
            "schema"         : SCHEMA_VER,
            "category"       : cat,
            "shard_index"    : idx,
            "total_shards"   : idx,   # corrected below once the final count is known
            "updated"        : str(date.today()),
            "base_url"       : BASE_RAW_URL,
            "lfs_batch_url"  : LFS_BATCH_URL,
            "drivers"        : [],
            "version_history": {},
        }

    def _exact_size(shard: Dict) -> int:
        return len(json.dumps(shard, indent=2, ensure_ascii=False).encode("utf-8"))

    empty_overhead = _exact_size(_skeleton(start_idx))       # bytes with an empty "drivers" list
    SAFETY_TARGET  = int(MANIFEST_SPLIT_THRESHOLD * 0.90)    # pack conservatively; exact-verify after
    
    # During packing, _estimated_size is called on skeletons with the same
    # structure but incrementally growing driver counts — most will have
    # identical overhead (same keys, updated date, etc.) and differ only by
    # entry count. Memoize by driver count to skip re-encoding.
    #
    # IMPORTANT: this memoized wrapper is ONLY safe for Pass 1's approximate
    # greedy estimate (_entry_marginal_cost), where being off by a little is
    # fine because Pass 2/3 are supposed to true it up exactly afterward.
    # Two different shards can easily land on the same driver COUNT while
    # having very different actual byte sizes (entries vary a lot in
    # HWID-list/description length) — caching by count alone means a later
    # shard can silently reuse an earlier, smaller shard's cached size and
    # sail past MANIFEST_SIZE_LIMIT with no warning. So Pass 2 and Pass 3
    # (the "exact true-up" passes) must call the original, unmemoized
    # _exact_size directly — never this cached version — so the true-up is
    # actually exact.
    _size_cache: Dict[int, int] = {}
    _original_exact_size = _exact_size

    def _estimated_size(shard: Dict) -> int:
        driver_count = len(shard.get("drivers", []))
        if driver_count not in _size_cache:
            _size_cache[driver_count] = _original_exact_size(shard)
        return _size_cache[driver_count]

    def _entry_marginal_cost(entry: Dict) -> int:
        # Measure the entry's real incremental byte cost against the *actual*
        # skeleton structure (same keys/nesting as what gets written), not a
        # mismatched bare-dict wrapper — otherwise the estimate can be wildly
        # wrong (even negative), letting shards balloon well past the target
        # before the exact true-up pass catches it. Estimate-only: safe to
        # use the memoized _estimated_size here.
        probe = _skeleton(start_idx)
        probe["drivers"] = [entry]
        return _estimated_size(probe) - empty_overhead

    # ── Pass 1: fast approximate greedy packing (O(n)) ──────────────────────
    # Re-serializing the whole growing shard on every single appended entry
    # would be O(n^2) and unusably slow for large batches, so estimate each
    # entry's encoded size individually and only true-up exactly afterward.
    shards: List[Dict] = []
    idx     = start_idx
    cur     = _skeleton(idx)
    cur_est = empty_overhead
    for entry in new_drivers:
        entry_est = _entry_marginal_cost(entry) + 4  # + trailing-comma slack for list continuation
        if cur["drivers"] and cur_est + entry_est > SAFETY_TARGET:
            shards.append(cur)
            idx += 1
            cur     = _skeleton(idx)
            cur_est = empty_overhead
        cur["drivers"].append(entry)
        cur_est += entry_est
    shards.append(cur)

    # ── Pass 2: exact true-up ────────────────────────────────────────────────
    # Verify byte-for-byte and cascade any straggling entries forward into
    # the next shard so every shard we actually write is guaranteed to be
    # at or under MANIFEST_SIZE_LIMIT (barring a single entry too large to
    # split on its own, which is written alone with a warning).
    i = 0
    while i < len(shards):
        while (len(shards[i]["drivers"]) > 1
               and _original_exact_size(shards[i]) > MANIFEST_SPLIT_THRESHOLD):
            overflow = shards[i]["drivers"].pop()
            if i + 1 >= len(shards):
                idx += 1
                shards.append(_skeleton(idx))
            shards[i + 1]["drivers"].insert(0, overflow)
        if len(shards[i]["drivers"]) == 1 and _original_exact_size(shards[i]) > MANIFEST_SIZE_LIMIT:
            warn(f"A single driver entry in [bold]{escape(cat)}[/bold] exceeds "
                 f"{fmt_size(MANIFEST_SIZE_LIMIT)} on its own — writing it as an oversized shard.")
        i += 1

    shards = [s for s in shards if s["drivers"]] or [_skeleton(start_idx)]
    for n, s in enumerate(shards):
        s["shard_index"] = start_idx + n

    # ── Pass 3: re-verify the final shard once version_history is attached ──
    # version_history is only known/attached *after* Pass 1/2 finish packing
    # drivers (both of which size shards against an empty "version_history":
    # {} skeleton). A category with a long run of packs accumulates a
    # version_history blob that only ever grows, so gluing the real payload
    # onto shards[-1] here can silently push an already-full shard well past
    # MANIFEST_SIZE_LIMIT with no check catching it (this is what produced
    # oversized shards like Audio.manifest.2.json). Shed driver entries off
    # the tail into a fresh trailing shard — which then inherits
    # version_history — until the shard actually carrying the history fits,
    # or there are no more drivers left to shed off it.
    shards[-1]["version_history"] = version_hist
    while _original_exact_size(shards[-1]) > MANIFEST_SPLIT_THRESHOLD and shards[-1]["drivers"]:
        idx += 1
        tail = _skeleton(idx)
        tail["version_history"]       = shards[-1]["version_history"]
        shards[-1]["version_history"] = {}
        while (shards[-1]["drivers"]
               and _original_exact_size(shards[-1]) > MANIFEST_SPLIT_THRESHOLD):
            tail["drivers"].insert(0, shards[-1]["drivers"].pop())
        shards.append(tail)
        for n, s in enumerate(shards):
            s["shard_index"] = start_idx + n

    if _original_exact_size(shards[-1]) > MANIFEST_SIZE_LIMIT:
        warn(f"version_history for [bold]{escape(cat)}[/bold] alone exceeds "
             f"{fmt_size(MANIFEST_SIZE_LIMIT)} on its own — writing final shard oversized.")

    final_idx = shards[-1]["shard_index"]
    for i, shard in enumerate(shards):
        shard["total_shards"] = final_idx
        if i < len(shards) - 1:
            next_name = _shard_name(shard["shard_index"] + 1)
            shard["note"]       = f"Sealed — continued in {next_name}"
            shard["next_shard"] = next_name

    # Every shard below targets its own distinct file — the sealed shard
    # being closed off, plus each new/overflow shard — so all of them are
    # queued up front and written concurrently instead of one at a time.
    pending_writes: List[Tuple[Path, bytes]] = []

    if sealed_data:
        sealed_data["note"]         = f"Sealed — continued in {_shard_name(start_idx)}"
        sealed_data["next_shard"]   = _shard_name(start_idx)
        sealed_data["total_shards"] = final_idx
        sealed_bytes = json.dumps(sealed_data, indent=2, ensure_ascii=False).encode("utf-8")
        pending_writes.append((active_path, sealed_bytes))

    shard_paths: List[Path] = []
    shard_sizes: Dict[Path, int] = {}
    for shard in shards:
        path = repo / _shard_name(shard["shard_index"])
        data = json.dumps(shard, indent=2, ensure_ascii=False).encode("utf-8")
        pending_writes.append((path, data))
        shard_paths.append(path)
        shard_sizes[path] = len(data)

    _write_shards_parallel(pending_writes)

    last_path = active_path
    for shard, path in zip(shards, shard_paths):
        ok(f"New manifest shard: [bold]{escape(path.name)}[/bold]  "
           f"({len(shard['drivers'])} driver(s), {fmt_size(shard_sizes[path])})")
        last_path = path

    # Same reasoning as the single-shard early-return above: this path can
    # introduce new shard files (new category, or a new overflow shard for
    # an existing one), so the discovery cache must not be allowed to serve
    # a listing from before this write.
    _clear_discovery_cache(repo)

    if update_index:
        _refresh_index_and_badge(repo)
    return last_path


# ── Installer manifest ────────────────────────────────────────────────────────
# Sharded the same way as the per-category driver manifests: shard 1 is the
# bare "installers.manifest.json", overflow shards are
# "installers.manifest.2.json", "installers.manifest.3.json", etc.
_INSTALLER_MANIFEST_STEM = INSTALLER_MANIFEST_REL.replace(".manifest.json", "")


def _installer_shard_name(idx: int) -> str:
    return f"{_INSTALLER_MANIFEST_STEM}.manifest.json" if idx == 1 \
        else f"{_INSTALLER_MANIFEST_STEM}.manifest.{idx}.json"


def _count_total_drivers(repo: Path) -> int:
    total = 0
    for cat_rel in _all_category_manifest_rels(repo):
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                total += sum(1 for e in data.get("drivers", []) if e.get("enabled", True))
            except Exception:
                pass
    return total


def _refresh_index_and_badge(repo: Path, preloaded_manifest: Optional[Dict] = None) -> int:
    """Refresh index.json and README badge from manifest shards, reusing loaded data.
    
    If preloaded_manifest is provided (e.g. from load_all_manifests_combined()
    called moments earlier in the same save session), that data is used directly
    instead of re-reading shards from disk. This eliminates a full-repo JSON
    parse when the data is already in memory.
    
    Falls back to reading from disk if preloaded_manifest is None, so callers
    outside the hot save loop (where manifest data isn't available) still work.

    Those two calls used to independently walk every category manifest shard
    on disk and json.loads() it — once each — meaning every save_manifest()
    write paid for two full-repo scans. Every enabled driver entry already
    has a type (falling back to "Other"), so the total driver count is just
    sum(category_summary.values()); this reads and parses each shard exactly
    once and derives both numbers from that single pass, then writes
    index.json and updates the README badge from the result.

    Returns the total driver count so callers (e.g. the per-pack save loop)
    can reuse it instead of triggering another full scan just to log it.
    _save_index()/_count_total_drivers() themselves are left untouched for
    any other existing caller — only this new combined path is used in the
    hot per-save loop.
    """
    cat_rels = _all_category_manifest_rels(repo)
    manifest_shards: List[Dict] = []
    category_summary: Dict[str, int] = Counter()

    if preloaded_manifest is not None:
        # Fast path: manifest data was already loaded and parsed in memory.
        # Extract the category summary from the preloaded data and build the
        # manifest_shards index from actual shard files (which we need for
        # sizing info), but skip the expensive JSON re-parse.
        for e in preloaded_manifest.get("drivers", []):
            if e.get("enabled", True):
                cat = e.get("type") or e.get("category_type") or "Other"
                category_summary[cat] += 1
        
        # Now build manifest_shards metadata (shard filenames, sizes, status)
        # from the filesystem — these are just envelope/header info, not the
        # full driver lists, so we still need to stat() the files but skip
        # the JSON parse. Batch the stats via a single manifest_dir listdir.
        manifest_dir = repo / MANIFEST_DIR
        try:
            all_shard_files: List[Path] = sorted(manifest_dir.glob("*.manifest.json"))
        except OSError:
            all_shard_files = []
        
        shard_file_set = {p.name for p in all_shard_files}
        
        for cat_rel in cat_rels:
            shard_paths = _manifest_shard_paths_for(repo, cat_rel)
            for i, p in enumerate(shard_paths):
                # Stat this one file (unavoidable — we need size info for
                # the index) instead of re-reading and parsing its JSON.
                try:
                    st = p.stat()
                    sz = st.st_size
                except OSError:
                    sz = 0
                
                sealed = (sz >= MANIFEST_SIZE_LIMIT) or (i < len(shard_paths) - 1)
                manifest_shards.append({
                    "filename"  : p.relative_to(repo).as_posix(),
                    "size_bytes": sz,
                    "active"    : not sealed,
                    "note"      : "overflow -> next shard" if sealed else "active",
                })
    else:
        # Slow path (fallback): no preloaded data available — must read and
        # parse shards from disk. This is the path taken by _save_index() and
        # _count_total_drivers() when called from outside the pack loop.
        for cat_rel in cat_rels:
            shard_paths = _manifest_shard_paths_for(repo, cat_rel)
            for i, p in enumerate(shard_paths):
                try:
                    st = p.stat()
                    sz = st.st_size
                except OSError:
                    sz = 0
                
                sealed = (sz >= MANIFEST_SIZE_LIMIT) or (i < len(shard_paths) - 1)
                manifest_shards.append({
                    "filename"  : p.relative_to(repo).as_posix(),
                    "size_bytes": sz,
                    "active"    : not sealed,
                    "note"      : "overflow -> next shard" if sealed else "active",
                })
                
                # Only parse JSON when preloaded data isn't available.
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    for e in data.get("drivers", []):
                        if e.get("enabled", True):
                            cat = e.get("type") or e.get("category_type") or "Other"
                            category_summary[cat] += 1
                except Exception:
                    pass

    if not manifest_shards:
        manifest_shards.append({
            "filename"  : _category_manifest_rel("Other"),
            "size_bytes": 0,
            "active"    : True,
            "note"      : "not yet created",
        })

    idx_data = {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : manifest_shards,
        "category_summary": dict(sorted(category_summary.items())),
    }
    p = repo / INDEX_FILE_NAME
    _write_bytes_tracked(p, json.dumps(idx_data, indent=2, ensure_ascii=False).encode("utf-8"))

    total = sum(category_summary.values())

    cat_str = "  ".join(f"{k}={v}" for k, v in sorted(category_summary.items()))
    ok(f"{INDEX_FILE_NAME} updated  ({len(cat_rels)} category manifest(s) / {len(manifest_shards)} shard(s))")
    if cat_str:
        info(f"Category summary: {cat_str}")

    _update_readme_badge(repo, total)
    return total


# ── Manifest entry builder ────────────────────────────────────────────────────
def build_entries(
    pack     : str,
    inf_data : List[Tuple[Path, Dict]],
    zip_map  : Dict[Path, List[PartInfo]],
    rel_dir  : str,
    src      : Path,
    zip_dest : Path,
    type_map : Dict[Path, str],
) -> Tuple[List[Dict], List[str]]:
    entries  : List[Dict] = []
    warnings : List[str]  = []

    hwid_owners: Dict[str, List[Tuple[Path, Dict]]] = defaultdict(list)
    for inf_path, d in inf_data:
        for h in d.get("hwids", []):
            hwid_owners[h].append((inf_path, d))

    for hwid, owners in hwid_owners.items():
        if len(owners) > 1:
            versions  = {d.get("version", "") for _, d in owners}
            archs     = {d.get("arch",    "") for _, d in owners}
            inf_names = [p.name for p, _ in owners]
            if len(versions) > 1 or len(archs) > 1:
                warnings.append(
                    f"HWID conflict  {hwid}\n"
                    f"        Claimed by {len(owners)} INFs: {', '.join(inf_names)}\n"
                    f"        Versions: {', '.join(v or 'n/a' for v in versions)}"
                )

    seen_sigs: Dict[str, Path] = {}
    skipped  : Set[Path]       = set()

    for inf_path, d in inf_data:
        hwid_key_set = frozenset(d.get("hwids", []))
        sig = "::".join([
            "|".join(sorted(hwid_key_set)),
            d.get("version",  ""),
            d.get("arch",     ""),
            d.get("category", ""),
        ])
        if hwid_key_set and sig in seen_sigs:
            warnings.append(
                f"Duplicate skipped  {inf_path.name}\n"
                f"        Identical to: {seen_sigs[sig].name}  (same HWIDs, version, arch)"
            )
            skipped.add(inf_path)
        elif hwid_key_set:
            seen_sigs[sig] = inf_path

    for inf_path, d in inf_data:
        if inf_path in skipped:
            continue
        group_folder = inf_path.parent
        parts_list   = zip_map.get(group_folder, [])
        if not parts_list:
            continue

        driver_type = type_map.get(group_folder, _TYPE_FALLBACK)
        version     = d.get("version",  "")
        driver_date = d.get("driver_date", "")
        arch        = d.get("arch",     "x64")
        category    = d.get("category", "")
        provider    = d.get("provider", "")
        hwids       = d.get("hwids",    [])

        id_src = "|".join([
            "|".join(sorted(hwids)),
            version, arch, category.lower(),
        ])
        entry_id = f"drv-{hashlib.sha256(id_src.encode()).hexdigest()[:16]}"

        part_meta: List[Dict] = []
        for pi in parts_list:
            fname = pi.path.name
            url   = _driver_raw_url(f"{rel_dir}/{driver_type}/DP_{pack}/{fname}")
            part_meta.append({
                "part_num"  : pi.part_num,
                "filename"  : fname,
                "size_bytes": pi.size_bytes,
                "sha256"    : pi.sha256,
                "url"       : url,
            })

        primary_url = part_meta[0]["url"] if part_meta else ""

        entry: Dict = {
            "id"             : entry_id,
            "pack"           : pack,
            "type"           : driver_type,
            "provider"       : provider,
            "category"       : category,
            "version"        : version,
            "driver_date"    : driver_date,
            "arch"           : arch,
            "hwids"          : hwids,
            "compatible_ids" : d.get("compatible_ids", []),
            "descriptions"   : d.get("descriptions",   []),
            "os_targets"     : d.get("os_targets",     []),
            "zip"            : primary_url,
            "zip_parts"      : len(part_meta),
            "parts"          : part_meta,
            "date_added"     : str(date.today()),
            "enabled"        : True,
            "supersedes"     : None,
            "superseded_by"  : None,
            "notes"          : "",
        }
        entries.append(entry)

    return entries, warnings


# ── Version enrichment ────────────────────────────────────────────────────────
def enrich_versions(
    new_entries : List[Dict],
    manifest    : Dict,
) -> Tuple[List[Dict], List[str]]:
    history  = manifest.setdefault("version_history", {})
    existing : Dict[str, Dict] = {e["id"]: e for e in manifest.get("drivers", [])}
    warnings : List[str] = []
    enriched_new: List[Dict] = []

    def _key(hwids, arch, cat):
        return f"{arch}|{cat.lower()}|{'|'.join(sorted(hwids))}"

    existing_by_key: Dict[str, Dict] = defaultdict(list)
    for e in manifest.get("drivers", []):
        if not e.get("enabled", True):
            continue
        k = _key(e.get("hwids", []), e.get("arch", ""), e.get("category", ""))
        existing_by_key[k].append(e)

    for new_e in new_entries:
        new_id  = new_e["id"]
        new_ver = new_e.get("version", "")
        k = _key(new_e.get("hwids", []), new_e.get("arch", ""), new_e.get("category", ""))

        prior_list = existing_by_key.get(k, [])
        if not prior_list:
            enriched_new.append(new_e)
            continue

        best_prior = max(prior_list, key=lambda e: _parse_ver(e.get("version", "")))
        old_id  = best_prior.get("id", "")
        old_ver = best_prior.get("version", "")

        if new_id == old_id:
            enriched_new.append(new_e)
            continue

        if new_ver and old_ver and version_is_newer(new_ver, old_ver):
            if old_id in existing:
                existing[old_id]["enabled"]       = False
                existing[old_id]["superseded_by"] = new_id
                existing[old_id]["notes"] = (
                    existing[old_id].get("notes", "")
                    + f" | Superseded by {new_id} on {date.today()}"
                )
            new_e["supersedes"] = old_id
            ok(f"Version upgrade: {old_id} {old_ver or '?'}  →  {new_id} {new_ver or '?'}")
            hist_list = history.setdefault(k, [])
            for h in hist_list:
                if h["id"] == old_id:
                    h["superseded_by"] = new_id
                    break
            hist_list.append({
                "id"          : new_id,
                "version"     : new_ver,
                "date_added"  : str(date.today()),
                "superseded_by": None,
            })
        elif new_ver and old_ver and not version_is_newer(new_ver, old_ver) and new_ver != old_ver:
            warnings.append(
                f"Older version incoming: {new_id} ({new_ver}) is older than "
                f"existing {old_id} ({old_ver}).  Both kept."
            )

        enriched_new.append(new_e)

    return enriched_new, warnings


# ── README badge ──────────────────────────────────────────────────────────────
# Matches only the digits inside this script's own shields.io drivers badge
# URL, e.g. ".../badge/drivers-21474-brightgreen?style=flat-square" — group 1
# and group 3 anchor the exact text immediately before/after the count, so
# subbing in the new count changes nothing else about the URL (color, style
# query params) or anything around it on the line (a "**Drivers:**" label,
# neighboring Workflow/Page/Worker badges, banner image, div wrapper, etc.).
_DRIVER_BADGE_COUNT_RE = re.compile(r"(img\.shields\.io/badge/drivers-)(\d+)(-)")


def _update_readme_badge(repo: Path, count: int) -> None:
    """Update just the driver-count number in README.md's shields.io badge.

    This does a surgical in-place substitution of the digits inside the
    existing badge URL rather than replacing a whole block of text, so
    real-world READMEs that put the badge alongside other content on the
    same line — a bold "**Drivers:**" label, additional Workflow/Page/
    Worker badges, a banner image, a <div align="center"> wrapper, etc. —
    come back byte-for-byte identical apart from that one number. It also
    works whether or not a DRIVERDEX_DRIVER_BADGE marker comment is present,
    since it matches the badge URL directly instead of relying on markers.

    Reads via _read_bytes_tracked() so that if this same README was already
    written earlier in this run (e.g. an earlier pack in the same session),
    the read is served from memory instead of hitting disk again. Writes go
    through _write_bytes_tracked() so the commit-time blob upload doesn't
    have to re-read the file we just wrote. No write happens at all if the
    count hasn't actually changed, so an unchanged badge never touches disk.
    """
    readme_path = repo / "README.md"
    badge = (
        f"![Drivers]"
        f"(https://img.shields.io/badge/drivers-{count}-brightgreen?style=flat-square)"
    )

    if not readme_path.exists():
        block = f"{BADGE_MARKER_START}\n{badge}\n{BADGE_MARKER_END}"
        _write_bytes_tracked(
            readme_path, f"# 🚀 DriverDex Builder\n\n{block}\n".encode("utf-8")
        )
        return

    text = _read_bytes_tracked(readme_path).decode("utf-8")

    # Primary path: replace only the count digits inside whatever drivers
    # badge URL already exists in the file, wherever it sits — every other
    # character in the README (other badges, labels, whitespace, the banner,
    # markup structure) is left completely untouched. If the same badge URL
    # happens to appear more than once, every occurrence is kept in sync.
    new_text, n_subs = _DRIVER_BADGE_COUNT_RE.subn(
        lambda m: f"{m.group(1)}{count}{m.group(3)}", text,
    )

    if n_subs == 0:
        # No existing drivers badge found anywhere in the file (fresh README,
        # or one that's been hand-edited to remove it) — fall back to the
        # marker-block scheme so a badge still gets added somewhere.
        block = f"{BADGE_MARKER_START}\n{badge}\n{BADGE_MARKER_END}"
        if BADGE_MARKER_START in text:
            new_text = re.sub(
                re.escape(BADGE_MARKER_START) + r".*?" + re.escape(BADGE_MARKER_END),
                block, text, flags=re.DOTALL,
            )
        else:
            new_text = block + "\n\n" + text

    if new_text != text:
        _write_bytes_tracked(readme_path, new_text.encode("utf-8"))


# ==============================================================================
#  DriverDex Contribute  --  Host Driver Auto-Upload  (standalone, no prompts)
# ------------------------------------------------------------------------------
#  Run this file with no arguments and it always does one thing:
#    1. Exports every third-party driver installed on THIS PC (DISM + pnputil).
#    2. Looks each one up on DriverDex by Hardware ID.
#    3. Classifies it PRESENT / UPDATED / MISSING.
#    4. Auto-pushes the MISSING + UPDATED ones through the archive -> manifest
#       -> commit -> push pipeline below (zip_all_drivers, build_entries,
#       enrich_versions, save_manifest, github_commit_push).
#
#  Authentication:
#    The GitHub token comes exclusively from the encrypted split-secret
#    scheme below (_dd_bootstrap_token) — a KDF salt + passphrase compiled
#    into this build decrypt an AES-256-GCM ciphertext fetched from GitHub.
#    It is never read from an environment variable or a config file, and it
#    is never written to disk — it lives in memory for the process lifetime
#    only. This is what lets an ordinary end-user run this file and push
#    detected drivers without ever owning a personal GitHub token.
#
#  This file is intentionally self-contained (compiles to a single .exe) —
#  it shares no runtime dependency on driverdex-reset.py (the separate Bulk
#  Builder tool for maintainers/power users, which keeps its own
#  interactive personal-token flow untouched). The shared upload/manifest/
#  push machinery below (github_commit_push, zip_all_drivers, build_entries,
#  etc.) is duplicated from that project's more advanced dual-repo/quota-
#  fallback infrastructure, not re-implemented from scratch.
# ==============================================================================

# ── Administrator elevation (Windows) ─────────────────────────────────────────
def _is_windows() -> bool:
    return os.name == "nt" or platform.system().lower().startswith("win")


# ── Local driver extraction (DISM + pnputil) ──────────────────────────────────
# Suppress any console window that child processes (DISM, pnputil) would
# otherwise pop up on screen.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

def _run_capture(cmd: List[str], timeout: int = 900) -> Tuple[int, str]:
    """Run a command, returning (returncode, combined_output). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace", creationflags=_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout}s"
    except Exception as exc:
        return 1, str(exc)


def _export_with_dism(dest: Path) -> Tuple[bool, int]:
    """Export all third-party drivers from the running OS with DISM."""
    dest.mkdir(parents=True, exist_ok=True)
    before = sum(1 for _ in dest.rglob("*.inf"))
    rc, out = _run_capture(
        ["dism", "/online", "/export-driver", f"/destination:{dest}"])
    after = sum(1 for _ in dest.rglob("*.inf"))
    gained = max(0, after - before)
    if rc == 0:
        ok(f"DISM exported {gained} driver package(s).")
        return True, gained
    warn(f"DISM export returned code {rc}. {out.strip().splitlines()[-1] if out.strip() else ''}")
    return False, gained


def _export_with_pnputil(dest: Path) -> Tuple[bool, int]:
    """Export all third-party drivers with pnputil (complements DISM)."""
    dest.mkdir(parents=True, exist_ok=True)
    before = sum(1 for _ in dest.rglob("*.inf"))
    # pnputil needs the destination to exist; '*' exports every OEM driver.
    rc, out = _run_capture(
        ["pnputil", "/export-driver", "*", str(dest)])
    after = sum(1 for _ in dest.rglob("*.inf"))
    gained = max(0, after - before)
    if rc == 0:
        ok(f"pnputil exported {gained} additional driver package(s).")
        return True, gained
    warn(f"pnputil export returned code {rc}. {out.strip().splitlines()[-1] if out.strip() else ''}")
    return False, gained


def extract_local_drivers(workspace: Path) -> Optional[Path]:
    """Export every third-party driver installed on this PC to a temp folder.

    Runs BOTH DISM and pnputil into the same destination so that drivers only
    one tool exposes are still captured (the .inf set is de-duplicated naturally
    by folder, and the downstream scanner reads every .inf recursively).

    Returns the destination folder, or None if nothing could be exported.
    """
    if not _is_windows():
        warn("Driver extraction is only supported on Windows.")
        return None

    dest = workspace / "extracted_drivers"
    # Start clean so a stale previous run never inflates the comparison set.
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    rule("STEP 2  |  Extracting Installed Drivers", style="bright_cyan")
    C.print()
    with C.status("[bold bright_cyan]  Exporting drivers with DISM …[/bold bright_cyan]",
                  spinner="dots12"):
        _export_with_dism(dest)
    with C.status("[bold bright_cyan]  Exporting drivers with pnputil …[/bold bright_cyan]",
                  spinner="dots12"):
        _export_with_pnputil(dest)

    total_inf = sum(1 for _ in dest.rglob("*.inf"))
    if total_inf == 0:
        warn("No drivers were exported — nothing to compare.")
        return None
    ok(f"Export complete — {total_inf} .inf file(s) found.")
    return dest


# ── SSL-safe URL opening ──────────────────────────────────────────────────────
# On fresh Windows installs Python may not find the system CA store, causing
# "SSL: CERTIFICATE_VERIFY_FAILED / unable to get local issuer certificate".
# _urlopen_ssl_safe() transparently retries with certifi, then unverified, so
# the script works on every machine without disabling TLS globally.
def _ssl_ctx(verified: bool = True):
    """Return an SSL context, falling back gracefully when the system CA store
    is broken or missing (common on corporate/fresh Windows machines)."""
    import ssl
    if not verified:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    return ssl.create_default_context()


def _urlopen_ssl_safe(req, timeout: int = 60):
    """Drop-in replacement for urllib.request.urlopen with automatic SSL fallback.

    Priority:
      1. Verified with certifi CA bundle (best — cross-platform, always up to date).
      2. Verified with the system CA store.
      3. Unverified (last resort — only reached when both verified paths fail with
         an SSL error, e.g. missing intermediate CA on a fresh Windows install).
    """
    import ssl
    # --- attempt 1: certifi / system CA (verified) ---
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(verified=True))
    except ssl.SSLError:
        pass
    except urllib.error.URLError as _e:
        # Non-SSL URLError (e.g. timeout, connection refused) — re-raise immediately
        if not isinstance(getattr(_e, "reason", None), ssl.SSLError):
            raise
    # --- attempt 2: unverified (SSL cert chain is broken on this machine) ---
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(verified=False))


# ── Encrypted GitHub token — the ONLY auth source for Automatic Contribute ───
# The token is never bundled in plaintext. tools/make_token_blob.py encrypts
# it with scrypt-derived AES-256-GCM; the salt + passphrase below are
# compiled into the build, and the ciphertext itself is fetched from
# ENC_TOKEN_URL at runtime and decrypted in memory only.
EMBED_SALT_B64       = "dMhAqGKpeR24Ot7pL6CAUKO7e3KGZr/sgQ0RxpMCjcM="
EMBED_PASSPHRASE     = "yMv-nvMx2aoFUs3P1-f32izIVjFR-mhNxiDSFJozxy_YeIPv"

# scrypt (DDX2) work factors — memory-hard, far stronger than the legacy
# PBKDF2-HMAC-SHA256 scheme used by DDX1 blobs. N must be a power of two.
EMBED_SCRYPT_N       = 32768      # CPU/memory cost (32768)
EMBED_SCRYPT_R       = 8              # block size
EMBED_SCRYPT_P       = 1             # parallelization
# Legacy PBKDF2 iteration count — only used to decrypt old DDX1 blobs.
EMBED_KDF_ITERATIONS = 200000

ENC_TOKEN_URL        = "https://raw.githubusercontent.com/rhshourav/driverdex/refs/heads/main/Docs/ki/encript"
_DDX_MAGIC           = b"DDX2"        # current format: scrypt + AES-256-GCM (AAD = magic)
_DDX_MAGIC_LEGACY    = b"DDX1"        # legacy format: PBKDF2 + AES-256-GCM (no AAD)

# DriverDex worker API — used only by the HWID-comparison check below, has
# nothing to do with GitHub auth.
DRIVERDEX_BASE_URL   = "https://driverdex-check.driverdex.workers.dev/"

# scrypt with N=32768,r=8 needs ~128*r*N ≈ 32 MiB; give it headroom.
_DDX_SCRYPT_MAXMEM   = 128 * 1024 * 1024


def _dd_derive_key_scrypt(passphrase: str, salt: bytes,
                          n: int, r: int, p: int) -> bytes:
    """Derive a 32-byte AES key with the memory-hard scrypt KDF (DDX2)."""
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=n, r=r, p=p, dklen=32, maxmem=_DDX_SCRYPT_MAXMEM,
    )


def _dd_derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """Legacy PBKDF2-HMAC-SHA256 key derivation — DDX1 blobs only."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def _dd_decrypt_blob(blob: bytes, passphrase: str, salt: bytes, iterations: int) -> str:
    """Decrypt a DDX2 (scrypt) or legacy DDX1 (PBKDF2) token blob.

    DDX2 binds the magic header as AES-GCM additional authenticated data so a
    blob cannot be silently downgraded/relabelled. DDX1 is still accepted so an
    older encrypted key file keeps working until it is rotated.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:
        raise RuntimeError("The 'cryptography' package is required to decrypt the token.") from exc

    magic_len = len(_DDX_MAGIC)  # DDX2 and DDX1 are both 4 bytes
    if len(blob) < magic_len + 12 + 16:
        raise ValueError("Encrypted token blob is too short / corrupted.")

    magic = blob[:magic_len]
    nonce = blob[magic_len: magic_len + 12]
    ciphertext = blob[magic_len + 12:]

    if magic == _DDX_MAGIC:
        key = _dd_derive_key_scrypt(passphrase, salt,
                                    EMBED_SCRYPT_N, EMBED_SCRYPT_R, EMBED_SCRYPT_P)
        aad: Optional[bytes] = _DDX_MAGIC
    elif magic == _DDX_MAGIC_LEGACY:
        key = _dd_derive_key(passphrase, salt, iterations)
        aad = None
    else:
        raise ValueError("Encrypted token blob has an unexpected format (bad magic).")

    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise ValueError("Failed to decrypt token (wrong salt/passphrase or tampered blob).") from exc
    return plaintext.decode("utf-8").strip()


def _dd_fetch_encrypted_blob(url: str, timeout: int = 30, max_attempts: int = 4) -> bytes:
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "driverdex-check/1.0", "Cache-Control": "no-cache"})
            with _urlopen_ssl_safe(req, timeout=timeout) as resp:
                raw = resp.read()
            # The key file may be stored as base64 text or as raw binary bytes.
            # Prefer base64 (what tools/make_token_blob.py writes); if it does not
            # decode to a valid DDX blob, fall back to using the raw bytes.
            text = raw.decode("utf-8", "ignore").strip()
            try:
                decoded = base64.b64decode(text, validate=True)
                if decoded[:4] in (_DDX_MAGIC, _DDX_MAGIC_LEGACY):
                    return decoded
            except Exception:
                pass
            return raw
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (404, 403):
                break
        except Exception as exc:
            last_exc = exc
        if attempt < max_attempts:
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"Could not fetch encrypted token blob from {url}: {last_exc}")


def _dd_bootstrap_token(*, quiet: bool = False) -> Optional[str]:
    """Resolve the GitHub token from the encrypted split-secret scheme ONLY.

    Downloads the AES-256-GCM ciphertext from ENC_TOKEN_URL (GitHub raw) and
    decrypts it with the base64 salt + passphrase embedded in this build. The
    decrypted token lives in memory only — it is never written to disk, an
    environment variable, or any config file.

    Returns the token string, or None if the build still carries placeholder
    secrets or the encrypted blob could not be fetched / decrypted.
    """
    def _say(msg: str) -> None:
        if not quiet:
            info(msg)

    placeholder = (EMBED_PASSPHRASE.startswith("REPLACE_ME")
                   or set(EMBED_SALT_B64.rstrip("=")) <= {"A"})
    if placeholder:
        _say("DriverDex secret still has placeholder values — run tools/make_token_blob.py and rebuild.")
        return None

    # Reject a salt that is the wrong length for the DDX2 scheme (32 bytes).
    try:
        _salt_probe = base64.b64decode(EMBED_SALT_B64)
        if len(_salt_probe) < 16:
            _say("Embedded salt looks too small for DDX2 — regenerate with tools/make_token_blob.py.")
    except Exception:
        pass

    try:
        salt = base64.b64decode(EMBED_SALT_B64)
    except Exception as exc:
        _say(f"Invalid EMBED_SALT_B64: {exc}")
        return None
    if not salt:
        _say("Embedded salt is empty.")
        return None

    try:
        blob = _dd_fetch_encrypted_blob(ENC_TOKEN_URL)
        token = _dd_decrypt_blob(blob, EMBED_PASSPHRASE, salt, EMBED_KDF_ITERATIONS)
    except Exception as exc:
        _say(f"Could not bootstrap token from encrypted key file: {exc}")
        return None

    if token:
        _say("Bootstrapped GitHub token from encrypted raw key file.")
        return token
    _say("Decrypted token was empty.")
    return None


# ── GitHub token — the ONLY auth source in this file ─────────────────────────
_TOKEN_CACHE: List[str] = [""]
_TOKEN_CACHE_LOCK = threading.Lock()


def _load_token() -> str:
    """Return the GitHub token, decrypting it from the embedded/encrypted
    source on first use and caching it in memory only. This is the SINGLE
    source of truth in this file — there is no environment-variable,
    config-file, or interactive fallback. Every _api()/_api_with_retry()
    call routes through this.
    """
    if _TOKEN_CACHE[0]:
        return _TOKEN_CACHE[0]
    with _TOKEN_CACHE_LOCK:
        if _TOKEN_CACHE[0]:
            return _TOKEN_CACHE[0]
        tok = _dd_bootstrap_token(quiet=True) or ""
        _TOKEN_CACHE[0] = tok
        return tok


def _refresh_github_token(label: str = "", failed_token: str = "") -> bool:
    """Recover from an HTTP 401.

    Because the token comes only from the encrypted split-secret scheme, the
    sole recovery path is to re-fetch and re-decrypt the key file from the
    repo (in case it was rotated since this process started). No prompt is
    ever shown — there is no personal token to paste in this file.

    On permanent failure _TOKEN_REFRESH_DECLINED is set so all parallel
    upload threads stop immediately instead of each looping on 401.

    `failed_token`, when supplied, is the exact token string that just got
    rejected. Under 12-way concurrent uploads, a single revoked/rotated
    token makes EVERY in-flight thread hit 401 at roughly the same moment,
    and each one used to independently clear the shared cache and re-fetch
    + re-decrypt the same blob from scratch — a thundering herd of redundant
    network round-trips that also raced each other to write the cache. Now,
    once a thread gets the lock, it first checks whether the cache already
    moved past `failed_token` (i.e. another thread already refreshed it
    moments ago); if so, that already-fresh token is reused immediately
    instead of fetching it all over again.
    """
    if _TOKEN_REFRESH_DECLINED.is_set():
        return False
    with _TOKEN_CACHE_LOCK:
        if failed_token and _TOKEN_CACHE[0] and _TOKEN_CACHE[0] != failed_token:
            return True  # someone else already refreshed it — reuse, don't re-fetch
        _TOKEN_CACHE[0] = ""
        new_tok = _dd_bootstrap_token(quiet=True) or ""
        _TOKEN_CACHE[0] = new_tok
    if new_tok:
        ok("Re-decrypted GitHub token from encrypted key file"
           + (f" [dim]({escape(label)})[/dim]" if label else "") + " — resuming …")
        return True
    _TOKEN_REFRESH_DECLINED.set()
    err("HTTP 401 — the embedded GitHub token was rejected and could not be "
        "recovered. Rotate the encrypted key file with tools/make_token_blob.py "
        "and rebuild.")
    return False



def check_github_token() -> bool:
    """GitHub token check.

    Checks both the manifests repo and the drivers repo (github_commit_push
    below splits pushes across the two), sourced only from the encrypted
    embedded scheme — failures never suggest creating a personal token.
    """
    C.print()
    _load_repo_state()   # restore the last-known-active drivers repo (quota fallback)
    tok = _load_token()
    if not tok:
        err("No usable GitHub token.")
        hint("This build could not decrypt an embedded write token. Either it "
             "still carries placeholder secrets, or the encrypted key file could "
             "not be fetched/decrypted. Rotate the key file with "
             "tools/make_token_blob.py and rebuild.")
        return False

    with C.status("[bold bright_cyan]  Verifying GitHub token …[/bold bright_cyan]", spinner="dots12"):
        try:
            status, resp = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}", token=tok)
        except KeyboardInterrupt:
            C.print()
            warn("Token check interrupted.")
            return False

    if status != 200:
        if status == 401:
            err("Embedded GitHub token is invalid or expired — rotate the encrypted key file.")
        elif status == 403:
            err("Embedded GitHub token lacks required permissions (needs 'repo' scope).")
        elif status == 404:
            err(f"Repo {REPO_OWNER}/{REPO_NAME} not found.")
        else:
            err(f"GitHub API returned HTTP {status}: {resp.get('message', '')}")
        return False

    ok(f"GitHub token accepted — repo: {resp.get('full_name', REPO_OWNER + '/' + REPO_NAME)}")

    if (DRIVERS_REPO_OWNER, DRIVERS_REPO_NAME) != (REPO_OWNER, REPO_NAME):
        with C.status("[bold bright_cyan]  Verifying drivers repo access …[/bold bright_cyan]", spinner="dots12"):
            try:
                d_status, d_resp = _api("GET", f"/repos/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}", token=tok)
            except KeyboardInterrupt:
                C.print()
                warn("Drivers-repo check interrupted.")
                return False
        if d_status == 200:
            ok(f"Drivers repo accessible: {d_resp.get('full_name', DRIVERS_REPO_OWNER + '/' + DRIVERS_REPO_NAME)}")
        elif d_status == 404:
            err(f"Drivers repo {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} not found — "
                f"create it (with an initial commit on '{GH_BRANCH}') so driver archives have a home.")
            return False
        elif d_status == 403:
            if _is_repo_quota_error(d_status, d_resp):
                warn(f"Drivers repo {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} is above its size quota.")
                _mark_repo_spent(DRIVERS_REPO_NAME)
                if not _advance_drivers_repo_or_notify():
                    return False
                ok(f"Auto-switched to {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} before the first push.")
            else:
                err(f"Embedded token cannot access {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} (needs 'repo' push scope).")
                return False
        else:
            warn(f"Drivers repo check returned HTTP {d_status}: {d_resp.get('message', '')} "
                 f"— driver uploads may fail.")
    return True


# ── DriverDex API client (cached, retrying) ─────────────────────────────────────
class _DDCacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: object, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class DriverDexAPI:
    """REST client for the DriverDex worker API with caching + exponential backoff."""

    def __init__(self, base_url: str = DRIVERDEX_BASE_URL, timeout: int = 30,
                 cache_ttl: int = 3600, max_attempts: int = 5, backoff_base: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self._cache: Dict[str, _DDCacheEntry] = {}
        self._cache_lock = threading.Lock()

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, object]:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "driverdex-check/1.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_st, last_payload = 0, {}
        for attempt in range(1, self.max_attempts + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
                with _urlopen_ssl_safe(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return resp.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as exc:
                last_st = exc.code
                try:
                    last_payload = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    last_payload = {"message": str(exc)}
                if exc.code in (401, 403, 404):
                    return exc.code, last_payload
                wait = self._retry_wait(attempt, exc.headers.get("Retry-After") if exc.headers else None)
            except Exception as exc:
                last_st, last_payload = 0, {"message": str(exc)}
                wait = self._retry_wait(attempt, None)
            if last_st not in _RETRY_STATUSES and last_st != 0:
                break
            if attempt < self.max_attempts:
                time.sleep(wait)
        return last_st, last_payload

    def _retry_wait(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except ValueError:
                pass
        return _jitter(min(self.backoff_base * (2 ** (attempt - 1)), 120.0))

    def _cached_get(self, path: str, ttl: Optional[int] = None) -> object:
        ttl = self.cache_ttl if ttl is None else ttl
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(path)
            if hit and hit.expires_at > now:
                return hit.value
        st, payload = self._request("GET", path)
        if st == 200:
            with self._cache_lock:
                self._cache[path] = _DDCacheEntry(payload, now + ttl)
            return payload
        raise RuntimeError(f"GET {path} failed (HTTP {st}): {self._msg(payload)}")

    @staticmethod
    def _msg(payload: object) -> str:
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or payload)
        return str(payload)

    def get_stats(self) -> dict:
        return self._cached_get("/api/stats")  # type: ignore[return-value]

    def get_facets(self) -> dict:
        return self._cached_get("/api/facets")  # type: ignore[return-value]

    def get_driver(self, driver_id: str) -> dict:
        return self._cached_get(f"/api/driver/{driver_id}")  # type: ignore[return-value]

    def get_driver_versions(self, driver_id: str) -> list:
        return self._cached_get(f"/api/driver/{driver_id}/versions")  # type: ignore[return-value]

    def get_hwid(self, hwid: str) -> dict:
        return self._cached_get(f"/api/hwid/{hwid}")  # type: ignore[return-value]

    def search(self, page_size: int = 100, **filters) -> List[dict]:
        results: List[dict] = []
        page = 1
        while True:
            params = {k: v for k, v in filters.items() if v not in (None, "")}
            params.update({"page": page, "pageSize": page_size, "enabledOnly": 1, "includeNoHwid": 1})
            query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            payload = self._cached_get(f"/api/search?{query}")
            batch = payload.get("results", payload) if isinstance(payload, dict) else payload
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
            if page > 1000:
                break
        return results


# ── local PC vs DriverDex comparison (Hardware ID + class) ───────────────────
def _norm_hwid(h: str) -> str:
    """Canonicalise a hardware/compatible ID for matching."""
    return re.sub(r"\s+", "", (h or "").strip().upper())


def _norm_class(s: str) -> str:
    """Normalise a driver class/category string to a comparable token.

    Remote records may carry either a raw Windows setup class (e.g. "Net",
    "HDC") or a friendly DriverDex type; map both through the same table so a
    local "Net" matches a remote "network", etc.
    """
    s = (s or "").strip().lower()
    return _CLASS_TO_TYPE.get(s, s)


def _parse_driver_date(s: str) -> Optional[Tuple[int, int, int]]:
    """Parse an INF DriverVer date 'MM/DD/YYYY' into a sortable (Y, M, D).

    Note: this script's parse_inf() does not currently split a bare
    "MM/DD/YYYY" DriverVer value into a separate driver_date field the way
    driverDexBG.py's does — it only populates "version". That just means the
    date-tiebreak branch of _local_is_newer() below rarely fires here;
    version-based comparison (the primary signal) is unaffected.
    """
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", (s or "").strip())
    if not m:
        return None
    mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000 if yr < 70 else 1900
    return (yr, mo, da)


def _dd_parse_version(v: str) -> Tuple[int, ...]:
    if not v:
        return (0,)
    head = str(v).strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: List[int] = []
    for chunk in head.replace("_", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _dd_cmp_versions(local: str, remote: str) -> int:
    a, b = _dd_parse_version(local), _dd_parse_version(remote)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def _local_is_newer(local_ver: str, local_date: str,
                    remote_ver: str, remote_date: str) -> bool:
    """Return True when the local driver should supersede the DriverDex record.

    Primary signal is the INF DriverVer *version*; the *date* breaks ties or is
    used when a version string is missing/equal on both sides.
    """
    vc = _dd_cmp_versions(local_ver or "0", remote_ver or "0")
    if vc > 0:
        return True
    if vc < 0:
        return False
    ld, rd = _parse_driver_date(local_date), _parse_driver_date(remote_date)
    if ld and rd:
        return ld > rd
    return False


def _dd_build_local_hwid_index(
    repo: Path, shard_cache: Optional[Dict[Path, Dict]] = None
) -> Dict[str, List[dict]]:
    """Build an in-memory Hardware-ID -> [driver record] index straight from
    the category manifest shards already synced to disk under
    `repo/manifests/` (STEP 1 / `github_pull_manifest()`).

    This replaces the old approach of firing one `/api/hwid/:hwid` HTTP
    request per unique Hardware ID (`DriverDexAPI.get_hwid()` via the old
    `_prefetch_hwids()`): the manifest IS the same catalog that endpoint
    reads from, and it's already sitting on disk by the time this runs. On a
    PC with 10k+ Hardware IDs, that used to mean 10k+ network round-trips
    (even at 16-way concurrency, still slow and rate-limit-prone); reading
    the shards once and bucketing every driver by each of its HWIDs turns
    every lookup afterward into an O(1) local dict read with zero network
    I/O.
    """
    combined = load_all_manifests_combined(repo, shard_cache=shard_cache)
    index: Dict[str, List[dict]] = defaultdict(list)
    for d in combined.get("drivers", []):
        for h in d.get("hwids", []):
            nh = _norm_hwid(h)
            if nh:
                index[nh].append(d)
    return index


class LocalPCComparator:
    """Compare drivers extracted from this PC against the DriverDex database.

    Matching identity is **Hardware ID + class**: a local driver is considered
    'already in DriverDex' only when a remote record shares one of its hardware
    IDs *and* the same driver class. Selection rules:

        • no HWID+class match in DriverDex            -> MISSING  (upload)
        • match exists but local INF version/date is
          newer than every matching remote record     -> UPDATED  (upload)
        • match exists and remote is same/newer        -> PRESENT  (skip)
    """

    def __init__(self, repo_dir: Path, extracted_dir: Path) -> None:
        self.repo_dir = Path(repo_dir)
        self.extracted_dir = Path(extracted_dir)
        # Built once, straight from the manifest shards already on disk --
        # no network calls. See _dd_build_local_hwid_index().
        self._hwid_index: Dict[str, List[dict]] = _dd_build_local_hwid_index(self.repo_dir)

    # -- local side -----------------------------------------------------------
    def _scan_local(self) -> List[Dict]:
        records: List[Dict] = []
        groups = group_infs_by_folder(scan_infs(self.extracted_dir))
        for folder, group in groups:
            hwids: set = set()
            versions: List[str] = []
            dates: List[str] = []
            providers: List[str] = []
            for _, d in group:
                for h in d.get("hwids", []) + d.get("compatible_ids", []):
                    nh = _norm_hwid(h)
                    if nh:
                        hwids.add(nh)
                if d.get("version"):
                    versions.append(d["version"])
                if d.get("driver_date"):
                    dates.append(d["driver_date"])
                if d.get("provider"):
                    providers.append(d["provider"])
            if not hwids:
                continue  # nothing to match on
            best_ver = max(versions, key=_dd_parse_version) if versions else ""
            best_date, best_dt = "", None
            for ds in dates:
                pd = _parse_driver_date(ds)
                if pd and (best_dt is None or pd > best_dt):
                    best_dt, best_date = pd, ds
            records.append({
                "folder": folder,
                "infs": [p for p, _ in group],
                "hwids": sorted(hwids),
                "type": classify_group(group),
                "version": best_ver,
                "driver_date": best_date,
                "provider": providers[0] if providers else "",
            })
        return records

    # -- remote side (now: local-manifest side) --------------------------------
    def _remote_for_hwid(self, hwid: str) -> List[dict]:
        """O(1) lookup against the in-memory index built from the manifest
        shards already on disk -- no network call, no cache-miss branch."""
        return self._hwid_index.get(hwid, [])

    # -- comparison -----------------------------------------------------------
    def get_upload_candidates(self) -> Dict:
        local_records = self._scan_local()
        missing: List[Dict] = []
        updated: List[Dict] = []
        present: List[Dict] = []

        prog = task = None
        if local_records:
            prog = Progress(
                SpinnerColumn("dots12", style="bold bright_cyan"),
                TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
                BarColumn(bar_width=None, complete_style="bold bright_cyan",
                          finished_style="bold bright_green"),
                TaskProgressColumn(style="bold white"), MofNCompleteColumn(),
                console=C, transient=True, expand=True,
            )
            prog.start()
            task = prog.add_task("Comparing to DriverDex", total=len(local_records))

        try:
            for rec in local_records:
                ltype = _norm_class(rec["type"])
                # gather de-duplicated remote drivers across this driver's HWIDs
                remote_by_id: Dict[str, dict] = {}
                for hwid in rec["hwids"]:
                    for rd in self._remote_for_hwid(hwid):
                        rid = str(rd.get("id") or rd.get("driver_id") or id(rd))
                        remote_by_id.setdefault(rid, rd)

                class_matches = []
                for rd in remote_by_id.values():
                    rcat = _norm_class(rd.get("category") or rd.get("class") or "")
                    if not rcat or rcat == ltype:
                        class_matches.append(rd)

                if not class_matches:
                    rec["reason"] = "missing"
                    missing.append(rec)
                else:
                    newer_than_all = True
                    remote_ver = ""
                    for rd in class_matches:
                        rver = str(rd.get("version") or rd.get("latest_version") or "0")
                        rdate = str(rd.get("driver_date") or rd.get("date") or "")
                        remote_ver = remote_ver or rver
                        if not _local_is_newer(rec["version"], rec["driver_date"], rver, rdate):
                            newer_than_all = False
                    if newer_than_all:
                        rec["reason"] = "updated"
                        rec["remote_version"] = remote_ver
                        updated.append(rec)
                    else:
                        present.append(rec)

                if prog is not None:
                    prog.advance(task, 1)
        finally:
            if prog is not None:
                prog.stop()

        return {
            "missing": missing,
            "updated": updated,
            "present": present,
            "local_total": len(local_records),
        }


# ── uploader identity (7-second timeout, single prompt) ─────────────────────────
class UserIdentityCollector:
    def __init__(self, timeout_sec: float = 7.0) -> None:
        self.timeout_sec = timeout_sec

    def prompt_for_identity(self) -> Tuple[str, str]:
        C.print("  [yellow]Enter your name/username/email:[/yellow]  "
                f"[dim](Press Enter to skip, timeout in {int(self.timeout_sec)}s)[/dim]")
        result: Dict[str, str] = {}

        def _reader() -> None:
            try:
                result["value"] = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                result["value"] = ""

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(self.timeout_sec)
        if t.is_alive():
            return self._fallback("timeout")
        entered = result.get("value", "")
        if entered:
            if entered.lower() == "generic":
                return ("driverdex Community upload", "user_input")
            return (entered, "user_input")
        return self._fallback("system_fallback")

    @staticmethod
    def _fallback(source: str) -> Tuple[str, str]:
        try:
            name = getpass.getuser()
        except Exception:
            name = "unknown"
        if not name or name.lower() == "generic":
            name = "driverdex Community upload"
        return (name, source)


# ── reporting ────────────────────────────────────────────────────────────────-
def _dd_render_hwid_report(report: dict) -> None:
    """Render the result of the Hardware-ID check against DriverDex."""
    missing = report.get("missing", [])
    updated = report.get("updated", [])
    present = report.get("present", [])
    body = "\n".join([
        f"  Local drivers checked : [white]{report.get('local_total', 0)}[/white]",
        f"  Already in DriverDex  : [bright_green]{len(present)}[/bright_green]",
        f"  Newer locally (upload): [yellow]{len(updated)}[/yellow]",
        f"  Missing (upload)      : [bright_magenta]{len(missing)}[/bright_magenta]",
    ])
    C.print(Panel(body, title="[cyan]HWID Check[/cyan]", border_style="bright_cyan", padding=(0, 2)))

    rows = [("MISSING", "bright_magenta", missing), ("UPDATED", "yellow", updated)]
    if any(items for _, _, items in rows):
        table = Table(title="Drivers to upload", border_style="dim cyan", expand=True)
        for col in ("Status", "Driver / Folder", "Type", "Local ver", "HWIDs"):
            table.add_column(col)
        for label, color, items in rows:
            for d in items[:50]:
                folder = Path(d.get("folder", "")).name or d.get("folder", "")
                hwids = d.get("hwids", [])
                hwid_preview = ", ".join(hwids[:2]) + (f" +{len(hwids) - 2}" if len(hwids) > 2 else "")
                table.add_row(f"[{color}]{label}[/{color}]", folder, d.get("type", ""),
                              d.get("version", "") or "n/a", hwid_preview)
        C.print(table)


# ── non-interactive upload of HWID-detected gaps ────────────────────────────────
def _dd_upload_candidates(workspace: Path, extracted_dir: Path,
                          candidates: List[dict], identity: str) -> dict:
    """Archive + manifest + push the local driver folders flagged by the HWID check.

    Reuses the exact same archiving/manifest/commit pipeline as the Bulk
    Builder pack flow above (zip_all_drivers, build_entries, enrich_versions,
    save_manifest, github_commit_push), but runs unattended on just the
    candidate folders.
    """
    pack_name = re.sub(r'[^A-Za-z0-9_.\-]', '_',
                       f"LocalPC_{platform.node()}_{datetime.now():%Y%m%d}")[:32] or "LocalPC"

    # This dict is what actually reaches _SESSION_STATS["packs"] (see the
    # try/finally at the bottom). Previously nothing ever appended anything
    # there, so _send_session_completion() was always summing over an empty
    # list -- every field in the "SESSION COMPLETE" notification (Packs,
    # Drivers, Installers, Raw/Compressed size, Errors) was structurally
    # guaranteed to read 0/none no matter what actually happened during the
    # run. Building this up as the pipeline progresses, and appending it
    # unconditionally in `finally`, makes the notification reflect reality
    # even on early-return or exception paths.
    pack_stats: Dict = {
        "pack_name"        : pack_name,
        "push_ok"          : False,
        "drivers_added"    : 0,
        # This single-PC flow only ever writes category driver manifests
        # (build_entries/save_manifest below) -- it never touches
        # installers.manifest.json, that's the Bulk Builder's job. So 0 here
        # is the correct steady-state value, not a placeholder.
        "installers_added" : 0,
        "total_raw_bytes"  : 0,
        "total_arc_bytes"  : 0,
        "group_types"      : Counter(),
        "errors"           : [],
    }

    try:
        all_inf = scan_infs(extracted_dir)
        groups = group_infs_by_folder(all_inf)
        cand_folders = {str(Path(c["folder"]).resolve()) for c in candidates if c.get("folder")}
        selected = [(folder, grp) for folder, grp in groups
                    if str(Path(folder).resolve()) in cand_folders]
        if not selected:
            warn("No matching local driver folders to upload.")
            pack_stats["errors"].append("no matching local driver folders to upload")
            return {"uploaded": 0, "committed": False}

        inf_data: List[Tuple[Path, Dict]] = [pair for _, grp in selected for pair in grp]
        type_map: Dict[Path, str] = {folder: classify_group(grp) for folder, grp in selected}

        # Pull the remote index + every existing manifest so the new drivers are
        # APPENDED to the current catalog, never overwriting it. reset.py's
        # github_pull_rebase() already pulls every category + installer + README
        # manifest in one call (a superset of driverDexBG.py's narrower
        # github_pull_skeleton()+github_pull_category_manifests(touched_cats)), so
        # it's reused as-is here instead of porting those two functions. If the
        # pull fails we abort instead of risking an overwrite.
        if not github_pull_rebase(workspace):
            err("Could not pull existing manifests — aborting upload to protect the remote catalog.")
            pack_stats["errors"].append("github_pull_rebase failed")
            return {"uploaded": 0, "committed": False}

        # Baseline entry count, taken right after the pull and before this run
        # writes anything. save_manifest() is only ever supposed to APPEND (see
        # the extend() call below) -- this number is what "no data was deleted"
        # gets checked against once every category touched by this run has been
        # saved, further down.
        baseline_total = _count_total_drivers(workspace)

        staging_dir = workspace / DRIVERS_DIR / f"_staging_DP_{pack_name}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        new_entries: List[Dict] = []
        try:
            rule("UPLOAD  |  Archiving local drivers", style="bright_cyan")
            C.print()
            zip_map, total_verified, total_raw_bytes = zip_all_drivers(
                src=extracted_dir, dest_dir=staging_dir, pack=pack_name, inf_data=inf_data)
            ok(f"Archiving complete — {total_verified} verified archive part(s).")
            pack_stats["total_raw_bytes"] = total_raw_bytes
            pack_stats["total_arc_bytes"] = sum(
                pi.size_bytes for parts in zip_map.values() for pi in parts)

            # Move archives into drivers/<Type>/DP_<pack>/
            type_to_groups: Dict[str, List[Tuple[Path, List[PartInfo]]]] = defaultdict(list)
            for folder, parts in zip_map.items():
                type_to_groups[type_map.get(folder, _TYPE_FALLBACK)].append((folder, parts))
            for dtype, group_parts in type_to_groups.items():
                dest_root = workspace / DRIVERS_DIR / dtype / f"DP_{pack_name}"
                dest_root.mkdir(parents=True, exist_ok=True)
                for _folder, parts in group_parts:
                    for pi in parts:
                        dest = dest_root / pi.path.name
                        if dest.exists():
                            # shutil.move() silently replaces an existing file
                            # at `dest` -- this only happens on a same-day
                            # rerun that regenerates an identically-named
                            # archive part (workspace/ is additive across
                            # runs, never wiped -- see setup_workspace()).
                            # Nothing on GitHub is affected either way (this
                            # is pre-push local staging), but it should never
                            # be silent.
                            warn(f"Local staging collision: {dest.name} already existed "
                                 f"under {dest.parent.name}/ — replacing with this run's "
                                 f"freshly archived copy.")
                        shutil.move(str(pi.path), str(dest))
                        pi.path = dest

            manifest = load_all_manifests_combined(workspace)
            new_entries, build_warnings = build_entries(
                pack=pack_name, inf_data=inf_data, zip_map=zip_map,
                rel_dir=DRIVERS_DIR, src=extracted_dir, zip_dest=staging_dir, type_map=type_map)
            for w in build_warnings:
                warn(w)

            if new_entries:
                new_entries, ver_warnings = enrich_versions(new_entries, manifest)
                for w in ver_warnings:
                    warn(w)
                by_type: Dict[str, List[Dict]] = defaultdict(list)
                for e in new_entries:
                    by_type[e["type"]].append(e)
                for dtype, type_entries in by_type.items():
                    cat_manifest = load_manifest(workspace, cat=dtype)
                    cat_manifest.setdefault("drivers", []).extend(type_entries)
                    save_manifest(cat_manifest, workspace, cat=dtype)
                    ok(f"[bold]{escape(_category_manifest_rel(dtype))}[/bold] — {len(type_entries)} driver(s) saved.")
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

        if not new_entries:
            warn("Nothing new to commit after manifest build.")
            pack_stats["errors"].append("no new entries after manifest build")
            return {"uploaded": 0, "committed": False}

        pack_stats["drivers_added"] = len(new_entries)
        pack_stats["group_types"] = Counter(e["type"] for e in new_entries)

        # ── never-shrink guard ───────────────────────────────────────────────
        # save_manifest() is only ever supposed to append (cat_manifest["drivers"]
        # is extended, never filtered/reassigned), so the total across every
        # category manifest on disk right now must be at least baseline_total +
        # len(new_entries). If it's lower, something clobbered existing entries
        # -- a stale/partial local shard, a category-discovery miss, manual
        # editing, anything -- and pushing would silently delete real DriverDex
        # data. Refuse instead: nothing has been pushed to GitHub yet at this
        # point, so refusing here costs nothing but this run's upload, while
        # pushing would corrupt the shared catalog for everyone.
        post_save_total = _count_total_drivers(workspace)
        expected_min = baseline_total + len(new_entries)
        if post_save_total < expected_min:
            guard_msg = (
                f"manifest entry count after save ({post_save_total}) is lower "
                f"than expected ({expected_min} = {baseline_total} existing + "
                f"{len(new_entries)} new) — push refused to protect the remote catalog"
            )
            err(
                f"Refusing to push — {guard_msg}. The workspace's local manifests/ "
                f"folder has NOT been pushed and can be inspected before re-running; "
                f"a fresh run will re-pull a clean copy from GitHub regardless."
            )
            pack_stats["errors"].append(guard_msg)
            return {"uploaded": 0, "committed": False}

        _update_readme_badge(workspace, _count_total_drivers(workspace))
        commit_msg = f"DriverDex sync: {len(new_entries)} local driver(s) by {identity}"
        committed = github_commit_push(workspace=workspace, commit_msg=commit_msg)
        pack_stats["push_ok"] = bool(committed)
        if committed:
            ok(f"Push complete — {_count_total_drivers(workspace)} total drivers in repo.")
        else:
            err("Push failed — staged manifests remain in workspace for manual recovery.")
            pack_stats["errors"].append("github_commit_push failed")
        return {"uploaded": len(new_entries), "committed": bool(committed)}
    except Exception as exc:
        pack_stats["errors"].append(str(exc))
        raise
    finally:
        _SESSION_STATS["packs"].append(pack_stats)


def _dd_render_completion(summary: dict) -> None:
    body = "\n".join([
        f"  Status         : [white]{summary['status']}[/white]",
        f"  Local drivers  : [white]{summary.get('local_total', 0)}[/white]",
        f"  Already present: [bright_green]{summary.get('present', 0)}[/bright_green]",
        f"  Uploaded       : [bright_magenta]{summary.get('uploaded', 0)}[/bright_magenta]",
        f"  Uploader       : [white]{summary['uploader']}[/white]",
        f"  Duration       : [white]{summary['duration']}[/white]",
    ])
    style = "bright_green" if summary["status"] == "SUCCESS" else "yellow"
    C.print(Panel(body, title=f"[bold {style}]Sync Complete[/bold {style}]",
                  border_style=style, padding=(0, 2)))


# ── optional tuning knobs (never credentials — see drivedex_config.json note
#    at the top of the file) ───────────────────────────────────────────────────
def _dd_load_config() -> dict:
    try:
        if _CONFIG_FILE.exists():
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ── orchestrator: discover -> download -> identity -> upload ────────────────────
def sync_drivers_to_driverdex(
    repo_dir: Optional[Path] = None,
    driverdex_base_url: str = DRIVERDEX_BASE_URL,
    parallel_workers: int = 12,
    skip_upload: bool = False,
    cache_ttl: int = 3600,
    identity_timeout: float = 7.0,
    auto_confirm: bool = False,
) -> dict:
    """Check this PC's drivers against DriverDex by Hardware ID and upload gaps.

    The flow is entirely **HWID-driven** — it never pulls the remote catalog or
    downloads remote manifests up front. Instead it:

        1. Exports the third-party drivers installed on this PC (DISM + pnputil).
        2. Collects each driver's Hardware IDs and queries /api/hwid/:hwid to see
           whether DriverDex already has a matching record.
        3. Classifies every local driver as PRESENT / UPDATED / MISSING.
        4. Auto-uploads the MISSING (and locally-newer UPDATED) drivers.
    """
    start = time.time()
    errors: List[str] = []
    cfg = _dd_load_config()
    base_url = cfg.get("driverdex_base_url", driverdex_base_url)
    cache_ttl = int(cfg.get("driverdex_cache_ttl", cache_ttl))
    parallel_workers = int(cfg.get("driverdex_upload_parallel_workers", parallel_workers))
    identity_timeout = float(cfg.get("driverdex_identity_prompt_timeout", identity_timeout))

    if not skip_upload:
        if not _dd_bootstrap_token(quiet=False):
            warn("GitHub token could not be bootstrapped — uploads may fail.")

    workspace = Path(repo_dir) if repo_dir else WORKSPACE_DIR

    # ── 1. Export this PC's installed drivers ────────────────────────────────────
    try:
        extracted_dir = extract_local_drivers(workspace)
    except Exception as exc:
        errors.append(f"driver extraction failed: {exc}")
        extracted_dir = None

    empty_report = {"missing": [], "updated": [], "present": [], "local_total": 0}
    if not extracted_dir:
        warn("No local drivers available to check — skipping DriverDex sync.")
        _dd_render_hwid_report(empty_report)
        return {"hwid_report": empty_report, "upload_results": {}, "errors": errors,
                "stats": {"duration": round(time.time() - start, 1)}}

    # ── 2. Compare against DriverDex by Hardware ID ──────────────────────────────
    try:
        with C.status("[bold bright_cyan]  Checking Hardware IDs against DriverDex …[/bold bright_cyan]",
                      spinner="dots12"):
            report = LocalPCComparator(workspace, extracted_dir).get_upload_candidates()
    except Exception as exc:
        errors.append(f"HWID check failed: {exc}")
        report = dict(empty_report)

    _dd_render_hwid_report(report)

    # Upload candidates = drivers not in DriverDex (missing) + locally newer (updated).
    candidates = report.get("missing", []) + report.get("updated", [])

    if not candidates:
        ok("Every local driver is already in DriverDex — nothing to upload.")
        return {"hwid_report": report, "upload_results": {}, "errors": errors,
                "stats": {"duration": round(time.time() - start, 1)}}

    if auto_confirm:
        info(f"Auto-uploading {len(candidates)} driver(s) missing from DriverDex.")

    if auto_confirm:
        identity, source = UserIdentityCollector._fallback("system_auto")
    else:
        identity, source = UserIdentityCollector(identity_timeout).prompt_for_identity()
    info(f"Uploader identity: [cyan]{identity}[/cyan] [dim]({source})[/dim]")

    # ── 3. Upload the gaps ───────────────────────────────────────────────────────
    upload_results: dict = {"uploaded": 0, "committed": False}
    if skip_upload:
        info("skip_upload=True — HWID report only, no GitHub commit.")
    else:
        try:
            upload_results = _dd_upload_candidates(workspace, extracted_dir, candidates, identity)
            if not upload_results.get("committed"):
                errors.append("github_commit_push reported failure")
        except Exception as exc:
            errors.append(f"upload failed: {exc}")
            err(f"Upload failed: {exc}")

    status = "SUCCESS" if not errors else ("PARTIAL" if upload_results.get("committed") else "FAILED")
    summary = {
        "status": status,
        "local_total": report.get("local_total", 0),
        "uploaded": upload_results.get("uploaded", 0),
        "present": len(report.get("present", [])),
        "missing": len(report.get("missing", [])),
        "updated": len(report.get("updated", [])),
        "uploader": identity,
        "duration": _fmt_duration(time.time() - start),
    }
    _dd_render_completion(summary)
    return {"hwid_report": report, "upload_results": upload_results,
            "errors": errors, "stats": summary}


def driverdex_sync_entry(repo_dir: Path, *, skip_upload: bool = False,
                         auto_confirm: bool = False) -> dict:
    """Menu-friendly wrapper used by run_auto_contribute() below."""
    rule("DRIVERDEX  |  Check & Sync", style="bright_cyan")
    C.print()
    try:
        return sync_drivers_to_driverdex(
            repo_dir=repo_dir, skip_upload=skip_upload, auto_confirm=auto_confirm)
    except Exception as exc:
        err(f"DriverDex sync failed: {exc}")
        return {"errors": [str(exc)]}


# ── entry point ───────────────────────────────────────────────────────────────
def run_auto_contribute() -> None:
    """Fully automatic path: no menu, no prompts, one pass, then exit.

    Exports this PC's drivers, checks them against DriverDex, and pushes
    anything missing/newer through the workspace/pull/commit/push pipeline
    below. The GitHub token is sourced entirely from _load_token() (the
    embedded encrypted scheme, defined above) — there is nothing else to
    configure.
    """
    _SESSION_STATS["start_time"] = time.time()
    _SESSION_STATS["user"]       = getpass.getuser()
    _SESSION_STATS["pc"]         = platform.node()
    _SESSION_STATS["ips"]        = _get_local_ips()
    threading.Thread(target=_telemetry_startup, daemon=True).start()

    show_banner()
    check_python()

    if not check_github_token():
        die("No usable embedded GitHub token — see the message above.",
            fix="Rotate the encrypted key file with tools/make_token_blob.py and rebuild.")

    repo_dir = setup_workspace()
    _update_readme_badge(repo_dir, _count_total_drivers(repo_dir))

    result = driverdex_sync_entry(repo_dir, auto_confirm=True)

    threading.Thread(target=_send_session_completion, daemon=True).start()

    elapsed = time.time() - _SESSION_STATS.get("start_time", time.time())
    stats = result.get("stats", {}) if isinstance(result, dict) else {}
    log("SECTION", f"AUTOMATIC CONTRIBUTE COMPLETE — "
                   f"uploaded={stats.get('uploaded', 0)} elapsed={_fmt_duration(elapsed)}")
    # give daemon telemetry threads a moment to fire before the process exits
    time.sleep(2)


if __name__ == "__main__":
    try:
        run_auto_contribute()
    except KeyboardInterrupt:
        C.print()
        warn("Interrupted.")
        sys.exit(130)