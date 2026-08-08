#!/usr/bin/env python3
# ==============================================================================
#  DriverDex Builder  --  Driver & Installer Manifest Builder / GitHub Uploader
#  Author  : rhshourav
#  Version : 6.1.1-rest
#  Repo    : https://github.com/rhshourav/driverdex
#  Part of : Windows-Scripts  --  github.com/rhshourav/Windows-Scripts
#
#  Changes in v6.1.1-rest  (fix: category manifests could be silently
#  overwritten/deleted when drivedex_index.json was unreadable)
#  ----------------------------------------------------------------------
#  * github_pull_manifest()'s fallback path -- used whenever
#    drivedex_index.json can't be downloaded or parsed (rate-limited,
#    missing, or corrupted; a GitHub 429 response body landing where JSON
#    should be is one real-world way this happens) -- used to re-derive
#    the list of category manifests to pull from the hardcoded
#    _CLASS_TO_TYPE map alone. Any category that only exists in repo
#    history (retired/renamed hardware classes, categories added by an
#    older script version, etc.) has no entry in that map, so it was
#    silently skipped during the pull. The next save_manifest() call for
#    that category then found zero local shards, assumed it was brand
#    new, and wrote a fresh empty shard 1 -- which overwrote/deleted every
#    real entry that category had on GitHub once pushed. This is the bug
#    behind "old data gets overwritten" reports.
#  * Fixed by _list_remote_manifest_dir(): when the index can't be read,
#    the true list of category manifest files is now read straight from
#    GitHub's Contents API listing of manifests/ (ground truth, not
#    derived from any hardcoded list), unioned with the known-category
#    list as a safety net. If that listing ALSO can't be retrieved after
#    every retry, the tool now aborts with a clear error instead of
#    silently continuing with a guessed, possibly-incomplete category set
#    -- same "refuse rather than risk corrupting real data" philosophy
#    already used elsewhere in this file for failed shard downloads.
#
#  Changes in v6.1.0-rest  (repo-state now lives on GitHub, not just on disk)
#  ----------------------------------------------------------------------
#  * driverdex_repo_state.json (active repo + spent-repos set, introduced in
#    v6.0.0-rest) was only ever written next to the script on the local
#    disk -- so the whole point of it (skip repos already known to be full,
#    zero wasted API calls) only worked on the one machine that discovered
#    the quota error. A second machine, a fresh clone, or a wiped workspace
#    started from zero knowledge every time and had to rediscover the same
#    403s the expensive way.
#  * The file is now committed to REPO_OWNER/REPO_NAME (driverdex) at the
#    repo root -- same place README.md/index.json already live -- via the
#    single-file GitHub Contents API (create-or-update in one call). This
#    is deliberately NOT the multi-blob/tree/commit Git Data API used for
#    driver archives elsewhere in this file; that machinery is overhead
#    this tiny, rarely-changing file doesn't need.
#  * _load_repo_state() now checks GitHub first and falls back to the local
#    cache only if GitHub can't answer (no token yet, offline, rate-limited,
#    or nothing pushed there yet). _save_repo_state() writes the local
#    cache first (instant, always succeeds -- still what protects
#    _mark_repo_spent() if the process dies right after) and then pushes
#    the same data to GitHub via the new _push_repo_state_to_github(),
#    which retries once on a stale-sha conflict (409/422) by re-fetching
#    the current sha before giving up and falling back to "local cache has
#    it, next save reconciles."
#  * Net effect: every machine running this tool now converges on the same
#    active repo and the same spent-set after its first sync, instead of
#    each one maintaining its own local-only history.
#
#  Changes in v6.0.0-rest  (automatic upload + repo-quota fallback, no prompts)
#  ----------------------------------------------------------------------
#  * Upload mode is no longer a manual prompt at STEP 7. Every push now
#    starts with Parallel blobs (raw Git Blob API, max throughput) and
#    transparently falls back to Pipeline REST (a genuine git-lfs batch
#    upload) if that fails for a non-quota reason -- a real, different
#    transfer mechanism against the SAME already-archived files, not a
#    relabeled retry. See _push_driver_files_with_fallback().
#  * Fixed diagnose_api_error() misreporting a 403 "Repository is above
#    its size quota" response as a missing-'repo'-scope permissions
#    error. It now checks for "quota" in the GitHub error message first
#    (_is_repo_quota_error()) and reports the real cause, both in the
#    startup token check and during the actual push.
#  * New automatic drivers-repo fallback: when the active drivers repo
#    hits its GitHub size quota, the tool switches to the next repo in
#    DRIVERS_REPO_FALLBACK_CHAIN (currently drivers -> driver_1) and
#    retries automatically -- no manual re-run needed. The active repo
#    is persisted to driverdex_repo_state.json next to the script, so a
#    later run starts on the correct repo instead of re-discovering the
#    same quota error.
#  * If every repo in the fallback chain is also full/inaccessible, the
#    tool now sends a Telegram notification asking the user to create
#    the next repo (default: driver_3) -- 10 reminders, 30s apart,
#    checking GitHub after each one. If the repo still doesn't exist
#    afterward, it reports this and exits. Requires telegram_bot_token /
#    telegram_chat_id in drivedex_config.json (or TELEGRAM_BOT_TOKEN /
#    TELEGRAM_CHAT_ID env vars) -- see _print_telegram_setup_guide(). No
#    credentials are bundled; these are user-supplied placeholders.
#  * Fixed a pre-existing bug where Pipeline mode's "skip re-upload of
#    already-LFS'd archives" logic (already_uploaded_rrs) was computed
#    but never actually used -- archives were silently re-committed as
#    full-size raw blobs instead of small LFS pointer blobs. Now handled
#    uniformly by _upload_archives_via_lfs() / _create_lfs_pointer_blob().
#
#  Changes in v5.10.0-rest  (drop LFS-aggressive upload mode)
#  ----------------------------------------------------------------------
#  * Removed mode [3] "LFS aggressive" from the upload-mode selector.
#    Investigated first: despite its name/description ("all files via
#    LFS"), UPLOAD_MODE_LFS_AGGR never actually spoke the real git-lfs
#    batch protocol — it went through the exact same plain Git Blob API
#    path (_upload_as_blob) as mode [1] Parallel blobs, just with the
#    meta-files bucket merged into the archive bucket instead of split
#    into two separate progress passes. So it wasn't a distinct transfer
#    mechanism, just a relabeled/reshuffled variant of mode 1 — safe to
#    drop with no loss of real functionality. Selector now offers only
#    [1] Parallel blobs / [2] Pipeline REST. Removed UPLOAD_MODE_LFS_AGGR
#    and every branch/dict entry that referenced it in
#    _select_upload_mode() and _commit_files_to_repo(). Mode [2] Pipeline
#    REST's real git-lfs batch-API code path (_upload_lfs_file,
#    _download_via_lfs_batch, _parse_lfs_pointer, LFS_BATCH_URL) is
#    untouched — that one genuinely uses LFS and still needs it.
#
#  Changes in v5.9.0-rest  (quieter warnings + no-prompt pack naming)
#  ----------------------------------------------------------------------
#  * build_entries()/enrich_versions() warnings (HWID conflicts, skipped
#    duplicate INFs, older-version notices) used to be printed to the
#    console one full multi-line block per item — on packs with several
#    conflicts this could dump dozens of lines. New _warn_summary() still
#    logs every warning to the log file in full (nothing lost, still fully
#    auditable) but the console now only gets one line per pack, e.g.
#    "4 HWID conflict(s) — full details in <logfile>". Nothing about which
#    drivers get included changed: HWID conflicts were always advisory
#    only (see v5.8.0 note below re: what actually gets skipped).
#  * Pack name no longer prompts. suggest_pack_name()'s auto-detected name
#    is now applied directly — same sanitize/32-char-truncate rule as
#    before, just without the "Enter to accept" step.
#
#  Changes in v5.8.0-rest  (auto workspace cleanup + archiving ETA fix)
#  ----------------------------------------------------------------------
#  * Workspace cleanup at end of session no longer prompts. If >=1 pack was
#    pushed successfully this run, the local workspace mirror is deleted
#    automatically (it's fully recoverable — the next run re-clones and
#    re-syncs from GitHub). If nothing was pushed, it's left in place, also
#    without prompting. The per-pack "Delete source folder?" prompt is
#    unchanged — that one still asks every time, since the source folder
#    (unlike the workspace mirror) isn't backed up anywhere else.
#  * Fixed broken ETA during archiving. _pack_7z_cli_threaded() (used
#    whenever py7zr isn't installed and the tool falls back to the 7z CLI)
#    was calling on_progress() exactly once, after subprocess.run() fully
#    blocked until the whole group finished compressing — so the progress
#    bar and TimeRemainingColumn sat frozen for the entire archive, then
#    jumped 0% -> 100% in one frame. It now polls the growing output
#    archive size on disk every 0.4s while 7z runs, converts that back
#    into an "input bytes processed" estimate using a running compression-
#    ratio average (seeded at 1:1, refined after every group), and reports
#    progress incrementally. A hard-correction after the subprocess exits
#    guarantees the exact input total is always reached regardless of how
#    the estimate tracked mid-run. The py7zr backend already reported
#    progress correctly per-file and needed no change.
#
#  Changes in v5.7.1-rest  (finish wiring the shard caches + README fix)
#  ----------------------------------------------------------------------
#  * The by_type loop in _process_one_pack() called load_manifest(repo_dir,
#    cat=dtype) without passing shard_cache=_driver_shard_cache — so despite
#    load_all_manifests_combined() having just parsed every shard moments
#    earlier, this call re-read + re-parsed the active shard from disk a
#    second time, every dtype, every pack. Now passes shard_cache through,
#    so it's actually served from the cache populated above.
#  * Same miss on the installer side: the post-archive dedup scan called
#    load_all_installers_combined(repo_dir) with no shard_cache at all, even
#    though load_installer_manifest(..., shard_cache=_installer_shard_cache)
#    had already parsed the active shard right above it. Now passes
#    shard_cache=_installer_shard_cache through.
#  * That installer fix would have been a no-op on its own:
#    load_all_installers_combined() only ever wrote into shard_cache, it
#    never checked shard_cache first — so it unconditionally re-read every
#    installer shard from disk regardless of what was passed in. Rewritten
#    to be a genuine read-through cache (check-then-read-then-populate),
#    matching load_all_manifests_combined()'s existing contract, so a shard
#    already parsed by either loader is never parsed twice by the other.
#  * _update_readme_badge() used to replace the *entire* text between the
#    DRIVERDEX_DRIVER_BADGE markers with a bare, unlabeled Drivers badge —
#    correct only if that block contains nothing else. Real-world READMEs
#    that put a bold "**Drivers:**" label and additional Workflow/Page/
#    Worker badges on the same line (or that don't have the marker comments
#    at all) would have those neighbors silently deleted on the very next
#    badge refresh. Replaced with a targeted regex substitution that edits
#    only the digits inside the existing ".../badge/drivers-<N>-..." URL,
#    wherever it sits in the file — every other badge, label, and character
#    in README.md now comes back byte-for-byte identical, and it no longer
#    depends on the marker comments being present. Also skips the write
#    entirely when the count hasn't changed.
#  * No entry-merge, dedup, or manifest-writing logic was touched by any of
#    the above — these are pure "read/write the same bytes with less
#    redundant I/O" fixes finishing off the caching work already in place.
#
#  Changes in v5.7.0-rest  (kill redundant full-repo rescans + parallel pull)
#  ----------------------------------------------------------------------
#  * save_manifest() was calling _update_readme_badge()+_count_total_drivers()
#    AND _save_index() on every single call — and it's called once PER DRIVER
#    TYPE in a pack (by_type loop in _process_one_pack). Both of those helpers
#    independently re-read and re-parse every manifest shard in the entire
#    repo from disk. For a pack touching C categories, that's C x (2 full
#    repo rescans) = 2C redundant multi-MB JSON parses, even though only the
#    state after the LAST call in the loop is ever actually used (nothing
#    reads the intermediate index.json/README between calls). New
#    save_manifest(..., update_index=True) parameter: the by_type loop now
#    calls it with update_index=False and does exactly one combined refresh
#    after the loop finishes — same final on-disk state, ~2C fewer full-repo
#    scans per pack. No entry-merge/append logic touched.
#  * New _refresh_index_and_badge(): merges what _count_total_drivers() and
#    _save_index() used to do as two separate full-repo scans into one —
#    each shard is now read+parsed exactly once per refresh instead of twice
#    (total driver count is derived from the same per-type tallies
#    category_summary already needs, since every enabled entry has a type).
#    _save_index() and _count_total_drivers() are left in place untouched
#    for any other caller; only the hot path inside save_manifest changed.
#  * The two post-push driver-count messages in _process_one_pack() now reuse
#    the count _refresh_index_and_badge() just computed instead of triggering
#    yet another full-repo scan each — safe because installer entries (the
#    only thing that changes between that point and the messages) are
#    excluded from the driver count already.
#  * github_pull_manifest() downloaded every manifest/installer shard one
#    HTTP request at a time, fully sequentially, on every pre-pack sync —
#    pure network latency with zero overlap. Category manifest chains,
#    the installer manifest chain, and README.md are all independent of each
#    other, so they now run concurrently on a ThreadPoolExecutor(PULL_WORKERS
#    =12) — matching the concurrency level already used for blob uploads
#    (ul_workers=12). The sequential *probing* within one category/installer
#    chain (shard 2, then 3, then 4… until not_found) is unchanged and still
#    runs in order — only independent chains were made to overlap. Every
#    shard that used to be downloaded is still downloaded, the same
#    not_found/error handling and RuntimeError-on-failure behavior is
#    preserved; results are merged from each job's own return value rather
#    than a shared mutable list, so no locking is needed for correctness
#    there (a lock is still used around the shared progress-bar bookkeeping).
#  * _commit_files_to_repo()'s metadata-file blob upload (index.json, README,
#    manifest shards) looped one file at a time — each a separate network
#    round trip — even though the archive-blob upload right above it already
#    used a 12-thread pool. Metadata blobs now upload the same way (same
#    _upload_as_blob() helper, same gather-then-check failure handling as the
#    archive blobs), removing N sequential round-trips from every push.
#  * New end-of-session prompt: after SESSION COMPLETE, optionally delete the
#    local workspace folder (drivedex_workspace) to reclaim disk space. Off
#    by default (explicit "y" required). Safe to delete at any time — GitHub
#    is the actual source of truth and setup_workspace() re-syncs everything
#    fresh on the next run; the saved GitHub token
#    (drivedex_config.json) lives next to the script, not inside the
#    workspace, and is never touched by this.
#  * None of the above changes what gets written to a manifest or in what
#    order — every change is either "compute the same result with fewer
#    scans" or "do independent I/O concurrently instead of sequentially".
#    No existing driver/installer entry is ever overwritten or deleted by
#    these changes.
#
#  Changes in v5.6.4-rest  (don't re-prompt for a token that's still valid)
#  ----------------------------------------------------------------------
#  * _refresh_github_token() treated every 401 as proof the token was dead
#    and immediately interrupted the run to demand a freshly pasted token —
#    even when GITHUB_TOKEN in the environment was completely fine and the
#    401 was actually transient (a GitHub-side hiccup, a rate-limit edge
#    case, or a dropped Authorization header under the 12-thread parallel
#    blob upload). That's why uploads could stop and ask for a new token
#    mid-run despite the env var never having changed.
#  * New _verify_github_token(): makes one cheap, isolated GET /user call
#    with the current token before ever prompting. If that call succeeds,
#    the token is fine — _refresh_github_token() logs "401 looked
#    transient … retrying automatically" and returns True with no user
#    interaction at all. The interactive prompt is now only shown once this
#    dedicated check *also* comes back 401, i.e. the token is genuinely
#    expired or revoked. A non-401 hiccup on the check call itself gets one
#    extra retry before giving up, so a flaky network doesn't get
#    misread as a dead token either.
#
#  Changes in v5.6.3-rest  (parallel archiving + hashing, no size changes)
#  ----------------------------------------------------------------------
#  * build_installer_entries() archived installer packages one at a time in
#    a plain "for pkg in exe_files" loop -- every 7z/LZMA2 compression ran
#    fully sequentially even though driver-group archiving (zip_and_upload_
#    pipeline) already ran on an ARCHIVE_WORKERS thread pool. Installers now
#    archive on that same thread pool. LZMA2 compression releases the GIL,
#    so this is real wall-clock parallelism, not just I/O overlap. Per-
#    package failures still only skip that one entry (same as before);
#    output order is unchanged regardless of which worker finishes first.
#  * Fixed a latent overwrite bug this uncovered: two installer packages
#    that sanitize to the same archive stem (e.g. two installers both named
#    "setup") shared one destination .7z path. Sequentially, the second
#    package's cleanup pass would silently delete + overwrite the first
#    package's just-written archive while the first package's manifest
#    entry still pointed at that now-corrupted file. New
#    _unique_installer_stems() disambiguates any same-run collisions up
#    front (suffixing only the colliding names, e.g. "setup_1"/"setup_2");
#    _archive_installer_package() takes that as stem_override. Packages with
#    a unique name keep the exact filename they always had.
#  * _dedup_files() SHA-256'd every file in a group one at a time -- minutes
#    of work on large dumps by its own docstring. Hashing is now spread
#    across a small thread pool (DEDUP_WORKERS); the dedup decision itself
#    (first occurrence of a hash wins) still runs as one sequential pass
#    afterwards in original file order, so results are byte-for-byte
#    identical to before -- only the hashing got faster.
#  * None of the above touches SPLIT_BYTES (15 MB archive volume size),
#    MANIFEST_SIZE_LIMIT (15 MB), or MANIFEST_SPLIT_THRESHOLD (13 MB) -- all
#    three are unchanged. No manifest-writing or entry-merge logic was
#    touched, so existing entries are never deleted or overwritten by these
#    changes; the stem-collision fix above only makes same-run archiving
#    safer, on top of that.
#
#  Changes in v5.6.2-rest  (drop manifest cleanup + MofN markup fix)
#  ----------------------------------------------------------------------
#  * Removed the MANIFEST CLEANUP step entirely: deduplicate_manifests()
#    and its "rule('MANIFEST CLEANUP')" call between STEP 1 and PACK 1 are
#    gone. Manifests are no longer rewritten/deduped before packing; every
#    entry the scan produces flows straight through, same as the v5.6.0
#    removal of the pre-flight duplicate/conflict audit. The Pack-1
#    skip-pull optimization stays (still valid on its own: nothing pushes
#    between Step 1's sync and Pack 1 starting) but its comment no longer
#    references the now-deleted dedup step.
#  * Fixed every MofNCompleteColumn(separator="[dim white]/[/dim white]")
#    progress column across the file. rich's stock MofNCompleteColumn
#    builds its output as plain Text(f"...{separator}...") -- Text() takes
#    its argument as literal characters and never parses console markup
#    (only Console.print()/Text.from_markup() do that) -- so the tags were
#    printed as literal text instead of dimming the slash, e.g.
#    "15[dim white]/[/dim white]15" instead of a dim "15/15". Replaced with
#    a small DimMofNColumn subclass that applies the dim style as a real
#    Text style span, so the separator now actually renders dim.
#
#  Changes in v5.6.1-rest  (fix: dedup fix was being silently discarded)
#  ----------------------------------------------------------------------
#  * The per-pack loop unconditionally re-ran github_pull_rebase() (a full
#    re-download of every manifest shard, not an incremental git pull)
#    before EVERY pack -- including Pack 1, mere seconds after Step 1 had
#    just synced the same files. Since deduplicate_manifests() (MANIFEST
#    CLEANUP) only ever wrote its cleaned-up JSON to local disk and never
#    pushed it, that redundant Pack-1 re-download would immediately
#    overwrite the just-deduplicated local manifests with the original,
#    un-deduplicated copies straight from GitHub -- before Pack 1's own
#    push had any chance to make the cleanup permanent. Net effect: the
#    same duplicate entries were "found" and "removed" on every single run,
#    forever, without the fix ever actually reaching GitHub.
#  * Fix: the pre-pack sync is now skipped specifically for Pack 1 (nothing
#    can have changed remotely between Step 1's sync and Pack 1 starting,
#    since nothing pushes in between), so the deduplicated manifests now
#    survive to be included in Pack 1's commit. Pack 2+ still re-syncs as
#    before, since by then a real push may have happened.
#
#  Changes in v5.6.0-rest  (scan performance/accuracy + audit removal)
#  -----------------------------------------------------------------
#  * Removed the pre-flight duplicate/conflict audit entirely. Every INF the
#    first scan finds now flows straight through to packing -- nothing is
#    ever skipped, superseded, or filtered out on the operator's behalf.
#    (Dropped: preflight_duplicate_audit(), DuplicateReport, the _ACT_*
#    action constants, and the now-unused _zip_exists_for_entry() helper.)
#  * scan_infs() now parses INFs concurrently on a small thread pool instead
#    of one at a time -- wall-clock time on large driver dumps scales with
#    worker count instead of file count, especially over slow/network disks.
#  * _read_inf() now reads each file's bytes once and picks its encoding via
#    BOM sniffing / strict-decode fallback (utf-8 -> cp1252 -> latin-1)
#    instead of trusting the first attempt unconditionally. Previously every
#    encoding attempt used errors="replace", which never raises -- so
#    non-UTF-8 INFs (common; many are UTF-16 or ANSI) were silently decoded
#    as UTF-8 and mined for metadata out of mojibake. Version/provider/
#    category/HWID extraction is now correct for those files instead of
#    just not crashing.
#  * _scan_exe_files() dropped its O(n^2) "compare every directory against
#    every other directory" grouping loop (which also leaned on
#    exception-based control flow via relative_to/_is_subpath for every
#    non-match). It's replaced with a single linear pass over directories
#    sorted by path parts, which places every directory in one contiguous
#    run right after its nearest tracked ancestor. Same grouping result,
#    no repeated scans. (Dropped the now-unused _is_subpath() helper.)
#
#  Changes in v5.4.0-rest  (rebrand + drop SQLite)
#  -----------------------------------------------
#  * Rebranded from "LDC Builder" to "DriverDex Builder" across the entire
#    tool (telemetry, panels, commit identity, config/workspace/index files).
#  * Removed the legacy drivers.db SQLite persistence layer entirely — the
#    per-category JSON manifests are now the single source of truth. All
#    SQLite read/write/rollback code paths and the db_shards index field
#    have been dropped.
#  * README driver-count badge now uses the DRIVERDEX_DRIVER_BADGE markers.
#
#  Previous changes (v5.3.4-rest, token persistence)
#  -------------------------------------------------
#  * _load_token():
#      - Reads token from drivedex_config.json first, then falls back
#        to the GITHUB_TOKEN environment variable.
#      - Eliminates stale/expired tokens cached in module-level variable at
#        startup — always picks up the freshest value available.
#  * _save_token():
#      - Persists a refreshed token to drivedex_config.json next to
#        the script so future runs never need to re-enter it.
#      - drivedex_config.json should be added to .gitignore to avoid exposing creds.
#  * _refresh_github_token():
#      - Now calls _save_token() after a successful paste so the new token
#        survives across runs without touching system environment variables.
#      - Improved prompt copy: clarifies that transient 401s can be retried
#        automatically — pressing Enter now triggers auto-retry with the
#        current env token before aborting, instead of immediately failing.
#  * _api():
#      - Now calls _load_token() on every request instead of reading the
#        stale module-level GITHUB_TOKEN — ensures cross-thread token refreshes
#        are always picked up without relying on global mutation.
#  * check_github_token():
#      - Updated to use _load_token() so startup validation always reflects
#        the most current token (config file or env var).
#
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
import struct
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
from rich.prompt   import Prompt, Confirm
from rich.text     import Text
from rich.rule     import Rule
from rich.align    import Align
from rich          import box
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

API_BASE            = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
LFS_BATCH_URL = (
    f"https://github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}.git/info/lfs/objects/batch"
)

WORKSPACE_DIR       = _APP_DIR / "drivedex_workspace"

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
MANIFEST_RAW_BASE = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main"
)


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


def _drivers_repo_history_line() -> str:
    """
    Render the drivers-repo sequence for the session summary, e.g.:
    "drivers (full) -> drivers_1 (full) -> drivers_2 (active)". Returns ""
    if no fallback has ever happened this history (nothing worth showing).
    """
    if not _SPENT_DRIVERS_REPOS:
        return ""
    cur_idx = _drivers_repo_index_for_name(DRIVERS_REPO_NAME)
    highest = max(
        [i for i in (_drivers_repo_index_for_name(n) for n in _SPENT_DRIVERS_REPOS) if i is not None]
        + ([cur_idx] if cur_idx is not None else [0]),
        default=0,
    )
    parts = []
    for i in range(highest + 1):
        name = _drivers_repo_name_for_index(i)
        if name == DRIVERS_REPO_NAME:
            parts.append(f"{name} (active)")
        elif name in _SPENT_DRIVERS_REPOS:
            parts.append(f"{name} (full)")
        else:
            parts.append(name)
    return " → ".join(parts)


C = Console(highlight=False)

# ── Config file path (sits next to the script, never committed to git) ────────
_CONFIG_FILE = _APP_DIR / "drivedex_config.json"


def _load_token() -> str:
    """
    Load the GitHub token with priority order:
      1. drivedex_config.json  (persisted by _save_token after a mid-run refresh)
      2. GITHUB_TOKEN environment variable  (system/user permanent env)
      3. Empty string  (will fail at check_github_token)

    Called on every API request so a cross-thread token refresh is always
    picked up without relying on a stale module-level variable.
    """
    try:
        if _CONFIG_FILE.exists():
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            tok = data.get("github_token", "").strip()
            if tok:
                return tok
    except Exception:
        pass
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _save_token(token: str) -> None:
    """
    Persist a refreshed token to drivedex_config.json next to the script.
    Merges with any existing keys so other config values are preserved.
    Add drivedex_config.json to .gitignore — it contains credentials.
    """
    try:
        data: Dict = {}
        if _CONFIG_FILE.exists():
            try:
                data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["github_token"] = token
        _CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        warn(f"Could not save token to {_CONFIG_FILE.name}: {exc}")


# ── GitHub token + thread-safe refresh ───────────────────────────────────────
# GITHUB_TOKEN is kept as a module-level cache for backward-compat with any
# code that reads it directly, but all API calls use _load_token() to always
# pick up the freshest value (config file > env var).
GITHUB_TOKEN = _load_token()

_TOKEN_REFRESH_LOCK     = threading.Lock()
_TOKEN_REFRESH_DONE     = threading.Event()
_TOKEN_LAST_REFRESHED   = [0.0]   # [timestamp] — prevents duplicate prompts within 5 s
_TOKEN_REFRESH_DECLINED = threading.Event()  # set when user declines; prevents re-prompting in same run


def _verify_github_token(tok: str, attempts: int = 2) -> bool:
    """Cheaply confirm whether `tok` is actually still valid, independent of
    whatever request just got the 401.

    With ARCHIVE-style concurrent uploads (many blob-upload threads hitting
    the API at once), a 401 doesn't always mean the token is dead — it can
    also be a transient GitHub hiccup, a rate-limit edge case, or (rarely) a
    dropped/garbled Authorization header under load. A genuinely expired or
    revoked token will fail this dedicated check the same way it failed the
    original request; a transient blip won't. This is what lets an
    already-correct GITHUB_TOKEN keep working without ever being re-typed.
    """
    if not tok:
        return False
    for i in range(attempts):
        try:
            st, _ = _api("GET", "/user", token=tok, timeout=15)
        except Exception:
            st = 0
        if st == 200:
            return True
        if st == 401:
            # A 401 on a dedicated, isolated check call — not a fluke.
            return False
        # Anything else (timeout, 5xx, secondary rate limit, ...) — this
        # check call itself may just have hit a blip; give it one more try
        # before concluding the token is the problem.
        if i < attempts - 1:
            time.sleep(1.5)
    return False


def _refresh_github_token(label: str = "") -> bool:
    """
    Block all upload threads except one; prompt the user for a new token once.
    Other threads wait on _TOKEN_REFRESH_DONE (up to 120 s) then reuse the result.
    Returns True if the existing token verified fine (self-healed, no prompt),
    a valid new token was entered, or the env token was auto-retried. Returns
    False only if the user explicitly pressed Enter with no input AND the
    existing env token also failed.

    Once the user declines (_TOKEN_REFRESH_DECLINED is set) every subsequent
    call returns False immediately — no further prompts are shown.

    On success the new token is:
      • Written to drivedex_config.json  (persists across runs)
      • Stored in os.environ["GITHUB_TOKEN"]  (available to sub-processes)
      • Cached in the module-level GITHUB_TOKEN  (for any direct reads)
    """
    global GITHUB_TOKEN

    # ── short-circuit: user already said "no" this session ───────────────────
    if _TOKEN_REFRESH_DECLINED.is_set():
        return False

    # ── self-heal first: is the current token actually still good? ───────────
    # Never bother the user for a 401 that a plain re-check clears on its
    # own — this is what stops the script from demanding a fresh token when
    # GITHUB_TOKEN was fine the whole time.
    current_tok = _load_token()
    if current_tok and _verify_github_token(current_tok):
        GITHUB_TOKEN = current_tok
        _TOKEN_LAST_REFRESHED[0] = time.time()
        info("401 looked transient — GITHUB_TOKEN still verifies fine, retrying automatically."
             + (f"  [dim]({escape(label)})[/dim]" if label else ""))
        return True

    # If another thread just finished a successful refresh, reuse that token.
    if time.time() - _TOKEN_LAST_REFRESHED[0] < 5.0 and not _TOKEN_REFRESH_DECLINED.is_set():
        return bool(_load_token())

    if not _TOKEN_REFRESH_LOCK.acquire(blocking=False):
        # Another thread is prompting — wait for it to finish.
        _TOKEN_REFRESH_DONE.wait(timeout=120)
        # After waking, honour a decline made by the prompting thread.
        if _TOKEN_REFRESH_DECLINED.is_set():
            return False
        return bool(_load_token())

    _TOKEN_REFRESH_DONE.clear()
    try:
        C.print()
        C.print(
            Panel(
                "\n".join([
                    "  [bold bright_red]HTTP 401 — GitHub token rejected[/bold bright_red]"
                    + (f"  [dim]({escape(label)})[/dim]" if label else "") + "\n",
                    "  The current GITHUB_TOKEN has expired or been revoked.",
                    "  [dim]Generate a new one at:[/dim]  "
                    "[cyan]https://github.com/settings/tokens/new[/cyan]  "
                    "[dim](scope: repo)[/dim]\n",
                    "  Paste a new token and press [bold]Enter[/bold] to resume.",
                    "  Press [bold]Enter[/bold] without typing to abort the upload.",
                ]),
                border_style="bright_red",
                title="[bold bright_red]  Token Expired — Refresh Required  [/bold bright_red]",
                padding=(0, 2),
            )
        )
        C.print()
        try:
            new_tok = Prompt.ask(
                "  [bold bright_cyan]Paste new GitHub token[/bold bright_cyan]  "
                "[dim](or Enter to abort)[/dim]",
                default="",
                password=True,
            ).strip()
        except (KeyboardInterrupt, EOFError):
            new_tok = ""

        if new_tok:
            # ── persist the new token so future runs never need to re-enter ──
            GITHUB_TOKEN = new_tok
            os.environ["GITHUB_TOKEN"] = new_tok
            _save_token(new_tok)
            _TOKEN_LAST_REFRESHED[0] = time.time()
            _TOKEN_REFRESH_DECLINED.clear()
            ok(
                "Token updated and saved to [dim]drivedex_config.json[/dim] "
                "— resuming upload …"
            )
            return True
        else:
            # User pressed Enter with no input — try the token that is
            # already in the env / config before giving up entirely.
            env_tok = _load_token()
            if env_tok and env_tok != GITHUB_TOKEN:
                # A fresher token appeared in the environment since startup.
                GITHUB_TOKEN = env_tok
                os.environ["GITHUB_TOKEN"] = env_tok
                _TOKEN_LAST_REFRESHED[0] = time.time()
                _TOKEN_REFRESH_DECLINED.clear()
                ok("Using updated environment token — resuming upload …")
                return True

            # Nothing usable — record the decline so all threads skip prompting.
            _TOKEN_REFRESH_DECLINED.set()
            _TOKEN_LAST_REFRESHED[0] = time.time()
            warn("No token entered — upload will abort.")
            return False
    finally:
        _TOKEN_REFRESH_DONE.set()
        _TOKEN_REFRESH_LOCK.release()


# ── Telemetry ───────────────────────────────────────────────────────────────  ─
_TELEMETRY_URL   = "https://cryocore.rhshourav.workers.dev/message"
_TELEMETRY_TOKEN = "shourav"

_TELEMETRY_MILESTONES = {
    "STEP 2 — SCAN COMPLETE"       : "📋 Drivers scanned — ready to upload",
    "STEP 6 — ARCHIVE COMPLETE"    : "📦 Compression done — moving to upload",
    "STEP 4 — PACK STATS"          : "📊 Pack stats calculated",
}

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


def _send_step(step: str, pack_st=None, extra: str = "") -> None:
    user = _SESSION_STATS.get("user", "") or getpass.getuser()
    pc   = _SESSION_STATS.get("pc",   "") or platform.node()
    pack = pack_st["pack_name"] if pack_st else "–"

    friendly = _TELEMETRY_MILESTONES.get(step)
    if friendly:
        lines = [
            f"[DriverDex Builder v{APP_VER}] {friendly}",
            f"PC: {pc}  Pack: {pack}",
        ]
        if extra:
            lines.append(extra)
    else:
        lines = [f"[DriverDex Builder v{APP_VER}] {step}  |  PC: {pc}  Pack: {pack}"]
        if extra:
            lines.append(extra)

    threading.Thread(
        target=_send_telemetry,
        args=("\n".join(lines),),
        daemon=True,
    ).start()


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

def _pack_stats_template(pack_name: str) -> Dict:
    return {
        "pack_name"      : pack_name,
        "start_time"     : time.time(),
        "end_time"       : 0.0,
        "drivers_added"  : 0,
        "installers_added": 0,
        "groups"         : 0,
        "group_types"    : {},
        "total_raw_bytes": 0,
        "total_arc_bytes": 0,
        "errors"         : [],
        "push_ok"        : False,
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


def _send_pack_completion(stats: Dict, repo_dir=None) -> None:
    elapsed   = stats["end_time"] - stats["start_time"]
    status    = "\u2705 UPLOAD COMPLETE" if stats["push_ok"] else "\u274c UPLOAD FAILED"
    raw_sz    = fmt_size(stats["total_raw_bytes"]) if stats["total_raw_bytes"] else "n/a"
    arc_sz    = fmt_size(stats["total_arc_bytes"]) if stats["total_arc_bytes"] else "n/a"
    gtype_str = "  ".join(f"{t}={n}" for t, n in sorted(stats["group_types"].items()))
    repo_total = _count_total_drivers(repo_dir) if repo_dir else "n/a"
    err_part  = (
        "\nErrors (" + str(len(stats["errors"])) + "): " + ", ".join(stats["errors"][:3])
        if stats["errors"] else ""
    )
    text = (
        f"[DriverDex Builder v{APP_VER}] {status}\n"
        f"Pack      : {stats['pack_name']}\n"
        f"PC        : {_SESSION_STATS['pc']}  User: {_SESSION_STATS['user']}\n"
        f"Drivers   : {stats['drivers_added']}  Installers: {stats['installers_added']}\n"
        f"Types     : {gtype_str or 'n/a'}\n"
        f"Raw size  : {raw_sz}  \u2192  Compressed: {arc_sz}\n"
        f"Repo total: {repo_total} drivers in GitHub\n"
        f"Duration  : {_fmt_duration(elapsed)}"
        + err_part
    )
    _send_telemetry(text)


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


# ── Robust source-folder deletion ─────────────────────────────────────────────
def _delete_source_folder(src: Path, pack_st=None) -> bool:
    if not src.exists():
        ok(f"Source folder already gone: {src}")
        _send_step("SOURCE DELETE — already gone", pack_st, str(src))
        return True

    for attempt in range(1, 4):
        failed_files: List[str] = []

        def _onerror(fn, fpath, exc, _ff=failed_files):
            try:
                os.chmod(fpath, 0o777)
                fn(fpath)
            except Exception:
                _ff.append(str(fpath))

        try:
            shutil.rmtree(str(src), onerror=_onerror)
        except Exception as _ex:
            failed_files.append(f"rmtree raised: {_ex}")

        if not src.exists():
            ok(f"Deleted source folder (attempt {attempt}): {src}")
            _send_step(f"SOURCE DELETE — OK (attempt {attempt})", pack_st, str(src))
            return True

        if attempt < 3:
            warn(
                f"  rmtree attempt {attempt}/3 left {len(failed_files)} file(s) "
                f"behind — retrying in {attempt}s …"
            )
            for fp in failed_files[:5]:
                hint(fp)
            time.sleep(attempt)

    warn("  Falling back to OS shell delete …")
    try:
        if os.name == "nt":
            ret = subprocess.run(
                ["cmd", "/c", "rd", "/s", "/q", str(src)],
                capture_output=True, timeout=60,
            )
        else:
            ret = subprocess.run(
                ["rm", "-rf", str(src)],
                capture_output=True, timeout=60,
            )
        if not src.exists():
            ok(f"Deleted source folder (OS shell): {src}")
            _send_step("SOURCE DELETE — OK (OS shell)", pack_st, str(src))
            return True
        warn(f"  OS shell returned code {ret.returncode} but folder still exists.")
    except Exception as _ex:
        warn(f"  OS shell delete failed: {_ex}")

    if os.name == "nt":
        try:
            import tempfile
            tmp_target = (
                Path(tempfile.gettempdir())
                / f"_drivedex_del_{src.name}_{int(time.time())}"
            )
            src.rename(tmp_target)
            warn(
                f"  Could not fully delete — renamed to temp location:\n"
                f"  {tmp_target}\n"
                "  Remove it manually or it will be cleaned up on next reboot."
            )
            _send_step(
                "SOURCE DELETE — renamed to temp (reboot to finish)",
                pack_st, str(tmp_target),
            )
            return True
        except Exception as _ex:
            warn(f"  Rename fallback also failed: {_ex}")

    remaining = list(src.rglob("*")) if src.exists() else []
    err(
        f"  Could not delete source folder after all attempts. "
        f"{len(remaining)} item(s) remain in {src}"
    )
    for item in remaining[:10]:
        hint(str(item))
    _send_step(
        "SOURCE DELETE — FAILED", pack_st,
        f"{len(remaining)} items remain in {src}",
    )
    return False


def _delete_workspace_folder(ws: Path) -> bool:
    """Robust delete of the local workspace mirror (WORKSPACE_DIR).

    Mirrors _delete_source_folder()'s retry/fallback strategy (chmod+retry,
    OS-shell fallback, Windows rename-to-temp last resort) since the same
    "files sometimes locked/read-only on Windows" problem applies here too.

    Safe to call at any time: everything in the workspace is either already
    pushed to GitHub (the actual source of truth) or disposable local
    staging state, and setup_workspace() recreates the folder and re-syncs
    every manifest fresh from GitHub on the next run. The saved GitHub
    token (drivedex_config.json) lives in _APP_DIR next to the script, not
    inside WORKSPACE_DIR, so deleting the workspace never touches it.
    """
    if not ws.exists():
        ok(f"Workspace folder already gone: {ws}")
        return True

    for attempt in range(1, 4):
        failed_files: List[str] = []

        def _onerror(fn, fpath, exc, _ff=failed_files):
            try:
                os.chmod(fpath, 0o777)
                fn(fpath)
            except Exception:
                _ff.append(str(fpath))

        try:
            shutil.rmtree(str(ws), onerror=_onerror)
        except Exception as _ex:
            failed_files.append(f"rmtree raised: {_ex}")

        if not ws.exists():
            ok(f"Deleted workspace folder (attempt {attempt}): {ws}")
            return True

        if attempt < 3:
            warn(
                f"  rmtree attempt {attempt}/3 left {len(failed_files)} file(s) "
                f"behind — retrying in {attempt}s …"
            )
            for fp in failed_files[:5]:
                hint(fp)
            time.sleep(attempt)

    warn("  Falling back to OS shell delete …")
    try:
        if os.name == "nt":
            ret = subprocess.run(
                ["cmd", "/c", "rd", "/s", "/q", str(ws)],
                capture_output=True, timeout=60,
            )
        else:
            ret = subprocess.run(
                ["rm", "-rf", str(ws)],
                capture_output=True, timeout=60,
            )
        if not ws.exists():
            ok(f"Deleted workspace folder (OS shell): {ws}")
            return True
        warn(f"  OS shell returned code {ret.returncode} but folder still exists.")
    except Exception as _ex:
        warn(f"  OS shell delete failed: {_ex}")

    if os.name == "nt":
        try:
            import tempfile
            tmp_target = (
                Path(tempfile.gettempdir())
                / f"_drivedex_del_{ws.name}_{int(time.time())}"
            )
            ws.rename(tmp_target)
            warn(
                f"  Could not fully delete — renamed to temp location:\n"
                f"  {tmp_target}\n"
                "  Remove it manually or it will be cleaned up on next reboot."
            )
            return True
        except Exception as _ex:
            warn(f"  Rename fallback also failed: {_ex}")

    remaining = list(ws.rglob("*")) if ws.exists() else []
    err(
        f"  Could not delete workspace folder after all attempts. "
        f"{len(remaining)} item(s) remain in {ws}"
    )
    for item in remaining[:10]:
        hint(str(item))
    return False


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


def _warn_summary(warnings: List[str]) -> None:
    """Every warning is still written to the log file in full. The console
    only gets a single-line count instead of the whole list — per-item
    detail (which HWIDs conflicted, which INFs were skipped, etc.) is
    always fully recoverable from the log file, it's just not dumped into
    the terminal for every pack."""
    if not warnings:
        return
    for w in warnings:
        log("WARNING", w)
    kinds = ("HWID conflict", "Duplicate skipped", "Older version incoming")
    labels = {
        "HWID conflict"          : "HWID conflict(s)",
        "Duplicate skipped"      : "duplicate INF(s) skipped",
        "Older version incoming" : "older-version notice(s)",
        "other"                  : "other warning(s)",
    }
    counts: Dict[str, int] = defaultdict(int)
    for w in warnings:
        matched = next((k for k in kinds if w.startswith(k)), "other")
        counts[matched] += 1
    bits = [f"{n} {labels[k]}" for k, n in counts.items()]
    warn(f"{', '.join(bits)}  —  full details in {_log_file_path()}")


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
# 401 = expired/bad token; 403 permission errors are NOT retried (token won't help)
_AUTH_STATUSES  = {401}

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
        st, resp = _api(
            method, path, data=data, token=token or _load_token(), timeout=timeout,
            extra_headers=extra_headers,
        )
        last_st, last_resp = st, resp

        # ── 401: one-shot token refresh then immediate retry ──────────────────
        if st == 401 and not token_refreshed:
            if _refresh_github_token(label=label):
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


# ── GitHub token check ────────────────────────────────────────────────────────
def check_github_token() -> bool:
    C.print()
    _load_repo_state()   # restore the last-known-active drivers repo (if a
                          # prior run already fell back due to a quota error)
    tok = _load_token()
    if not tok:
        err("No GitHub token found.")
        hint(
            "Set [bold]GITHUB_TOKEN[/bold] environment variable  "
            "or create [dim]drivedex_config.json[/dim] with key [dim]github_token[/dim]."
        )
        _print_token_guide()
        return False

    # Show where the token came from so the user knows which one is active.
    if _CONFIG_FILE.exists():
        try:
            cfg_tok = json.loads(_CONFIG_FILE.read_text(encoding="utf-8")).get("github_token", "")
            if cfg_tok and cfg_tok == tok:
                info(f"Token source: [dim]{_CONFIG_FILE.name}[/dim]")
            else:
                info("Token source: [dim]GITHUB_TOKEN[/dim] environment variable")
        except Exception:
            info("Token source: [dim]GITHUB_TOKEN[/dim] environment variable")
    else:
        info("Token source: [dim]GITHUB_TOKEN[/dim] environment variable")

    with C.status("[bold bright_cyan]  Verifying GitHub token …[/bold bright_cyan]", spinner="dots12"):
        try:
            status, resp = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}")
        except KeyboardInterrupt:
            C.print()
            warn("Token check interrupted.")
            return False
    if status == 200:
        ok(f"GitHub token accepted — repo: {resp.get('full_name', REPO_OWNER+'/'+REPO_NAME)}")
        # Also confirm access to the dedicated drivers repo (archives live here).
        if (DRIVERS_REPO_OWNER, DRIVERS_REPO_NAME) != (REPO_OWNER, REPO_NAME):
            with C.status("[bold bright_cyan]  Verifying drivers repo access …[/bold bright_cyan]", spinner="dots12"):
                try:
                    d_status, d_resp = _api("GET", f"/repos/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}")
                except KeyboardInterrupt:
                    C.print()
                    warn("Drivers-repo check interrupted.")
                    return False
            if d_status == 200:
                ok(f"Drivers repo accessible: {d_resp.get('full_name', DRIVERS_REPO_OWNER+'/'+DRIVERS_REPO_NAME)}")
            elif d_status == 404:
                err(f"Drivers repo {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} not found — "
                    f"create it (with an initial commit on '{GH_BRANCH}') so driver archives have a home.")
                return False
            elif d_status == 403:
                if _is_repo_quota_error(d_status, d_resp):
                    warn(f"Drivers repo {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} is above its size quota.")
                    _mark_repo_spent(DRIVERS_REPO_NAME)
                    if _advance_drivers_repo_or_notify():
                        ok(f"Auto-switched to {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} "
                           f"before the first push — no wasted upload attempt.")
                    else:
                        return False
                else:
                    err(f"Token cannot access {DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME} (needs 'repo' push scope).")
                    return False
            else:
                warn(f"Drivers repo check returned HTTP {d_status}: {d_resp.get('message', '')} "
                     f"— driver uploads may fail.")
        return True
    if status == 401:
        err("GitHub token is invalid or expired.")
    elif status == 403:
        err("GitHub token lacks required permissions (needs 'repo' scope).")
    elif status == 404:
        err(f"Repo {REPO_OWNER}/{REPO_NAME} not found.")
    else:
        err(f"GitHub API returned HTTP {status}: {resp.get('message', '')}")
    _print_token_guide()
    return False


def _print_token_guide() -> None:
    lines = [
        "[bold yellow]How to create a GitHub Personal Access Token (PAT)[/bold yellow]\n",
        "[dim]1.[/dim]  Go to:  [cyan]https://github.com/settings/tokens/new[/cyan]",
        "[dim]2.[/dim]  Select scope:  [white]✓ repo[/white]  (Full control of private repositories)",
        "[dim]3.[/dim]  Set [bold]Expiration: No expiration[/bold] to avoid mid-upload failures.",
        "[dim]4.[/dim]  Click  [bold]Generate token[/bold]  and copy it.",
        "[dim]5.[/dim]  Set it via env var [bold]or[/bold] config file:\n",
        "     [bold]Option A — Environment variable (PowerShell):[/bold]",
        "     [bold yellow]$env:GITHUB_TOKEN = \"ghp_YourTokenHere\"[/bold yellow]  (current session)",
        "     [bold yellow][System.Environment]::SetEnvironmentVariable(\"GITHUB_TOKEN\", \"ghp_YourTokenHere\", \"User\")[/bold yellow]  (permanent)\n",
        "     [bold]Option B — Config file (recommended — auto-persists after paste):[/bold]",
        "     Create [dim]drivedex_config.json[/dim] next to the script:",
        "     [bold yellow]{ \"github_token\": \"ghp_YourTokenHere\" }[/bold yellow]",
        "     [dim]Add drivedex_config.json to .gitignore so it is never committed.[/dim]\n",
        "[dim]6.[/dim]  Re-run the script.",
    ]
    C.print(Panel("\n".join(lines), border_style="yellow",
                  title="[bold yellow]  GitHub Token Setup  [/bold yellow]", padding=(1, 3)))


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
_VER_RE      = re.compile(r"^DriverVer\s*=\s*(.+)",          re.IGNORECASE | re.MULTILINE)
_CLASS_RE    = re.compile(r"^Class\s*=\s*([^\r\n;]+)",       re.IGNORECASE | re.MULTILINE)
_PROVIDER_RE = re.compile(r"^Provider\s*=\s*([^\r\n;]+)",    re.IGNORECASE | re.MULTILINE)
_CATALOG_RE  = re.compile(r"^CatalogFile\s*=\s*([^\r\n;]+)", re.IGNORECASE | re.MULTILINE)
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
    m = _VER_RE.search(txt)
    if m:
        raw = _resolve(m.group(1).strip(), strings)
        parts = [p.strip() for p in raw.split(",", 1)]
        version = parts[1].strip() if len(parts) == 2 else parts[0]

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


def suggest_pack_name(folder: Path, inf_data: List[Tuple[Path, Dict]]) -> str:
    providers  = Counter(d.get("provider", "").strip() for _, d in inf_data if d.get("provider", "").strip())
    categories = Counter(d.get("category", "").strip() for _, d in inf_data if d.get("category", "Unknown") not in ("Unknown", ""))
    archs      = Counter(d.get("arch", "x64") for _, d in inf_data)
    provider = providers.most_common(1)[0][0]  if providers  else ""
    category = categories.most_common(1)[0][0] if categories else ""
    arch     = archs.most_common(1)[0][0]      if archs      else "x64"
    p_clean = re.sub(r'[^A-Za-z0-9]', '', provider)[:14]
    c_clean = re.sub(r'[^A-Za-z0-9]', '', category)[:10]
    if p_clean and c_clean:
        return f"{p_clean}_{c_clean}_{arch}"
    if p_clean:
        return f"{p_clean}_{arch}"
    safe_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', folder.name)[:20] or "Unknown"
    return safe_name


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

_verify_zip = _verify_archive


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

_zip_stem = _archive_stem


def _slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


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
) -> Tuple[Dict[Path, List[PartInfo]], int]:
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
    return result, total_verified


# ── Pipeline orchestrator ─────────────────────────────────────────────────────
def zip_and_upload_pipeline(
    src       : Path,
    dest_dir  : Path,
    pack      : str,
    inf_data  : List[Tuple[Path, Dict]],
    type_map  : Dict[Path, str],
    repo_dir  : Path,
    pack_name : str,
    rb        : "_PackRollback",
) -> Tuple[Dict[Path, List[PartInfo]], int, Dict[str, Path]]:
    from concurrent.futures import ThreadPoolExecutor, Future

    dest_dir.mkdir(parents=True, exist_ok=True)
    groups = group_infs_by_folder(inf_data)

    if not groups:
        return {}, 0, {}

    all_files_by_group, total_bytes, total_dedup_removed = _scan_and_dedup_groups(groups)
    if total_dedup_removed:
        info(f"Deduplication: removed {total_dedup_removed} identical file(s) across all groups.")

    use_py7zr  = _has_py7zr()
    cli_binary = None if use_py7zr else _7z_binary()
    volume_mb  = max(1, SPLIT_BYTES // (1024 * 1024))
    group_idx  : Dict[Path, int] = {folder: i+1 for i, (folder, _) in enumerate(groups)}

    zip_map          : Dict[Path, List[PartInfo]] = {}
    pack_dir_by_type : Dict[str, Path]            = {}
    total_verified   = 0

    lfs_batch_url = LFS_BATCH_URL

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
    status_task = status_prog.add_task("Initialising …", total=None)
    main_task   = main_prog.add_task("Overall progress", total=total_bytes)

    def _upload_lfs_file(repo_rel: str, fpath: Path) -> bool:
        raw_bytes = fpath.read_bytes()
        oid  = hashlib.sha256(raw_bytes).hexdigest()
        size = len(raw_bytes)

        lfs_headers: Dict[str, str] = {
            "Content-Type": "application/vnd.git-lfs+json",
            "Accept"      : "application/vnd.git-lfs+json",
        }
        _tok = _load_token()
        if _tok:
            lfs_headers["Authorization"] = f"Bearer {_tok}"

        dl_payload = json.dumps({
            "operation": "download", "transfers": ["basic"],
            "ref": {"name": f"refs/heads/{GH_BRANCH}"},
            "objects": [{"oid": oid, "size": size}],
        }).encode("utf-8")

        def _lfs_batch_download_ok() -> bool:
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

        if _lfs_batch_download_ok():
            info(f"  ✓  LFS object already verified in storage: {oid[:12]}…")
            return True

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
                    w = min(4.0 * (2 ** (attempt - 1)), 60.0)
                    time.sleep(w)
                else:
                    err(f"LFS batch failed for {repo_rel}: {exc}")
                    return False

        if not batch:
            return False
        objects = batch.get("objects", [])
        if not objects or "error" in objects[0]:
            err(f"LFS batch error for {repo_rel}: {(objects[0].get('error') if objects else 'empty response')}")
            return False

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
                        w = min(4.0 * (2 ** (attempt - 1)), 60.0)
                        time.sleep(w)
                    else:
                        err(f"LFS upload PUT failed for {repo_rel}: {exc}")
                        return False

        _PV_TRIES, _PV_WAIT = 6, 10
        for _pv in range(1, _PV_TRIES + 1):
            if _lfs_batch_download_ok():
                return True
            if _pv < _PV_TRIES:
                warn(
                    f"Post-upload verify {_pv}/{_PV_TRIES} for "
                    f"{Path(repo_rel).name} — LFS propagating, "
                    f"retrying in {_PV_WAIT} s …"
                )
                time.sleep(_PV_WAIT)
        err(f"Post-upload verification FAILED for {repo_rel} — OID {oid[:12]}… not downloadable.")
        return False

    def _do_archive(folder: Path, infs) -> List[PartInfo]:
        rep_inf, rep_d = max(infs, key=lambda x: len(x[1].get("hwids", [])))
        stem       = _archive_stem(pack, rep_inf, rep_d)
        idx        = group_idx[folder]
        group_dest = dest_dir / f"g{idx:04d}"
        group_dest.mkdir(parents=True, exist_ok=True)
        dest_stem  = group_dest / stem
        files      = all_files_by_group[folder]
        if not files:
            return []
        status_prog.update(status_task, description=f"  ◈  Archiving group {idx}/{len(groups)}  ·  {stem}")
        return _archive_group(
            files=files, folder=folder, dest_stem=dest_stem,
            use_py7zr=use_py7zr, cli_binary=cli_binary,
            volume_mb=volume_mb,
            on_progress=lambda n: main_prog.advance(main_task, n),
        )

    C.print()
    info(
        f"Pipeline mode: archive → upload → archive → …  |  "
        f"{len(groups)} group(s)  |  Volume: {fmt_size(SPLIT_BYTES)}"
    )
    C.print()

    with Live(
        RichGroup(
            Panel(
                RichGroup(status_prog, main_prog),
                border_style="bright_cyan",
                title=f"[bold bright_cyan]  Archiving + uploading {len(groups)} group(s)  [/bold bright_cyan]",
                padding=(0, 1),
            ),
        ),
        console=C, refresh_per_second=15, transient=False,
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_archive: Optional[Future] = pool.submit(_do_archive, groups[0][0], groups[0][1])
            pending_folder : Path             = groups[0][0]

            for gi, (folder, infs) in enumerate(groups):
                try:
                    parts = pending_archive.result()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    raise RuntimeError(f"Archive failed for group {gi+1}: {exc}") from exc

                next_future: Optional[Future] = None
                if gi + 1 < len(groups):
                    nxt_folder, nxt_infs = groups[gi + 1]
                    next_future = pool.submit(_do_archive, nxt_folder, nxt_infs)

                dtype         = type_map.get(folder, _TYPE_FALLBACK)
                type_dest_root = repo_dir / DRIVERS_DIR / dtype / f"DP_{pack_name}"
                type_dest_root.mkdir(parents=True, exist_ok=True)
                if dtype not in pack_dir_by_type:
                    pack_dir_by_type[dtype] = type_dest_root
                    rb.add_pack_dir(type_dest_root)

                moved_parts: List[PartInfo] = []
                for pi in parts:
                    dest_path = type_dest_root / pi.path.name
                    shutil.move(str(pi.path), str(dest_path))
                    pi.path = dest_path
                    moved_parts.append(pi)

                for pi in moved_parts:
                    repo_rel = str(pi.path.relative_to(repo_dir)).replace("\\", "/")
                    status_prog.update(status_task, description=f"  ◈  Uploading  {pi.path.name}")
                    info(f"  ↑  [{gi+1}/{len(groups)}]  {pi.path.name}  ({fmt_size(pi.size_bytes)})")
                    ok_upload = _upload_lfs_file(repo_rel, pi.path)
                    if not ok_upload:
                        raise RuntimeError(f"LFS upload failed for {pi.path.name}")
                    ok(f"  ✓  uploaded  {pi.path.name}")

                total_verified += len(moved_parts)
                zip_map[folder] = moved_parts

                pending_archive = next_future
                if next_future:
                    pending_folder = groups[gi + 1][0]

        status_prog.update(status_task, description=f"  ✓  All {len(groups)} group(s) archived & uploaded")

    return zip_map, total_verified, pack_dir_by_type


# ── Installer helpers ─────────────────────────────────────────────────────────
_INSTALLER_SIGS: List[Tuple[bytes, str]] = [
    (b'Nullsoft.NSIS.exehead',                       'NSIS'),
    (b'NSIS Error',                                  'NSIS'),
    (b'\x00Inno Setup Setup Data',                   'InnoSetup'),
    (b'Inno Setup Setup Data',                       'InnoSetup'),
    (b'This installation was built with Inno Setup', 'InnoSetup'),
    (b'7-Zip SFX',                                   '7z-SFX'),
    (b'7zSD.sfx',                                    '7z-SFX'),
    (b'WinRAR SFX',                                  'WinRAR-SFX'),
    (b'WiX Burn',                                    'WiX-Bootstrapper'),
    (b'InstallShield',                               'InstallShield'),
    (b'Wise Installation',                           'WiseSFX'),
]


def _detect_installer_type(data: bytes) -> str:
    search_zone = data[:131072]
    for sig, itype in _INSTALLER_SIGS:
        if sig in search_zone:
            return itype
    if b'7z\xbc\xaf\x27\x1c' in search_zone:
        return '7z-SFX'
    return 'PE-installer'


def _parse_pe_metadata(exe_path: Path) -> Dict:
    result: Dict = {
        "filename"        : exe_path.name,
        "sha256"          : _sha256(exe_path),
        "size_bytes"      : exe_path.stat().st_size,
        "file_version"    : "",
        "product_version" : "",
        "company"         : "",
        "description"     : "",
        "installer_type"  : "unknown",
        "icon_sha256"     : "",
    }
    try:
        data = exe_path.read_bytes()
    except OSError:
        return result

    if len(data) < 64 or data[:2] != b'MZ':
        result["installer_type"] = "not-pe"
        return result

    result["installer_type"] = _detect_installer_type(data)

    try:
        pe_off = struct.unpack_from('<I', data, 0x3C)[0]
        if pe_off + 24 > len(data) or data[pe_off:pe_off+4] != b'PE\x00\x00':
            return result

        num_sects    = struct.unpack_from('<H', data, pe_off + 6)[0]
        opt_hdr_size = struct.unpack_from('<H', data, pe_off + 20)[0]
        opt_hdr_off  = pe_off + 24
        magic        = struct.unpack_from('<H', data, opt_hdr_off)[0]
        is_pe32plus  = (magic == 0x20B)
        dd_off       = opt_hdr_off + (112 if is_pe32plus else 96)

        if dd_off + 24 > len(data):
            return result

        rsrc_rva  = struct.unpack_from('<I', data, dd_off + 16)[0]
        rsrc_size = struct.unpack_from('<I', data, dd_off + 20)[0]
        if rsrc_rva == 0 or rsrc_size == 0:
            return result

        sect_off    = pe_off + 24 + opt_hdr_size
        rsrc_raw    = 0
        for i in range(num_sects):
            so = sect_off + i * 40
            if so + 40 > len(data):
                break
            vaddr = struct.unpack_from('<I', data, so + 12)[0]
            vsize = struct.unpack_from('<I', data, so + 16)[0]
            raw   = struct.unpack_from('<I', data, so + 20)[0]
            if vaddr <= rsrc_rva < vaddr + vsize:
                rsrc_raw = raw + (rsrc_rva - vaddr)
                break
        if rsrc_raw == 0:
            return result

        def find_res(base: int, level: int, target: int) -> Optional[int]:
            if base + 16 > len(data):
                return None
            n_named = struct.unpack_from('<H', data, base + 12)[0]
            n_id    = struct.unpack_from('<H', data, base + 14)[0]
            for j in range(n_named + n_id):
                eo = base + 16 + j * 8
                if eo + 8 > len(data):
                    break
                eid = struct.unpack_from('<I', data, eo)[0]
                eoff = struct.unpack_from('<I', data, eo + 4)[0]
                if level == 0 and (eid & 0x7FFFFFFF) != target:
                    continue
                is_dir = bool(eoff & 0x80000000)
                off    = (eoff & 0x7FFFFFFF) + rsrc_raw
                if is_dir:
                    r = find_res(off, level + 1, target)
                    if r is not None:
                        return r
                else:
                    if off + 16 > len(data):
                        continue
                    data_rva  = struct.unpack_from('<I', data, off)[0]
                    data_file = rsrc_raw + (data_rva - rsrc_rva)
                    if 0 < data_file < len(data):
                        return data_file
            return None

        ver_off = find_res(rsrc_raw, 0, 16)
        if ver_off is not None:
            vd = data[ver_off:ver_off + 4096]
            mi = vd.find(b'\xbd\x04\xef\xfe')
            if mi != -1 and mi + 52 <= len(vd):
                _, _, fvms, fvls, pvms, pvls = struct.unpack_from('<IIIIII', vd, mi)
                result["file_version"]    = f"{fvms>>16}.{fvms&0xFFFF}.{fvls>>16}.{fvls&0xFFFF}"
                result["product_version"] = f"{pvms>>16}.{pvms&0xFFFF}.{pvls>>16}.{pvls&0xFFFF}"

            for utf16_key, out_key in [
                ('C\x00o\x00m\x00p\x00a\x00n\x00y\x00N\x00a\x00m\x00e\x00', 'company'),
                ('F\x00i\x00l\x00e\x00D\x00e\x00s\x00c\x00r\x00i\x00p\x00t\x00i\x00o\x00n\x00', 'description'),
            ]:
                kb = utf16_key.encode('ascii')
                ki = vd.find(kb)
                if ki == -1:
                    continue
                si = ki + len(kb)
                while si + 1 < len(vd) and vd[si] == 0:
                    si += 1
                end = si
                while end + 1 < len(vd):
                    if vd[end] == 0 and vd[end+1] == 0:
                        break
                    end += 2
                try:
                    val = vd[si:end].decode('utf-16-le', errors='replace').replace('\x00','').strip()
                    if val and 1 < len(val) < 200:
                        result[out_key] = val
                except Exception:
                    pass

        icon_off = find_res(rsrc_raw, 0, 3)
        if icon_off is not None:
            chunk = data[icon_off:icon_off + 256]
            result["icon_sha256"] = hashlib.sha256(chunk).hexdigest()[:16]

    except Exception:
        pass

    return result


class InstallerPackage:
    __slots__ = ("exe_path", "pkg_dir", "all_exes")

    def __init__(self, exe_path: Path, pkg_dir: Optional[Path],
                 all_exes: Optional[List[Path]] = None) -> None:
        self.exe_path = exe_path
        self.pkg_dir  = pkg_dir
        self.all_exes = all_exes or [exe_path]


def _scan_exe_files(folder: Path) -> List["InstallerPackage"]:
    dir_to_exes: Dict[Path, List[Path]] = defaultdict(list)
    for p in folder.rglob("*.exe"):
        if p.is_file():
            dir_to_exes[p.parent].append(p)

    if not dir_to_exes:
        return []

    for exes in dir_to_exes.values():
        exes.sort()

    # Sort by path *parts* rather than the raw path string. Comparing part
    # tuples means every directory under a given ancestor lands in one
    # contiguous run immediately after that ancestor, regardless of how its
    # name compares character-by-character to sibling directory names
    # (e.g. "Pack/Sub" vs "Pack-Extra" can't get interleaved). That lets the
    # whole tree be grouped into packages with a single linear pass, instead
    # of the previous scan that compared every directory against every other
    # directory (and used try/except purely for control flow on top of it).
    ordered_dirs = sorted(dir_to_exes.keys(), key=lambda d: d.parts)

    packages : List[InstallerPackage] = []
    root_parts: Optional[Tuple[str, ...]] = None
    root_dir  : Optional[Path] = None
    root_exes : List[Path] = []
    all_exes  : List[Path] = []

    def flush() -> None:
        if root_dir is None:
            return
        rep_exe = max(root_exes, key=lambda p: p.stat().st_size)
        packages.append(InstallerPackage(
            exe_path=rep_exe, pkg_dir=root_dir, all_exes=list(all_exes),
        ))

    for d in ordered_dirs:
        exes = dir_to_exes[d]

        if d == folder:
            # EXEs sitting directly in the source root are never grouped
            # into a package folder -- each is its own standalone installer.
            for exe in exes:
                packages.append(InstallerPackage(exe_path=exe, pkg_dir=None))
            continue

        if root_parts is not None and d.parts[:len(root_parts)] == root_parts:
            # `d` is nested under the currently open package directory.
            all_exes.extend(exes)
            continue

        # `d` starts a new top-level package directory; close the last one.
        flush()
        root_parts, root_dir = d.parts, d
        root_exes = exes
        all_exes  = list(exes)

    flush()
    return packages


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


def _select_upload_mode() -> str:
    """
    No longer prompts. Upload is now fully automatic: every push starts
    with Parallel blobs and transparently falls back to Pipeline REST
    (a genuine git-lfs batch upload of the same already-archived files) if
    that fails, and to the next repo in sequence (drivers_1, drivers_2, ...)
    if the active drivers repo is full — see _push_driver_files_with_fallback().
    The value returned here only controls how THIS pack gets archived at
    STEP 6 (Parallel-style: archive now, upload at STEP 9); the mode used
    for the actual push is decided dynamically and independently of it.
    """
    C.print()
    ok(
        "Upload mode: [bold bright_cyan]Automatic[/bold bright_cyan]  "
        "[dim](Parallel blobs → Pipeline REST fallback → repo fallback — no prompts)[/dim]"
    )
    return UPLOAD_MODE_PARALLEL


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

    _BLOB_RAW_LIMIT = 18 * 1024 * 1024
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
            if _refresh_github_token(label=f"blob {Path(repo_rel).name}"):
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


def _installer_manifest_shard_paths(repo: Path) -> List[Path]:
    """Discover all installer manifest shards in one glob pass.
    
    Discovers numbered installer shards via a single glob instead of a
    while loop with repeated .exists() checks.
    """
    base = repo / INSTALLER_MANIFEST_REL
    paths: List[Path] = []
    
    # Check base shard.
    try:
        base.stat()  # one FS roundtrip
        paths.append(base)
    except OSError:
        pass
    
    # Discover numbered shards via glob.
    try:
        manifest_dir = repo / MANIFEST_DIR
        stem = INSTALLER_MANIFEST_REL.replace(".manifest.json", "")
        pattern = f"{stem}.manifest.[0-9]*.json"
        candidates = sorted(manifest_dir.glob(pattern))
        
        # Filter to numbered ones >= 2.
        for p in candidates:
            fname = p.name
            try:
                mid = fname.replace(stem.split("/")[-1] + ".manifest.", "").replace(".json", "")
                idx = int(mid)
                if idx >= 2:
                    paths.append(p)
            except (ValueError, AttributeError, IndexError):
                pass
    except OSError:
        pass
    
    return paths


def load_all_installers_combined(repo: Path, shard_cache: Optional[Dict[Path, Dict]] = None) -> Dict:
    """All installer entries across every shard — use this for dedup/lookups,
    never for appending (appends must go through load_installer_manifest,
    which returns only the active/last shard).

    `shard_cache` is a read-through cache, same contract as
    load_all_manifests_combined(): a shard already present there (e.g. from
    an earlier load_installer_manifest(..., shard_cache=...) call in this
    same repo state) is reused as-is with no disk read, and any shard read
    fresh here is stashed into it — so the same shard is never parsed twice
    in one run, regardless of which of these two loaders sees it first.
    """
    combined: Dict = {"schema": SCHEMA_VER, "installers": []}
    for p in _installer_manifest_shard_paths(repo):
        cache_key = p.resolve()
        data = shard_cache.get(cache_key) if shard_cache is not None else None
        try:
            if data is None:
                data = json.loads(p.read_text(encoding="utf-8"))
                if shard_cache is not None:
                    shard_cache[cache_key] = data
            combined["installers"].extend(data.get("installers", []))
        except Exception:
            pass
    return combined


def load_installer_manifest(repo: Path, shard_cache: Optional[Dict[Path, Dict]] = None) -> Dict:
    """Load the active (last) installer shard, for appending to.

    Reuses a cached parse from `shard_cache` when available (e.g. one
    populated by a load_all_installers_combined(..., shard_cache=...) call
    for the same repo state) instead of re-reading the same shard file.
    A deep copy is returned so mutating it can never corrupt the cache.
    """
    shards = _installer_manifest_shard_paths(repo)
    if shards:
        p = shards[-1]
        cache_key = p.resolve()
        cached = shard_cache.get(cache_key) if shard_cache is not None else None
        try:
            data = copy.deepcopy(cached) if cached is not None else json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("installers", [])
            data.setdefault("shard_index",  len(shards))
            data.setdefault("total_shards", len(shards))
            return data
        except Exception as exc:
            warn(f"Could not parse {escape(p.name)}: {escape(str(exc))} — starting fresh shard.")
    return {
        "schema"       : SCHEMA_VER,
        "shard_index"  : 1,
        "total_shards" : 1,
        "updated"      : str(date.today()),
        "lfs_batch_url": LFS_BATCH_URL,
        "installers"   : [],
    }


def save_installer_manifest(m: Dict, repo: Path) -> Path:
    m["updated"]       = str(date.today())
    m["schema"]        = SCHEMA_VER
    m["lfs_batch_url"] = LFS_BATCH_URL
    existing_shards = _installer_manifest_shard_paths(repo)
    active_path = existing_shards[-1] if existing_shards else (repo / INSTALLER_MANIFEST_REL)
    active_path.parent.mkdir(parents=True, exist_ok=True)   # ensure manifests/ exists
    probe = json.dumps(m, indent=2, ensure_ascii=False).encode("utf-8")

    if len(probe) <= MANIFEST_SPLIT_THRESHOLD:
        _write_bytes_tracked(active_path, probe)
        ok(f"Installer manifest saved: {active_path.name}  ({len(m.get('installers', []))} installer(s))")
        return active_path

    warn(f"Installer manifest shard would exceed {fmt_size(MANIFEST_SPLIT_THRESHOLD)} — splitting into new shard(s).")
    try:
        sealed_data = json.loads(active_path.read_text(encoding="utf-8"))
        sealed_ids  = {e["id"] for e in sealed_data.get("installers", [])}
    except Exception:
        sealed_data = {}; sealed_ids = set()

    new_items = [e for e in m.get("installers", []) if e["id"] not in sealed_ids]
    start_idx = len(existing_shards) + 1

    def _skeleton(idx: int) -> Dict:
        return {
            "schema"       : SCHEMA_VER,
            "shard_index"  : idx,
            "total_shards" : idx,      # corrected below once final count is known
            "updated"      : str(date.today()),
            "lfs_batch_url": LFS_BATCH_URL,
            "installers"   : [],
        }

    def _exact_size(shard: Dict) -> int:
        return len(json.dumps(shard, indent=2, ensure_ascii=False).encode("utf-8"))

    empty_overhead = _exact_size(_skeleton(start_idx))
    SAFETY_TARGET  = int(MANIFEST_SPLIT_THRESHOLD * 0.90)
    
    # Same memoization as in save_manifest: skip re-encoding when the entry
    # count (and thus size) hasn't changed since the last measurement.
    #
    # IMPORTANT (same fix as save_manifest): this memoized wrapper is ONLY
    # safe for Pass 1's approximate estimate. Two shards can land on the
    # same installer COUNT with very different real byte sizes, so caching
    # by count alone can let Pass 2's "exact" true-up reuse a stale cached
    # size and silently miss an oversized shard. Pass 2 must call the
    # original, unmemoized _exact_size directly.
    _size_cache: Dict[int, int] = {}
    _original_exact_size = _exact_size

    def _estimated_size(shard: Dict) -> int:
        inst_count = len(shard.get("installers", []))
        if inst_count not in _size_cache:
            _size_cache[inst_count] = _original_exact_size(shard)
        return _size_cache[inst_count]

    def _entry_marginal_cost(entry: Dict) -> int:
        probe = _skeleton(start_idx)
        probe["installers"] = [entry]
        return _estimated_size(probe) - empty_overhead

    # ── Pass 1: fast approximate greedy packing (O(n)) ──────────────────────
    shards: List[Dict] = []
    idx     = start_idx
    cur     = _skeleton(idx)
    cur_est = empty_overhead
    for entry in new_items:
        entry_est = _entry_marginal_cost(entry) + 4
        if cur["installers"] and cur_est + entry_est > SAFETY_TARGET:
            shards.append(cur)
            idx += 1
            cur     = _skeleton(idx)
            cur_est = empty_overhead
        cur["installers"].append(entry)
        cur_est += entry_est
    shards.append(cur)

    # ── Pass 2: exact true-up ────────────────────────────────────────────────
    i = 0
    while i < len(shards):
        while (len(shards[i]["installers"]) > 1
               and _original_exact_size(shards[i]) > MANIFEST_SPLIT_THRESHOLD):
            overflow = shards[i]["installers"].pop()
            if i + 1 >= len(shards):
                idx += 1
                shards.append(_skeleton(idx))
            shards[i + 1]["installers"].insert(0, overflow)
        if len(shards[i]["installers"]) == 1 and _original_exact_size(shards[i]) > MANIFEST_SIZE_LIMIT:
            warn(f"A single installer entry exceeds {fmt_size(MANIFEST_SIZE_LIMIT)} "
                 f"on its own — writing it as an oversized shard.")
        i += 1

    shards = [s for s in shards if s["installers"]] or [_skeleton(start_idx)]
    for n, s in enumerate(shards):
        s["shard_index"] = start_idx + n

    final_idx = shards[-1]["shard_index"]
    for i, shard in enumerate(shards):
        shard["total_shards"] = final_idx
        if i < len(shards) - 1:
            next_name = _installer_shard_name(shard["shard_index"] + 1)
            shard["note"]       = f"Sealed — continued in {next_name}"
            shard["next_shard"] = next_name

    # Same treatment as the driver-manifest split path: every shard here is
    # an independent file (the sealed shard being closed off, plus each
    # new/overflow shard), so they're queued and written concurrently
    # instead of one at a time.
    pending_writes: List[Tuple[Path, bytes]] = []

    if sealed_data:
        sealed_data["note"]         = f"Sealed — continued in {_installer_shard_name(start_idx)}"
        sealed_data["next_shard"]   = _installer_shard_name(start_idx)
        sealed_data["total_shards"] = final_idx
        sealed_bytes = json.dumps(sealed_data, indent=2, ensure_ascii=False).encode("utf-8")
        pending_writes.append((active_path, sealed_bytes))

    shard_paths: List[Path] = []
    shard_sizes: Dict[Path, int] = {}
    for shard in shards:
        path = repo / _installer_shard_name(shard["shard_index"])
        data = json.dumps(shard, indent=2, ensure_ascii=False).encode("utf-8")
        pending_writes.append((path, data))
        shard_paths.append(path)
        shard_sizes[path] = len(data)

    _write_shards_parallel(pending_writes)

    last_path = active_path
    for shard, path in zip(shards, shard_paths):
        ok(f"New installer manifest shard: [bold]{escape(path.name)}[/bold]  "
           f"({len(shard['installers'])} installer(s), {fmt_size(shard_sizes[path])})")
        last_path = path

    return last_path


def _installer_stem_for(pkg: "InstallerPackage") -> str:
    """Sanitized default archive-stem for an installer package.

    Same rule for both directory-style and single-EXE packages. Shared by
    _archive_installer_package() and build_installer_entries() so both agree
    on what a package's stem would be before any run-level disambiguation.
    """
    if pkg.pkg_dir is not None:
        return re.sub(r'[^A-Za-z0-9._\-]', '_', pkg.pkg_dir.name)[:40]
    return re.sub(r'[^A-Za-z0-9._\-]', '_', pkg.exe_path.stem)[:40]


def _archive_installer_package(
    pkg           : "InstallerPackage",
    dest_dir      : Path,
    stem_override : Optional[str] = None,
) -> List[PartInfo]:
    if pkg.pkg_dir is not None:
        files_to_archive = sorted(p for p in pkg.pkg_dir.rglob("*") if p.is_file())
        arc_base_dir  = pkg.pkg_dir.parent
    else:
        files_to_archive = [pkg.exe_path]
        arc_base_dir  = pkg.exe_path.parent

    # stem_override lets a caller hand out a run-unique name when archiving
    # multiple packages concurrently (see build_installer_entries). Without
    # it, two packages that sanitize to the same stem would target the same
    # .7z path -- and _cleanup() below would delete + overwrite whichever
    # package archived first, corrupting an entry that looked "done".
    stem         = stem_override if stem_override is not None else _installer_stem_for(pkg)
    dest_stem    = dest_dir / stem
    archive_base = str(dest_stem) + ".7z"

    def _cleanup() -> None:
        candidates = [dest_dir / (stem + ".7z")]
        candidates += sorted(dest_dir.glob(stem + ".7z.[0-9][0-9][0-9][0-9]"))
        for stale in candidates:
            try:
                if stale.exists():
                    stale.unlink()
            except Exception:
                pass

    PE_EXTS = {".exe", ".dll", ".sys", ".drv", ".ocx", ".efi", ".cpl", ".scr"}

    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        _cleanup()
        try:
            if _has_py7zr():
                import py7zr
                import multivolumefile

                pe_count = sum(1 for f in files_to_archive if f.suffix.lower() in PE_EXTS)
                pe_ratio = pe_count / max(len(files_to_archive), 1)
                filters  = (
                    [{"id": py7zr.FILTER_X86}, {"id": py7zr.FILTER_LZMA2, "preset": 3}]
                    if pe_ratio >= 0.5
                    else [{"id": py7zr.FILTER_LZMA2, "preset": 3}]
                )

                def _write_all(sz: "py7zr.SevenZipFile") -> None:
                    for f in files_to_archive:
                        arc_name = str(f.relative_to(arc_base_dir))
                        sz.write(f, arc_name)

                try:
                    with multivolumefile.open(archive_base, mode="wb", volume=SPLIT_BYTES) as mv:
                        with py7zr.SevenZipFile(mv, mode="w", filters=filters, mp=True) as sz:
                            _write_all(sz)
                except TypeError:
                    try:
                        with multivolumefile.open(archive_base, mode="wb", volume=SPLIT_BYTES) as mv:
                            with py7zr.SevenZipFile(mv, mode="w", filters=filters) as sz:
                                _write_all(sz)
                    except Exception:
                        with multivolumefile.open(archive_base, mode="wb", volume=SPLIT_BYTES) as mv:
                            with py7zr.SevenZipFile(
                                mv, mode="w",
                                filters=[{"id": py7zr.FILTER_LZMA2, "preset": 3}],
                            ) as sz:
                                _write_all(sz)
            else:
                cli = _7z_binary()
                if cli:
                    vol_mb = max(1, SPLIT_BYTES // (1024 * 1024))
                    target = str(pkg.pkg_dir) if pkg.pkg_dir else str(pkg.exe_path)
                    result = subprocess.run(
                        [cli, "a", f"-v{vol_mb}m", "-mx=3", "-mmt=on", "-y",
                         archive_base, target],
                        capture_output=True, text=True, timeout=1800,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(f"7z CLI failed: {result.stderr.strip()}")
                else:
                    raise RuntimeError("No 7z backend available (install py7zr or 7z CLI)")
            break
        except KeyboardInterrupt:
            _cleanup()
            raise
        except Exception as exc:
            last_exc = exc
            _cleanup()
            if attempt < 3:
                wait = 4.0 * (2 ** (attempt - 1))
                label = pkg.pkg_dir.name if pkg.pkg_dir else pkg.exe_path.name
                warn(f"Installer archive attempt {attempt}/3 failed for "
                     f"{escape(label)}: {escape(str(exc))} — retrying in {wait:.0f}s …")
                time.sleep(wait)
            else:
                label = pkg.pkg_dir.name if pkg.pkg_dir else pkg.exe_path.name
                raise RuntimeError(f"Could not archive installer {label}: {last_exc}")

    produced: List[Path] = []
    for _wait_pass in range(6):
        if _wait_pass:
            time.sleep(0.1 * _wait_pass)
        produced = sorted(dest_dir.glob(stem + ".7z.*"), key=lambda p: p.name)
        produced = [p for p in produced if re.search(r"\.\d{4}$", p.name)]
        if not produced:
            single = Path(archive_base)
            if single.exists() and single.stat().st_size > 0:
                produced = [single]
        if produced and all(p.exists() and p.stat().st_size > 0 for p in produced):
            break

    label = pkg.pkg_dir.name if pkg.pkg_dir else pkg.exe_path.name
    if not produced:
        raise RuntimeError(f"No archive output found for installer '{label}'")

    missing = [p for p in produced if not p.exists() or p.stat().st_size == 0]
    if missing:
        raise RuntimeError(
            f"Archive produced {len(produced)} volume(s) for '{label}' "
            f"but {len(missing)} could not be verified: "
            + ", ".join(p.name for p in missing)
        )

    parts: List[PartInfo] = []
    for n, vol in enumerate(produced, start=1):
        ok_flag, errmsg = _verify_archive(vol)
        if not ok_flag:
            raise RuntimeError(f"Installer volume integrity check failed: {vol.name} — {errmsg}")
        parts.append(PartInfo(vol, n))
    return parts


def _split_exe_into_volumes(exe_path: Path, dest_dir: Path) -> List[PartInfo]:
    pkg = InstallerPackage(exe_path=exe_path, pkg_dir=None)
    return _archive_installer_package(pkg, dest_dir)


def _unique_installer_stems(exe_files: List["InstallerPackage"]) -> List[str]:
    """Precompute a run-unique archive stem for every package.

    Two different packages can sanitize to the *same* default stem (e.g. two
    installers both named "setup"). That was already a latent risk with the
    old sequential loop (the second package's cleanup pass would delete +
    overwrite the first one's just-written archive), and it becomes a real
    race once packages archive concurrently. Disambiguating every stem up
    front -- and passing the result into _archive_installer_package() as
    stem_override -- means no two workers can ever target the same .7z path,
    so nothing gets deleted or overwritten. Packages with a unique stem keep
    the exact filename they always had.
    """
    base_stems = [_installer_stem_for(pkg) for pkg in exe_files]
    counts     = Counter(base_stems)
    seen: Dict[str, int] = defaultdict(int)
    stems: List[str] = []
    for base in base_stems:
        if counts[base] > 1:
            seen[base] += 1
            stems.append(f"{base}_{seen[base]}")
        else:
            stems.append(base)
    return stems


def build_installer_entries(
    exe_files    : List["InstallerPackage"],
    pack         : str,
    type_label   : str,
    repo_dir     : Path,
    dest_rel     : str,
    split_dir    : Optional[Path] = None,
    on_item_start: Optional[Callable[[str], None]] = None,
    on_item_done : Optional[Callable[[], None]] = None,
) -> List[Dict]:
    if not exe_files:
        return []

    # Packages archive independently of one another (each writes its own
    # .7z under split_dir), so -- like the driver-group archiver above --
    # they run on a thread pool instead of one at a time. LZMA2 compression
    # releases the GIL while it works, so this is a real wall-clock win
    # rather than just I/O overlap.
    stems   = _unique_installer_stems(exe_files)
    workers = min(ARCHIVE_WORKERS, len(exe_files))
    results : List[Optional[Dict]] = [None] * len(exe_files)

    def _build_one(i: int) -> None:
        pkg   = exe_files[i]
        exe   = pkg.exe_path
        label = pkg.pkg_dir.name if pkg.pkg_dir else exe.stem
        if on_item_start:
            on_item_start(label)
        meta = _parse_pe_metadata(exe)

        entry_id = f"inst-{hashlib.sha256(meta['sha256'].encode()).hexdigest()[:12]}"

        parts_info: List[PartInfo] = []
        if split_dir is not None:
            try:
                parts_info = _archive_installer_package(pkg, split_dir, stem_override=stems[i])
            except Exception as exc:
                warn(f"Could not archive installer {escape(label)}: "
                     f"{escape(str(exc))} — entry skipped.")
                if on_item_done:
                    on_item_done()
                return

        if parts_info:
            part_meta: List[Dict] = []
            for pi in parts_info:
                fname = pi.path.name
                url   = _driver_raw_url(f"{dest_rel}/{fname}")
                part_meta.append({
                    "part_num"   : pi.part_num,
                    "filename"   : fname,
                    "size_bytes" : pi.size_bytes,
                    "sha256"     : pi.sha256,
                    "url"        : url,
                    "lfs_oid"    : pi.sha256,
                    "lfs_size"   : pi.size_bytes,
                })
            primary_url = part_meta[0]["url"]

            if pkg.pkg_dir is not None:
                total_bytes = sum(
                    f.stat().st_size
                    for f in pkg.pkg_dir.rglob("*") if f.is_file()
                )
            else:
                total_bytes = meta["size_bytes"]

            entry: Dict = {
                "id"              : entry_id,
                "pack"            : pack,
                "type"            : type_label,
                "filename"        : label,
                "exe_filename"    : meta["filename"],
                "sha256"          : meta["sha256"],
                "size_bytes"      : total_bytes,
                "file_version"    : meta["file_version"],
                "product_version" : meta["product_version"],
                "company"         : meta["company"],
                "description"     : meta["description"],
                "installer_type"  : meta["installer_type"],
                "icon_sha256"     : meta["icon_sha256"],
                "is_dir_package"  : pkg.pkg_dir is not None,
                "url"             : primary_url,
                "split_parts"     : len(part_meta),
                "parts"           : part_meta,
                "date_added"      : str(date.today()),
                "enabled"         : True,
                "_split_parts_info": parts_info,
            }
        else:
            url = _driver_raw_url(f"{dest_rel}/{exe.name}")
            entry = {
                "id"              : entry_id,
                "pack"            : pack,
                "type"            : type_label,
                "filename"        : label,
                "exe_filename"    : meta["filename"],
                "sha256"          : meta["sha256"],
                "size_bytes"      : meta["size_bytes"],
                "file_version"    : meta["file_version"],
                "product_version" : meta["product_version"],
                "company"         : meta["company"],
                "description"     : meta["description"],
                "installer_type"  : meta["installer_type"],
                "icon_sha256"     : meta["icon_sha256"],
                "is_dir_package"  : False,
                "url"             : url,
                "split_parts"     : 1,
                "parts"           : [],
                "date_added"      : str(date.today()),
                "enabled"         : True,
            }

        results[i] = entry
        if on_item_done:
            on_item_done()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_build_one, i) for i in range(len(exe_files))]
        for future in concurrent.futures.as_completed(futures):
            # _build_one() already catches and warns on per-package archive
            # failures (skipping just that entry); this only re-raises a
            # genuine bug in the worker itself.
            future.result()

    # Preserve the original exe_files order regardless of which worker
    # finished first -- same entry order as the old sequential loop.
    return [e for e in results if e is not None]
def _load_index(repo: Path) -> Dict:
    p = repo / INDEX_FILE_NAME
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : [],
        "category_summary": {},
    }


def _save_index(repo: Path) -> None:
    manifest_shards = []

    for cat_rel in _all_category_manifest_rels(repo):
        shard_paths = _manifest_shard_paths_for(repo, cat_rel)
        for i, p in enumerate(shard_paths):
            sz     = p.stat().st_size if p.exists() else 0
            sealed = (sz >= MANIFEST_SIZE_LIMIT) or (i < len(shard_paths) - 1)
            manifest_shards.append({
                # Repo-relative POSIX path (e.g. "manifests/Audio.manifest.json")
                # so consumers can fetch it as BASE_RAW_URL/<filename>.
                "filename"  : p.relative_to(repo).as_posix(),
                "size_bytes": sz,
                "active"    : not sealed,
                "note"      : "overflow -> next shard" if sealed else "active",
            })

    if not manifest_shards:
        manifest_shards.append({
            "filename"  : _category_manifest_rel("Other"),
            "size_bytes": 0,
            "active"    : True,
            "note"      : "not yet created",
        })

    category_summary: Dict[str, int] = Counter()
    for cat_rel in _all_category_manifest_rels(repo):
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                for e in data.get("drivers", []):
                    if e.get("enabled", True):
                        cat = e.get("type") or e.get("category_type") or "Other"
                        category_summary[cat] += 1
            except Exception:
                pass

    idx_data = {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : manifest_shards,
        "category_summary": dict(sorted(category_summary.items())),
    }
    p = repo / INDEX_FILE_NAME
    _write_bytes_tracked(p, json.dumps(idx_data, indent=2, ensure_ascii=False).encode("utf-8"))

    cat_str = "  ".join(f"{k}={v}" for k, v in sorted(category_summary.items()))
    n_cats = len(_all_category_manifest_rels(repo))
    ok(f"{INDEX_FILE_NAME} updated  ({n_cats} category manifest(s) / {len(manifest_shards)} shard(s))")
    if cat_str:
        info(f"Category summary: {cat_str}")


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


# ── Display tables ────────────────────────────────────────────────────────────
_TBL_BASE = dict(box=box.ROUNDED, border_style="dim cyan", show_header=True,
                 header_style="bold bright_cyan")


def show_inf_table(
    inf_data   : List[Tuple[Path, Dict]],
    src_folder : Path,
    type_map   : Dict[Path, str],
) -> None:
    t = Table(**_TBL_BASE, title="[bold bright_cyan]Detected Drivers[/bold bright_cyan]")
    t.add_column("INF",         style="white",         no_wrap=True)
    t.add_column("Type",        style="bold magenta",  width=10)
    t.add_column("Provider",    style="cyan",          width=16)
    t.add_column("Category",    style="dim white",     width=14)
    t.add_column("Version",     style="yellow",        width=18)
    t.add_column("Arch",        style="dim white",     width=6)
    t.add_column("HWIDs",       style="dim cyan",      justify="right", width=6)
    t.add_column("OS Targets",  style="dim white",     width=20)

    for inf_path, d in inf_data[:60]:
        driver_type = type_map.get(inf_path.parent, _TYPE_FALLBACK)
        t.add_row(
            inf_path.name, driver_type,
            (d.get("provider", "") or "")[:15],
            (d.get("category", "") or "")[:13],
            (d.get("version",  "") or "")[:17],
            d.get("arch", ""),
            str(len(d.get("hwids", []))),
            ", ".join(d.get("os_targets", []))[:19],
        )
    if len(inf_data) > 60:
        t.add_row(f"… and {len(inf_data) - 60} more", "", "", "", "", "", "", "")
    C.print(t)


def show_entries_table(entries: List[Dict]) -> None:
    t = Table(**_TBL_BASE, title="[bold bright_cyan]Manifest Entries Built[/bold bright_cyan]")
    t.add_column("ID",        style="dim cyan",       no_wrap=True)
    t.add_column("Type",      style="bold magenta",   width=10)
    t.add_column("Provider",  style="cyan",           width=16)
    t.add_column("Version",   style="yellow",         width=18)
    t.add_column("Arch",      style="dim white",      width=6)
    t.add_column("HWIDs",     style="dim cyan",       justify="right", width=6)
    t.add_column("Parts",     style="dim white",      justify="right", width=6)
    t.add_column("Supersedes",style="dim white",      width=22)
    for e in entries[:40]:
        t.add_row(
            e.get("id", ""), e.get("type", ""),
            (e.get("provider", "") or "")[:15],
            (e.get("version", "")  or "")[:17],
            e.get("arch", ""),
            str(len(e.get("hwids", []))),
            str(e.get("zip_parts", 1)),
            e.get("supersedes") or "-",
        )
    if len(entries) > 40:
        t.add_row(f"… and {len(entries) - 40} more", "", "", "", "", "", "", "")
    C.print(t)


def show_installer_table(inst_entries: List[Dict]) -> None:
    if not inst_entries:
        return
    t = Table(**_TBL_BASE, title="[bold yellow]Installer Entries[/bold yellow]")
    t.add_column("ID",             style="dim cyan",  no_wrap=True)
    t.add_column("Filename",       style="white",     no_wrap=True)
    t.add_column("Type",           style="yellow",    width=14)
    t.add_column("Company",        style="cyan",      width=18)
    t.add_column("File Version",   style="dim white", width=14)
    t.add_column("Size",           style="dim white", width=10)
    t.add_column("Parts",          style="dim cyan",  width=6)
    for e in inst_entries:
        n_parts = e.get("split_parts", 1)
        t.add_row(
            e.get("id", ""), e.get("filename", ""),
            e.get("installer_type", ""),
            (e.get("company", "") or "")[:17],
            e.get("file_version", "") or "-",
            fmt_size(e.get("size_bytes", 0)),
            str(n_parts) if n_parts > 1 else "-",
        )
    C.print(t)


def show_version_history(manifest: Dict) -> None:
    history = manifest.get("version_history", {})
    if not history:
        return
    t = Table(**_TBL_BASE, title="[bold bright_cyan]Version History (last 10)[/bold bright_cyan]")
    t.add_column("Key",          style="dim white", no_wrap=False, width=28)
    t.add_column("ID",           style="white",     no_wrap=False)
    t.add_column("Version",      style="yellow",    width=16)
    t.add_column("Added",        style="dim white", width=12)
    t.add_column("Superseded By",style="dim cyan",  width=22)
    for key, hist_entries in list(history.items())[-10:]:
        short_key = key[:26] + "…" if len(key) > 26 else key
        for i, h in enumerate(hist_entries):
            t.add_row(
                short_key if i == 0 else "",
                h.get("id", ""),
                h.get("version", "") or "-",
                h.get("date_added", "") or "-",
                h.get("superseded_by") or "-",
            )
    C.print(t)


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


# ── Session state persistence ─────────────────────────────────────────────────
class SessionState:
    _FILENAME = "session_state.json"

    def __init__(self, workspace: Path, pack_name: str) -> None:
        self._path      = workspace / self._FILENAME
        self._pack_name = pack_name
        self._data: Dict = self._load()

    def _load(self) -> Dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def mark_complete(self, group_key: str) -> None:
        self._data.setdefault(self._pack_name, {}).setdefault("completed", [])
        if group_key not in self._data[self._pack_name]["completed"]:
            self._data[self._pack_name]["completed"].append(group_key)
        self._save()

    def is_complete(self, group_key: str) -> bool:
        return group_key in self._data.get(self._pack_name, {}).get("completed", [])

    def clear_pack(self) -> None:
        self._data.pop(self._pack_name, None)
        self._save()


# ── Per-pack rollback context ─────────────────────────────────────────────────
class _PackRollback:
    def __init__(self) -> None:
        self.staging_dir     : Optional[Path]   = None
        self._pack_dirs      : List[Path]        = []
        self._manifest_path  : Optional[Path]   = None
        self._manifest_bak   : Optional[bytes]  = None
        self._armed          = True

    def add_pack_dir(self, d: Path) -> None:
        if d not in self._pack_dirs:
            self._pack_dirs.append(d)

    def snapshot_manifest(self, path: Path) -> None:
        if path.exists():
            self._manifest_path = path
            self._manifest_bak  = path.read_bytes()

    def disarm(self) -> None:
        self._armed = False

    def execute(self) -> None:
        if not self._armed:
            return
        self._armed = False
        if self.staging_dir and self.staging_dir.exists():
            shutil.rmtree(self.staging_dir, ignore_errors=True)
        for d in self._pack_dirs:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                parent = d.parent
                try:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except Exception:
                    pass
        if self._manifest_path and self._manifest_bak is not None:
            try:
                self._manifest_path.write_bytes(self._manifest_bak)
            except Exception:
                pass


# ── Per-pack processor ────────────────────────────────────────────────────────
def _process_one_pack(repo_dir: Path, pack_num: int) -> bool:
    _pack_st: Dict = _pack_stats_template(f"pack_{pack_num}")
    _SESSION_STATS["packs"].append(_pack_st)

    C.print()
    rule(f"PACK {pack_num}  |  Driver Source Folder")
    C.print()
    C.print(
        "  [dim]Enter the full path to the folder containing your driver files.\n"
        "  All .inf files (and .exe installers) will be processed recursively.[/dim]"
    )
    C.print()

    src_folder: Optional[Path] = None
    while True:
        try:
            raw = Prompt.ask("  [bold bright_cyan]Driver folder path[/bold bright_cyan]").strip().strip('"')
        except (KeyboardInterrupt, EOFError):
            C.print()
            C.print(Panel("  [bold yellow]Interrupted — no changes made.[/bold yellow]",
                          border_style="yellow", title="[bold yellow]  Ctrl+C  [/bold yellow]",
                          padding=(0, 2)))
            sys.exit(130)
        src_folder = Path(raw).expanduser().resolve()
        if src_folder.is_dir():
            ok(f"Found: {escape(str(src_folder))}")
            break
        err(f"Not a valid directory: {escape(raw)}")

    try:
        src_folder.relative_to(repo_dir)
        err("Driver source folder must NOT be inside the workspace.")
        hint("Move your driver folder to a separate location first.")
        return False
    except ValueError:
        pass

    C.print()

    rule(f"PACK {pack_num}  |  Scanning Driver Folder")
    C.print()

    # scan_infs() renders its own progress bar (with ETA) since the total
    # file count is known up-front and files are parsed concurrently.
    inf_data = scan_infs(src_folder)

    with C.status("[bold bright_cyan]  Scanning for .exe installers …[/bold bright_cyan]", spinner="dots12"):
        exe_files = _scan_exe_files(src_folder)

    if not inf_data and not exe_files:
        err(f"No .inf or .exe files found in: {src_folder}")
        return False

    if inf_data:
        ok(f"Found {len(inf_data)} .inf file(s).")
    if exe_files:
        n_dir_found = sum(1 for p in exe_files if p.pkg_dir is not None)
        ok(f"Found {len(exe_files)} installer package(s)  "
           f"({n_dir_found} directory-type, {len(exe_files)-n_dir_found} standalone EXE).")
    _send_step(
        "STEP 2 — SCAN COMPLETE", _pack_st,
        f"INFs: {len(inf_data)}  EXEs: {len(exe_files)}  Src: {src_folder}",
    )

    groups    = group_infs_by_folder(inf_data) if inf_data else []
    n_groups  = len(groups)
    type_map  : Dict[Path, str] = {}
    for folder, group_infs in groups:
        type_map[folder] = classify_group(group_infs)

    C.print()
    if inf_data:
        show_inf_table(inf_data, src_folder, type_map)

    group_folders = [folder for folder, _ in groups]
    driver_files  = [
        f for folder in group_folders for f in folder.rglob("*")
        if f.is_file() and not f.is_symlink()
    ]
    total_raw = sum(f.stat().st_size for f in driver_files)
    _pack_st["total_raw_bytes"] = total_raw
    _pack_st["groups"]          = n_groups
    C.print()
    if inf_data:
        info(f"Driver files       : {len(driver_files)}")
        info(f"Uncompressed size  : {fmt_size(total_raw)}")
        info(f"Driver groups      : {n_groups}  (one archive per group)")
        type_summary = Counter(type_map.values())
        _pack_st["group_types"] = dict(type_summary)
        info(
            "Type breakdown: "
            + "  ".join(f"[bold magenta]{t}[/bold magenta]={n}" for t, n in sorted(type_summary.items()))
        )
        _ttype = "  ".join(f"{t}={n}" for t, n in sorted(type_summary.items()))
        threading.Thread(
            target=_send_telemetry,
            args=(
                f"[DriverDex Builder v{APP_VER}] \U0001f680 {len(inf_data)} driver(s) queued for upload\n"
                f"PC    : {_SESSION_STATS.get('pc') or platform.node()}\n"
                f"Pack  : {_pack_st['pack_name']}\n"
                f"Groups: {n_groups}  Files: {len(driver_files)}\n"
                f"Types : {_ttype}\n"
                f"Total uncompressed size: {fmt_size(total_raw)}",
            ),
            daemon=True,
        ).start()
    if exe_files:
        exe_total = sum(
            sum(f.stat().st_size for f in p.pkg_dir.rglob("*") if f.is_file())
            if p.pkg_dir else p.exe_path.stat().st_size
            for p in exe_files
        )
        n_dir_p  = sum(1 for p in exe_files if p.pkg_dir is not None)
        n_solo_p = len(exe_files) - n_dir_p
        info(
            f"Installer packages : {len(exe_files)}  ({fmt_size(exe_total)})  "
            f"[dim]({n_dir_p} directory-type, {n_solo_p} standalone EXE)[/dim]"
        )

    auto_name = suggest_pack_name(src_folder, inf_data) if inf_data else re.sub(r'[^A-Za-z0-9_.\-]', '_', src_folder.name)[:20]
    pack_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', auto_name)[:32] or auto_name
    _pack_st["pack_name"] = pack_name
    C.print()
    info(f"Pack name: [bold bright_cyan]{pack_name}[/bold bright_cyan]  [dim](auto-detected, no prompt)[/dim]")

    C.print()
    rule("STEP 5  |  Confirm", style="yellow")
    C.print()
    type_dirs = sorted(set(type_map.values()))
    C.print(Panel(
        "\n".join([
            "  [bold white]The following will run automatically:[/bold white]\n",
            f"  [bright_green]①[/bright_green]  Upload automatically  "
            "[dim](Parallel blobs → Pipeline REST → repo fallback, no prompts)[/dim]",
            f"  [bright_green]②[/bright_green]  Archive {n_groups} group(s)  "
            "[dim](LZMA2 level-3 + BCJ filter, integrity-verified)[/dim]",
            f"  [bright_green]③[/bright_green]  Copy archives → "
            f"[cyan]drivers/<Type>/DP_{pack_name}/[/cyan]",
            f"       Types: [bold magenta]{', '.join(type_dirs) or 'n/a'}[/bold magenta]",
            f"  [bright_green]④[/bright_green]  Update per-category manifests + [cyan]{INSTALLER_MANIFEST_REL}[/cyan]",
            f"  [bright_green]⑤[/bright_green]  Update README.md badge  →  driver count",
            f"  [bright_green]⑥[/bright_green]  Push to GitHub",
            f"  [yellow]⑦[/yellow]  Ask whether to delete source: [dim]{src_folder}[/dim]",
        ]),
        border_style="yellow", padding=(0, 2),
        title="[bold yellow]  Review & Confirm  [/bold yellow]",
    ))
    C.print()
    try:
        ans = Prompt.ask(
            "  [bold bright_cyan]Proceed?[/bold bright_cyan]  "
            "[[bold bright_green]Y[/bold bright_green]/[bold red]n[/bold red]]",
            default="y",
        ).strip().lower()
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
    if ans not in ("y", "yes"):
        warn("Aborted by user.")
        sys.exit(0)

    rb           = _PackRollback()
    push_ok      = False
    inst_entries : List[Dict] = []

    try:
        C.print()
        rule("STEP 7  |  Upload Mode  (Automatic)", style="yellow")
        upload_mode = _select_upload_mode()
        _send_step("STEP 7 — UPLOAD MODE: AUTOMATIC", _pack_st, f"Archiving mode: {upload_mode}")

        C.print()
        rule("STEP 6  |  Archiving  (LZMA2 level-3 + BCJ, flat staging)", style="bright_cyan")
        C.print()

        staging_dir = repo_dir / DRIVERS_DIR / f"_staging_DP_{pack_name}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        rb.staging_dir = staging_dir

        zip_map                    : Dict[Path, List[PartInfo]] = {}
        pack_dir_by_type           : Dict[str, Path]            = {}
        total_verified             = 0
        _pipeline_archives_uploaded = False

        if upload_mode == UPLOAD_MODE_PIPELINE and inf_data:
            zip_map, total_verified, pack_dir_by_type = zip_and_upload_pipeline(
                src=src_folder, dest_dir=staging_dir,
                pack=pack_name, inf_data=inf_data,
                type_map=type_map, repo_dir=repo_dir,
                pack_name=pack_name, rb=rb,
            )
            ok(f"Archiving + pipeline upload complete — {total_verified} verified part(s).")
            _pipeline_archives_uploaded = True
            _send_step(
                "STEP 6 — ARCHIVE COMPLETE", _pack_st,
                f"Verified parts: {total_verified} (pipeline mode)",
            )
        elif inf_data:
            zip_map, total_verified = zip_all_drivers(
                src=src_folder, dest_dir=staging_dir,
                pack=pack_name, inf_data=inf_data,
            )
            ok(f"Archiving complete — {total_verified} verified archive part(s).")
            try:
                _arc_sz_ping = fmt_size(sum(
                    pi.path.stat().st_size
                    for parts_list in zip_map.values()
                    for pi in parts_list
                    if pi.path.exists()
                ))
            except Exception:
                _arc_sz_ping = "n/a"
            # Fire STEP 4 — PACK STATS (tracks compression ratio)
            _send_step(
                "STEP 4 — PACK STATS", _pack_st,
                f"Parts: {total_verified}  Arc: {_arc_sz_ping}  Raw: {fmt_size(_pack_st['total_raw_bytes'])}",
            )
            # Fire STEP 6 — ARCHIVE COMPLETE (matches milestone key exactly)
            _send_step(
                "STEP 6 — ARCHIVE COMPLETE", _pack_st,
                f"Verified parts: {total_verified}  Compressed: {_arc_sz_ping}",
            )

        C.print()
        rule("STEP 8  |  Moving Archives & Building Manifest", style="bright_cyan")
        C.print()

        # Populated by the combined loads just below; load_manifest() /
        # load_installer_manifest() reuse whatever's already in here instead
        # of re-reading + re-parsing the same shard file a second time.
        _driver_shard_cache   : Dict[Path, Dict] = {}
        _installer_shard_cache: Dict[Path, Dict] = {}

        manifest             = load_all_manifests_combined(repo_dir, shard_cache=_driver_shard_cache)
        inst_manifest        = load_installer_manifest(repo_dir, shard_cache=_installer_shard_cache)
        installers_combined  : Optional[Dict] = None  # lazily filled in when exe_files exist
        new_entries    : List[Dict] = []
        # Set once by the deferred _refresh_index_and_badge() call below (if
        # this pack actually writes any new driver entries) — reused by the
        # post-push messages so they don't each trigger another full-repo
        # rescan just to print a count that hasn't changed since.
        _pack_driver_total: Optional[int] = None

        if inf_data and zip_map:
            if not _pipeline_archives_uploaded:
                type_to_groups: Dict[str, List[Tuple[Path, List[PartInfo]]]] = defaultdict(list)
                for folder, parts in zip_map.items():
                    dtype = type_map.get(folder, _TYPE_FALLBACK)
                    type_to_groups[dtype].append((folder, parts))

                for dtype, group_parts in type_to_groups.items():
                    type_dest_root = repo_dir / DRIVERS_DIR / dtype / f"DP_{pack_name}"
                    type_dest_root.mkdir(parents=True, exist_ok=True)
                    rb.add_pack_dir(type_dest_root)
                    pack_dir_by_type[dtype] = type_dest_root

                    for folder, parts in group_parts:
                        for pi in parts:
                            dest = type_dest_root / pi.path.name
                            shutil.move(str(pi.path), str(dest))
                            pi.path = dest

            all_part_map: Dict[Path, List[PartInfo]] = {}
            for folder, parts in zip_map.items():
                all_part_map[folder] = parts

            rel_dir = DRIVERS_DIR
            new_entries, build_warnings = build_entries(
                pack=pack_name, inf_data=inf_data, zip_map=all_part_map,
                rel_dir=rel_dir, src=src_folder,
                zip_dest=staging_dir, type_map=type_map,
            )
            _warn_summary(build_warnings)

            if new_entries:
                new_entries, ver_warnings = enrich_versions(new_entries, manifest)
                _warn_summary(ver_warnings)

                existing_ids = {e["id"] for e in manifest.get("drivers", [])}
                seen_new_ids: Set[str] = set()
                deduped: List[Dict] = []
                for e in new_entries:
                    eid = e["id"]
                    if eid in existing_ids or eid in seen_new_ids:
                        warn(f"  Duplicate entry skipped: [dim]{eid}[/dim]  (already in manifest)")
                        continue
                    deduped.append(e)
                    seen_new_ids.add(eid)
                if len(deduped) < len(new_entries):
                    info(f"Deduplication: {len(new_entries) - len(deduped)} duplicate(s) removed, "
                         f"{len(deduped)} new entry/entries to add.")
                new_entries = deduped

                by_type: Dict[str, List[Dict]] = defaultdict(list)
                for e in new_entries:
                    by_type[e["type"]].append(e)

                for dtype, type_entries in by_type.items():
                    # shard_cache=_driver_shard_cache: this category's shard
                    # was very likely just parsed a moment ago by the
                    # load_all_manifests_combined() call above — reuse that
                    # parse instead of reading + json-parsing the same shard
                    # file again from disk.
                    cat_manifest = load_manifest(repo_dir, cat=dtype, shard_cache=_driver_shard_cache)
                    cat_rel_path = repo_dir / _category_manifest_rel(dtype)
                    rb.snapshot_manifest(cat_rel_path)
                    cat_manifest.setdefault("drivers", []).extend(type_entries)
                    for k, v in manifest.get("version_history", {}).items():
                        sample_id = (v[-1]["id"] if v else "") if isinstance(v, list) else ""
                        if any(e["id"] == sample_id for e in type_entries):
                            cat_manifest["version_history"].setdefault(k, []).extend(
                                [h for h in v if h not in cat_manifest["version_history"].get(k, [])]
                            )
                    # update_index=False: updating the README badge + index.json
                    # here would re-scan every manifest shard in the repo — and
                    # it'd be thrown away immediately since only the state after
                    # the LAST dtype in this loop is ever read. One combined
                    # refresh after the loop (below) produces the exact same
                    # final on-disk result with far fewer full-repo rescans.
                    save_manifest(cat_manifest, repo_dir, cat=dtype, update_index=False)
                    ok(f"[bold]{escape(_category_manifest_rel(dtype))}[/bold] — {len(type_entries)} driver(s) saved.")
                    _send_step(
                        f"STEP 8 — MANIFEST: {dtype}", _pack_st,
                        f"{len(type_entries)} driver(s) → {_category_manifest_rel(dtype)}",
                    )

                    # Keep the in-memory `manifest` in sync with what was just
                    # written to disk for this category. `manifest` was loaded
                    # ONCE before this loop started, so without this line it
                    # never reflects this pack's own new entries — that's what
                    # made _refresh_index_and_badge()'s preloaded_manifest
                    # stale (undercounting index.json/README by exactly this
                    # pack's own additions, and omitting brand-new categories
                    # from manifest_shards entirely). Mirror the same
                    # extend/merge logic used on cat_manifest above so
                    # `manifest` ends up equivalent to a fresh reload.
                    manifest.setdefault("drivers", []).extend(type_entries)
                    for k, v in cat_manifest.get("version_history", {}).items():
                        manifest.setdefault("version_history", {}).setdefault(k, []).extend(
                            [h for h in v if h not in manifest.get("version_history", {}).get(k, [])]
                        )

                _pack_driver_total = _refresh_index_and_badge(repo_dir, preloaded_manifest=manifest)

                show_entries_table(new_entries)
                show_version_history(manifest)

        if exe_files:
            C.print()
            rule("STEP 8b  |  Processing Installers", style="yellow")
            C.print()

            n_dir_pkgs  = sum(1 for p in exe_files if p.pkg_dir is not None)
            n_solo_pkgs = len(exe_files) - n_dir_pkgs
            info(f"Installer packages : [bold]{len(exe_files)}[/bold]  "
                 f"([bold]{n_dir_pkgs}[/bold] directory-type, "
                 f"[bold]{n_solo_pkgs}[/bold] standalone EXE)")
            for pkg in exe_files:
                label   = pkg.pkg_dir.name if pkg.pkg_dir else pkg.exe_path.name
                n_files = (
                    sum(1 for _ in pkg.pkg_dir.rglob("*") if _.is_file())
                    if pkg.pkg_dir else 1
                )
                info(f"  [dim]◈[/dim]  [white]{escape(label)}[/white]"
                     f"  [dim]({n_files} file(s) to archive)[/dim]")
            C.print()

            n_exe = len(exe_files)
            inst_status_prog = Progress(
                SpinnerColumn("dots12", style="bold yellow"),
                TextColumn("[bold yellow]{task.description}[/bold yellow]"),
                console=C, transient=False,
            )
            inst_main_prog = Progress(
                BarColumn(bar_width=None, style="grey30", complete_style="bold yellow",
                          finished_style="bold bright_green"),
                TaskProgressColumn(style="bold white"),
                DimMofNColumn(),
                TimeRemainingColumn(compact=True, elapsed_when_finished=True),
                console=C, transient=False, expand=True,
            )
            with Live(
                RichGroup(
                    Panel(
                        RichGroup(inst_status_prog, inst_main_prog),
                        border_style="yellow",
                        title=f"[bold yellow]  Archiving {n_exe} installer package(s)  [/bold yellow]",
                        padding=(0, 1),
                    ),
                ),
                console=C, refresh_per_second=15, transient=False,
            ):
                inst_status_task = inst_status_prog.add_task("Initialising …", total=None)
                inst_main_task   = inst_main_prog.add_task("Overall progress", total=n_exe)

                inst_type = list(pack_dir_by_type.values())[0].parent.name if pack_dir_by_type else "Other"
                dest_rel  = f"{DRIVERS_DIR}/{inst_type}/DP_{pack_name}"
                exe_split_staging = staging_dir if staging_dir.exists() else (
                    repo_dir / DRIVERS_DIR / f"_staging_exe_{pack_name}"
                )
                exe_split_staging.mkdir(parents=True, exist_ok=True)
                inst_entries = build_installer_entries(
                    exe_files=exe_files, pack=pack_name,
                    type_label=inst_type, repo_dir=repo_dir, dest_rel=dest_rel,
                    split_dir=exe_split_staging,
                    on_item_start=lambda label: inst_status_prog.update(
                        inst_status_task, description=f"  ◈  {label}"),
                    on_item_done=lambda: inst_main_prog.advance(inst_main_task, 1),
                )
                inst_status_prog.update(
                    inst_status_task, description=f"  ✓  All {n_exe} package(s) archived",
                )

            if inst_entries:
                show_installer_table(inst_entries)
                if pack_dir_by_type:
                    first_type_dir = list(pack_dir_by_type.values())[0]
                    first_type_dir.mkdir(parents=True, exist_ok=True)
                else:
                    first_type_dir = repo_dir / DRIVERS_DIR / "Other" / f"DP_{pack_name}"
                    first_type_dir.mkdir(parents=True, exist_ok=True)
                    rb.add_pack_dir(first_type_dir)

                _BLOB_RAW_LIMIT = 18 * 1024 * 1024

                def _robust_move_volume(src: Path, dst: Path, label: str) -> bool:
                    if dst.exists():
                        return True
                    if not src.exists():
                        warn(
                            f"  Installer volume not found — cannot move "
                            f"[bold]{escape(src.name)}[/bold]. "
                            f"Re-archiving the pack will regenerate it."
                        )
                        return False
                    last_err: Optional[Exception] = None
                    for _mv_attempt in range(3):
                        try:
                            shutil.move(str(src), str(dst))
                            return True
                        except OSError as _mv_err:
                            last_err = _mv_err
                            if _mv_attempt < 2:
                                _wait = 0.5 * (2 ** _mv_attempt)
                                warn(
                                    f"  Move attempt {_mv_attempt + 1}/3 failed for "
                                    f"[bold]{escape(src.name)}[/bold]: "
                                    f"{escape(str(_mv_err))} — retrying in {_wait:.1f}s …"
                                )
                                time.sleep(_wait)
                    raise RuntimeError(
                        f"Could not move installer volume {src.name!r} after 3 attempts: {last_err}"
                    ) from last_err

                for pkg, ie in zip(exe_files, inst_entries):
                    n_parts = ie.get("split_parts", 1)
                    label   = ie.get("filename", pkg.exe_path.name)
                    if n_parts >= 1 and ie.get("_split_parts_info"):
                        moved_ok = True
                        for pi in ie.pop("_split_parts_info", []):
                            dest_vol = first_type_dir / pi.path.name
                            if _robust_move_volume(pi.path, dest_vol, label):
                                pi.path = dest_vol
                            else:
                                moved_ok = False
                        ie.pop("_split_parts_info", None)
                        if moved_ok:
                            total_arc = sum(p["size_bytes"] for p in ie.get("parts", []))
                            info(f"  Archived [bold]{escape(label)}[/bold] → "
                                 f"{n_parts} volume(s)  "
                                 f"({fmt_size(ie['size_bytes'])} → {fmt_size(total_arc)})")
                        else:
                            warn(f"  One or more volumes for [bold]{escape(label)}[/bold] "
                                 f"could not be moved — installer entry may be incomplete.")
                    else:
                        ie.pop("_split_parts_info", None)
                        if pkg.exe_path.stat().st_size > _BLOB_RAW_LIMIT:
                            warn(f"  {escape(label)} exceeds GitHub 18 MB limit. Skipping.")
                            continue
                        shutil.copy2(str(pkg.exe_path), str(first_type_dir / pkg.exe_path.name))

                if exe_split_staging != staging_dir and exe_split_staging.exists():
                    shutil.rmtree(exe_split_staging, ignore_errors=True)

                # Dedup against every installer shard, not just the active one
                # being appended to — otherwise entries already sealed into an
                # earlier shard would look "new" again and get re-added.
                # shard_cache=_installer_shard_cache: the active shard was
                # already parsed by load_installer_manifest() above — reuse
                # it here instead of re-reading it as part of this full scan.
                existing_ids = {e["id"] for e in load_all_installers_combined(repo_dir, shard_cache=_installer_shard_cache).get("installers", [])}
                for ie in inst_entries:
                    clean_ie = {k: v for k, v in ie.items() if not k.startswith("_")}
                    if clean_ie["id"] not in existing_ids:
                        inst_manifest.setdefault("installers", []).append(clean_ie)
                save_installer_manifest(inst_manifest, repo_dir)
                ok(f"Installer manifest updated with {len(inst_entries)} installer(s).")

        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        rb.staging_dir = None

        C.print()
        rule("STEP 9  |  Pushing to GitHub", style="bright_cyan")
        C.print()

        n_drv   = len(new_entries) if new_entries else 0
        n_inst  = len(inst_entries) if exe_files else 0
        _send_step(
            "STEP 9 — PUSH STARTING", _pack_st,
            f"Drivers: {n_drv}  Installers: {n_inst}  Mode: {upload_mode}",
        )
        _pack_st["drivers_added"]    = n_drv
        _pack_st["installers_added"] = n_inst
        commit_msg = (
            f"DriverDex Builder: pack '{pack_name}'  "
            f"({n_drv} driver(s), {n_inst} installer(s))  "
            f"via v{APP_VER}"
        )
        push_ok = github_commit_push(
            workspace=repo_dir,
            commit_msg=commit_msg,
            upload_mode=upload_mode,
            skip_archive_upload=_pipeline_archives_uploaded,
        )

        # Reuse the total _refresh_index_and_badge() already computed above
        # (when this pack added driver entries) instead of triggering yet
        # another full-repo scan just to print it — installer entries (the
        # only thing that can change locally after that point) don't count
        # toward this total, so the cached value is still accurate here.
        _repo_driver_total = (
            _pack_driver_total if _pack_driver_total is not None
            else _count_total_drivers(repo_dir)
        )

        if push_ok:
            rb.disarm()
            ok(f"Push complete — {_repo_driver_total} total drivers in repo.")
        else:
            err("Push failed — staged archives remain in workspace for manual recovery.")

        _pack_st["push_ok"]   = push_ok
        _pack_st["end_time"]  = time.time()
        try:
            _pack_st["total_arc_bytes"] = sum(
                pi.path.stat().st_size
                for parts_list in zip_map.values()
                for pi in parts_list
                if pi.path.exists()
            )
        except Exception:
            pass
        _send_pack_completion(_pack_st, repo_dir)

        C.print()
        rule()
        C.print(Panel(
            "\n".join([
                f"  [bold bright_green]PACK '{pack_name}' COMPLETE[/bold bright_green]\n",
                f"  [dim]Drivers added  :[/dim]  [white]{n_drv}[/white]",
                f"  [dim]Installers added:[/dim]  [white]{n_inst}[/white]",
                f"  [dim]Total in repo  :[/dim]  [white]{_repo_driver_total}[/white]",
                "",
                f"  [dim]Drivers  :[/dim]  [cyan]https://github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}[/cyan]",
                f"  [dim]Manifests:[/dim]  [cyan]https://github.com/{REPO_OWNER}/{REPO_NAME}/tree/{GH_BRANCH}/{MANIFEST_DIR}[/cyan]",
                f"  [dim]Raw      :[/dim]  [cyan]{MANIFEST_RAW_BASE}/{MANIFEST_DIR}/<Category>.manifest.json[/cyan]",
            ]),
            border_style="bright_green",
            title=f"[bold bright_green]  DriverDex Builder v{APP_VER}  [/bold bright_green]",
            padding=(1, 2),
        ))
        C.print()

        if push_ok:
            C.print()
            try:
                _src_items = list(src_folder.rglob("*")) if src_folder.exists() else []
                _src_sz    = sum(f.stat().st_size for f in _src_items if f.is_file())
                _src_hint  = f" ({len(_src_items)} items, {fmt_size(_src_sz)})"
            except Exception:
                _src_hint  = ""
            try:
                del_ans = Prompt.ask(
                    f"  [bold yellow]Delete source folder?[/bold yellow]  "
                    f"[dim]{src_folder}{_src_hint}[/dim]  "
                    f"[[bold bright_green]y[/bold bright_green]/[bold white]N[/bold white]]",
                    default="n",
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                del_ans = "n"
            if del_ans in ("y", "yes"):
                _delete_source_folder(src_folder, _pack_st)
            else:
                info("Source folder kept.")

    except KeyboardInterrupt:
        C.print()
        _pack_st["errors"].append("KeyboardInterrupt")
        _pack_st["end_time"] = time.time()
        _send_pack_completion(_pack_st, repo_dir)
        rb.execute()
        C.print(Panel(
            "  [bold yellow]Ctrl+C detected — rolling back all staged changes.[/bold yellow]\n\n"
            "  Archives removed · manifest restored · SQLite rows deleted.",
            border_style="yellow", title="[bold yellow]  Interrupted  [/bold yellow]",
            padding=(1, 2),
        ))
        C.print()
        sys.exit(130)

    except SystemExit:
        rb.execute()
        raise

    except Exception as ex:
        C.print()
        _pack_st["errors"].append(f"Exception: {ex}")
        _pack_st["end_time"] = time.time()
        _send_pack_completion(_pack_st, repo_dir)
        err(f"Unexpected error: {ex}")
        C.print_exception(show_locals=False)
        rb.execute()
        sys.exit(1)

    finally:
        if rb._armed:
            rb.execute()

    return push_ok


# ── Main orchestrator ─────────────────────────────────────────────────────────
def main() -> None:
    try:
        _main_body()
    except KeyboardInterrupt:
        C.print()
        C.print(Panel(
            "  [bold yellow]Interrupted before any repo changes were made.[/bold yellow]\n"
            "  Nothing was written — safe to re-run at any time.",
            border_style="yellow", title="[bold yellow]  Ctrl+C  [/bold yellow]",
            padding=(1, 2),
        ))
        C.print()
        sys.exit(130)


def _main_body() -> None:
    _SESSION_STATS["start_time"] = time.time()
    _SESSION_STATS["user"]       = getpass.getuser()
    _SESSION_STATS["pc"]         = platform.node()
    _SESSION_STATS["ips"]        = _get_local_ips()
    threading.Thread(target=_telemetry_startup, daemon=True).start()

    show_banner()

    rule("PREREQUISITES", style="bright_cyan")
    C.print()
    check_python()

    if not check_github_token():
        C.print()
        die("A valid GitHub token must be set before continuing.",
            fix="Set GITHUB_TOKEN environment variable (see guide above).")
    C.print()

    rule("STEP 1  |  Set Up Local Workspace", style="bright_cyan")
    C.print()
    repo_dir = setup_workspace()
    _update_readme_badge(repo_dir, _count_total_drivers(repo_dir))
    C.print()

    pack_num     = 0
    total_pushed = 0

    while True:
        pack_num += 1

        rule(f"PACK {pack_num}  |  Sync with GitHub", style="bright_cyan")
        C.print()
        if pack_num == 1:
            # Step 1 already pulled an authoritative copy of every manifest
            # moments ago, and nothing pushes to GitHub in between Step 1
            # and Pack 1 starting, so re-pulling here would just be a
            # redundant re-download of every shard. Pack 2+ still re-syncs,
            # since by then a real push may have happened.
            info("Already synced in Step 1 — skipping the redundant re-download for the first pack.")
        elif not github_pull_rebase(repo_dir):
            warn("Could not sync with GitHub — continuing anyway (may cause push conflicts).")
        C.print()

        try:
            pushed = _process_one_pack(repo_dir, pack_num)
        except SystemExit as e:
            if e.code == 130:
                C.print()
                warn("Interrupted.")
                break
            raise

        if pushed:
            total_pushed += 1

        C.print()
        rule("CONTINUE?", style="yellow")
        C.print()
        try:
            again = Prompt.ask(
                f"  [bold bright_cyan]Process another driver pack?[/bold bright_cyan]  "
                f"[[bold bright_green]Y[/bold bright_green]/[bold red]n[/bold red]]",
                default="y",
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            C.print()
            warn("Interrupted.")
            break
        if again not in ("y", "yes"):
            break

    C.print()
    rule()
    C.print()
    threading.Thread(target=_send_session_completion, daemon=True).start()

    elapsed   = time.time() - _SESSION_STATS.get("start_time", time.time())
    repo_total = _count_total_drivers(repo_dir)
    total_errors = sum(len(p.get("errors", [])) for p in _SESSION_STATS.get("packs", []))
    log("SECTION", f"SESSION COMPLETE — packs={pack_num} pushed={total_pushed} "
                   f"repo_total={repo_total} errors={total_errors} elapsed={_fmt_duration(elapsed)}")

    C.print(Panel(
        "\n".join([
            "  [bold bright_green]SESSION COMPLETE[/bold bright_green]\n",
            f"  [dim]Packs attempted:[/dim]  [white]{pack_num}[/white]",
            f"  [dim]Packs pushed   :[/dim]  [white]{total_pushed}[/white]",
            f"  [dim]Repository total:[/dim] [bold cyan]{repo_total:,}[/bold cyan] driver(s)",
            f"  [dim]Errors         :[/dim]  [white]{total_errors}[/white]",
            f"  [dim]Elapsed        :[/dim]  [white]{_fmt_duration(elapsed)}[/white]",
            "",
                f"  [dim]Drivers  :[/dim]  [cyan]https://github.com/{DRIVERS_REPO_OWNER}/{DRIVERS_REPO_NAME}[/cyan]",
            ] + (
                [f"  [dim]Repo seq :[/dim]  [dim]{_drivers_repo_history_line()}[/dim]"]
                if _SPENT_DRIVERS_REPOS else []
            ) + [
                f"  [dim]Manifests:[/dim]  [cyan]https://github.com/{REPO_OWNER}/{REPO_NAME}/tree/{GH_BRANCH}/{MANIFEST_DIR}[/cyan]",
                f"  [dim]Log      :[/dim]  [dim]{_log_file_path()}[/dim]",
            ]),
            border_style="bright_green",
        title=f"[bold bright_green]  DriverDex Builder v{APP_VER}  [/bold bright_green]",
        padding=(1, 2),
    ))
    C.print()

    rule("WORKSPACE CLEANUP", style="yellow")
    C.print()
    try:
        _ws_items = list(repo_dir.rglob("*")) if repo_dir.exists() else []
        _ws_sz    = sum(f.stat().st_size for f in _ws_items if f.is_file())
        _ws_hint  = f"  ({len(_ws_items)} item(s), {fmt_size(_ws_sz)})"
    except Exception:
        _ws_hint = ""
    C.print(
        "  [dim]This only removes the local workspace mirror — every pack pushed\n"
        "  this session is already safely on GitHub. The next run recreates the\n"
        "  folder and re-syncs everything fresh automatically. Your saved GitHub\n"
        "  token is stored separately, next to the script, and is never\n"
        "  affected by this.[/dim]"
    )
    C.print()
    # Auto-cleanup, no prompt: if at least one pack was pushed successfully
    # this session, the workspace mirror is disposable (everything of value
    # is already on GitHub) so it's deleted automatically. If nothing was
    # pushed, it's left alone — also without asking — since re-syncing a
    # workspace that was never actually used yet would be pure churn.
    if total_pushed > 0:
        ok(f"{total_pushed} pack(s) pushed successfully — auto-deleting local workspace …")
        _delete_workspace_folder(repo_dir)
    else:
        info(f"No packs pushed this session — workspace kept.{_ws_hint}")
        hint(str(repo_dir))
    C.print()


if __name__ == "__main__":
    main()
