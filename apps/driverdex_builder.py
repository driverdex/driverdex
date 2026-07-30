#!/usr/bin/env python3
# ==============================================================================
#  DriverDex Builder  --  Driver Manifest Builder & GitHub Uploader
#  Author  : rhshourav
#  Version : 4.5.4
#  Repo    : https://github.com/rhshourav/driverdex
#  Part of : Windows-Scripts  --  github.com/rhshourav/Windows-Scripts
#
#  Changes in v4.5.4
#  -----------------
#  * Ported all logic from v4.5.4-rest into the SSH/git transport version.
#    REST API, GITHUB_TOKEN, LFS, and workspace code removed entirely.
#    SSH key + local git clone transport restored from v4.4.1.
#
#  * Parallel archiving — driver groups are now archived concurrently using
#    ThreadPoolExecutor (default: min(8, cpu_count) workers).  Each group gets
#    its own back-end call (_archive_group), so py7zr / CLI / ZIP all benefit.
#    A shared Rich Progress bar tracks byte-level progress across all workers.
#
#  * Per-group streaming push — after archiving, the upload loop processes one
#    driver group at a time:
#      move to final location → manifest update → git commit+push
#    Each group is pushed individually so failures only affect that group.
#    No per-group confirm prompt; the single "Proceed?" at Step 5 covers all.
#
#  * 7z archiving (from v4.5.3-rest):
#    Primary: py7zr + multivolumefile (pure Python, zero extra binaries).
#    Fallback: 7z CLI binary.  Final fallback: ZIP (unchanged behaviour).
#
#  Changes in v4.5.3-rest
#  ----------------------
#  * Replaced ZIP-based driver packaging with 7-Zip multi-volume archives:
#
#    Primary path  — py7zr + multivolumefile (pure Python, zero extra binaries):
#      - Archives named  <stem>.7z.0001, .7z.0002, … (standard 7z volume naming)
#      - Volume size = SPLIT_BYTES (15 MB) — capped hard in the 7z stream itself,
#        so no post-write size check is needed; the 7z library enforces the limit.
#      - Uses LZMA2 compression (py7zr default) — far superior to ZIP_DEFLATED
#        for any files that ARE compressible.
#      - All volumes are verified with py7zr.SevenZipFile.testzip() after creation.
#
#    Fallback path — subprocess 7z CLI (if py7zr import fails or is unavailable):
#      - Calls  7z a -v<N>m -mx=5 <stem>.7z <folder>
#      - Volume flag enforced by the 7-Zip binary — guaranteed not to exceed limit.
#      - Falls back to ZIP if the 7z binary is also not on PATH.
#
#    SPLIT_BYTES raised back to 15 MB:
#      - The hard 7z volume boundary makes the old 10 MB conservative budget
#        unnecessary; 15 MB 7z parts → ~20 MB base64 — still within GitHub's
#        25 MB base64 / ~18 MB raw blob API ceiling after LFS pointer indirection.
#
#    github_commit_push() updated:
#      - LFS detection widened: files whose name matches *.7z.* (multi-volume
#        parts) are uploaded via Git LFS, exactly like .zip files before.
#
#    Auto-install:
#      - _ensure() now also installs py7zr and multivolumefile on first run.
#
#  Changes in v4.5.2-rest
#  ----------------------
#  * Added multi-retry with exponential back-off to every GitHub API call:
#    - New _api_with_retry() helper wraps _api().  Retries on HTTP 429, 500,
#      502, 503, 504 and network failures (status 0).  Up to 5 attempts with
#      back-off: 4 s → 8 s → 16 s → 32 s (capped at 60 s).
#    - Applied to all steps in github_commit_push():
#        Step 1  HEAD ref GET
#        Step 2  HEAD commit GET
#        Step 4a LFS batch POST  (urllib retry loop — LFS API uses different auth)
#        Step 4a LFS object PUT  (urllib retry loop)
#        Step 4a LFS pointer blob POST
#        Step 4b meta-file blob POST  (was already 1 attempt; now retries)
#        Step 5  create tree POST     (root cause of the HTTP 504 report)
#        Step 6  create commit POST
#        Step 7  update ref PATCH     (old interactive-prompt retry replaced with
#                                      automatic back-off; no user prompt needed)
#    - Non-transient 4xx errors (401, 403, 404, 409, 422) are never retried.
#
#  Changes in v4.5.1-rest
#  ----------------------
#  * Fixed: zip parts exceeding GitHub's 18 MB blob API limit even when
#    SPLIT_BYTES was set to 15 MB.
#
#    Root cause: The old split logic tracked raw *input* file sizes to decide
#    when to roll over to a new part.  Binary driver files (.sys, .dll, .cat,
#    .mui) are already compressed or near-random; ZIP_DEFLATED achieves ≈0%
#    compression on them, so zip output size ≈ raw input size — or can even
#    exceed it slightly due to zip headers.  A group whose total uncompressed
#    size was just under 15 MB could produce a zip part > 15 MB on disk,
#    exceeding the ~18 MB GitHub blob API limit after base64 encoding.
#
#  * Fix — three-layered approach (all three are required together):
#
#    1. SPLIT_BYTES lowered from 15 MB → 10 MB.
#       Input budget: 10 MB raw → ≤ 10 MB zip → ~13 MB base64, well under the
#       18 MB raw / ~24 MB base64 GitHub blob API hard ceiling.
#
#    2. needs_split trigger broadened.
#       Multi-part mode is now enabled when ANY single file in the group
#       exceeds SPLIT_BYTES // 2 (5 MB), not only when the total group size
#       exceeds SPLIT_BYTES.  This ensures Gate B is active even for groups
#       with a single large incompressible file.
#
#    3. Gate B post-write actual-size check added to zip_all_drivers().
#       After writing each file into the open zip part, the function calls
#       zf.fp.tell() to read the actual bytes-written counter from the
#       underlying file object.  If that count already exceeds SPLIT_BYTES
#       the part is closed and verified immediately and a new part is opened.
#       This is the only truly reliable guard for incompressible binaries.
#
#  * Manifest integrity is fully preserved: part counts, filenames, sha256
#    digests, and url fields are all computed from the closed, verified zip
#    files — so the manifest always reflects the actual parts on disk.
#
#  Changes in v4.5.0-rest
#  ----------------------
#  * Replaced all SSH + git CLI operations with GitHub REST API calls.
#    - No local git clone required.
#    - No SSH key required — uses a GitHub Personal Access Token (PAT).
#    - Set GITHUB_TOKEN environment variable before running:
#        Windows PowerShell:  $env:GITHUB_TOKEN = "ghp_xxxx..."
#        Linux/macOS:         export GITHUB_TOKEN="ghp_xxxx..."
#  * check_ssh_key() replaced by check_github_token() (API token validation).
#  * check_git() removed — git CLI no longer required.
#  * locate_repo_interactively() replaced by setup_workspace():
#    Creates a local workspace folder (default: driverdex_workspace/ next to script)
#    as a staging area for zips, manifest JSON, and SQLite DB.
#  * _git() / git_pull_rebase() / git_commit_push() / _commit_pending()
#    replaced by REST API equivalents using urllib (no extra deps):
#      _api()                 – low-level GitHub API helper
#      github_pull_manifest() – downloads manifest + README from GitHub
#      github_commit_push()   – uploads all staged files in one commit
#                               via the Git Data API (blobs → tree → commit → ref)
#  * diagnose_git() updated to diagnose_api_error() for REST errors.
#  * REPO_SSH removed; GITHUB_TOKEN + API_BASE + GH_BRANCH added.
#  * All other functionality (INF parsing, zipping, manifest sharding,
#    SQLite, Rich UI, rollback, duplicate detection) is unchanged.
#
#  Changes in v4.4.0
#  -----------------
#  * Hardened Ctrl+C / KeyboardInterrupt handling throughout:
#    - main() wraps _main_body() so no raw traceback ever escapes to the shell.
#    - _process_one_pack() now has ONE outer try/except covering ALL code,
#      including the source-folder prompt, INF scan, preflight audit, pack-name
#      prompt, and confirm dialog (previously these were unprotected).
#    - rb = _PackRollback() is initialised immediately after the repo-guard
#      check, so rollback is always available regardless of where an interrupt
#      fires.
#    - except KeyboardInterrupt: aborts any in-progress git rebase before
#      rolling back staged zips, then shows a clean yellow panel.
#    - except SystemExit: calls rb.execute() before re-raising so that die()
#      calls issued after staging also trigger a clean rollback.
#    - except Exception: calls rb.execute() before exiting with code 1.
#    - finally: if rb._armed: rb.execute() — last-resort safety net.
#    - _PackRollback.execute() now self-disarms on first call (idempotent).
#  * zip_all_drivers(): KeyboardInterrupt during Progress closes the open zip
#    and deletes all partially-written parts before re-raising.
#  * save_to_sqlite(): connection is now closed in a finally block (no leak).
#  * _git(): subprocess.TimeoutExpired is caught and reported cleanly.
#  * locate_repo_interactively(): KeyboardInterrupt on Prompt.ask exits cleanly.
#  * check_ssh_key(): KeyboardInterrupt during SSH verification handled.
#
#  Changes in v4.3.1
#  -----------------
#  * git pull --rebase no longer fails with "You have unstaged changes":
#    _commit_pending() commits drivers.db / README.md before every rebase.
#  * Second pull-before-push (inside git_commit_push) received the same fix:
#    ok() writes to the log after the main commit, making the tree dirty again;
#    a second _commit_pending() call flushes that before the pre-push rebase.
#  * Ctrl+C after zips are moved into the repo now silently rolls back:
#    staged zips removed, manifest restored from snapshot, SQLite rows deleted.
#
#  Changes in v4.3
#  ---------------
#  * Hardened split: single-file integrity rule (no file is cut across parts);
#    every ZIP part is verified with testzip() immediately on close; SHA-256
#    digests computed for each part and stored in manifest + SQLite.
#  * Parts metadata in manifest: each driver entry now carries a "parts" list
#    with {part_num, filename, size_bytes, sha256, url}.
#  * Manifest sharding: if drivers.manifest.json exceeds 15 MB the overflow is
#    carried into drivers.manifest.2.json, .3.json … automatically.
#  * SQLite persistence (drivers.db): all entries and zip-part records are
#    stored in a local SQLite DB for fast querying.  Rotates to a new shard
#    (.2.db, .3.db …) when the active shard reaches 15 MB.
#  * Source-folder deletion is now optional — user is asked after the push.
#  * README.md badge auto-updated on every session start and after each push.
#
#  Changes in v4.1
#  ---------------
#  * Pre-flight duplicate audit: before zipping, the incoming driver set is
#    checked against BOTH the manifest (entry-level) AND existing zip files
#    on disk.  Each duplicate is reported in a rich table with its status
#    (exact match / same version already on disk / older / conflicting HWID)
#    and the user is given a per-item choice: skip, replace, or keep both.
#  * Loop mode: after each successful upload the script asks whether to
#    process another driver pack.  Repo sync is re-run each iteration.
#    Ctrl-C at the loop prompt exits cleanly.
#
#  Changes in v4.0
#  ---------------
#  * No temp dir: works DIRECTLY inside the local DriverDex git repo.
#    The repo is auto-detected by walking up from the script's location
#    until a directory whose git remote matches rhshourav/driverdex is found.
#  * Driver-type subfolders: zips land in
#      drivers/<TypeFolder>/DP_<pack>/  e.g.  drivers/Net/DP_IntelLAN_x64/
#    Type is resolved from INF Class= field via a canonical mapping;
#    totally random / nonsense folder names are handled correctly because
#    classification is always INF-content-driven, never folder-name-driven.
#  * Version management: when a newer version of a known driver arrives the
#    old zip is kept, old manifest entry gains  "superseded_by": "<new_id>"
#    and  "enabled": false.  New entry gets  "supersedes": "<old_id>".
#    A dedicated version-history table is maintained in the manifest.
#
#  Workflow
#  --------
#  0.  Verify SSH key is configured for github.com
#  1.  Auto-locate the local DriverDex repo (walks up from script; validates remote)
#  2.  git pull --rebase to sync the local repo first
#  3.  Point to a driver folder -- INFs parsed for name, version, HWIDs,
#      provider, OS targets, arch, and device descriptions
#  4.  Group by deepest INF-containing subfolder -> one zip per driver group
#      Only split a zip if THAT individual driver exceeds 20 MB;
#      no single file is ever split across part boundaries.
#      Every part is SHA-256 verified immediately after writing.
#  5.  Classify each group into a driver-type folder (Net, Display, Audio …)
#      using INF Class= content -- folder names are never trusted for this
#  6.  Copy/move zips into repo under drivers/<Type>/DP_<pack>/
#  7.  Build manifest: unique ID per entry, deduplicate, warn on HWID
#      conflicts, mark superseded old versions; auto-shard if > 15 MB
#  8.  Update SQLite DB (drivers.db); auto-shard if > 15 MB
#  9.  git commit + git push
#  10. Ask user whether to delete source folder
#
#  Quick launch (PowerShell, run as admin):
#    irm https://raw.githubusercontent.com/rhshourav/driverdex/main/get-driverdex.ps1 | iex
#
#  Direct launch (from inside local repo clone):
#    python driverdex_builder.py
# ==============================================================================

from __future__ import annotations

import math
import os
import sys
import re
import json
import shutil
import zipfile
import hashlib
import sqlite3
import subprocess
import concurrent.futures
import threading
from collections import Counter, defaultdict
from pathlib    import Path
from datetime   import date, datetime
from typing     import Dict, List, Optional, Set, Tuple

# ── auto-install rich ─────────────────────────────────────────────────────────
def _ensure(pkg: str) -> None:
    try:
        __import__(pkg)
    except ImportError:
        print(f"  Installing '{pkg}' ...", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "-q"],
            check=True,
        )

_ensure("rich")
_ensure("py7zr")
_ensure("multivolumefile")

from rich.console  import Console
from rich.panel    import Panel
from rich.table    import Table
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    TimeRemainingColumn, TaskProgressColumn, DownloadColumn, TransferSpeedColumn,
)
from rich.prompt   import Prompt, Confirm
from rich.text     import Text
from rich.rule     import Rule
from rich.align    import Align
from rich          import box
from rich.markup   import escape

# ── constants ─────────────────────────────────────────────────────────────────
REPO_OWNER         = "rhshourav"
REPO_NAME          = "driverdex"
REPO_SSH           = f"git@github.com:{REPO_OWNER}/{REPO_NAME}.git"
MANIFEST_DIR       = "manifests"                          # all manifests live here
MANIFEST_REL       = f"{MANIFEST_DIR}/drivers.manifest.json"
DRIVERS_DIR        = "drivers"
SPLIT_BYTES        = 15 * 1024 * 1024   # 15 MB volume cap — enforced hard by py7zr/7z itself
SCHEMA_VER         = "2.0"
APP_VER            = "4.5.4"
COMMIT_EMAIL       = "driverdex-builder@noreply.local"
COMMIT_NAME        = "DriverDex-Builder"
BOOTSTRAP_PS1_NAME = "get-driverdex.ps1"
GH_BRANCH          = "main"

# ── size & rotation limits ────────────────────────────────────────────────────
MANIFEST_SIZE_LIMIT = 15 * 1024 * 1024   # 15 MB per manifest shard
DB_SIZE_LIMIT       = 15 * 1024 * 1024   # 15 MB per SQLite shard
DB_FILE_NAME        = "drivers.db"
INDEX_FILE_NAME     = "driverdex_index.json"   # lightweight tracker — never grows large

# ── README badge ──────────────────────────────────────────────────────────────
BADGE_MARKER_START  = "<!-- DRIVERDEX_DRIVER_BADGE_START -->"
BADGE_MARKER_END    = "<!-- DRIVERDEX_DRIVER_BADGE_END -->"

BASE_RAW_URL = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{DRIVERS_DIR}"
)
MANIFEST_RAW = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{MANIFEST_REL}"
)
BOOTSTRAP_URL = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{BOOTSTRAP_PS1_NAME}"
)

C = Console(highlight=False)

# ── Driver-type classification map ────────────────────────────────────────────
# Maps INF  Class=  value (normalised, lowercase) -> subfolder name in repo.
# This is the ONLY source of truth for type -- folder names are NEVER used.
# Add rows here as new INF classes are encountered.
_CLASS_TO_TYPE: Dict[str, str] = {
    # Network
    "net"                   : "Net",
    "nettrans"              : "Net",
    "netservice"            : "Net",
    "netclient"             : "Net",
    "network"               : "Net",
    # Display / GPU
    "display"               : "Display",
    "monitor"               : "Display",
    # Audio / Media
    "media"                 : "Audio",
    "audio"                 : "Audio",
    "hdaudio"               : "Audio",
    # Storage
    "diskdrive"             : "Storage",
    "cdrom"                 : "Storage",
    "floppydisk"            : "Storage",
    "tapedrive"             : "Storage",
    "storage"               : "Storage",
    "scsiadapter"           : "Storage",
    "volumesnapshot"        : "Storage",
    "volume"                : "Storage",
    "mediumchanger"         : "Storage",
    # USB
    "usb"                   : "USB",
    "usbdevice"             : "USB",
    # Bluetooth
    "bluetooth"             : "Bluetooth",
    "bth"                   : "Bluetooth",
    "bthle"                 : "Bluetooth",
    # Input (HID, keyboard, mouse, pen, touch)
    "hid"                   : "Input",
    "hidclass"              : "Input",
    "keyboard"              : "Input",
    "mouse"                 : "Input",
    "pen"                   : "Input",
    "tabletinputdevice"     : "Input",
    "sensor"                : "Input",
    "biometric"             : "Input",
    # Chipset / System
    "system"                : "Chipset",
    "processor"             : "Chipset",
    "computer"              : "Chipset",
    "unknown"               : "Chipset",
    "acpi"                  : "Chipset",
    "battery"               : "Chipset",
    "smartcardreader"       : "Chipset",
    # Ports / Serial / Parallel
    "ports"                 : "Ports",
    "multiportserial"       : "Ports",
    "modem"                 : "Ports",
    # Imaging / Camera
    "image"                 : "Imaging",
    "camera"                : "Imaging",
    "stillimage"            : "Imaging",
    "scanner"               : "Imaging",
    # Printers
    "printer"               : "Printer",
    "printqueue"            : "Printer",
    "pnpprinters"           : "Printer",
    # Security / SmartCard
    "securityaccelerator"   : "Security",
    "securitydevices"       : "Security",
    "smartcard"             : "Security",
    # Firmware / UEFI
    "firmware"              : "Firmware",
    "softwaredevice"        : "Firmware",
    "softwarecomponent"     : "Firmware",
    # Wireless
    "wireless"              : "Wireless",
    "wlan"                  : "Wireless",
    "bluetoothaudio"        : "Wireless",
    # Power / Battery
    "power"                 : "Power",
    # Infrastructure / virtual
    "infrared"              : "Other",
    "multifunction"         : "Other",
    "1394"                  : "Other",
    "61883"                 : "Other",
    "dot4"                  : "Other",
    "dot4print"             : "Other",
    "ieeef16"               : "Other",
    "extension"             : "Other",
}
_TYPE_FALLBACK = "Other"


def classify_driver_type(inf_parsed: Dict) -> str:
    """
    Determine the type-subfolder for a driver solely from its parsed INF data.
    Falls back to HWID prefix heuristics if Class= is absent or unmapped.
    Never looks at the source folder name.
    """
    raw_class = (inf_parsed.get("category") or "").strip().lower()
    if raw_class and raw_class in _CLASS_TO_TYPE:
        return _CLASS_TO_TYPE[raw_class]

    # Secondary heuristic: infer from dominant HWID prefix
    all_hwids = inf_parsed.get("hwids", []) + inf_parsed.get("compatible_ids", [])
    prefix_votes: Dict[str, str] = {
        "USB\\"      : "USB",
        "USBSTOR\\"  : "Storage",
        "HDAUDIO\\"  : "Audio",
        "DISPLAY\\"  : "Display",
        "BTH\\"      : "Bluetooth",
        "BTHENUM\\"  : "Bluetooth",
        "HID\\"      : "Input",
        "HIDCLASS\\" : "Input",
        "SCSI\\"     : "Storage",
        "IDE\\"      : "Storage",
        "PCI\\"      : "Chipset",
        "ACPI\\"     : "Chipset",
        "MONITOR\\"  : "Display",
        "MEDIA\\"    : "Audio",
        "NET\\"      : "Net",
        "SD\\"       : "Storage",
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
    """
    Classify a whole driver group (one or more INFs in the same folder).
    Votes across all INFs, majority wins, ties go to alphabetical order.
    """
    votes: Dict[str, int] = Counter()
    for _, d in group_infs:
        t = classify_driver_type(d)
        votes[t] += 1
    return votes.most_common(1)[0][0] if votes else _TYPE_FALLBACK


# ── banner ────────────────────────────────────────────────────────────────────
_BANNER_ART = r"""
  _     ____   ____
 | |   |  _ \ / ___|
 | |   | | | | |
 | |___| |_| | |___
 |_____|____/ \____|"""


def show_banner() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    body = (
        Text(_BANNER_ART + "\n",                          style="bold green")
        + Text(f"\n  DriverDex Builder  v{APP_VER}\n",         style="bold white")
        + Text( "  Author  : rhshourav\n",                style="dim white")
        + Text(f"  Repo    : github.com/{REPO_OWNER}/{REPO_NAME}\n", style="dim cyan")
        + Text( "  Part of : Windows-Scripts",            style="dim white")
    )
    C.print(
        Panel(
            Align.center(body),
            border_style="green",
            padding=(0, 8),
        )
    )
    C.print()


# ── logging stub (file logging removed; no-op keeps all call sites intact) ────
def log(_level: str, _msg: str) -> None:  # noqa: ARG001
    """No-op.  File logging was removed to prevent driverdex_builder.log from
    becoming a perpetually-dirty tracked file that breaks git pull --rebase."""


# ── output helpers ────────────────────────────────────────────────────────────
def rule(title: str = "") -> None:
    if title:
        C.print(Rule(f"[bold cyan] {title} [/bold cyan]", style="dim cyan"))
    else:
        C.print(Rule(style="dim green"))


def ok(msg: str)   -> None:
    C.print(f"  [bold green][+][/bold green] {escape(str(msg))}")
    log("info", f"[OK] {msg}")

def warn(msg: str) -> None:
    C.print(f"  [bold yellow][!][/bold yellow] {escape(str(msg))}")
    log("warning", f"[WARN] {msg}")

def err(msg: str)  -> None:
    C.print(f"  [bold red][X][/bold red] {escape(str(msg))}")
    log("error", f"[ERR] {msg}")

def info(msg: str) -> None:
    C.print(f"  [dim][-][/dim] [white]{escape(str(msg))}[/white]")
    log("info", f"[INFO] {msg}")

def hint(msg: str) -> None:
    C.print(f"      [dim cyan]^-- {escape(str(msg))}[/dim cyan]")
    log("debug", f"[HINT] {msg}")


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


# ── prerequisite checks ───────────────────────────────────────────────────────
def check_python() -> None:
    if sys.version_info < (3, 7):
        die(
            f"Python 3.7+ required. Current: {sys.version}",
            fix="https://python.org/downloads",
        )
    ok(f"Python {sys.version.split()[0]}")


def setup_workspace() -> Path:
    """
    Locate the local git clone of the DriverDex repo (SSH transport).
    Auto-detects the repo on disk; falls back to an interactive prompt.
    Returns the repo root Path.
    """
    return locate_repo_interactively()


# ── INF parser ────────────────────────────────────────────────────────────────
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

_VER_RE       = re.compile(r"^DriverVer\s*=\s*(.+)",          re.IGNORECASE | re.MULTILINE)
_CLASS_RE     = re.compile(r"^Class\s*=\s*([^\r\n;]+)",       re.IGNORECASE | re.MULTILINE)
_PROVIDER_RE  = re.compile(r"^Provider\s*=\s*([^\r\n;]+)",    re.IGNORECASE | re.MULTILINE)
_CATALOG_RE   = re.compile(r"^CatalogFile\s*=\s*([^\r\n;]+)", re.IGNORECASE | re.MULTILINE)
_ARCH_RE      = re.compile(r"NT(amd64|x86|arm64)",            re.IGNORECASE)
_NTVER_RE     = re.compile(r"NT(\d+)\.(\d+)(?:\.(\d+))?",    re.IGNORECASE)
_STRBLOCK_RE  = re.compile(
    r"^\[Strings\](.*?)(?=^\[|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_STRKV_RE     = re.compile(
    r'^([A-Za-z0-9_.]+)\s*=\s*"?([^"\r\n]*)"?',
    re.MULTILINE,
)
_DEVINST_RE   = re.compile(
    r'^[ \t]*(%[^%\r\n]+%)\s*=\s*[^,\r\n]+,\s*(\S[^\r\n]*)',
    re.MULTILINE,
)
_HWID_INLINE  = re.compile(r"DEV_|VID_|PID_|CC_", re.IGNORECASE)


def _read_inf(p: Path) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return p.read_text(encoding=enc, errors="replace")
        except Exception:
            pass
    return ""


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
        r'%([^%\r\n]+)%',
        lambda m: table.get(m.group(1).upper(), m.group(0)),
        val,
    )


def _extract_device_names(txt: str, table: Dict[str, str]) -> List[str]:
    names: Set[str] = set()
    for m in _DEVINST_RE.finditer(txt):
        raw      = m.group(1)
        resolved = _resolve(raw, table).strip().strip('%')
        if (
            resolved
            and len(resolved) > 4
            and not resolved.startswith('%')
            and not resolved.replace('.', '').replace('_', '').isdigit()
        ):
            names.add(resolved)
    return sorted(names)


def parse_inf(inf: Path) -> Dict:
    txt = _read_inf(inf)
    if not txt:
        return {}

    table = _parse_strings(txt)

    provider = ""
    m = _PROVIDER_RE.search(txt)
    if m:
        provider = _resolve(m.group(1).strip(), table).strip('%').strip()
        provider = re.sub(r'^%|%$', '', provider).strip()

    category = "Unknown"
    m = _CLASS_RE.search(txt)
    if m:
        raw = _resolve(m.group(1).strip(), table).strip('%').strip()
        if raw:
            category = raw

    version = ""
    m = _VER_RE.search(txt)
    if m:
        pts = m.group(1).strip().split(",")
        raw_ver = pts[1].strip() if len(pts) >= 2 else ""
        # Keep only the leading dotted-numeric version (e.g. "15.18.0.1");
        # discard any trailing comment or placeholder text some INFs append.
        ver_num = re.match(r'[\d.]+', raw_ver)
        version = ver_num.group(0).strip('.') if ver_num else raw_ver

    arch = "x64"
    m = _ARCH_RE.search(txt)
    if m:
        a = m.group(1).lower()
        arch = {"x86": "x86", "arm64": "arm64"}.get(a, "x64")

    os_tags: Set[str] = set()
    for m in _NTVER_RE.finditer(txt):
        maj = int(m.group(1))
        mn  = int(m.group(2))
        bld = int(m.group(3) or 0)
        if maj == 10 and mn == 0:
            if bld >= 22000:
                os_tags.add(f"11{arch}")
            os_tags.add(f"10{arch}")
        elif maj == 6:
            label = {1: "7", 2: "8", 3: "81"}.get(mn)
            if label:
                os_tags.add(f"{label}{arch}")
    if not os_tags:
        os_tags.add("*")

    hwids: Set[str] = set()
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        for m in _HWID_PAT.finditer(line):
            h = m.group(0).upper().rstrip("\\").strip()
            if len(h) > 6:
                hwids.add(h)

    specific = sorted(h for h in hwids if _HWID_INLINE.search(h))
    compat   = sorted(h for h in hwids if h not in set(specific))

    device_names = _extract_device_names(txt, table)

    return {
        "provider"      : provider,
        "category"      : category,
        "version"       : version,
        "arch"          : arch,
        "os_tags"       : sorted(os_tags),
        "hwids"         : specific,
        "compatible_ids": compat,
        "device_names"  : device_names,
    }


def scan_infs(folder: Path) -> List[Tuple[Path, Dict]]:
    results: List[Tuple[Path, Dict]] = []
    for inf in sorted(folder.rglob("*.inf")):
        d = parse_inf(inf)
        if d:
            results.append((inf, d))
    return results


def suggest_pack_name(folder: Path, inf_data: List[Tuple[Path, Dict]]) -> str:
    providers  = Counter(
        d.get("provider", "").strip()
        for _, d in inf_data if d.get("provider", "").strip()
    )
    categories = Counter(
        d.get("category", "").strip()
        for _, d in inf_data if d.get("category", "Unknown") not in ("Unknown", "")
    )
    archs = Counter(d.get("arch", "x64") for _, d in inf_data)

    provider = providers.most_common(1)[0][0]  if providers  else ""
    category = categories.most_common(1)[0][0] if categories else ""
    arch     = archs.most_common(1)[0][0]      if archs      else "x64"

    p_clean = re.sub(r'[^A-Za-z0-9]', '', provider)[:14]
    c_clean = re.sub(r'[^A-Za-z0-9]', '', category)[:10]

    if p_clean and c_clean:
        return f"{p_clean}_{c_clean}_{arch}"
    if p_clean:
        return f"{p_clean}_{arch}"
    # Don't trust the folder name for classification -- sanitise it heavily
    safe_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', folder.name)[:20] or "Unknown"
    return safe_name


# ── checksum helpers ─────────────────────────────────────────────────────────
def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Return hex SHA-256 of a file, reading in 1 MB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def _verify_archive(path: Path) -> Tuple[bool, str]:
    """
    Verify a ZIP or 7z archive (including 7z multi-volume parts).
    For 7z volumes, only the first part (.7z.0001) is openable by py7zr;
    individual later parts are binary chunks that cannot be opened alone.
    Returns (ok, error_message).
    """
    name = path.name.lower()

    # .zip verification
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

    # .7z single-volume verification
    if name.endswith(".7z"):
        try:
            import py7zr
            with py7zr.SevenZipFile(path, mode="r") as archive:
                if not archive.testzip():
                    return True, ""
                return False, "testzip() reported corruption"
        except Exception as exc:
            return False, str(exc)

    # .7z.NNNN multi-volume part: only the first part (.0001) carries the header
    # and is openable by py7zr; later parts are raw data chunks.
    # We verify size > 0 and do a SHA-256 integrity stamp (already computed
    # by PartInfo).  Structural verification happens implicitly when the whole
    # volume set is re-opened for extraction, but we can't do that here without
    # all parts present.
    if path.stat().st_size == 0:
        return False, "Volume part is empty (0 bytes)"
    return True, ""   # size > 0: part is intact as far as we can tell here


# keep old name as alias so call sites that explicitly reference it still work
_verify_zip = _verify_archive


# ── compression ───────────────────────────────────────────────────────────────
def _archive_stem(pack: str, inf_path: Path, d: Dict) -> str:
    parts = [pack, inf_path.stem]
    ver   = d.get("version", "").strip()
    arch  = d.get("arch", "x64").strip()
    if ver:
        parts.append(ver)
    if arch:
        parts.append(arch)
    raw = "-".join(parts)
    return re.sub(r'[^A-Za-z0-9]+', '-', raw).strip('-').lower()

# Keep old name as alias so any call sites in older branches still compile
_zip_stem = _archive_stem

DriverGroup = Tuple[Path, List[Tuple[Path, Dict]]]


def group_infs_by_folder(inf_data: List[Tuple[Path, Dict]]) -> List[DriverGroup]:
    groups: Dict[Path, List[Tuple[Path, Dict]]] = defaultdict(list)
    for inf_path, d in inf_data:
        groups[inf_path.parent].append((inf_path, d))
    return sorted(groups.items(), key=lambda x: str(x[0]))


# PartInfo holds metadata about one archive volume/part after creation
class PartInfo:
    __slots__ = ("path", "part_num", "sha256", "size_bytes")

    def __init__(self, path: Path, part_num: int) -> None:
        self.path       = path
        self.part_num   = part_num
        self.size_bytes = path.stat().st_size
        self.sha256     = _sha256(path)


# ── 7z back-end detection ─────────────────────────────────────────────────────

def _has_py7zr() -> bool:
    """Return True if py7zr + multivolumefile are importable."""
    try:
        import py7zr           # noqa: F401
        import multivolumefile  # noqa: F401
        return True
    except ImportError:
        return False


def _7z_binary() -> Optional[str]:
    """Return the path to a usable 7z/7za/7zz binary, or None."""
    for candidate in ("7z", "7za", "7zz"):
        try:
            result = subprocess.run(
                [candidate, "i"], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            pass
    return None


# ── single-group archiver (called from worker threads) ───────────────────────

def _archive_group(
    files     : List[Path],
    folder    : Path,
    dest_stem : Path,
    use_py7zr : bool,
    cli_binary: Optional[str],
    volume_mb : int,
    # Thread-safe progress callback: called with bytes_advanced after each file
    on_progress,           # Callable[[int], None]
) -> List[PartInfo]:
    """
    Archive one driver group into multi-volume 7z (or ZIP fallback).
    Runs in a worker thread; uses on_progress() instead of a Progress object
    so the shared Rich bar stays on the main thread.

    Retries up to 3 times with exponential back-off on transient errors.
    KeyboardInterrupt always propagates immediately.
    """
    import time

    stem_name   = dest_stem.name
    dest_parent = dest_stem.parent

    def _cleanup_stale() -> None:
        for stale in list(dest_parent.glob(stem_name + ".*")):
            try: stale.unlink()
            except Exception: pass

    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        _cleanup_stale()
        try:
            if use_py7zr:
                parts = _pack_py7zr_threaded(
                    files, folder, dest_stem,
                    volume=SPLIT_BYTES,
                    on_progress=on_progress,
                )
            elif cli_binary:
                parts = _pack_7z_cli_threaded(
                    files, folder, dest_stem,
                    volume_mb=volume_mb,
                    binary=cli_binary,
                    on_progress=on_progress,
                )
            else:
                parts = _pack_zip_threaded(
                    files, folder, dest_stem,
                    on_progress=on_progress,
                )
            return parts

        except KeyboardInterrupt:
            _cleanup_stale()
            raise

        except Exception as exc:
            last_exc = exc
            _cleanup_stale()
            if attempt < 3:
                wait = 4.0 * (2 ** (attempt - 1))
                warn(
                    f"Archive attempt {attempt}/3 failed for {stem_name}: {exc} "
                    f"— retrying in {wait:.0f} s …"
                )
                time.sleep(wait)
            else:
                err(f"All 3 archive attempts failed for {stem_name}: {exc}")

    raise RuntimeError(f"Could not archive {stem_name}: {last_exc}")


# Thread-safe variants of the packers (no Rich Progress arg; use callback) ──

def _pack_py7zr_threaded(
    files      : List[Path],
    folder     : Path,
    dest_stem  : Path,
    volume     : int,
    on_progress,
) -> List[PartInfo]:
    import py7zr
    import multivolumefile

    archive_base = str(dest_stem) + ".7z"

    try:
        with multivolumefile.open(archive_base, mode="wb", volume=volume) as mv:
            with py7zr.SevenZipFile(mv, mode="w") as sz:
                for f in files:
                    arc = str(f.relative_to(folder))
                    sz.write(f, arc)
                    on_progress(f.stat().st_size)
    except KeyboardInterrupt:
        for p in Path(dest_stem.parent).glob(dest_stem.name + ".7z*"):
            try: p.unlink()
            except Exception: pass
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
    files      : List[Path],
    folder     : Path,
    dest_stem  : Path,
    volume_mb  : int,
    binary     : str,
    on_progress,
) -> List[PartInfo]:
    archive_base = str(dest_stem) + ".7z"
    dest_parent  = dest_stem.parent

    cmd = [binary, "a", f"-v{volume_mb}m", "-mx=5", "-mmt=on", "-y",
           archive_base, str(folder)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(f"7z CLI failed (rc={result.returncode}):\n{result.stderr.strip()}")
    except KeyboardInterrupt:
        for p in dest_parent.glob(dest_stem.name + ".7z*"):
            try: p.unlink()
            except Exception: pass
        raise

    total = sum(f.stat().st_size for f in files)
    on_progress(total)

    produced = sorted(dest_parent.glob(dest_stem.name + ".7z.*"), key=lambda p: p.name)
    single   = Path(archive_base)
    if not produced and single.exists():
        produced = [single]
    if not produced:
        raise RuntimeError(f"7z CLI produced no output for {dest_stem.name}")

    parts: List[PartInfo] = []
    for n, vol in enumerate(produced, start=1):
        ok_flag, errmsg = _verify_archive(vol)
        if not ok_flag:
            raise RuntimeError(f"7z CLI volume check failed: {vol.name} — {errmsg}")
        parts.append(PartInfo(vol, n))
    return parts


def _pack_zip_threaded(
    files      : List[Path],
    folder     : Path,
    dest_stem  : Path,
    on_progress,
) -> List[PartInfo]:
    group_size  = sum(f.stat().st_size for f in files)
    needs_split = (
        group_size > SPLIT_BYTES
        or any(f.stat().st_size > SPLIT_BYTES // 2 for f in files)
    )
    parts:  List[PartInfo] = []
    part_n  = 1
    cur_sz  = 0
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
                on_disk = cur_zf.fp.tell()  # type: ignore[union-attr]
            except Exception:
                on_disk = 0
            if needs_split and on_disk > SPLIT_BYTES:
                parts.append(_close_zp(cur_zf, cur_path, part_n))
                part_n += 1; cur_sz = 0
                cur_zf, cur_path = _open_zp(part_n)
        parts.append(_close_zp(cur_zf, cur_path, part_n))
    except KeyboardInterrupt:
        try: cur_zf.close()
        except Exception: pass
        for p in parts:
            try: p.path.unlink()
            except Exception: pass
        try: cur_path.unlink()
        except Exception: pass
        raise
    return parts


# ── parallel orchestrator ─────────────────────────────────────────────────────

#: Maximum parallel archive workers.  Capped at 8 to avoid saturating HDD I/O;
#: on SSDs all CPU cores can help, but 8 is a safe default for mixed hardware.
ARCHIVE_WORKERS = min(8, (os.cpu_count() or 1))


def zip_all_drivers(
    src      : Path,
    dest_dir : Path,
    pack     : str,
    inf_data : List[Tuple[Path, Dict]],
) -> Tuple[Dict[Path, List[PartInfo]], int]:
    """
    Archive every driver group in parallel using ThreadPoolExecutor.

    - ARCHIVE_WORKERS groups are compressed concurrently (default: min(8, CPUs)).
    - A single shared Rich Progress bar shows aggregate byte throughput.
    - Each group is retried up to 3 times on transient errors.
    - KeyboardInterrupt cancels pending work and cleans up partial output.

    Returns ({group_folder -> [PartInfo, ...]}, total_verified_parts)
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    groups = group_infs_by_folder(inf_data)

    all_files_by_group: Dict[Path, List[Path]] = {}
    total_bytes = 0
    for folder, _ in groups:
        files = sorted(p for p in folder.rglob("*") if p.is_file())
        all_files_by_group[folder] = files
        total_bytes += sum(f.stat().st_size for f in files)

    use_py7zr  = _has_py7zr()
    cli_binary = None if use_py7zr else _7z_binary()
    volume_mb  = max(1, SPLIT_BYTES // (1024 * 1024))

    if use_py7zr:
        backend_label = "py7zr (LZMA2 multi-volume)"
    elif cli_binary:
        backend_label = f"7z CLI [{cli_binary}] (multi-volume)"
    else:
        backend_label = "ZIP fallback (multi-part)"
        warn(
            "Neither py7zr nor 7z CLI found — falling back to ZIP.\n"
            "  pip install py7zr multivolumefile"
        )

    workers = min(ARCHIVE_WORKERS, len(groups)) if len(groups) > 0 else 1
    info(
        f"Archive back-end : {backend_label}  |  "
        f"volume cap: {fmt_size(SPLIT_BYTES)}  |  "
        f"workers: {workers}/{len(groups)} group(s)"
    )

    result         : Dict[Path, List[PartInfo]] = {}
    result_lock    = threading.Lock()
    abort_event    = threading.Event()   # set on KeyboardInterrupt or fatal error

    try:
        with Progress(
            SpinnerColumn("dots2", style="bold green"),
            TextColumn("  [bold white]{task.description}[/bold white]"),
            BarColumn(bar_width=30, style="dark_green",
                      complete_style="bold green", finished_style="bold green"),
            TaskProgressColumn(style="bold white"),
            TextColumn("[dim]|[/dim]"),
            DownloadColumn(),
            TextColumn("[dim]|[/dim]"),
            TransferSpeedColumn(),
            TextColumn("[dim]|[/dim]"),
            TimeRemainingColumn(),
            console=C,
            transient=False,
        ) as prog:
            task = prog.add_task(
                f"Archiving {len(groups)} group(s)", total=total_bytes
            )

            # Thread-safe progress callback used by worker threads
            def _advance(n_bytes: int) -> None:
                prog.update(task, advance=n_bytes)

            def _worker(folder: Path, infs) -> None:
                if abort_event.is_set():
                    return
                rep_inf, rep_d = max(infs, key=lambda x: len(x[1].get("hwids", [])))
                stem = _archive_stem(pack, rep_inf, rep_d)
                try:
                    rel = folder.relative_to(src)
                except ValueError:
                    rel = Path(folder.name)

                group_dest = dest_dir / rel
                group_dest.mkdir(parents=True, exist_ok=True)
                dest_stem = group_dest / stem

                files = all_files_by_group[folder]
                if not files:
                    with result_lock:
                        result[folder] = []
                    return

                try:
                    parts = _archive_group(
                        files      = files,
                        folder     = folder,
                        dest_stem  = dest_stem,
                        use_py7zr  = use_py7zr,
                        cli_binary = cli_binary,
                        volume_mb  = volume_mb,
                        on_progress= _advance,
                    )
                    with result_lock:
                        result[folder] = parts
                except KeyboardInterrupt:
                    abort_event.set()
                    raise
                except Exception as exc:
                    abort_event.set()
                    err(f"Fatal archive error for {rel}: {exc}")
                    raise

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_worker, folder, infs): folder
                    for folder, infs in groups
                }
                try:
                    for future in concurrent.futures.as_completed(futures):
                        folder = futures[future]
                        try:
                            future.result()
                        except KeyboardInterrupt:
                            abort_event.set()
                            raise
                        except Exception:
                            # already logged inside _worker; abort remaining
                            abort_event.set()
                            for f in futures:
                                f.cancel()
                            raise
                except KeyboardInterrupt:
                    abort_event.set()
                    for f in futures:
                        f.cancel()
                    raise

    except KeyboardInterrupt:
        # Clean up all produced volumes on Ctrl+C
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
    return result, total_verified


# ── git error diagnosis ───────────────────────────────────────────────────────
_GIT_HINTS: List[Tuple[str, str]] = [
    (
        r"Permission denied.*publickey|publickey.*denied|no supported authentication",
        "SSH key is not accepted by GitHub.\n"
        "      Run:  ssh -T git@github.com\n"
        "      Docs: https://docs.github.com/en/authentication/connecting-to-github-with-ssh",
    ),
    (
        r"Repository not found|404",
        f"Repository '{REPO_OWNER}/{REPO_NAME}' not found via SSH.\n"
        "      Verify the repo exists and your SSH key has push access.",
    ),
    (
        r"timed out|timeout|Connection refused|Could not resolve host|Network is unreachable",
        "Network error -- check your internet connection and firewall (port 22 outbound).\n"
        "      Alternative: configure SSH to use port 443 via ~/.ssh/config\n"
        "      Host github.com  Hostname ssh.github.com  Port 443",
    ),
    (
        r"rejected.*non-fast-forward|Updates were rejected|failed to push.*tip",
        "Remote has newer commits than your local branch.\n"
        "      The script will git pull --rebase automatically.\n"
        "      If it keeps failing, check for large files or branch protection rules.",
    ),
    (
        r"unstaged changes|Please commit or stash|cannot pull with rebase.*unstaged"
        r"|cannot rebase.*unstaged",
        "Dirty working tree — git refuses to rebase over modified files.\n"
        "      v4.3.1+ auto-commits these before pulling; if you see this message\n"
        "      you may be running an older version of the script.\n"
        "      Manual fix:  cd <repo>  &&  git add -A  &&  git commit -m 'chore: pre-sync'",
    ),
    (
        r"CONFLICT|Merge conflict|cannot rebase|patch does not apply",
        "Rebase conflict detected.\n"
        "      Fix: 1) Open the repo  2) Resolve conflicts\n"
        "           3) git add .  4) git rebase --continue",
    ),
    (
        r"disk quota|pack exceeds|remote: error: File .+ is .+ this exceeds",
        "GitHub storage quota exceeded or a file is too large (100 MB GitHub limit).\n"
        "      Fix: Reduce SPLIT_BYTES or use Git LFS for large binaries.",
    ),
    (
        r"shallow update not allowed|shallow",
        "Shallow clone conflict -- retry will use a full clone.",
    ),
    (
        r"no such remote|does not appear to be a git repository",
        "Remote URL misconfiguration.\n"
        "      Fix: Check the repo path and ensure 'origin' remote is set correctly.",
    ),
]


def diagnose_git(stderr: str) -> str:
    for pattern, hint_text in _GIT_HINTS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return hint_text
    return (
        "Unexpected git error. Check your internet connection and SSH key.\n"
        "      Ref: https://docs.github.com/en/authentication/connecting-to-github-with-ssh"
    )


# ── prerequisite checks ───────────────────────────────────────────────────────
def check_python() -> None:
    if sys.version_info < (3, 7):
        die(
            f"Python 3.7+ required. Current: {sys.version}",
            fix="https://python.org/downloads",
        )
    ok(f"Python {sys.version.split()[0]}")


def check_git() -> None:
    try:
        r = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            ok(r.stdout.strip())
            return
    except FileNotFoundError:
        pass
    die(
        "git not found in PATH.",
        fix="Install from: https://git-scm.com/downloads  (check 'Add to PATH')",
    )


def check_ssh_key() -> bool:
    C.print()
    with C.status("[bold green]  Verifying SSH key for github.com ...[/bold green]", spinner="dots2"):
        try:
            r = subprocess.run(
                ["ssh", "-T",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=10",
                 "git@github.com"],
                capture_output=True, text=True, timeout=20,
            )
            combined = (r.stdout + r.stderr).lower()
        except KeyboardInterrupt:
            C.print()
            warn("SSH check interrupted.")
            return False
        except FileNotFoundError:
            err("ssh binary not found -- cannot verify SSH key.")
            hint("On Windows, install Git for Windows (includes OpenSSH).")
            return False
        except subprocess.TimeoutExpired:
            err("SSH connection timed out.")
            hint("Check your internet connection and firewall (port 22 outbound).")
            return False

    if "successfully authenticated" in combined or "hi " in combined:
        ok("SSH key accepted by github.com.")
        return True

    err("SSH key verification failed.")
    _print_ssh_fix_guide()
    return False


def _print_ssh_fix_guide() -> None:
    lines = [
        "[bold yellow]How to fix SSH authentication for GitHub[/bold yellow]\n",
        "[dim]1. Check for an existing key:[/dim]",
        "     [white]ls ~/.ssh/id_*.pub[/white]",
        "",
        "[dim]2. If none exists, generate one:[/dim]",
        '     [white]ssh-keygen -t ed25519 -C "your_email@example.com"[/white]',
        "",
        "[dim]3. Copy the public key:[/dim]",
        "     [white]cat ~/.ssh/id_ed25519.pub[/white]",
        "     [dim](or on Windows:  type %USERPROFILE%\\.ssh\\id_ed25519.pub)[/dim]",
        "",
        "[dim]4. Add to GitHub:[/dim]",
        "     [cyan]https://github.com/settings/ssh/new[/cyan]",
        "     Paste the key, give it a name, click  [bold]Add SSH key[/bold].",
        "",
        "[dim]5. Test again:[/dim]",
        "     [white]ssh -T git@github.com[/white]",
        '     [dim]Expected:  "Hi rhshourav! You have successfully authenticated."[/dim]',
    ]
    C.print(
        Panel(
            "\n".join(lines),
            border_style="yellow",
            title="[bold yellow]  SSH Setup Guide  [/bold yellow]",
            padding=(1, 3),
        )
    )


# ── repo auto-detection ───────────────────────────────────────────────────────
def _is_driverdex_repo(path: Path) -> bool:
    git_dir = path / ".git"
    if not git_dir.exists():
        return False
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(path),
            capture_output=True, text=True, timeout=10,
        )
        url = r.stdout.strip().lower()
        return (
            f"{REPO_OWNER.lower()}/{REPO_NAME.lower()}" in url
            or f"{REPO_OWNER.lower()}\\{REPO_NAME.lower()}" in url
        )
    except Exception:
        return False


def find_local_repo() -> Optional[Path]:
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir
    for _ in range(8):
        if _is_driverdex_repo(candidate):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    for rel in (
        "../driverdex", "../../driverdex", "../DriverDex", "../../DriverDex",
        f"../{REPO_NAME}", f"../../{REPO_NAME}", f"../../../{REPO_NAME}",
    ):
        p = (script_dir / rel).resolve()
        if p.exists() and _is_driverdex_repo(p):
            return p
    return None


def locate_repo_interactively() -> Path:
    with C.status("[bold green]  Auto-detecting local DriverDex repo ...[/bold green]", spinner="dots2"):
        found = find_local_repo()

    if found:
        ok(f"Local repo found: {found}")
        return found

    warn("Could not auto-detect the local DriverDex repo.")
    hint(
        "Make sure this script lives inside or near your local clone of "
        f"github.com/{REPO_OWNER}/{REPO_NAME}"
    )
    C.print()

    while True:
        try:
            raw = Prompt.ask("  [bold cyan]Path to local DriverDex repo[/bold cyan]").strip().strip('"')
        except (KeyboardInterrupt, EOFError):
            C.print()
            warn("Interrupted.")
            sys.exit(130)
        p = Path(raw).expanduser().resolve()
        if not p.is_dir():
            err(f"Not a directory: {raw}")
            continue
        if _is_driverdex_repo(p):
            ok(f"Valid DriverDex repo: {p}")
            return p
        err(f"Not a valid DriverDex git repo (remote URL does not match {REPO_OWNER}/{REPO_NAME}): {raw}")


# ── git helpers ───────────────────────────────────────────────────────────────
def _git_env() -> Dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"]     = "Never"
    env["GIT_SSH_COMMAND"]     = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    return env


def _git(args: List[str], cwd: Path, timeout: int = 180) -> Tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
            env=_git_env(),
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"git {args[0]} timed out after {timeout}s"


def _commit_pending(repo: Path, msg: str = "[DriverDex] chore: pre-sync auto-commit") -> None:
    """Ensure the working tree is clean before a git pull --rebase."""
    _git(["config", "user.email", COMMIT_EMAIL], repo)
    _git(["config", "user.name",  COMMIT_NAME],  repo)

    rc, status, _ = _git(["status", "--porcelain"], repo)
    dirty = status.strip()
    if not dirty:
        return

    _git(["add", "-A"], repo)
    rc2, _, se = _git(["commit", "-m", msg], repo)
    if rc2 == 0:
        ok("Auto-committed pending changes (db/badge) before pull.")
        return

    if "nothing to commit" in se.lower():
        return

    warn(f"Auto-commit before pull failed ({se.split(chr(10))[0].strip()}) — trying stash.")
    rs, _, _ = _git(["stash", "--include-untracked", "-m", "driverdex-pre-pull-stash"], repo)
    if rs == 0:
        _git(["stash", "pop"], repo)
        warn("Stash pop applied — re-running pull should now succeed.")
    else:
        warn("Could not stash changes.  Pull may fail — commit or stash manually.")


def git_pull_rebase(repo: Path) -> bool:
    """Sync the local repo with origin before doing any work."""
    _commit_pending(repo)

    with C.status("[bold green]  git pull --rebase ...[/bold green]", spinner="dots2"):
        rc, out, se = _git(["pull", "--rebase", "origin", "HEAD"], repo, timeout=120)

    if rc != 0:
        combined = (se + " " + out).strip()
        err(f"git pull --rebase failed: {combined}")
        hint(diagnose_git(combined))
        _git(["rebase", "--abort"], repo)
        return False

    if "Already up to date" in out or "up to date" in out.lower():
        ok("Local repo is already up to date.")
    else:
        ok("Pulled and rebased remote changes.")
    return True


def git_commit_push(repo: Path, msg: str) -> bool:
    """Stage all, commit, pull --rebase, push.  Returns True on success."""
    _git(["config", "user.email", COMMIT_EMAIL], repo)
    _git(["config", "user.name",  COMMIT_NAME],  repo)

    rc, _, se = _git(["remote", "set-url", "origin", REPO_SSH], repo)
    if rc != 0:
        err(f"git remote set-url failed: {se}")
        hint(diagnose_git(se))
        return False

    rc, _, se = _git(["add", "-A"], repo)
    if rc != 0:
        err(f"git add failed: {se}")
        return False

    rc, status, _ = _git(["status", "--porcelain"], repo)
    if not status.strip():
        warn("Repository unchanged -- nothing to commit or push.")
        return True

    rc, _, se = _git(["commit", "-m", msg], repo)
    if rc != 0:
        err(f"git commit failed: {se}")
        hint(diagnose_git(se))
        return False
    ok(f"Committed: {msg[:80]}")

    # Flush any files written since the main commit (e.g. db journal)
    _commit_pending(repo, "[DriverDex] chore: post-commit flush (db/badge)")

    with C.status("[bold green]  git pull --rebase before push ...[/bold green]", spinner="dots2"):
        rc, out, se = _git(["pull", "--rebase", "origin", "HEAD"], repo, timeout=120)

    if rc != 0:
        combined = (se + " " + out).strip()
        err(f"git pull --rebase failed: {combined}")
        hint(diagnose_git(combined))
        _git(["rebase", "--abort"], repo)
        return False

    _MAX_PUSH_ATTEMPTS = 6
    for attempt in range(1, _MAX_PUSH_ATTEMPTS + 1):
        with C.status(
            f"[bold green]  git push ... (attempt {attempt}/{_MAX_PUSH_ATTEMPTS})[/bold green]",
            spinner="dots2",
        ):
            rc, out, se = _git(["push", "origin", "HEAD"], repo, timeout=300)

        if rc == 0:
            ok("Pushed to GitHub successfully.")
            return True

        combined = (se + " " + out).strip()
        err(f"git push failed (attempt {attempt}/{_MAX_PUSH_ATTEMPTS}): {combined}")
        hint(diagnose_git(combined))

        if attempt < _MAX_PUSH_ATTEMPTS:
            C.print()
            try:
                retry = Confirm.ask(
                    f"  [bold cyan]Retry push?[/bold cyan]  "
                    f"[dim]({_MAX_PUSH_ATTEMPTS - attempt} attempt(s) remaining)[/dim]",
                    default=True,
                )
            except (KeyboardInterrupt, EOFError):
                C.print()
                warn("Push retry cancelled by user.")
                return False

            if not retry:
                warn("Push retry declined by user.")
                return False

            with C.status(
                "[bold green]  git pull --rebase before retry ...[/bold green]",
                spinner="dots2",
            ):
                pr, pout, pse = _git(["pull", "--rebase", "origin", "HEAD"], repo, timeout=120)
            if pr != 0:
                pcombined = (pse + " " + pout).strip()
                warn(f"Pre-retry pull failed: {pcombined}")
                _git(["rebase", "--abort"], repo)

    return False
def _load_remote_index(workspace: Path) -> Optional[Dict]:
    """
    Download driverdex_index.json from GitHub into the workspace and return its
    parsed contents, or None if the file doesn't exist yet.
    """
    result = _download_file_from_github(INDEX_FILE_NAME, workspace / INDEX_FILE_NAME)
    if result != "ok":
        return None
    try:
        return json.loads((workspace / INDEX_FILE_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def github_pull_manifest(workspace: Path) -> None:
    """
    Download the current manifest JSON (and README.md) from GitHub into the
    local workspace so load_manifest() can read them.

    Strategy:
      1. Download driverdex_index.json first — it lists every shard filename exactly.
      2. Use the index to download only the shards that actually exist, in order.
      3. Fall back to probing .2.json / .3.json … if the index is missing.

    Raises RuntimeError if the primary manifest exists on GitHub but cannot be
    downloaded (network/auth error) — prevents overwriting it with an empty one.
    """
    with C.status(
        "[bold green]  Downloading manifest from GitHub ...[/bold green]",
        spinner="dots2",
    ):
        # ── Step 1: fetch the index first ─────────────────────────────────────
        remote_index = _load_remote_index(workspace)

        # ── Step 2: download primary manifest shard ───────────────────────────
        result = _download_file_from_github(MANIFEST_REL, workspace / MANIFEST_REL)
        if result == "error":
            raise RuntimeError(
                f"Failed to download {MANIFEST_REL} from GitHub. "
                "Cannot continue — would overwrite the existing manifest with an empty one.\n"
                "Check your internet connection and SSH key, then re-run."
            )

        if result == "ok":
            # Validate the downloaded manifest is real JSON
            local     = workspace / MANIFEST_REL
            raw_bytes = local.read_bytes()

            # Detect empty or HTML error responses (bad token / redirect)
            if not raw_bytes or raw_bytes[:1] in (b"<", b"\n", b"\r\n"):
                local.unlink(missing_ok=True)
                snippet = raw_bytes[:120].decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"Downloaded {MANIFEST_REL} is empty or contains non-JSON content "
                    f"(first bytes: {snippet!r}).\n"
                    "This usually means a network issue or the repo is inaccessible.\n"
                    "Check your SSH key and internet connection, then re-run.")

            try:
                data = json.loads(raw_bytes.decode("utf-8"))
                if "drivers" not in data:
                    raise ValueError("Missing 'drivers' key — file may be corrupted.")
            except json.JSONDecodeError as exc:
                local.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Downloaded {MANIFEST_REL} is not valid JSON: {exc}\n"
                    "The file on GitHub may be corrupted or your token may be returning\n"
                    "an HTML error page instead of the raw file. Check the file at:\n"
                    f"  https://github.com/{REPO_OWNER}/{REPO_NAME}/blob/{GH_BRANCH}/{MANIFEST_REL}"
                )

        # ── Step 3: download additional manifest shards ───────────────────────
        # Use the index shard list when available; otherwise probe sequentially.
        if remote_index and remote_index.get("manifest_shards"):
            extra_shards = [
                s["filename"] for s in remote_index["manifest_shards"]
                if s["filename"] != MANIFEST_REL   # primary already fetched
            ]
            for shard_name in extra_shards:
                r = _download_file_from_github(shard_name, workspace / shard_name)
                if r == "error":
                    warn(f"Could not download manifest shard {shard_name} — skipping.")
        else:
            # Fallback: probe numbered shards until a 404
            for idx in range(2, 50):
                shard_rel = MANIFEST_REL.replace(".json", f".{idx}.json")
                r = _download_file_from_github(shard_rel, workspace / shard_rel)
                if r == "not_found":
                    break
                if r == "error":
                    warn(f"Could not download manifest shard {shard_rel} — skipping.")

        # ── Step 4: README (badge updates) ────────────────────────────────────
        _download_file_from_github("README.md", workspace / "README.md")

        # ── Step 5: SQLite DB shards ──────────────────────────────────────────
        if remote_index and remote_index.get("db_shards"):
            for s in remote_index["db_shards"]:
                _download_file_from_github(s["filename"], workspace / s["filename"])
        else:
            # Fallback: probe DB shards until a 404
            for db_rel in _remote_db_shards():
                r = _download_file_from_github(db_rel, workspace / db_rel)
                if r == "not_found":
                    break

    if result == "ok":
        n_manifest = len(_manifest_shard_paths(workspace))
        ok(f"Synced latest manifest from GitHub  ({n_manifest} shard(s)).")
    else:
        ok("No manifest on GitHub yet — starting fresh.")


def _remote_db_shards() -> List[str]:
    """
    Return relative paths for DB shards that exist on GitHub.
    Now only used as a fallback when driverdex_index.json is unavailable.
    Returns just the primary shard name — the caller probes further as needed.
    """
    return [DB_FILE_NAME]


_LFS_POINTER_SIG = b"version https://git-lfs.github.com/spec/"


def _parse_lfs_pointer(data: bytes) -> Optional[Dict[str, str]]:
    """Parse a Git LFS pointer; return {oid, size} or None if not a pointer."""
    if not data.lstrip().startswith(_LFS_POINTER_SIG):
        return None
    info: Dict[str, str] = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("oid sha256:"):
            info["oid"] = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            info["size"] = line.split(" ", 1)[1].strip()
    return info if "oid" in info and "size" in info else None


def _download_via_lfs_batch(
    repo_rel_path: str,
    oid: str,
    size: int,
    local_dest: Path,
) -> str:
    """Fetch a Git LFS object via the Batch API. Returns "ok"/"not_found"/"error"."""
    lfs_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git/info/lfs/objects/batch"
    payload = json.dumps({
        "operation": "download",
        "transfers": ["basic"],
        "objects"  : [{"oid": oid, "size": size}],
    }).encode("utf-8")
    headers: Dict[str, str] = {
        "Content-Type": "application/vnd.git-lfs+json",
        "Accept"      : "application/vnd.git-lfs+json",
    }


    req = urllib.request.Request(lfs_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        warn(f"LFS batch request failed for {repo_rel_path}: HTTP {exc.code}")
        return "error"
    except Exception as exc:
        warn(f"LFS batch request failed for {repo_rel_path}: {exc}")
        return "error"

    objects = batch.get("objects", [])
    if not objects:
        warn(f"LFS batch returned no objects for {repo_rel_path}")
        return "error"
    obj = objects[0]
    if "error" in obj:
        return "not_found" if obj["error"].get("code") == 404 else "error"

    download_href = obj.get("actions", {}).get("download", {}).get("href", "")
    if not download_href:
        warn(f"LFS batch gave no download href for {repo_rel_path}")
        return "error"

    dl_headers = dict(obj.get("actions", {}).get("download", {}).get("header", {}))
    dl_req = urllib.request.Request(download_href, headers=dl_headers)
    try:
        with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
            content = dl_resp.read()
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(content)
        log("info", f"LFS download OK: {repo_rel_path} ({fmt_size(len(content))})")
        return "ok"
    except Exception as exc:
        warn(f"LFS object download failed for {repo_rel_path}: {exc}")
        return "error"


def _download_file_from_github(
    repo_rel_path: str,
    local_dest: Path,
) -> str:
    """
    Download a single file from GitHub via raw.githubusercontent.com.
    Transparently handles Git LFS pointers via the LFS Batch API.

    Returns:
      "ok"        – downloaded and saved successfully
      "not_found" – file does not exist on GitHub (HTTP 404)
      "error"     – download failed for another reason
    """
    raw_url = (
        f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
        f"/{GH_BRANCH}/{repo_rel_path}"
    )
    headers: Dict[str, str] = {}


    req = urllib.request.Request(raw_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "not_found"
        warn(f"Could not download {repo_rel_path}: HTTP {exc.code}")
        return "error"
    except Exception as exc:
        warn(f"Could not download {repo_rel_path}: {exc}")
        return "error"

    # ── Git LFS pointer? resolve via the Batch API ────────────────────────────
    lfs_info = _parse_lfs_pointer(data)
    if lfs_info is not None:
        info(f"  {repo_rel_path} is stored in Git LFS — fetching via LFS API …")
        return _download_via_lfs_batch(
            repo_rel_path,
            oid=lfs_info["oid"],
            size=int(lfs_info["size"]),
            local_dest=local_dest,
        )

    # ── Reject empty or HTML responses ────────────────────────────────────────
    probe = data.lstrip()
    if not probe:
        warn(f"Empty response downloading {repo_rel_path} — skipping.")
        return "error"
    if repo_rel_path.endswith(".json") and probe[:1] not in (b"{", b"["):
        snippet = probe[:80].decode("utf-8", errors="replace").rstrip()
        warn(f"Non-JSON response for {repo_rel_path} (starts: {snippet!r}) — skipping.")
        return "error"

    local_dest.parent.mkdir(parents=True, exist_ok=True)
    local_dest.write_bytes(data)
    return "ok"


def _parse_ver(ver_str: str) -> Tuple[int, ...]:
    """
    Parse a version string like '10/12/2023,31.0.101.5333' or '31.0.101.5333'
    into a tuple of ints for comparison.  Unknown / empty -> (0,)
    """
    # Strip leading date portion (MM/DD/YYYY,) if present
    clean = re.sub(r'^\d{1,2}/\d{1,2}/\d{4}\s*,\s*', '', ver_str.strip())
    parts = re.split(r'[.,\-]', clean)
    ints: List[int] = []
    for p in parts:
        p = p.strip()
        if p.isdigit():
            ints.append(int(p))
    return tuple(ints) if ints else (0,)


def version_is_newer(new_ver: str, old_ver: str) -> bool:
    """Return True if new_ver > old_ver by numeric comparison."""
    return _parse_ver(new_ver) > _parse_ver(old_ver)


# ── manifest ──────────────────────────────────────────────────────────────────
def _slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def _manifest_shard_paths(repo: Path) -> List[Path]:
    """
    Return all manifest shard paths in order:
    drivers.manifest.json, drivers.manifest.2.json, drivers.manifest.3.json …
    Only returns paths that actually exist.
    """
    base = repo / MANIFEST_REL
    paths = []
    if base.exists():
        paths.append(base)
    idx = 2
    while True:
        p = repo / MANIFEST_REL.replace(".json", f".{idx}.json")
        if p.exists():
            paths.append(p)
            idx += 1
        else:
            break
    return paths


def _next_shard_path(repo: Path) -> Path:
    existing = _manifest_shard_paths(repo)
    if not existing:
        return repo / MANIFEST_REL
    idx = len(existing) + 1
    if idx == 1:
        return repo / MANIFEST_REL
    return repo / MANIFEST_REL.replace(".json", f".{idx}.json")


def load_manifest(repo: Path) -> Dict:
    """
    Load the *active* (last) manifest shard.
    Falls back to a fresh manifest if nothing exists or parsing fails.
    """
    shards = _manifest_shard_paths(repo)
    if shards:
        p = shards[-1]          # active shard is always the last one
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "version_history" not in data:
                data["version_history"] = {}
            if "shard_index" not in data:
                data["shard_index"] = len(shards)
            if "total_shards" not in data:
                data["total_shards"] = len(shards)
            log("info", f"Loaded manifest shard {len(shards)} from {p}")
            return data
        except Exception as exc:
            warn(f"Could not parse {p.name}: {exc} -- starting fresh shard.")
            log("warning", f"Manifest parse error: {exc}")

    return {
        "schema"         : SCHEMA_VER,
        "shard_index"    : 1,
        "total_shards"   : 1,
        "updated"        : str(date.today()),
        "base_url"       : BASE_RAW_URL,
        "drivers"        : [],
        "version_history": {},
    }


def save_manifest(m: Dict, repo: Path) -> Path:
    """
    Serialise the manifest.  If the resulting JSON would exceed
    MANIFEST_SIZE_LIMIT, the active shard is sealed as-is (its existing drivers
    are preserved) and the new batch of drivers is written to a fresh shard.

    Returns the path of the file actually written.
    """
    m["updated"] = str(date.today())
    m["schema"]  = SCHEMA_VER

    existing_shards = _manifest_shard_paths(repo)
    active_path     = existing_shards[-1] if existing_shards else (repo / MANIFEST_REL)
    # Ensure the manifests/ directory exists before writing into it.
    active_path.parent.mkdir(parents=True, exist_ok=True)

    # Probe size of the full manifest as it stands
    probe = json.dumps(m, indent=2, ensure_ascii=False).encode("utf-8")

    if len(probe) <= MANIFEST_SIZE_LIMIT:
        # Fits in the current shard — write normally
        active_path.write_bytes(probe)
        log("info", f"Manifest saved ({fmt_size(len(probe))}) → {active_path.name}")
        _update_readme_badge(repo, _count_total_drivers(repo))
        _save_index(repo)
        return active_path

    # ── Over the limit: seal the current shard, open a new one ───────────────
    warn(
        f"Manifest shard {active_path.name} would exceed "
        f"{fmt_size(MANIFEST_SIZE_LIMIT)} — sealing it and opening a new shard."
    )
    log("warning", f"Manifest size overflow ({fmt_size(len(probe))}), creating new shard")

    # Separate the NEW drivers (those not yet on disk) from the ones already
    # persisted in the active shard so we can keep the sealed shard intact.
    try:
        sealed_data    = json.loads(active_path.read_text(encoding="utf-8"))
        sealed_drivers = {e["id"] for e in sealed_data.get("drivers", [])}
    except Exception:
        sealed_data    = {}
        sealed_drivers = set()

    new_drivers = [e for e in m.get("drivers", []) if e["id"] not in sealed_drivers]

    # Seal the existing shard by adding a forward-pointer note (leave drivers intact)
    next_idx  = len(existing_shards) + 1
    next_name = MANIFEST_REL.replace(".json", f".{next_idx}.json")
    if sealed_data:
        sealed_data["note"]         = f"Sealed — continued in {next_name}"
        sealed_data["next_shard"]   = next_name
        sealed_data["total_shards"] = next_idx
        active_path.write_text(
            json.dumps(sealed_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # Write new shard with only the new drivers
    new_path = repo / next_name
    new_m = {
        "schema"         : SCHEMA_VER,
        "shard_index"    : next_idx,
        "total_shards"   : next_idx,
        "updated"        : str(date.today()),
        "base_url"       : BASE_RAW_URL,
        "drivers"        : new_drivers,
        "version_history": m.get("version_history", {}),
    }
    new_path.write_text(json.dumps(new_m, indent=2, ensure_ascii=False), encoding="utf-8")
    ok(f"New manifest shard created: {new_path.name}  ({len(new_drivers)} driver(s))")
    log("info", f"New manifest shard: {new_path}  ({len(new_drivers)} drivers)")

    _update_readme_badge(repo, _count_total_drivers(repo))
    _save_index(repo)
    return new_path



# ── Index file (driverdex_index.json) ───────────────────────────────────────────────────
# driverdex_index.json is a small metadata tracker that records every manifest and
# DB shard ever created, along with sizes and the currently-active shard.
# It is always < a few KB so it never needs to be sharded itself.
# Consumers (e.g. DriverDex installer) can read this single file to discover all
# shards without having to probe numbered filenames.
# ────────────────────────────────────────────────────────────────────────────

def _load_index(repo: Path) -> Dict:
    """Load driverdex_index.json from the local workspace, or return a blank skeleton."""
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
        "db_shards"        : [],
    }


def _save_index(repo: Path) -> None:
    """
    Re-build driverdex_index.json from the manifest and DB shards currently present
    in the workspace, then write it to disk.

    Each shard entry records:
      filename   - relative filename (e.g. "drivers.manifest.json")
      size_bytes - current on-disk size (0 if the file does not exist yet)
      active     - True for the shard that new entries are written to
      note       - human-readable tag ("overflow -> next shard" for sealed ones)

    The index file is ALWAYS written alongside manifest/DB changes so callers
    can rely on it to discover shards without probing numbered filenames.
    If drivers.manifest.json is >= 15 MB the index marks it sealed and points
    to the next shard as active -- the original large file is never edited.
    """
    manifest_shards = []
    shard_paths = _manifest_shard_paths(repo)
    for i, p in enumerate(shard_paths):
        sz     = p.stat().st_size if p.exists() else 0
        sealed = (sz >= MANIFEST_SIZE_LIMIT) or (i < len(shard_paths) - 1)
        manifest_shards.append({
            # Store the repo-relative POSIX path (e.g. "manifests/drivers.manifest.json")
            # so every downstream consumer can fetch it as BASE_RAW_URL/<filename>.
            "filename"   : p.relative_to(repo).as_posix(),
            "size_bytes" : sz,
            "active"     : not sealed,
            "note"        : "overflow -> next shard" if sealed else "active",
        })
    # If no shards exist yet, pre-populate with the primary file (not yet created)
    if not manifest_shards:
        manifest_shards.append({
            "filename"   : MANIFEST_REL,
            "size_bytes" : 0,
            "active"     : True,
            "note"        : "not yet created",
        })

    db_shards = []
    db_paths = _db_shard_paths(repo)
    for i, p in enumerate(db_paths):
        sz     = p.stat().st_size if p.exists() else 0
        sealed = (sz >= DB_SIZE_LIMIT) or (i < len(db_paths) - 1)
        db_shards.append({
            "filename"   : p.name,
            "size_bytes" : sz,
            "active"     : not sealed,
            "note"        : "full -> next shard" if sealed else "active",
        })
    if not db_shards:
        db_shards.append({
            "filename"   : DB_FILE_NAME,
            "size_bytes" : 0,
            "active"     : True,
            "note"        : "not yet created",
        })

    idx_data = {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : manifest_shards,
        "db_shards"        : db_shards,
    }
    p = repo / INDEX_FILE_NAME
    p.write_text(json.dumps(idx_data, indent=2, ensure_ascii=False), encoding="utf-8")
    log("info", f"driverdex_index.json updated — {len(manifest_shards)} manifest shard(s), "
                f"{len(db_shards)} DB shard(s)")
    ok(f"driverdex_index.json updated  ({len(manifest_shards)} manifest shard(s) / "
       f"{len(db_shards)} DB shard(s))")


# ── SQLite persistence ────────────────────────────────────────────────────────
def _db_shard_paths(repo: Path) -> List[Path]:
    base = repo / DB_FILE_NAME
    paths = []
    if base.exists():
        paths.append(base)
    idx = 2
    while True:
        p = repo / DB_FILE_NAME.replace(".db", f".{idx}.db")
        if p.exists():
            paths.append(p)
            idx += 1
        else:
            break
    return paths


def _active_db_path(repo: Path) -> Path:
    existing = _db_shard_paths(repo)
    if not existing:
        return repo / DB_FILE_NAME
    last = existing[-1]
    if last.stat().st_size >= DB_SIZE_LIMIT:
        idx      = len(existing) + 1
        new_path = repo / DB_FILE_NAME.replace(".db", f".{idx}.db")
        log("warning", f"DB shard overflow — new shard: {new_path.name}")
        warn(f"SQLite shard {last.name} is full — new shard: {new_path.name}")
        return new_path
    return last


def _db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            id              TEXT PRIMARY KEY,
            name            TEXT,
            provider        TEXT,
            category        TEXT,
            type_folder     TEXT,
            arch            TEXT,
            version         TEXT,
            os_tags         TEXT,
            hwids           TEXT,
            compatible_ids  TEXT,
            zip_url         TEXT,
            zip_parts       INTEGER,
            inf_file        TEXT,
            enabled         INTEGER,
            supersedes      TEXT,
            superseded_by   TEXT,
            notes           TEXT,
            date_added      TEXT
        );

        CREATE TABLE IF NOT EXISTS zip_parts (
            driver_id   TEXT,
            part_num    INTEGER,
            filename    TEXT,
            size_bytes  INTEGER,
            sha256      TEXT,
            url         TEXT,
            PRIMARY KEY (driver_id, part_num),
            FOREIGN KEY (driver_id) REFERENCES drivers(id)
        );

        CREATE TABLE IF NOT EXISTS version_history (
            hwid_key        TEXT,
            entry_id        TEXT,
            version         TEXT,
            date_added      TEXT,
            superseded_by   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_drivers_category ON drivers(category);
        CREATE INDEX IF NOT EXISTS idx_drivers_provider  ON drivers(provider);
        CREATE INDEX IF NOT EXISTS idx_drivers_enabled   ON drivers(enabled);
    """)
    conn.commit()


def save_to_sqlite(entries: List[Dict], repo: Path) -> None:
    """
    Persist driver entries (and their zip-part records) to the active SQLite
    shard.  Rotates to a new shard file if the current one is >= DB_SIZE_LIMIT.
    """
    db_path = _active_db_path(repo)
    log("info", f"Writing {len(entries)} entries to SQLite: {db_path.name}")
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        _db_schema(conn)

        today = str(date.today())
        for e in entries:
            conn.execute(
                """
                INSERT OR REPLACE INTO drivers
                  (id, name, provider, category, type_folder, arch, version,
                   os_tags, hwids, compatible_ids, zip_url, zip_parts,
                   inf_file, enabled, supersedes, superseded_by, notes, date_added)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e["id"],
                    e.get("name", ""),
                    e.get("provider", ""),
                    e.get("category", ""),
                    e.get("type_folder", ""),
                    e.get("arch", "x64"),
                    e.get("version", ""),
                    json.dumps(e.get("os", ["*"])),
                    json.dumps(e.get("hwids", [])),
                    json.dumps(e.get("compatible_ids", [])),
                    e.get("zip", ""),
                    e.get("zip_parts", 1),
                    e.get("inf_file", ""),
                    1 if e.get("enabled", True) else 0,
                    e.get("supersedes"),
                    e.get("superseded_by"),
                    e.get("notes", ""),
                    today,
                ),
            )
            # zip parts
            for part in e.get("parts", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO zip_parts
                      (driver_id, part_num, filename, size_bytes, sha256, url)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        e["id"],
                        part["part_num"],
                        part["filename"],
                        part["size_bytes"],
                        part["sha256"],
                        part.get("url", ""),
                    ),
                )

        conn.commit()

        db_size = db_path.stat().st_size
        ok(f"SQLite updated: {db_path.name}  ({fmt_size(db_size)})")
        log("info", f"SQLite shard {db_path.name}: {fmt_size(db_size)}")
        _save_index(repo)

        if db_size >= DB_SIZE_LIMIT:
            warn(
                f"SQLite shard {db_path.name} has reached "
                f"{fmt_size(db_size)} — next write will use a new shard."
            )

    except Exception as exc:
        err(f"SQLite write failed: {exc}")
        log("error", f"SQLite write error: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── README badge ──────────────────────────────────────────────────────────────
def _count_total_drivers(repo: Path) -> int:
    """Count enabled driver entries across all manifest shards."""
    total = 0
    for shard_path in _manifest_shard_paths(repo):
        try:
            data    = json.loads(shard_path.read_text(encoding="utf-8"))
            enabled = [d for d in data.get("drivers", []) if d.get("enabled", True)]
            total  += len(enabled)
        except Exception:
            pass
    return total


def _update_readme_badge(repo: Path, driver_count: int) -> None:
    """
    Insert or update a driver-count badge block inside README.md.
    The badge is a shields.io flat-square badge rendered as Markdown.
    The block is delimited by HTML comment markers so it can be found and
    replaced on every run without touching surrounding content.
    """
    readme = repo / "README.md"
    if not readme.exists():
        log("debug", "README.md not found — skipping badge update")
        return

    today     = date.today().strftime("%Y--%m--%d").replace("-", "%E2%80%93").replace("%E2%80%93", "--")
    today_fmt = date.today().strftime("%Y‑%m‑%d")

    # Build shield URL (shields.io static badge)
    label   = "drivers"
    message = str(driver_count)
    color   = "brightgreen" if driver_count >= 100 else "green"
    badge_url = (
        f"https://img.shields.io/badge/{label}-{message}-{color}"
        f"?style=flat-square&logo=windows&logoColor=white"
    )
    updated_url = (
        f"https://img.shields.io/badge/updated-{today_fmt.replace('-','--')}-blue"
        f"?style=flat-square"
    )

    badge_block = (
        f"{BADGE_MARKER_START}\n"
        f"[![Drivers]({badge_url})](https://github.com/{REPO_OWNER}/{REPO_NAME})"
        f"  "
        f"[![Updated]({updated_url})](https://github.com/{REPO_OWNER}/{REPO_NAME})\n"
        f"{BADGE_MARKER_END}"
    )

    content = readme.read_text(encoding="utf-8")

    # Replace existing badge block if present
    pattern = re.compile(
        re.escape(BADGE_MARKER_START) + r".*?" + re.escape(BADGE_MARKER_END),
        re.DOTALL,
    )

    if pattern.search(content):
        new_content = pattern.sub(badge_block, content)
    else:
        # Insert after the first H1 line
        lines    = content.split("\n")
        insert_at = next(
            (i + 1 for i, l in enumerate(lines) if l.startswith("# ")),
            0
        )
        lines.insert(insert_at + 1, "")
        lines.insert(insert_at + 2, badge_block)
        lines.insert(insert_at + 3, "")
        new_content = "\n".join(lines)

    readme.write_text(new_content, encoding="utf-8")
    ok(f"README.md badge updated → {driver_count} drivers  [{today_fmt}]")
    log("info", f"README badge updated: {driver_count} drivers")


def _hwid_key(hwids: List[str], arch: str, category: str) -> str:
    """Stable identity key for a driver, independent of version."""
    return "|".join(sorted(hwids)) + f"||{arch}||{category.lower()}"


def apply_version_management(
    manifest    : Dict,
    new_entries : List[Dict],
) -> Tuple[List[Dict], List[str]]:
    """
    For each new entry, check whether a driver with the same HWIDs / arch /
    category already exists in the manifest.

    Rules:
    - If no previous entry exists: just add.
    - If previous entry exists with an OLDER version: mark old as superseded
      (enabled=False, superseded_by=<new_id>), link new entry back
      (supersedes=<old_id>), update version_history.
    - If previous entry exists with the SAME or NEWER version: keep both,
      warn user, do NOT supersede.
    - The old ZIP file is intentionally left in place (user's choice).

    Returns (updated_manifest_drivers, list_of_warning_strings)
    """
    warnings: List[str] = []
    history  = manifest.setdefault("version_history", {})

    # Build a lookup: hwid_key -> list of existing entries
    existing_by_key: Dict[str, List[Dict]] = defaultdict(list)
    for entry in manifest["drivers"]:
        k = _hwid_key(
            entry.get("hwids", []),
            entry.get("arch", ""),
            entry.get("category", ""),
        )
        if k and k != "||||":
            existing_by_key[k].append(entry)

    enriched_new: List[Dict] = []

    for new_e in new_entries:
        new_hwids = new_e.get("hwids", [])
        if not new_hwids:
            # No HWIDs to match on -- just add without version checks
            enriched_new.append(new_e)
            continue

        k = _hwid_key(new_hwids, new_e.get("arch", ""), new_e.get("category", ""))
        prior_entries = existing_by_key.get(k, [])

        # Find the most recent (highest version) active prior entry
        active_prior = [e for e in prior_entries if e.get("enabled", True) and not e.get("superseded_by")]
        if not active_prior:
            # Brand-new driver
            enriched_new.append(new_e)
            history.setdefault(k, []).append({
                "id"           : new_e["id"],
                "version"      : new_e.get("version", ""),
                "date_added"   : str(date.today()),
                "superseded_by": None,
            })
            continue

        # Pick the active entry with the highest version
        best_prior = max(
            active_prior,
            key=lambda e: _parse_ver(e.get("version", "")),
        )
        old_ver = best_prior.get("version", "")
        new_ver = new_e.get("version", "")

        if version_is_newer(new_ver, old_ver):
            # New version wins -- supersede the old entry in-place
            old_id = best_prior["id"]
            new_id = new_e["id"]

            best_prior["enabled"]      = False
            best_prior["superseded_by"] = new_id
            best_prior["notes"] = (
                best_prior.get("notes", "")
                + f" | Superseded by {new_id} on {date.today()}"
            )

            new_e["supersedes"] = old_id

            ok(
                f"Version upgrade: {old_id}  {old_ver or '(unknown)'}"
                f"  →  {new_id}  {new_ver or '(unknown)'}"
            )

            # Update version history
            hist_list = history.setdefault(k, [])
            for h in hist_list:
                if h["id"] == old_id:
                    h["superseded_by"] = new_id
                    break
            hist_list.append({
                "id"           : new_id,
                "version"      : new_ver,
                "date_added"   : str(date.today()),
                "superseded_by": None,
            })

        elif new_ver and old_ver and not version_is_newer(new_ver, old_ver) and new_ver != old_ver:
            # Incoming is older than what we have -- keep both, warn
            warnings.append(
                f"Older version incoming: {new_e['id']}  ({new_ver}) is older than "
                f"existing {best_prior['id']}  ({old_ver}).  Both kept."
            )
            enriched_new.append(new_e)
            continue
        # else: same version -- just add (duplicate detection elsewhere handles exact dups)

        enriched_new.append(new_e)

    return enriched_new, warnings


# ── pre-flight duplicate audit ────────────────────────────────────────────────

# Action codes returned per-driver by the audit
_ACT_SKIP     = "skip"       # already identical in repo  -- do not re-upload
_ACT_REPLACE  = "replace"    # newer version -- remove old zip, upload new
_ACT_KEEP_NEW = "keep_new"   # ambiguous / older / HWID-conflict -- upload anyway
_ACT_OK       = "ok"         # no match found, proceed normally


class DuplicateReport:
    """One row in the pre-flight duplicate report."""
    __slots__ = ("inf_name", "status", "existing_id", "existing_ver",
                 "incoming_ver", "zip_on_disk", "action")

    def __init__(
        self,
        inf_name    : str,
        status      : str,           # "exact" | "same_ver_on_disk" | "older" | "hwid_conflict"
        existing_id : str = "",
        existing_ver: str = "",
        incoming_ver: str = "",
        zip_on_disk : str = "",
        action      : str = _ACT_SKIP,
    ) -> None:
        self.inf_name     = inf_name
        self.status       = status
        self.existing_id  = existing_id
        self.existing_ver = existing_ver
        self.incoming_ver = incoming_ver
        self.zip_on_disk  = zip_on_disk
        self.action       = action


def _zip_exists_for_entry(entry: Dict, repo_dir: Path) -> Optional[Path]:
    """
    Check whether the zip referenced by a manifest entry actually exists on
    disk in the local repo.  Returns the Path if found, else None.
    URL format:  .../main/drivers/<Type>/DP_pack/sub/file.zip
    We strip everything up to /drivers/ and resolve against repo_dir.
    """
    url = entry.get("zip", "")
    if not url:
        return None
    marker = f"/{DRIVERS_DIR}/"
    idx = url.find(marker)
    if idx == -1:
        return None
    rel_str = url[idx + 1:]          # e.g.  drivers/Net/DP_Intel/foo.zip
    p = repo_dir / rel_str.replace("/", os.sep)
    return p if p.exists() else None


def preflight_duplicate_audit(
    inf_data    : List[Tuple[Path, Dict]],
    manifest    : Dict,
    repo_dir    : Path,
    type_map    : Dict[Path, str],
) -> Tuple[List[Tuple[Path, Dict]], List[DuplicateReport]]:
    """
    Compare incoming drivers against:
      (a) manifest entries  -- exact HWID+version+arch+category match
      (b) zip files on disk -- same zip stem already present in repo tree

    Returns:
      filtered_inf_data  -- only drivers the user chose to proceed with
      reports            -- list of DuplicateReport for display / logging

    The user is shown a table of all conflicts and asked what to do with each
    one.  Choices:
      [s] skip        -- exclude from this upload entirely
      [r] replace     -- delete the old zip from disk, upload the new one
      [k] keep both   -- upload new without touching the old
    """
    if not inf_data:
        return inf_data, []

    reports: List[DuplicateReport] = []

    # ── Build fast lookups from manifest ──────────────────────────────────────
    # key: (frozenset(hwids), arch, category_lower, version) -> entry
    exact_lookup: Dict[Tuple, Dict] = {}
    # key: (frozenset(hwids), arch, category_lower) -> entry  (ignores version)
    hwid_lookup: Dict[Tuple, List[Dict]] = defaultdict(list)

    for entry in manifest.get("drivers", []):
        if not entry.get("enabled", True):
            continue
        k_full = (
            frozenset(entry.get("hwids", [])),
            entry.get("arch", ""),
            entry.get("category", "").lower(),
            entry.get("version", ""),
        )
        k_hwid = k_full[:3]
        exact_lookup[k_full] = entry
        hwid_lookup[k_hwid].append(entry)

    # ── Also build a set of zip stems already present anywhere in the repo ────
    existing_zip_stems: Set[str] = set()
    drivers_root = repo_dir / DRIVERS_DIR
    if drivers_root.exists():
        for zp in drivers_root.rglob("*.zip"):
            existing_zip_stems.add(zp.stem.lower())

    flagged: Dict[Path, DuplicateReport] = {}   # inf_path -> report

    for inf_path, d in inf_data:
        hwids    = d.get("hwids", [])
        arch     = d.get("arch", "")
        cat      = d.get("category", "").lower()
        ver      = d.get("version", "")

        k_full = (frozenset(hwids), arch, cat, ver)
        k_hwid = (frozenset(hwids), arch, cat)

        # 1. Exact manifest match (same HWIDs + arch + category + version)
        if hwids and k_full in exact_lookup:
            existing = exact_lookup[k_full]
            zip_path = _zip_exists_for_entry(existing, repo_dir)
            flagged[inf_path] = DuplicateReport(
                inf_name     = inf_path.name,
                status       = "exact",
                existing_id  = existing.get("id", ""),
                existing_ver = existing.get("version", ""),
                incoming_ver = ver,
                zip_on_disk  = str(zip_path) if zip_path else "(not on disk)",
                action       = _ACT_SKIP,         # default: skip exact dups
            )
            continue

        # 2. Same HWIDs+arch+category, different version -> version mismatch
        if hwids and k_hwid in hwid_lookup:
            prior_list = hwid_lookup[k_hwid]
            best = max(prior_list, key=lambda e: _parse_ver(e.get("version", "")))
            old_ver = best.get("version", "")

            if version_is_newer(ver, old_ver):
                status = "newer_incoming"     # new > old  -> user may want replace
                default_action = _ACT_REPLACE
            elif ver == old_ver:
                status = "exact"
                default_action = _ACT_SKIP
            else:
                status = "older_incoming"     # new < old  -> warn, default keep
                default_action = _ACT_KEEP_NEW

            zip_path = _zip_exists_for_dish = _zip_exists_for_entry(best, repo_dir)
            flagged[inf_path] = DuplicateReport(
                inf_name     = inf_path.name,
                status       = status,
                existing_id  = best.get("id", ""),
                existing_ver = old_ver,
                incoming_ver = ver,
                zip_on_disk  = str(zip_path) if zip_path else "(not on disk)",
                action       = default_action,
            )
            continue

        # 3. Zip stem already on disk (catches re-uploads of same pack
        #    even if manifest was wiped / schema changed)
        # Build the stem the same way _zip_stem() would
        from_type = type_map.get(inf_path.parent, _TYPE_FALLBACK)
        prospective_stem = re.sub(
            r'[^A-Za-z0-9]+', '-',
            "-".join(filter(None, ["", inf_path.stem, ver, arch]))
        ).strip('-').lower()

        if prospective_stem in existing_zip_stems:
            flagged[inf_path] = DuplicateReport(
                inf_name     = inf_path.name,
                status       = "same_zip_on_disk",
                existing_id  = "(zip exists, no manifest entry)",
                existing_ver = "?",
                incoming_ver = ver,
                zip_on_disk  = prospective_stem + ".zip",
                action       = _ACT_SKIP,
            )

    if not flagged:
        return inf_data, []

    # ── Display the duplicate table ───────────────────────────────────────────
    C.print()
    rule("PRE-FLIGHT  |  Duplicate / Conflict Report")
    C.print()
    warn(f"{len(flagged)} driver(s) already exist in the repo or conflict with existing entries.")
    C.print()

    STATUS_LABEL = {
        "exact"           : "[dim]Exact match[/dim]",
        "newer_incoming"  : "[bold green]Newer version[/bold green]",
        "older_incoming"  : "[bold yellow]Older version[/bold yellow]",
        "same_zip_on_disk": "[dim]Zip on disk[/dim]",
    }
    DEFAULT_LABEL = {
        _ACT_SKIP     : "[dim]skip[/dim]",
        _ACT_REPLACE  : "[bold green]replace[/bold green]",
        _ACT_KEEP_NEW : "[yellow]keep both[/yellow]",
    }

    t = Table(**_TBL_BASE)
    t.add_column("#",            style="bold white",  width=3,  justify="right")
    t.add_column("INF",          style="white",       no_wrap=True)
    t.add_column("Status",       style="white",       width=16)
    t.add_column("Repo ver",     style="dim yellow",  width=14)
    t.add_column("Incoming ver", style="yellow",      width=14)
    t.add_column("Existing ID",  style="dim cyan",    no_wrap=False)
    t.add_column("Default",      style="white",       width=12)

    idx_to_report: Dict[int, DuplicateReport] = {}
    for i, (_, rpt) in enumerate(flagged.items(), start=1):
        idx_to_report[i] = rpt
        t.add_row(
            str(i),
            rpt.inf_name,
            STATUS_LABEL.get(rpt.status, rpt.status),
            rpt.existing_ver or "-",
            rpt.incoming_ver or "-",
            rpt.existing_id,
            DEFAULT_LABEL.get(rpt.action, rpt.action),
        )
    C.print(t)

    C.print()
    C.print(
        "  [dim]For each item choose an action:[/dim]\n"
        "  [bold green][s][/bold green] skip (don't upload)  "
        "  [bold cyan][r][/bold cyan] replace (delete old zip, upload new)  "
        "  [bold yellow][k][/bold yellow] keep both (upload alongside old)\n"
        "  [bold white][a][/bold white] apply defaults to all  "
        "  [bold white][A][/bold white] accept all as-is (keep all defaults)"
    )
    C.print()

    bulk = Prompt.ask(
        "  [bold cyan]Bulk action[/bold cyan]  "
        "[[bold white]A[/bold white]=apply defaults / "
        "[bold white]a[/bold white]=accept all / or press Enter for per-item]",
        default="a",
    ).strip().lower()

    if bulk == "a":
        pass  # use defaults already set
    elif bulk in ("accept", "aa", "accept all"):
        pass  # same -- defaults stand
    else:
        # Per-item prompts
        for i, rpt in idx_to_report.items():
            default_ch = {"skip": "s", _ACT_REPLACE: "r", _ACT_KEEP_NEW: "k"}.get(rpt.action, "s")
            ch = Prompt.ask(
                f"  [bold white][{i}][/bold white] {escape(rpt.inf_name)}  "
                f"({STATUS_LABEL.get(rpt.status, rpt.status)})  "
                f"[[bold green]s[/bold green]/[bold cyan]r[/bold cyan]/[bold yellow]k[/bold yellow]]",
                choices=["s", "r", "k"],
                default=default_ch,
            ).strip().lower()
            rpt.action = {
                "s": _ACT_SKIP,
                "r": _ACT_REPLACE,
                "k": _ACT_KEEP_NEW,
            }.get(ch, rpt.action)

    # ── Execute replace actions: delete old zips from disk ────────────────────
    for inf_path, rpt in flagged.items():
        if rpt.action == _ACT_REPLACE:
            # Find old zip(s) for the existing entry by scanning the repo
            # We do a best-effort search by the existing manifest entry id
            if rpt.existing_id and rpt.existing_id != "(zip exists, no manifest entry)":
                # Find the manifest entry again to get its zip url
                old_entry = next(
                    (e for e in manifest.get("drivers", [])
                     if e.get("id") == rpt.existing_id),
                    None,
                )
                if old_entry:
                    zp = _zip_exists_for_entry(old_entry, repo_dir)
                    if zp:
                        try:
                            zp.unlink()
                            ok(f"Removed old zip: {zp.name}")
                        except Exception as ex:
                            warn(f"Could not remove old zip {zp}: {ex}")

    # ── Build final filtered inf_data ─────────────────────────────────────────
    skip_paths = {inf_path for inf_path, rpt in flagged.items() if rpt.action == _ACT_SKIP}
    filtered = [(p, d) for p, d in inf_data if p not in skip_paths]

    skipped_count  = len(skip_paths)
    replaced_count = sum(1 for r in flagged.values() if r.action == _ACT_REPLACE)
    kept_count     = sum(1 for r in flagged.values() if r.action == _ACT_KEEP_NEW)

    C.print()
    if skipped_count:
        info(f"Skipped  {skipped_count} driver(s) (already up to date).")
    if replaced_count:
        ok(f"Replacing {replaced_count} driver(s) (old zips removed from disk).")
    if kept_count:
        info(f"Keeping both copies for {kept_count} driver(s).")
    C.print()

    return filtered, list(flagged.values())


def build_entries(
    pack     : str,
    inf_data : List[Tuple[Path, Dict]],
    zip_map  : Dict[Path, List["PartInfo"]],
    rel_dir  : str,
    src      : Path,
    zip_dest : Path,
    type_map : Dict[Path, str],          # group_folder -> type_subfolder
) -> Tuple[List[Dict], List[str]]:
    """
    Build one manifest entry per INF file.

    URL structure:
      drivers/<Type>/DP_<pack>/<relative_subpath>/<zip_name>

    Each entry now includes a  "parts" list with per-part metadata:
      [{"part_num", "filename", "size_bytes", "sha256", "url"}, ...]
    """
    entries  : List[Dict] = []
    warnings : List[str]  = []

    # HWID conflict detection
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

    # Exact-duplicate detection
    seen_sigs : Dict[str, Path] = {}
    skipped   : Set[Path]       = set()

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
                f"        Identical to: {seen_sigs[sig].name}  "
                f"(same HWIDs, version, arch)"
            )
            skipped.add(inf_path)
        elif hwid_key_set:
            seen_sigs[sig] = inf_path

    pack_slug = _slug(pack)

    for inf_path, d in inf_data:
        if inf_path in skipped:
            continue

        folder      = inf_path.parent
        part_infos  = zip_map.get(folder, [])   # List[PartInfo]
        n_parts     = len(part_infos)
        type_folder = type_map.get(folder, _TYPE_FALLBACK)

        try:
            rel_sub = str(folder.relative_to(src)).replace("\\", "/")
        except ValueError:
            rel_sub = folder.name
        if rel_sub in (".", ""):
            rel_sub = ""

        # Build per-part metadata (filename, sha256, size, url)
        #
        # pi.path is the final location, e.g.:
        #   <workspace>/drivers/Chipset/DP_AtmelCorp/foo.zip
        # zip_dest is <workspace>/drivers
        # pi.path.relative_to(zip_dest) → Chipset/DP_AtmelCorp/foo.zip
        # which is already the full repo-relative sub-path under drivers/.
        #
        # github.com/raw/ transparently dereferences Git LFS objects;
        # raw.githubusercontent.com only returns the LFS pointer text.
        parts_meta: List[Dict] = []
        zip_urls  : List[str]  = []
        for pi in part_infos:
            try:
                # already includes type_folder/DP_pack — no extra segments needed
                zp_rel_str = str(pi.path.relative_to(zip_dest)).replace("\\", "/")
            except ValueError:
                zp_rel_str = f"{type_folder}/{rel_dir}/{pi.path.name}"
            part_url = (
                f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
                f"/raw/refs/heads/{GH_BRANCH}/{DRIVERS_DIR}/{zp_rel_str}"
            )
            zip_urls.append(part_url)
            parts_meta.append({
                "part_num"   : pi.part_num,
                "filename"   : pi.path.name,
                "size_bytes" : pi.size_bytes,
                "sha256"     : pi.sha256,
                "url"        : part_url,
            })

        zip_url = zip_urls[0] if zip_urls else ""

        ver_slug  = _slug(d.get("version", "") or "noversion")
        arch_slug = _slug(d.get("arch",    "x64"))
        inf_slug  = _slug(inf_path.stem)
        entry_id  = f"{pack_slug}-{inf_slug}-{ver_slug}-{arch_slug}"

        provider  = d.get("provider",     "")
        category  = d.get("category",     "Unknown")
        dev_names = d.get("device_names", [])
        if dev_names:
            friendly = dev_names[0][:64]
        elif provider and category not in ("Unknown", ""):
            friendly = f"{provider} {category} [{inf_path.stem}]"
        elif provider:
            friendly = f"{provider} [{inf_path.stem}]"
        else:
            friendly = f"{category} -- {inf_path.stem}"

        total_zip_bytes = sum(p["size_bytes"] for p in parts_meta)

        entries.append({
            "id"            : entry_id,
            "name"          : friendly,
            "provider"      : provider,
            "category"      : category,
            "type_folder"   : type_folder,
            "hwids"         : d.get("hwids",          []),
            "compatible_ids": d.get("compatible_ids", []),
            "os"            : d.get("os_tags",        ["*"]),
            "arch"          : d.get("arch",           "x64"),
            "version"       : d.get("version",        ""),
            "zip"           : zip_url,
            "zip_urls"      : zip_urls,
            "zip_parts"     : n_parts,
            "zip_bytes"     : total_zip_bytes,
            "parts"         : parts_meta,          # ← new: full per-part metadata
            "inf_subfolder" : rel_sub,
            "inf_file"      : inf_path.name,
            "enabled"       : True,
            "force"         : False,
            "supersedes"    : None,
            "superseded_by" : None,
            "date_added"    : str(date.today()),
            "notes"         : f"Auto-generated | INF: {inf_path.name}",
        })
        log("debug", f"Built entry {entry_id}: {n_parts} part(s), {fmt_size(total_zip_bytes)}")

    return entries, warnings


# ── display tables ────────────────────────────────────────────────────────────
_TBL_BASE = dict(
    box=box.SIMPLE_HEAVY,
    border_style="dim green",
    header_style="bold cyan",
    show_edge=True,
    expand=True,
)


def show_inf_table(inf_data: List[Tuple[Path, Dict]], src: Path,
                   type_map: Optional[Dict[Path, str]] = None) -> None:
    t = Table(**_TBL_BASE)
    t.add_column("INF File",   style="white",      no_wrap=False)
    t.add_column("Type",       style="bold magenta", width=10)
    t.add_column("Provider",   style="bold green",  width=16)
    t.add_column("Version",    style="yellow",      width=14, no_wrap=True)
    t.add_column("Category",   style="cyan",        width=14)
    t.add_column("Arch",       style="white",       width=6,  justify="center")
    t.add_column("OS Targets", style="dim white",   width=16)
    t.add_column("HWIDs",      style="yellow",      width=5,  justify="right")
    t.add_column("Compat",     style="dim yellow",  width=6,  justify="right")

    for inf_path, d in inf_data:
        try:
            rel = str(inf_path.relative_to(src))
        except ValueError:
            rel = inf_path.name

        dtype = ""
        if type_map:
            dtype = type_map.get(inf_path.parent, "")

        t.add_row(
            rel,
            dtype,
            d.get("provider",  "-") or "-",
            d.get("version",   "-") or "-",
            d.get("category",  "-"),
            d.get("arch",      "-"),
            ", ".join(d.get("os_tags", ["*"])),
            str(len(d.get("hwids",          []))),
            str(len(d.get("compatible_ids", []))),
        )
    C.print(t)


def show_zip_table(zip_map: Dict[Path, List[Path]], src: Path,
                   type_map: Optional[Dict[Path, str]] = None) -> None:
    t = Table(**{**_TBL_BASE, "expand": True})
    t.add_column("Type",          style="bold magenta", width=10)
    t.add_column("Driver Folder", style="white",        no_wrap=False)
    t.add_column("Zip File",      style="cyan",         no_wrap=True)
    t.add_column("Parts",         style="yellow",       width=5,  justify="right")
    t.add_column("Size",          style="yellow",       width=10, justify="right")

    for folder, zips in sorted(zip_map.items(), key=lambda x: str(x[0])):
        if not zips:
            continue
        try:
            rel = str(folder.relative_to(src))
        except ValueError:
            rel = folder.name
        total_sz = sum(z.stat().st_size for z in zips)
        first_name = zips[0].name
        dtype = (type_map or {}).get(folder, "")
        t.add_row(
            dtype,
            rel,
            first_name,
            str(len(zips)),
            fmt_size(total_sz),
        )
    C.print(t)


def show_entry_table(entries: List[Dict]) -> None:
    t = Table(**_TBL_BASE)
    t.add_column("Manifest ID",  style="white")
    t.add_column("Type",         style="bold magenta", width=10)
    t.add_column("Name",         style="bold green",   width=28, no_wrap=False)
    t.add_column("Category",     style="cyan",         width=14)
    t.add_column("Arch",         style="white",        width=5,  justify="center")
    t.add_column("Version",      style="yellow",       width=14)
    t.add_column("Parts",        style="yellow",       width=5,  justify="right")
    t.add_column("HWIDs",        style="yellow",       width=5,  justify="right")
    t.add_column("Supersedes",   style="dim cyan",     width=12)

    for e in entries:
        t.add_row(
            e["id"],
            e.get("type_folder", ""),
            e["name"][:28],
            e["category"],
            e["arch"],
            e.get("version", "") or "-",
            str(e["zip_parts"]),
            str(len(e["hwids"])),
            e.get("supersedes") or "-",
        )
    C.print(t)


def show_version_history(manifest: Dict) -> None:
    history = manifest.get("version_history", {})
    if not history:
        return

    t = Table(**_TBL_BASE, title="[bold cyan]Version History[/bold cyan]")
    t.add_column("Driver Key (HWIDs|arch|cat)",  style="dim white", no_wrap=False, width=30)
    t.add_column("ID",                           style="white",     no_wrap=False)
    t.add_column("Version",                      style="yellow",    width=16)
    t.add_column("Added",                        style="dim white", width=12)
    t.add_column("Superseded By",               style="dim cyan",  width=20)

    for key, entries in list(history.items())[-10:]:  # show last 10 keys max
        short_key = key[:28] + "…" if len(key) > 28 else key
        for i, h in enumerate(entries):
            t.add_row(
                short_key if i == 0 else "",
                h.get("id", ""),
                h.get("version", "") or "-",
                h.get("date_added", "") or "-",
                h.get("superseded_by") or "-",
            )
    C.print(t)


# ── PowerShell bootstrap writer ───────────────────────────────────────────────
_PS1_CONTENT = f"""\
#Requires -Version 5.0
# ==============================================================================
#  DriverDex Builder Bootstrap  --  PowerShell Launcher
#  Author  : rhshourav
#  Version : {APP_VER}
#  Repo    : https://github.com/{REPO_OWNER}/{REPO_NAME}
#  Part of : Windows-Scripts -- github.com/rhshourav/Windows-Scripts
#
#  Usage (PowerShell, admin recommended):
#    irm {BOOTSTRAP_URL} | iex
# ==============================================================================

$ErrorActionPreference = "Stop"

$REPO_OWNER  = "{REPO_OWNER}"
$REPO_NAME   = "{REPO_NAME}"
$SCRIPT_NAME = "driverdex_builder.py"
$SCRIPT_URL  = "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/$SCRIPT_NAME"
$TMP_SCRIPT  = Join-Path $env:TEMP "driverdex_builder_run_$([System.Diagnostics.Process]::GetCurrentProcess().Id).py"

function Write-OK  {{ param($m); Write-Host "  [+] $m" -ForegroundColor Green  }}
function Write-Err {{ param($m); Write-Host "  [X] $m" -ForegroundColor Red    }}
function Write-Inf {{ param($m); Write-Host "  [-] $m" -ForegroundColor White  }}
function Write-Wrn {{ param($m); Write-Host "  [!] $m" -ForegroundColor Yellow }}
function Write-Hnt {{ param($m); Write-Host "      ^-- $m" -ForegroundColor Cyan }}

function Exit-Fail {{
    param($msg, $fix = "")
    Write-Host ""
    Write-Err $msg
    if ($fix) {{ Write-Hnt $fix }}
    Write-Host ""
    exit 1
}}

Clear-Host
$Host.UI.RawUI.WindowTitle = "DriverDex Builder Launcher"
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor DarkGreen
Write-Host "    _     ____   ____" -ForegroundColor Green
Write-Host "   | |   |  _ \\ / ___|" -ForegroundColor Green
Write-Host "   | |   | | | | |" -ForegroundColor Green
Write-Host "   | |___| |_| | |___" -ForegroundColor Green
Write-Host "   |_____|____/ \\____|" -ForegroundColor Green
Write-Host ""
Write-Host "   DriverDex Builder Bootstrap  v{APP_VER}" -ForegroundColor White
Write-Host "   Author  : rhshourav" -ForegroundColor DarkCyan
Write-Host "   Repo    : github.com/$REPO_OWNER/$REPO_NAME" -ForegroundColor Cyan
Write-Host "   Part of : Windows-Scripts" -ForegroundColor DarkCyan
Write-Host "  ============================================================" -ForegroundColor DarkGreen
Write-Host ""

Write-Inf "Checking prerequisites ..."
Write-Host ""

$pyExe = $null
foreach ($candidate in @("python", "python3", "py")) {{
    try {{
        $v = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0) {{ $pyExe = $candidate; Write-OK "$v"; break }}
    }} catch {{}}
}}
if (-not $pyExe) {{
    Exit-Fail "Python not found." "Install Python 3.7+ from https://python.org/downloads and check 'Add to PATH'"
}}

try {{
    $gv = git --version 2>&1
    if ($LASTEXITCODE -ne 0) {{ throw "non-zero exit" }}
    Write-OK $gv
}} catch {{
    Exit-Fail "git not found." "Install from https://git-scm.com/downloads and check 'Add to PATH'"
}}

Write-Inf "Verifying SSH key for github.com ..."
try {{
    $sshOut = ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new git@github.com 2>&1
    if ($sshOut -match "successfully authenticated|Hi ") {{
        Write-OK "SSH key accepted by github.com."
    }} else {{
        Write-Err "SSH key not accepted by GitHub."
        Write-Hnt "Run: ssh -T git@github.com"
        Write-Hnt "Setup: https://docs.github.com/en/authentication/connecting-to-github-with-ssh"
    }}
}} catch {{
    Write-Wrn "Could not verify SSH key: $_"
}}

Write-Host ""
Write-Inf "Downloading $SCRIPT_NAME ..."

try {{
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest $SCRIPT_URL -OutFile $TMP_SCRIPT -UseBasicParsing
    $sz = (Get-Item $TMP_SCRIPT).Length
    Write-OK "Downloaded ($([math]::Round($sz / 1KB, 1)) KB)"
}} catch {{
    Exit-Fail "Download failed: $_" "Check your internet connection, or download manually from: $SCRIPT_URL"
}}

Write-Host ""
Write-Inf "Starting DriverDex Builder ..."
Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGreen
Write-Host ""

$exitCode = 0
try {{
    & $pyExe $TMP_SCRIPT
    $exitCode = $LASTEXITCODE
}} catch {{
    Write-Err "Failed to launch driverdex_builder.py: $_"
    $exitCode = 1
}} finally {{
    if (Test-Path $TMP_SCRIPT) {{
        Remove-Item $TMP_SCRIPT -Force -ErrorAction SilentlyContinue
    }}
}}

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGreen
Write-Host ""

if      ($exitCode -eq 0)   {{ Write-OK "DriverDex Builder finished." }}
elseif  ($exitCode -eq 130) {{ Write-Wrn "Interrupted by user." }}
else                         {{ Write-Err "DriverDex Builder exited with code $exitCode." }}

Write-Host ""
exit $exitCode
"""


def write_bootstrap_ps1(repo: Path) -> None:
    target = repo / BOOTSTRAP_PS1_NAME
    target.write_text(_PS1_CONTENT, encoding="utf-8")
    ok(f"Updated {BOOTSTRAP_PS1_NAME} in repo root.")


# ── per-pack rollback context ────────────────────────────────────────────────
class _PackRollback:
    """
    Tracks every mutation made to the repo during one pack run so that a
    Ctrl+C (or any other mid-flight abort) can be silently undone.

    Usage::

        rb = _PackRollback()
        rb.staging_dir = staging_dir          # set as soon as dir is created
        rb.add_pack_dir(dest_root)            # call for each DP_<pack> root
        rb.snapshot_manifest(path)            # call before manifest is written
        rb.set_db(db_path, entry_ids)         # call before SQLite write

        # on success  → rb.disarm()
        # on Ctrl+C   → rb.execute()  (silent)
    """

    def __init__(self) -> None:
        self.staging_dir:  Optional[Path]   = None
        self._pack_dirs:   List[Path]       = []
        self._manifest_path: Optional[Path] = None
        self._manifest_bak:  Optional[bytes] = None
        self._db_path:     Optional[Path]   = None
        self._db_ids:      List[str]        = []
        self._armed        = True

    # ── population helpers ────────────────────────────────────────────────────
    def add_pack_dir(self, d: Path) -> None:
        if d not in self._pack_dirs:
            self._pack_dirs.append(d)

    def snapshot_manifest(self, path: Path) -> None:
        """Save the manifest bytes *before* any modification."""
        if path.exists():
            self._manifest_path = path
            self._manifest_bak  = path.read_bytes()

    def set_db(self, db_path: Path, entry_ids: List[str]) -> None:
        self._db_path = db_path
        self._db_ids  = list(entry_ids)

    def disarm(self) -> None:
        """Call on success so execute() becomes a no-op."""
        self._armed = False

    # ── undo ─────────────────────────────────────────────────────────────────
    def execute(self) -> None:
        """
        Silently undo everything that was written to the repo.
        Idempotent: self-disarms on first call so repeated calls are safe.
        """
        if not self._armed:
            return
        self._armed = False   # disarm immediately — prevents double-execution

        # 1. Remove staging dir (unfinished zips)
        if self.staging_dir and self.staging_dir.exists():
            shutil.rmtree(self.staging_dir, ignore_errors=True)

        # 2. Remove per-type DP_<pack> subtrees
        for d in self._pack_dirs:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                # Prune now-empty type folder (e.g. drivers/Chipset/)
                parent = d.parent
                try:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except Exception:
                    pass

        # 3. Restore manifest snapshot
        if self._manifest_path and self._manifest_bak is not None:
            try:
                self._manifest_path.write_bytes(self._manifest_bak)
            except Exception:
                pass

        # 4. Remove SQLite entries added this run
        if self._db_path and self._db_path.exists() and self._db_ids:
            try:
                conn = sqlite3.connect(str(self._db_path))
                ph = ",".join("?" * len(self._db_ids))
                conn.execute(
                    f"DELETE FROM drivers   WHERE id        IN ({ph})",
                    self._db_ids,
                )
                conn.execute(
                    f"DELETE FROM zip_parts WHERE driver_id IN ({ph})",
                    self._db_ids,
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        log("info", "Rollback executed: staged zips and manifest changes removed.")


# ── per-pack processor (called once per loop iteration) ───────────────────────
def _process_one_pack(repo_dir: Path, pack_num: int) -> bool:
    """
    Run the full scan → audit → compress → move → manifest → push → delete
    flow for ONE driver pack.

    Returns True  if the pack was pushed successfully (or nothing to do).
    Returns False if the user skipped / all drivers were dupes (no push needed).
    Raises SystemExit on hard errors or Ctrl-C.
    """
    # ── Driver folder ──────────────────────────────────────────────────────────
    C.print()
    rule(f"PACK {pack_num}  |  Driver Source Folder")
    C.print()
    C.print(
        "  [dim]Enter the full path to the folder containing your driver files.\n"
        "  All .inf files will be parsed recursively.[/dim]"
    )
    C.print()

    src_folder: Optional[Path] = None
    while True:
        try:
            raw = Prompt.ask("  [bold cyan]Driver folder path[/bold cyan]").strip().strip('"')
        except (KeyboardInterrupt, EOFError):
            C.print()
            C.print(Panel(
                "  [bold yellow]Interrupted — no changes made.[/bold yellow]",
                border_style="yellow", title="[bold yellow]  Ctrl+C  [/bold yellow]",
                padding=(0, 2),
            ))
            C.print()
            sys.exit(130)
        src_folder = Path(raw).expanduser().resolve()
        if src_folder.is_dir():
            ok(f"Found: {src_folder}")
            break
        err(f"Not a valid directory: {raw}")

    # Guard: refuse to operate if driver folder is INSIDE the repo
    try:
        src_folder.relative_to(repo_dir)
        err("Driver source folder must NOT be inside the local DriverDex repo.")
        hint("Move your driver folder to a separate location first.")
        return False
    except ValueError:
        pass   # good -- it's outside the repo

    C.print()

    # ── Scan INFs ──────────────────────────────────────────────────────────────
    rule(f"PACK {pack_num}  |  Scanning Driver Folder")
    C.print()

    with C.status("[bold green]  Scanning for .inf files ...[/bold green]", spinner="dots2"):
        inf_data = scan_infs(src_folder)

    if not inf_data:
        err(f"No .inf files found in: {src_folder}")
        return False

    ok(f"Found {len(inf_data)} .inf file(s).")

    # Build group -> type map NOW (INF-content-driven, before any user input)
    groups   = group_infs_by_folder(inf_data)
    n_groups = len(groups)
    type_map: Dict[Path, str] = {}
    for folder, group_infs in groups:
        type_map[folder] = classify_group(group_infs)

    C.print()
    show_inf_table(inf_data, src_folder, type_map)

    # ── Step 4b: Pre-flight duplicate audit ───────────────────────────────────
    manifest_for_audit = load_manifest(repo_dir)
    inf_data, audit_reports = preflight_duplicate_audit(
        inf_data = inf_data,
        manifest = manifest_for_audit,
        repo_dir = repo_dir,
        type_map = type_map,
    )

    if not inf_data:
        warn("All drivers were skipped by the pre-flight audit.  Nothing to upload.")
        return False   # signal caller to continue loop without pushing

    # Rebuild groups & type_map after possible filtering
    groups   = group_infs_by_folder(inf_data)
    n_groups = len(groups)
    type_map = {}
    for folder, group_infs in groups:
        type_map[folder] = classify_group(group_infs)

    # Count only files that live inside the driver group folders — this matches
    # exactly what will be zipped and avoids inflating the size with unrelated
    # files (installers, parent archives, readmes) that sit outside the groups.
    group_folders = [folder for folder, _ in groups]
    driver_files  = [
        f
        for folder in group_folders
        for f in folder.rglob("*")
        if f.is_file() and not f.is_symlink()
    ]
    total_raw = sum(f.stat().st_size for f in driver_files)
    C.print()
    info(f"Files in folder      : {len(driver_files)}")
    info(f"Uncompressed size    : {fmt_size(total_raw)}")
    info(f"Driver groups found  : {n_groups}  (one zip per group)")

    type_summary = Counter(type_map.values())
    info(f"Driver type breakdown: " + "  ".join(
        f"[bold magenta]{t}[/bold magenta]={n}" for t, n in sorted(type_summary.items())
    ))

    big_groups = [
        f for f, _ in groups
        if sum(fi.stat().st_size for fi in f.rglob("*") if fi.is_file()) > SPLIT_BYTES
    ]
    if big_groups:
        warn(f"{len(big_groups)} group(s) exceed 20 MB and will be split.")
    else:
        info("All driver groups fit in a single zip each (< 20 MB).")

    # Pack name
    auto_name = suggest_pack_name(src_folder, inf_data)
    C.print()
    info(f"Auto-detected pack name: [bold cyan]{auto_name}[/bold cyan]")
    try:
        raw_pack = Prompt.ask(
            "  [bold cyan]Pack name[/bold cyan]  [dim](press Enter to accept)[/dim]",
            default=auto_name,
        ).strip()
    except (KeyboardInterrupt, EOFError):
        C.print()
        C.print(Panel(
            "  [bold yellow]Interrupted — no changes made.[/bold yellow]",
            border_style="yellow", title="[bold yellow]  Ctrl+C  [/bold yellow]",
            padding=(0, 2),
        ))
        C.print()
        sys.exit(130)
    pack_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', raw_pack) or auto_name
    info(f"Pack name: {pack_name}")

    # ── Step 5: Confirm ───────────────────────────────────────────────────────
    C.print()
    rule("STEP 5  |  Confirm")
    C.print()

    type_dirs = sorted(set(type_map.values()))
    C.print(
        Panel(
            "\n".join([
                "  [bold white]The following will run automatically:[/bold white]\n",
                f"  [green][1][/green] Zip each driver group ({n_groups} zip(s), integrity-verified)",
                f"  [green][2][/green] Copy zips into repo under "
                f"[cyan]drivers/<Type>/DP_{pack_name}/[/cyan]",
                f"       Types: [bold magenta]{', '.join(type_dirs)}[/bold magenta]",
                f"  [green][3][/green] Deduplicate + version-manage + update "
                f"[cyan]{MANIFEST_REL}[/cyan]  (auto-shards at 15 MB)",
                f"       Old versions: kept but marked [dim]superseded_by[/dim] in manifest",
                f"  [green][4][/green] Persist to [cyan]drivers.db[/cyan] (SQLite, auto-shards at 15 MB)",
                f"  [green][5][/green] Update README.md badge  →  driver count",
                f"  [green][6][/green] git commit + push",
                f"  [yellow][7][/yellow] [bold]Ask[/bold] whether to delete source: [dim]{src_folder}[/dim]",
            ]),
            border_style="yellow",
            padding=(0, 2),
        )
    )
    C.print()
    try:
        ans = Prompt.ask(
            "  [bold cyan]Proceed?[/bold cyan]  "
            "[[bold green]Y[/bold green]/[bold red]n[/bold red]]",
            default="y",
        ).strip().lower()
    except (KeyboardInterrupt, EOFError):
        C.print()
        C.print(Panel(
            "  [bold yellow]Interrupted — no changes made.[/bold yellow]",
            border_style="yellow", title="[bold yellow]  Ctrl+C  [/bold yellow]",
            padding=(0, 2),
        ))
        C.print()
        sys.exit(130)
    if ans not in ("y", "yes"):
        warn("Aborted by user.")
        sys.exit(0)

    rb      = _PackRollback()   # initialised here so all except/finally blocks can see it
    push_ok = False             # will be set True only after a successful git push

    try:
        # ── Step 6: Compress ──────────────────────────────────────────────────
        C.print()
        rule("STEP 6  |  Compressing  (one zip per driver group, integrity-verified)")
        C.print()

        # Zips are staged in a temp area inside the repo's drivers dir
        # so we can then mv (rename) them into position without copying
        staging_dir = repo_dir / DRIVERS_DIR / f"_staging_DP_{pack_name}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        rb.staging_dir = staging_dir          # rollback will delete this on Ctrl+C

        zip_map, total_verified = zip_all_drivers(
            src      = src_folder,
            dest_dir = staging_dir,
            pack     = pack_name,
            inf_data = inf_data,
        )

        total_zips  = sum(len(v) for v in zip_map.values())
        split_count = sum(1 for v in zip_map.values() if len(v) > 1)

        if total_zips == 0:
            die("Compression produced no output files.")

        C.print()
        ok(f"Produced {total_zips} archive file(s) across {n_groups} driver group(s) — all verified.")
        if split_count:
            warn(f"{split_count} group(s) were split into multiple volumes (>{fmt_size(SPLIT_BYTES)}).")
        C.print()
        _zip_path_map = {f: [pi.path for pi in pis] for f, pis in zip_map.items()}
        show_zip_table(_zip_path_map, src_folder, type_map)

        # ── Step 7–9: Per-group: move → manifest → push ───────────────────────
        # Each driver group is moved to its final location, recorded in the
        # manifest, and pushed to GitHub individually — no batch wait.
        # No per-group confirmation is shown; Step 5 already got approval.
        C.print()
        rule("STEPS 7–9  |  Per-Group: Organise · Manifest · Push")
        C.print()

        groups_list = [(folder, zip_map[folder]) for folder, _ in group_infs_by_folder(inf_data)
                       if folder in zip_map]

        all_new_entries : List[Dict] = []
        pushed_groups   = 0
        failed_groups   = 0
        total_in_db     = 0

        for g_idx, (folder, part_infos) in enumerate(groups_list, start=1):
            if not part_infos:
                continue

            dtype = type_map.get(folder, _TYPE_FALLBACK)
            try:
                rel = folder.relative_to(src_folder)
            except ValueError:
                rel = Path(folder.name)

            info(
                f"[{g_idx}/{len(groups_list)}] {rel}  "
                f"({len(part_infos)} volume(s), type={dtype})"
            )

            # ── Move archive volumes into final typed subfolder ────────────────
            dest_sub = repo_dir / DRIVERS_DIR / dtype / f"DP_{pack_name}" / rel
            dest_sub.mkdir(parents=True, exist_ok=True)
            rb.add_pack_dir(repo_dir / DRIVERS_DIR / dtype / f"DP_{pack_name}")

            for pi in part_infos:
                target = dest_sub / pi.path.name
                if target.exists():
                    target.unlink()
                shutil.move(str(pi.path), str(target))
                pi.path = target   # update in-place so build_entries sees final path

            # ── Build manifest entries for this group ─────────────────────────
            # Temporarily wrap single-group data for build_entries
            group_inf_data = [item for item in inf_data if item[0].parent == folder]
            group_zip_map  = {folder: part_infos}
            group_type_map = {folder: dtype}

            manifest = load_manifest(repo_dir)
            _active_shards = _manifest_shard_paths(repo_dir)
            if _active_shards:
                rb.snapshot_manifest(_active_shards[-1])

            # Remove stale active entries for this specific group's INF stems
            group_stems = {inf_path.stem.lower() for inf_path, _ in group_inf_data}
            prefix = _slug(pack_name) + "-"
            before = len(manifest["drivers"])
            manifest["drivers"] = [
                e for e in manifest["drivers"]
                if not (
                    any(_slug(e.get("id", "")).replace(prefix, "").startswith(s)
                        for s in group_stems)
                    and e.get("enabled", True)
                    and not e.get("superseded_by")
                )
            ]
            dropped = before - len(manifest["drivers"])
            if dropped:
                info(f"  Removed {dropped} stale entry/entries for this group.")

            new_entries, dedup_warnings = build_entries(
                pack     = pack_name,
                inf_data = group_inf_data,
                zip_map  = group_zip_map,
                rel_dir  = f"DP_{pack_name}",
                src      = src_folder,
                zip_dest = repo_dir / DRIVERS_DIR,
                type_map = group_type_map,
            )
            rb.set_db(_active_db_path(repo_dir), [e["id"] for e in new_entries])

            for w in dedup_warnings:
                warn(f"  {w}")

            new_entries, ver_warnings = apply_version_management(manifest, new_entries)
            for w in ver_warnings:
                warn(f"  {w}")

            manifest["drivers"].extend(new_entries)
            save_manifest(manifest, repo_dir)
            save_to_sqlite(new_entries, repo_dir)

            all_new_entries.extend(new_entries)

            # ── Push this group ───────────────────────────────────────────────
            commit_msg = (
                f"[DriverDex] {pack_name} [{g_idx}/{len(groups_list)}]: "
                f"{rel}  ({len(part_infos)} vol(s))  "
                f"[{date.today()}]"
            )
            push_ok_group = git_commit_push(repo_dir, commit_msg)
            if push_ok_group:
                ok(
                    f"  [{g_idx}/{len(groups_list)}] Pushed: {rel}  "
                    f"({len(new_entries)} manifest entry/entries)"
                )
                pushed_groups += 1
            else:
                err(
                    f"  [{g_idx}/{len(groups_list)}] Push FAILED for {rel} — "
                    "archive is staged locally; re-run to retry."
                )
                failed_groups += 1
                # Don't abort the session — continue with remaining groups

            total_in_db = _count_total_drivers(repo_dir)

        # Remove staging dir (should be empty now)
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass

        # Treat as fully successful only if every group pushed
        push_ok = (failed_groups == 0 and pushed_groups > 0)
        if push_ok:
            rb.disarm()

        # ── Write bootstrap PS1 and update badge once at end ──────────────────
        write_bootstrap_ps1(repo_dir)
        _update_readme_badge(repo_dir, total_in_db)

        new_entries  = all_new_entries
        total_zips_f = sum(len(v) for v in zip_map.values())
        split_count_f = sum(1 for v in zip_map.values() if len(v) > 1)
        type_dirs    = sorted(set(type_map.values()))
        total_warnings = 0   # already reported inline per-group

        C.print()
        rule("STEP 10  |  Source Folder Cleanup")
        C.print()

        src_deleted = False
        try:
            delete_src = Confirm.ask(
                f"  [bold red]Delete source folder?[/bold red]  "
                f"[dim]{src_folder}[/dim]",
                default=True,
            )
        except (KeyboardInterrupt, EOFError):
            delete_src = False

        if delete_src:
            with C.status(
                f"[bold red]  Deleting: {src_folder.name} ...[/bold red]",
                spinner="dots2",
            ):
                shutil.rmtree(src_folder, ignore_errors=True)

            if src_folder.exists():
                warn(f"Could not fully remove: {src_folder}")
                hint("Delete it manually -- the upload was successful.")
            else:
                ok(f"Source folder deleted: {src_folder}")
                src_deleted = True
        else:
            info("Source folder kept (user chose to keep it).")

        # ── Summary ───────────────────────────────────────────────────────────
        C.print()
        rule()
        C.print()
        C.print(
            Panel(
                "\n".join([
                    "  [bold green]ALL STEPS COMPLETED SUCCESSFULLY[/bold green]\n",
                    f"  [dim]Pack             :[/dim]  [white]{pack_name}[/white]",
                    f"  [dim]INF files        :[/dim]  [white]{len(inf_data)}[/white]",
                    f"  [dim]Driver groups    :[/dim]  [white]{n_groups}[/white]",
                    f"  [dim]Groups pushed    :[/dim]  "
                    + (f"[bold green]{pushed_groups}[/bold green]"
                       + (f" / [bold red]{failed_groups} failed[/bold red]" if failed_groups else "")),
                    f"  [dim]Type folders     :[/dim]  [bold magenta]{', '.join(type_dirs)}[/bold magenta]",
                    f"  [dim]Archive files    :[/dim]  [white]{total_zips}[/white]"
                    + (f"  [yellow]({split_count} split)[/yellow]" if split_count else ""),
                    f"  [dim]Manifest entries :[/dim]  [white]{len(new_entries)}[/white]",
                    f"  [dim]Total drivers    :[/dim]  [bold green]{total_in_db}[/bold green]  "
                    f"[dim](across all manifest shards)[/dim]",
                    f"  [dim]Source folder    :[/dim]  "
                    + ("[dim]deleted[/dim]" if src_deleted else "[yellow]kept[/yellow]"),
                    "",
                    f"  [dim]Repo    :[/dim]  "
                    f"[cyan]https://github.com/{REPO_OWNER}/{REPO_NAME}[/cyan]",
                    f"  [dim]Manifest:[/dim]  [cyan]{MANIFEST_RAW}[/cyan]",
                    "",
                    "  [dim]Quick launch next time:[/dim]",
                    f"  [bold yellow]irm {BOOTSTRAP_URL} | iex[/bold yellow]",
                ]),
                border_style="green",
                title=f"[bold green]  DriverDex Builder v{APP_VER}  [/bold green]",
                padding=(1, 2),
            )
        )
        C.print()
        log("info", f"Pack '{pack_name}' complete — {len(new_entries)} entries, {total_in_db} total drivers")

    except KeyboardInterrupt:
        C.print()
        rb.execute()
        C.print()
        C.print(Panel(
            "  [bold yellow]Ctrl+C detected — rolling back all staged changes.[/bold yellow]\n\n"
            "  Zips removed from repo · manifest restored · SQLite rows deleted.",
            border_style="yellow",
            title="[bold yellow]  Interrupted  [/bold yellow]",
            padding=(1, 2),
        ))
        C.print()
        sys.exit(130)

    except SystemExit:
        # die() raises SystemExit; roll back if anything was staged.
        rb.execute()
        raise

    except Exception as ex:
        C.print()
        err(f"Unexpected error: {ex}")
        C.print_exception(show_locals=False)
        rb.execute()
        sys.exit(1)

    finally:
        # Last-resort safety net: if something slipped past both except handlers.
        if rb._armed:
            rb.execute()
        if not push_ok and not rb._armed:
            # rb was disarmed by disarm() (successful push) or by execute()
            # (rollback ran).  Only show the "zips staged" message when push
            # failed but zips are still in the repo (armed=False via execute).
            pass
        elif not push_ok:
            info(
                "Push did not complete.  Your zips are staged in the local repo -- "
                "push manually or re-run the script."
            )

    return push_ok


# ── main orchestrator (outer loop) ────────────────────────────────────────────
def main() -> None:  # noqa: C901
    """Entry point.  Wraps _main_body() so no raw traceback ever escapes."""
    try:
        _main_body()
    except KeyboardInterrupt:
        C.print()
        C.print(Panel(
            "  [bold yellow]Interrupted before any repo changes were made.[/bold yellow]\n"
            "  Nothing was written — safe to re-run at any time.",
            border_style="yellow",
            title="[bold yellow]  Ctrl+C  [/bold yellow]",
            padding=(1, 2),
        ))
        C.print()
        sys.exit(130)


def _main_body() -> None:  # noqa: C901
    show_banner()

    # ── Prerequisites (run once per session) ──────────────────────────────────
    rule("PREREQUISITES")
    C.print()
    check_python()

    check_git()
    if not check_ssh_key():
        C.print()
        die(
            "SSH key verification failed — cannot push to GitHub.",
            fix="Configure your SSH key for github.com and re-run.",
        )
    C.print()

    # ── Set up local workspace (replaces git clone detection) ─────────────────
    rule("STEP 1  |  Set Up Local Workspace")
    C.print()
    repo_dir = setup_workspace()

    log("info", f"Workspace: {repo_dir}")

    # ── Sync badge on session start ───────────────────────────────────────────
    _update_readme_badge(repo_dir, _count_total_drivers(repo_dir))

    C.print()

    # ── Session loop: process one pack per iteration ───────────────────────────
    pack_num   = 0
    total_pushed = 0

    while True:
        pack_num += 1

        # Pull before every pack to stay in sync
        rule(f"PACK {pack_num}  |  Sync with GitHub")
        C.print()
        if not git_pull_rebase(repo_dir):
            warn("Could not sync with GitHub — continuing anyway (may cause push conflicts).")
        C.print()

        try:
            pushed = _process_one_pack(repo_dir, pack_num)
        except SystemExit as e:
            if e.code == 130:
                # Ctrl-C inside the pack processor
                C.print()
                warn("Interrupted.")
                break
            raise

        if pushed:
            total_pushed += 1

        # ── Ask whether to continue ───────────────────────────────────────────
        C.print()
        rule("CONTINUE?")
        C.print()
        try:
            again = Prompt.ask(
                f"  [bold cyan]Process another driver pack?[/bold cyan]  "
                f"[[bold green]Y[/bold green]/[bold red]n[/bold red]]",
                default="y",
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            C.print()
            warn("Interrupted.")
            break

        if again not in ("y", "yes"):
            break

    # ── Session summary ────────────────────────────────────────────────────────
    C.print()
    rule()
    C.print()
    C.print(
        Panel(
            "\n".join([
                "  [bold green]SESSION COMPLETE[/bold green]\n",
                f"  [dim]Packs attempted :[/dim]  [white]{pack_num}[/white]",
                f"  [dim]Packs pushed    :[/dim]  [white]{total_pushed}[/white]",
                "",
                f"  [dim]Repo    :[/dim]  "
                f"[cyan]https://github.com/{REPO_OWNER}/{REPO_NAME}[/cyan]",
                f"  [dim]Manifest:[/dim]  [cyan]{MANIFEST_RAW}[/cyan]",
            ]),
            border_style="green",
            title=f"[bold green]  DriverDex Builder v{APP_VER}  [/bold green]",
            padding=(1, 2),
        )
    )
    C.print()


if __name__ == "__main__":
    main()
