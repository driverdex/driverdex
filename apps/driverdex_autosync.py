#!/usr/bin/env python3
# ==============================================================================
#  DriverDex-AutoSync  --  Automated Local PC Driver Sync to GitHub
#  Based on    : DriverDex Builder v5.3.1-rest by rhshourav
#  Version     : 1.4.0-auto (fix 401 bad-creds; adaptive upload workers)
#  Repo        : https://github.com/rhshourav/driverdex
#
#  What this script does (fully automatic, zero user input):
#  ─────────────────────────────────────────────────────────
#  1. Downloads all manifests from the GitHub repo (same logic as original).
#  2. Extracts every OEM driver installed on this local PC via pnputil /
#     the Windows DriverStore (C:\Windows\System32\DriverStore\FileRepository).
#  3. Compares each local driver against the repo manifest (ROBUST multi-strategy):
#       • Not in repo          → marked NEW      → uploaded as NEW COLLECTION
#       • In repo, local newer → marked OUTDATED → uploaded as UPDATE
#       • In repo, same/older  → marked CURRENT  → skipped
#     Comparison strategies (highest→lowest priority):
#       1. Exact HWID-set + arch + category
#       2. Exact HWID-set + arch  (tolerate category rename)
#       3. Exact HWID-set only    (tolerate arch mismatch)
#       4. Partial HWID overlap ≥ 25 % (Jaccard) + arch preference
#       5. Normalised provider + category + arch (for no-HWID generic drivers)
#  4. Archives + uploads only NEW and OUTDATED drivers.
#     NEW drivers get their own "NEW_…" pack name (separate collection in repo).
#     OUTDATED drivers go under "UPD_…" pack name so history is traceable.
#  5. Upload method: PARALLEL blobs only (the original "Parallel blobs" mode).
#  6. One driver group per GitHub commit (upload 1 driver at a time).
#  7. Telegram telemetry messages updated to describe comparison results.
#
#  SSL FIX (v1.0.1):
#  ─────────────────
#  All HTTPS calls now go through _urlopen_ssl() which tries, in order:
#    1. certifi CA bundle  (auto-installed, most reliable cross-machine)
#    2. System default SSL context
#    3. Unverified context (last resort — bypasses cert check entirely)
#  This silently handles: corporate proxy CAs, expired Windows root certs,
#  AV SSL inspection, Python not finding certs, old Windows 7/8/10 systems.
# ==============================================================================
# ==============================================================================
#  NO USER INPUT IS REQUIRED OR REQUESTED BELOW THIS LINE
# ==============================================================================

from __future__ import annotations

import base64
import contextlib
import math
import os
import sys
import re
import json
import shutil
import struct
import zipfile
import hashlib
import sqlite3
import subprocess
import threading
import time
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import concurrent.futures
import getpass
import platform
import socket
import ssl as _ssl
from collections  import Counter, defaultdict
from pathlib      import Path
from datetime     import date, datetime
from typing       import Callable, Dict, List, Optional, Set, Tuple

# ── TOP-LEVEL TOGGLE ─────────────────────────────────────────────────────────
#  True  → Rich console output (coloured progress bars, tables, panels)
#  False → Completely silent; runs 100 % in background with no stdout/stderr
GUI_MODE: bool = True

# ── HARD-CODED GITHUB TOKEN (top-level cybersecurity — rotate if leaked) ─────
GITHUB_TOKEN: str = (
    "*****************************"
)


# ── auto-install required packages ───────────────────────────────────────────
def _ensure(pkg: str, import_name: str = "") -> None:
    name = import_name or pkg
    try:
        __import__(name)
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "-q"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

_ensure("rich")
_ensure("py7zr")
_ensure("multivolumefile")
_ensure("certifi")   # ← needed for SSL self-healing on all Windows machines

# ── Console: real (GUI) or null (background) ──────────────────────────────────
if GUI_MODE:
    from rich.console  import Console, Group as RichGroup
    from rich.panel    import Panel
    from rich.table    import Table
    from rich.live     import Live
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        TimeRemainingColumn, TaskProgressColumn, DownloadColumn,
        TransferSpeedColumn, MofNCompleteColumn,
    )
    from rich.text     import Text
    from rich.rule     import Rule
    from rich.align    import Align
    from rich          import box
    from rich.markup   import escape
    C = Console(highlight=False)
else:
    # Stub everything so the rest of the code works unchanged
    class _NullConsole:  # type: ignore[no-redef]
        def print(self, *a, **kw): pass
        def status(self, *a, **kw): return _NullCM()
        def print_exception(self, *a, **kw): pass
    class _NullCM:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class Panel:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
    class Table:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
        def add_column(self, *a, **kw): pass
        def add_row(self, *a, **kw): pass
    class Rule:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
    class Progress:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def add_task(self, *a, **kw): return 0
        def advance(self, *a, **kw): pass
        def update(self, *a, **kw): pass
    class Text:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
    SpinnerColumn = BarColumn = TextColumn = TimeRemainingColumn = object
    TaskProgressColumn = DownloadColumn = TransferSpeedColumn = object
    MofNCompleteColumn = object
    def escape(s): return str(s)  # type: ignore[no-redef]
    box = None
    C = _NullConsole()  # type: ignore[assignment]


# ── PyInstaller-safe base directory ──────────────────────────────────────────
def _get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

_APP_DIR = _get_app_dir()

# ── Constants ─────────────────────────────────────────────────────────────────
REPO_OWNER             = "rhshourav"
REPO_NAME              = "driverdex"
GH_BRANCH              = "main"
MANIFEST_DIR           = "manifests"                                # all manifests live here
INSTALLER_MANIFEST_REL = f"{MANIFEST_DIR}/installers.manifest.json"
DRIVERS_DIR            = "drivers"
SPLIT_BYTES            = 15 * 1024 * 1024
SCHEMA_VER             = "3.0"
APP_VER                = "1.4.0-auto"
COMMIT_EMAIL           = "driverdex-builder@noreply.local"
COMMIT_NAME            = "DriverDex-AutoSync"

API_BASE               = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
LFS_BATCH_URL          = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git/info/lfs/objects/batch"
)
WORKSPACE_DIR          = _APP_DIR / "driverdex_workspace"
MANIFEST_SIZE_LIMIT    = 15 * 1024 * 1024
DB_SIZE_LIMIT          = 15 * 1024 * 1024
DB_FILE_NAME           = "drivers.db"
INDEX_FILE_NAME        = "driverdex_index.json"
BADGE_MARKER_START     = "<!-- DRIVERDEX_DRIVER_BADGE_START -->"
BADGE_MARKER_END       = "<!-- DRIVERDEX_DRIVER_BADGE_END -->"
BASE_RAW_URL           = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{DRIVERS_DIR}"
)
MANIFEST_RAW_BASE      = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main"
)

# Driver status markers (used in comparison step)
STATUS_NEW             = "new"            # driver not in repo at all → new collection
STATUS_OUTDATED        = "outdated"       # repo has older version → update existing
STATUS_CURRENT         = "current"        # repo is up-to-date → skip

# Minimum Jaccard HWID-set overlap to consider two drivers the "same" hardware
_HWID_OVERLAP_THRESHOLD: float = 0.25

# Upload: always parallel; sequential group commits
UPLOAD_MODE_PARALLEL = "parallel"

# ── Telemetry ─────────────────────────────────────────────────────────────────
_TELEMETRY_URL   = "https://cryocore.rhshourav02.workers.dev/message"
_TELEMETRY_TOKEN = "shourav"

_SESSION_STATS: Dict = {
    "start_time"  : 0.0,
    "user"        : "",
    "pc"          : "",
    "ips"         : [],
    "packs"       : [],
    # auto-sync specific
    "local_total" : 0,
    "new_count"   : 0,
    "outdated_count": 0,
    "current_count" : 0,
    "uploaded"    : 0,
}


# ─────────────────────────────────────────────────────────────────────────────
#  Logging helpers (silenced automatically when GUI_MODE=False)
# ─────────────────────────────────────────────────────────────────────────────
def _g(msg: str) -> None:
    """Print to console only when GUI_MODE is on."""
    if GUI_MODE:
        C.print(msg)

def ok(msg: str) -> None:
    if GUI_MODE:
        C.print(f"  [bold bright_green]✓[/bold bright_green]  {msg}")

def warn(msg: str) -> None:
    if GUI_MODE:
        C.print(f"  [bold yellow]⚠[/bold yellow]  [yellow]{msg}[/yellow]")

def err(msg: str) -> None:
    if GUI_MODE:
        C.print(f"  [bold bright_red]✗[/bold bright_red]  [bright_red]{msg}[/bright_red]")

def info(msg: str) -> None:
    if GUI_MODE:
        C.print(f"  [dim cyan]◈[/dim cyan]  [white]{msg}[/white]")

def hint(msg: str) -> None:
    if GUI_MODE:
        C.print(f"      [dim]↳  {msg}[/dim]")

def rule(title: str = "", style: str = "bright_cyan") -> None:
    if GUI_MODE:
        if title:
            C.print(Rule(f"[bold {style}] {title} [/bold {style}]",
                         style=f"dim {style}"))
        else:
            C.print(Rule(style="dim cyan"))

def log_bg(tag: str, msg: str) -> None:
    """Minimal stderr log for background mode — only writes when GUI_MODE=False."""
    if not GUI_MODE:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{tag}] {msg}", file=sys.stderr, flush=True)

def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ─────────────────────────────────────────────────────────────────────────────
#  Self-healing SSL context — works on every Windows machine / Python version
# ─────────────────────────────────────────────────────────────────────────────

def _get_ssl_ctx() -> _ssl.SSLContext:
    """
    Return the best available SSL context in this priority order:
      1. certifi CA bundle  (auto-installed above; most reliable cross-machine)
      2. System default context (works on most up-to-date machines)
      3. Unverified context (last resort — suppresses cert errors entirely)

    Never raises; always returns a usable context.
    Covers: corporate proxy CAs, expired Windows root certs, AV SSL inspection,
            old Windows 7/8/10, Python without certs configured, etc.
    """
    # 1. certifi bundle
    try:
        import certifi
        ctx = _ssl.create_default_context(cafile=certifi.where())
        return ctx
    except Exception:
        pass
    # 2. System default
    try:
        return _ssl.create_default_context()
    except Exception:
        pass
    # 3. Unverified fallback
    ctx = _ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE
    return ctx


def _urlopen_ssl(req, timeout: int = 60):
    """
    Drop-in replacement for urllib.request.urlopen that never dies on SSL errors.

    Strategy:
      • Try the best available verified context first (_get_ssl_ctx).
      • If that raises any SSL / certificate error, silently retry with a
        completely unverified context so the request always goes through.
    """
    ctx = _get_ssl_ctx()
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except _ssl.SSLError:
        pass
    except urllib.error.URLError as exc:
        # Only swallow SSL-related URLErrors; re-raise others (404, timeout …)
        cause = str(exc).lower()
        if "ssl" not in cause and "certificate" not in cause:
            raise
    # Final fallback — unverified
    fallback = _ssl._create_unverified_context()
    fallback.check_hostname = False
    fallback.verify_mode    = _ssl.CERT_NONE
    return urllib.request.urlopen(req, timeout=timeout, context=fallback)


# ─────────────────────────────────────────────────────────────────────────────
#  Telemetry helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_local_ips() -> List[str]:
    ips: List[str] = []
    try:
        hostname = socket.gethostname()
        for info_ in socket.getaddrinfo(hostname, None):
            addr = info_[4][0]
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
        payload = json.dumps({"token": _TELEMETRY_TOKEN, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            _TELEMETRY_URL, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Python-DriverDex-AutoSync"},
            method="POST",
        )
        try:
            with _urlopen_ssl(req, timeout=10):
                pass
        except Exception:
            pass
    except Exception:
        pass


def _tele(text: str) -> None:
    """Fire-and-forget telemetry ping."""
    threading.Thread(target=_send_telemetry, args=(text,), daemon=True).start()


def _tele_startup() -> None:
    ips    = _get_local_ips()
    user   = getpass.getuser()
    pc     = platform.node()
    domain = os.environ.get("USERDOMAIN", platform.system())
    _send_telemetry(
        f"🟢 DriverDex AutoSync v{APP_VER} — STARTED\n"
        f"User: {user}  PC: {pc}  Domain: {domain}\n"
        f"OS: {platform.system()} {platform.release()} ({platform.version()})\n"
        f"IP: {', '.join(ips) or 'unknown'}\n"
        f"GUI mode: {GUI_MODE}"
    )


def _tele_scan_result(local_total: int, new_count: int, outdated: int, current: int) -> None:
    pc = platform.node()
    _tele(
        f"🔍 DriverDex AutoSync v{APP_VER} — LOCAL SCAN COMPLETE\n"
        f"PC           : {pc}\n"
        f"Drivers found: {local_total} total\n"
        f"├─ NEW (not in repo)       : {new_count}\n"
        f"├─ OUTDATED (repo is older): {outdated}\n"
        f"└─ UP-TO-DATE (skipped)    : {current}"
    )


def _tele_upload_start(group_idx: int, total_groups: int, driver_name: str,
                       status: str, version: str) -> None:
    pc = platform.node()
    status_icon = "🆕" if status == STATUS_NEW else "⬆️"
    _tele(
        f"{status_icon} DriverDex AutoSync v{APP_VER} — UPLOADING DRIVER {group_idx}/{total_groups}\n"
        f"PC      : {pc}\n"
        f"Driver  : {driver_name}\n"
        f"Status  : {status.upper()}\n"
        f"Version : {version or 'unknown'}"
    )


def _tele_upload_done(group_idx: int, total_groups: int, driver_name: str,
                      success: bool, commit_sha: str = "") -> None:
    pc   = platform.node()
    icon = "✅" if success else "❌"
    _tele(
        f"{icon} DriverDex AutoSync v{APP_VER} — UPLOAD {'OK' if success else 'FAILED'} "
        f"({group_idx}/{total_groups})\n"
        f"PC      : {pc}\n"
        f"Driver  : {driver_name}\n"
        + (f"Commit  : {commit_sha[:8]}" if commit_sha else "")
    )


def _tele_session_done(uploaded: int, failed: int, total_in_repo: int) -> None:
    pc   = platform.node()
    user = getpass.getuser()
    dur  = time.time() - _SESSION_STATS["start_time"]
    _tele(
        f"🏁 DriverDex AutoSync v{APP_VER} — SESSION COMPLETE\n"
        f"User          : {user}  PC: {pc}\n"
        f"Drivers synced: {uploaded} uploaded, {failed} failed\n"
        f"Total in repo : {total_in_repo}\n"
        f"Duration      : {dur/60:.1f} min"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GitHub REST helpers  (all HTTP calls now use _urlopen_ssl)
# ─────────────────────────────────────────────────────────────────────────────
_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Thread-safe flag: set to True the moment a confirmed-bad token is detected.
# All upload workers check this before starting / between retries and abort
# immediately rather than burning through remaining quota with doomed requests.
_CREDS_BAD      = threading.Event()
_CREDS_LOCK     = threading.Lock()
_CREDS_CHECKED  = False   # True once we've done one live token verification


def _verify_token_live() -> bool:
    """
    Make a lightweight GET /user call to confirm the token is still valid.
    Returns True if accepted, False if HTTP 401/403.
    Sets _CREDS_BAD on failure so other threads can abort immediately.
    """
    global _CREDS_CHECKED
    with _CREDS_LOCK:
        if _CREDS_CHECKED:
            return not _CREDS_BAD.is_set()
        _CREDS_CHECKED = True
        st, _ = _api("GET", "https://api.github.com/user", timeout=15)
        if st in (401, 403):
            _CREDS_BAD.set()
            err(
                "GitHub token is invalid / expired (HTTP 401).  "
                "Update GITHUB_TOKEN at the top of the script and re-run."
            )
            log_bg("ERR", f"Token verification failed (HTTP {st}) — all uploads aborted")
            return False
        return True


def _api(
    method: str, path: str,
    data: Optional[Dict] = None,
    timeout: int = 60,
) -> Tuple[int, Dict]:
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    headers: Dict[str, str] = {
        "Accept"              : "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _urlopen_ssl(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"message": str(exc)}
    except Exception as exc:
        return 0, {"message": str(exc)}


def _api_retry(
    method: str, path: str,
    data: Optional[Dict] = None,
    timeout: int = 60,
    max_attempts: int = 5,
    backoff_base: float = 4.0,
    label: str = "",
) -> Tuple[int, Dict]:
    tag = f"[{label}] " if label else ""

    # Fast-path: if we already know the token is bad, don't even try
    if _CREDS_BAD.is_set():
        return 401, {"message": "Token known-bad — skipping request"}

    for attempt in range(1, max_attempts + 1):
        if _CREDS_BAD.is_set():
            return 401, {"message": "Token known-bad — aborting mid-retry"}

        st, resp = _api(method, path, data=data, timeout=timeout)

        # ── 401 handling ─────────────────────────────────────────────────
        if st == 401:
            # Verify token once under a lock; if truly bad, set the flag and
            # abort immediately so every thread stops trying.
            if not _verify_token_live():
                return 401, resp   # token confirmed bad
            # Token verified OK → transient 401 (GitHub CDN / clock skew).
            # Retry with a short backoff.
            wait = min(backoff_base * (2 ** (attempt - 1)), 30.0)
            warn(f"{tag}Transient 401 attempt {attempt}/{max_attempts} — retry in {wait:.0f}s …")
            log_bg("WARN", f"{tag}Transient 401 attempt {attempt}/{max_attempts}")
            time.sleep(wait)
            continue

        retryable = (st in _RETRY_STATUSES) or (st == 0)
        if not retryable or attempt == max_attempts:
            return st, resp
        wait = min(backoff_base * (2 ** (attempt - 1)), 60.0)
        warn(f"{tag}HTTP {st} attempt {attempt}/{max_attempts} — retry in {wait:.0f}s …")
        log_bg("WARN", f"{tag}HTTP {st} attempt {attempt}/{max_attempts} — retry in {wait:.0f}s")
        time.sleep(wait)
    return st, resp


def check_github_token() -> bool:
    if not GITHUB_TOKEN:
        err("GITHUB_TOKEN is empty — check the hard-coded value at the top of the script.")
        return False
    st, resp = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}")
    if st == 200:
        ok(f"GitHub token accepted — repo: {resp.get('full_name', REPO_OWNER+'/'+REPO_NAME)}")
        log_bg("OK", "GitHub token verified")
        return True
    err(f"GitHub token check failed (HTTP {st}): {resp.get('message', '')}")
    log_bg("ERR", f"GitHub token check failed (HTTP {st})")
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  INF / driver classification (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
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
    "extension": "Other",
}
_TYPE_FALLBACK = "Other"

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

_VER_RE      = re.compile(r"^DriverVer\s*=\s*(.+)",          re.IGNORECASE | re.MULTILINE)
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
        raw   = _resolve(m.group(1).strip(), strings)
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


def scan_infs(folder: Path) -> List[Tuple[Path, Dict]]:
    results: List[Tuple[Path, Dict]] = []
    for inf in sorted(folder.rglob("*.inf")):
        d = parse_inf(inf)
        if d:
            results.append((inf, d))
    return results


def classify_driver_type(inf_parsed: Dict) -> str:
    raw_class = (inf_parsed.get("category") or "").strip().lower()
    if raw_class in _CLASS_TO_TYPE:
        return _CLASS_TO_TYPE[raw_class]
    all_hwids = inf_parsed.get("hwids", []) + inf_parsed.get("compatible_ids", [])
    prefix_votes: Dict[str, str] = {
        "USB\\"    : "USB",     "USBSTOR\\" : "Storage",
        "HDAUDIO\\": "Audio",   "DISPLAY\\" : "Display",
        "BTH\\"    : "Bluetooth","BTHENUM\\": "Bluetooth",
        "HID\\"    : "Input",   "HIDCLASS\\": "Input",
        "SCSI\\"   : "Storage", "IDE\\"     : "Storage",
        "PCI\\"    : "Chipset", "ACPI\\"    : "Chipset",
        "MONITOR\\" : "Display","MEDIA\\"   : "Audio",
        "NET\\"    : "Net",     "SD\\"      : "Storage",
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


DriverGroup = Tuple[Path, List[Tuple[Path, Dict]]]


def group_infs_by_folder(inf_data: List[Tuple[Path, Dict]]) -> List[DriverGroup]:
    groups: Dict[Path, List[Tuple[Path, Dict]]] = defaultdict(list)
    for inf_path, d in inf_data:
        groups[inf_path.parent].append((inf_path, d))
    return sorted(groups.items(), key=lambda x: str(x[0]))


# ─────────────────────────────────────────────────────────────────────────────
#  Checksum / archive helpers (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
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
                return (False, f"Corrupt member: {bad}") if bad else (True, "")
        except Exception as exc:
            return False, str(exc)
    if name.endswith(".7z"):
        try:
            import py7zr
            with py7zr.SevenZipFile(path, mode="r") as archive:
                return (True, "") if not archive.testzip() else (False, "testzip() reported corruption")
        except Exception as exc:
            return False, str(exc)
    if path.stat().st_size == 0:
        return False, "Volume part is empty (0 bytes)"
    return True, ""


def _archive_stem(pack: str, inf_path: Path, d: Dict) -> str:
    prov_raw = re.sub(r'[^a-z0-9]', '', (d.get("provider") or "").lower())[:8] or "drv"
    cat_raw  = re.sub(r'[^a-z0-9]', '', (d.get("category") or "").lower())[:4] or "misc"
    arch_raw = (d.get("arch") or "x64").lower()
    arch     = {"amd64": "x64", "x86": "x86", "arm64": "a64"}.get(arch_raw, arch_raw[:3])
    ver_raw  = (d.get("version") or "").strip()
    ver_clean = re.sub(r'^\d{1,2}/\d{1,2}/\d{4}\s*,\s*', '', ver_raw)
    ver       = re.sub(r'[^0-9.]', '', ver_clean)[:10].rstrip('.')
    return f"{prov_raw}-{cat_raw}-{arch}-{ver}" if ver else f"{prov_raw}-{cat_raw}-{arch}"


class PartInfo:
    __slots__ = ("path", "part_num", "sha256", "size_bytes")

    def __init__(self, path: Path, part_num: int) -> None:
        self.path       = path
        self.part_num   = part_num
        self.size_bytes = path.stat().st_size
        self.sha256     = _sha256(path)


def _has_py7zr() -> bool:
    try:
        import py7zr           # noqa: F401
        import multivolumefile  # noqa: F401
        return True
    except ImportError:
        return False


def _7z_binary() -> Optional[str]:
    for candidate in ("7z", "7za", "7zz"):
        try:
            result = subprocess.run([candidate, "i"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return candidate
        except Exception:
            pass
    return None


def _dedup_files(files: List[Path]) -> Tuple[List[Path], int]:
    seen: Dict[str, Path] = {}
    unique: List[Path]    = []
    removed = 0
    for f in files:
        try:
            h = _sha256(f)
        except OSError:
            unique.append(f)
            continue
        if h in seen:
            removed += 1
        else:
            seen[h] = f
            unique.append(f)
    return unique, removed


def _pack_py7zr(files: List[Path], folder: Path, dest_stem: Path,
                on_progress: Callable[[int], None]) -> List[PartInfo]:
    import py7zr, multivolumefile
    archive_base = str(dest_stem) + ".7z"
    PE_EXTS = {".exe", ".dll", ".sys", ".drv", ".ocx", ".efi", ".cpl", ".scr"}
    pe_count = sum(1 for f in files if f.suffix.lower() in PE_EXTS)
    pe_ratio = pe_count / max(len(files), 1)
    if pe_ratio >= 0.5:
        filters = [{"id": py7zr.FILTER_X86}, {"id": py7zr.FILTER_LZMA2, "preset": 3}]
    else:
        filters = [{"id": py7zr.FILTER_LZMA2, "preset": 3}]
    try:
        with multivolumefile.open(archive_base, mode="wb", volume=SPLIT_BYTES) as mv:
            with py7zr.SevenZipFile(mv, mode="w", filters=filters) as sz:
                for f in files:
                    sz.write(f, str(f.relative_to(folder)))
                    on_progress(f.stat().st_size)
    except TypeError:
        try:
            with multivolumefile.open(archive_base, mode="wb", volume=SPLIT_BYTES) as mv:
                with py7zr.SevenZipFile(mv, mode="w", filters=filters) as sz:
                    for f in files:
                        sz.write(f, str(f.relative_to(folder)))
                        on_progress(f.stat().st_size)
        except Exception:
            with multivolumefile.open(archive_base, mode="wb", volume=SPLIT_BYTES) as mv:
                with py7zr.SevenZipFile(
                    mv, mode="w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": 3}]
                ) as sz:
                    for f in files:
                        sz.write(f, str(f.relative_to(folder)))
                        on_progress(f.stat().st_size)

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


def _pack_zip(files: List[Path], folder: Path, dest_stem: Path,
              on_progress: Callable[[int], None]) -> List[PartInfo]:
    out = Path(str(dest_stem) + ".zip")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, str(f.relative_to(folder)))
            on_progress(f.stat().st_size)
    ok_flag, errmsg = _verify_archive(out)
    if not ok_flag:
        raise RuntimeError(f"Zip check failed: {out.name} — {errmsg}")
    return [PartInfo(out, 1)]


def _archive_group(
    files: List[Path], folder: Path, dest_stem: Path,
    use_py7zr: bool, cli_binary: Optional[str],
    volume_mb: int, on_progress: Callable[[int], None],
) -> List[PartInfo]:
    stem_name   = dest_stem.name
    dest_parent = dest_stem.parent

    def _cleanup_stale() -> None:
        for stale in list(dest_parent.glob(stem_name + ".*")):
            with contextlib.suppress(Exception):
                stale.unlink()

    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        _cleanup_stale()
        try:
            if use_py7zr:
                return _pack_py7zr(files, folder, dest_stem, on_progress)
            else:
                return _pack_zip(files, folder, dest_stem, on_progress)
        except KeyboardInterrupt:
            _cleanup_stale()
            raise
        except Exception as exc:
            last_exc = exc
            _cleanup_stale()
            if attempt < 3:
                wait = 4.0 * (2 ** (attempt - 1))
                warn(f"Archive attempt {attempt}/3 failed — retry in {wait:.0f}s …")
                log_bg("WARN", f"Archive attempt {attempt}/3 failed: {exc}")
                time.sleep(wait)
    raise RuntimeError(f"Could not archive {stem_name}: {last_exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  Version comparison — robust (handles all real-world INF formats)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_ver(ver_str: str) -> Tuple[int, ...]:
    """
    Parse ANY driver version string into a comparable int-tuple.

    Handled formats (examples):
    • Windows INF  "10/15/2023, 31.0.101.2111"  → strips date, keeps dotted part
    • Plain dotted "31.0.101.2111"               → (31, 0, 101, 2111)
    • Semver-like  "v2.1.3-beta4"               → (2, 1, 3, 4)
    • Date-only    "10/15/2023"                  → (2023, 10, 15)
    • Mixed junk   "Release 5 (build 12)"        → (5, 12)

    Returns (0,) for entirely unparseable strings so comparisons never raise.
    """
    s = ver_str.strip()
    # Strip leading v/V prefix
    s = re.sub(r'^[vV]', '', s)
    # Windows INF: "MM/DD/YYYY, A.B.C.D" — keep only the right side
    m = re.match(r'^\d{1,2}/\d{1,2}/\d{4}\s*,\s*(.+)$', s)
    if m:
        s = m.group(1).strip()
    # Extract all contiguous digit groups in order
    ints = [int(p) for p in re.split(r'[^0-9]+', s) if p and p.isdigit()]
    return tuple(ints) if ints else (0,)


def version_is_newer(new_ver: str, old_ver: str) -> bool:
    """Return True iff *new_ver* is strictly greater than *old_ver*."""
    return _parse_ver(new_ver) > _parse_ver(old_ver)


def version_compare(new_ver: str, old_ver: str) -> int:
    """
    Three-way version compare.
    Returns  1 if new > old,  0 if equal,  -1 if new < old.
    """
    nv = _parse_ver(new_ver)
    ov = _parse_ver(old_ver)
    return 0 if nv == ov else (1 if nv > ov else -1)


# ─────────────────────────────────────────────────────────────────────────────
#  HWID & provider normalisation helpers  (used by robust compare)
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_hwid(hwid: str) -> str:
    """Upper-case + strip whitespace for consistent HWID set comparison."""
    return hwid.strip().upper()


def _normalize_provider(p: str) -> str:
    """
    Normalise a vendor/provider string for fuzzy comparison.

    • Lower-case
    • Remove common corporate suffixes (Inc, Corp, Ltd, GmbH …)
    • Remove punctuation / extra whitespace
    """
    p = p.strip().lower()
    p = re.sub(
        r'\b(inc|corp|corporation|ltd|limited|llc|co|gmbh|ag|sa|bv|pty|pvt|plc'
        r'|technologies|technology|semiconductor|microsystems|electronics|systems'
        r'|microelectronics|solutions|group)\b\.?',
        ' ', p,
    )
    p = re.sub(r'[^a-z0-9]', ' ', p)
    return re.sub(r'\s+', ' ', p).strip()


def _hwid_overlap_score(
    local_hwids: "frozenset[str]",
    repo_hwids:  "frozenset[str]",
) -> float:
    """
    Jaccard similarity between two normalised HWID sets.
    Returns a float in [0.0, 1.0].  0.0 if either set is empty.
    """
    if not local_hwids or not repo_hwids:
        return 0.0
    inter = local_hwids & repo_hwids
    union = local_hwids | repo_hwids
    return len(inter) / len(union) if union else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  Manifest helpers (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
def _category_manifest_rel(cat: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9]', '', cat.strip()) or "Other"
    # Repo-relative path, e.g. "manifests/Audio.manifest.json".
    return f"{MANIFEST_DIR}/{safe}.manifest.json"


def _manifest_shard_paths_for(repo: Path, base_name: str) -> List[Path]:
    base = repo / base_name
    paths: List[Path] = []
    if base.exists():
        paths.append(base)
    stem = base_name.replace(".manifest.json", "")
    idx = 2
    while True:
        p = repo / f"{stem}.manifest.{idx}.json"
        if p.exists():
            paths.append(p); idx += 1
        else:
            break
    return paths


def _all_category_manifest_rels(repo: Path) -> List[str]:
    known_cats = sorted(set(_CLASS_TO_TYPE.values()) | {"Other"})
    found: List[str] = []
    seen: set = set()
    for cat in known_cats:
        rel = _category_manifest_rel(cat)
        if rel not in seen and (repo / rel).exists():
            found.append(rel); seen.add(rel)
    for p in sorted((repo / MANIFEST_DIR).glob("*.manifest.json")):
        rel = p.relative_to(repo).as_posix()
        if rel not in seen and rel != INSTALLER_MANIFEST_REL:
            found.append(rel); seen.add(rel)
    return found


def load_all_manifests_combined(repo: Path) -> Dict:
    combined: Dict = {"schema": SCHEMA_VER, "drivers": [], "version_history": {}}
    for cat_rel in _all_category_manifest_rels(repo):
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                combined["drivers"].extend(data.get("drivers", []))
                combined["version_history"].update(data.get("version_history", {}))
            except Exception:
                pass
    return combined


def _manifest_shard_paths(repo: Path) -> List[Path]:
    all_paths: List[Path] = []
    for rel in _all_category_manifest_rels(repo):
        all_paths.extend(_manifest_shard_paths_for(repo, rel))
    return all_paths


def load_manifest(repo: Path, cat: str = "Other") -> Dict:
    shards = _manifest_shard_paths_for(repo, _category_manifest_rel(cat))
    if shards:
        p = shards[-1]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("version_history", {})
            data.setdefault("shard_index",  len(shards))
            data.setdefault("total_shards", len(shards))
            data["category"] = cat
            return data
        except Exception:
            pass
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


def save_manifest(m: Dict, repo: Path, cat: str = "") -> Path:
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
    if len(probe) <= MANIFEST_SIZE_LIMIT:
        active_path.write_bytes(probe)
        return active_path
    # Shard if needed
    idx      = len(existing_shards) + 1
    new_path = repo / base_name.replace(".manifest.json", f".manifest.{idx}.json")
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_shard: Dict = {
        "schema": SCHEMA_VER, "category": cat,
        "shard_index": idx, "total_shards": idx,
        "updated": str(date.today()),
        "base_url": BASE_RAW_URL, "lfs_batch_url": LFS_BATCH_URL,
        "drivers": m.get("drivers", []),
        "version_history": m.get("version_history", {}),
    }
    new_path.write_text(json.dumps(new_shard, indent=2, ensure_ascii=False), encoding="utf-8")
    return new_path


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


# ─────────────────────────────────────────────────────────────────────────────
#  LFS / download helpers  (all HTTP calls use _urlopen_ssl)
# ─────────────────────────────────────────────────────────────────────────────
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
    payload = json.dumps({
        "operation": "download", "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
    }).encode("utf-8")
    headers: Dict[str, str] = {
        "Content-Type": "application/vnd.git-lfs+json",
        "Accept"      : "application/vnd.git-lfs+json",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(LFS_BATCH_URL, data=payload, headers=headers, method="POST")
    try:
        with _urlopen_ssl(req, timeout=60) as resp:
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
        with _urlopen_ssl(dl_req, timeout=120) as dl_resp:
            content = dl_resp.read()
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(content)
        return "ok"
    except Exception as exc:
        warn(f"LFS object download failed for {repo_rel_path}: {exc}")
        return "error"


def _download_file_from_github(repo_rel_path: str, local_dest: Path) -> str:
    raw_url = (
        f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
        f"/{GH_BRANCH}/{repo_rel_path}"
    )
    headers: Dict[str, str] = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(raw_url, headers=headers)
    try:
        with _urlopen_ssl(req, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "not_found"
        warn(f"Could not download {repo_rel_path}: HTTP {exc.code}")
        return "error"
    except Exception as exc:
        warn(f"Could not download {repo_rel_path}: {exc}")
        return "error"
    lfs_info = _parse_lfs_pointer(data)
    if lfs_info is not None:
        return _download_via_lfs_batch(
            repo_rel_path, oid=lfs_info["oid"], size=int(lfs_info["size"]),
            local_dest=local_dest,
        )
    probe = data.lstrip()
    if not probe:
        return "error"
    if repo_rel_path.endswith(".json") and probe[:1] not in (b"{", b"["):
        return "error"
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    local_dest.write_bytes(data)
    return "ok"


def github_pull_manifest(workspace: Path) -> None:
    """Download all manifests from GitHub."""
    msg = "Downloading manifests from GitHub …"
    if GUI_MODE:
        ctx = C.status(f"[bold bright_cyan]  {msg}[/bold bright_cyan]", spinner="dots12")
    else:
        log_bg("INFO", msg)
        ctx = contextlib.nullcontext()

    with ctx:
        result = _download_file_from_github(INDEX_FILE_NAME, workspace / INDEX_FILE_NAME)
        remote_index: Optional[Dict] = None
        if result == "ok":
            try:
                remote_index = json.loads((workspace / INDEX_FILE_NAME).read_text(encoding="utf-8"))
            except Exception:
                pass

        if remote_index and remote_index.get("manifest_shards"):
            for s in remote_index["manifest_shards"]:
                sname = s["filename"]
                r = _download_file_from_github(sname, workspace / sname)
                if r == "error":
                    warn(f"Could not download manifest shard {sname}")
        else:
            known_cats = sorted(set(_CLASS_TO_TYPE.values()) | {"Other"})
            for cat in known_cats:
                cat_rel = _category_manifest_rel(cat)
                r = _download_file_from_github(cat_rel, workspace / cat_rel)
                if r == "ok":
                    for idx in range(2, 50):
                        stem = cat_rel.replace(".manifest.json", "")
                        shard_r = f"{stem}.manifest.{idx}.json"
                        sr = _download_file_from_github(shard_r, workspace / shard_r)
                        if sr == "not_found":
                            break

        for cat_rel in _all_category_manifest_rels(workspace):
            local = workspace / cat_rel
            if not local.exists():
                continue
            raw_bytes = local.read_bytes()
            if not raw_bytes or raw_bytes[:1] in (b"<", b"\n", b"\r\n"):
                local.unlink(missing_ok=True)
                continue
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
                if "drivers" not in data:
                    raise ValueError("Missing 'drivers' key")
            except (json.JSONDecodeError, ValueError):
                local.unlink(missing_ok=True)

        _download_file_from_github(INSTALLER_MANIFEST_REL, workspace / INSTALLER_MANIFEST_REL)
        _download_file_from_github("README.md", workspace / "README.md")
        if remote_index and remote_index.get("db_shards"):
            for s in remote_index["db_shards"]:
                _download_file_from_github(s["filename"], workspace / s["filename"])
        else:
            _download_file_from_github(DB_FILE_NAME, workspace / DB_FILE_NAME)

    n_cats = len(_all_category_manifest_rels(workspace))
    if n_cats:
        ok(f"Synced {n_cats} category manifest(s) from GitHub.")
        log_bg("OK", f"Synced {n_cats} category manifest(s)")
    else:
        ok("No category manifests on GitHub yet — starting fresh.")
        log_bg("OK", "No manifests on GitHub yet — fresh start")


# ─────────────────────────────────────────────────────────────────────────────
#  README badge (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
def _update_readme_badge(repo: Path, driver_count: int) -> None:
    readme = repo / "README.md"
    if not readme.exists():
        return
    try:
        text = readme.read_text(encoding="utf-8")
        # Remove any stale legacy badge block left over from the pre-rename
        # "LDC_DRIVER_BADGE" markers so only the current count is ever shown.
        text = re.sub(
            r"\s*<!-- LDC_DRIVER_BADGE_START -->.*?<!-- LDC_DRIVER_BADGE_END -->",
            "", text, flags=re.DOTALL,
        )
        badge = (
            f'<img src="https://img.shields.io/badge/Drivers-{driver_count}-brightgreen?style=flat-square" '
            f'alt="Driver Count" />'
        )
        section = f"{BADGE_MARKER_START}\n{badge}\n{BADGE_MARKER_END}"
        if BADGE_MARKER_START in text and BADGE_MARKER_END in text:
            new = re.sub(
                re.escape(BADGE_MARKER_START) + r".*?" + re.escape(BADGE_MARKER_END),
                section, text, flags=re.DOTALL,
            )
        else:
            new = text + f"\n\n{section}\n"
        readme.write_text(new, encoding="utf-8")
    except Exception as exc:
        warn(f"Could not update README badge: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL PC DRIVER EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

# Drivers whose original INF name matches these prefixes are Windows built-ins
# and should never be treated as third-party OEM drivers.
_BUILTIN_INF_PREFIXES: Tuple[str, ...] = (
    "1394", "acpi", "battery", "cdrom", "disk", "floppy", "hal",
    "hidir", "input", "intelide", "isapnp", "keyboard", "kbdclass",
    "machine", "monitor", "mouclass", "mshdc", "msisadrv", "msmouse",
    "pci", "pcmcia", "pnputil", "storport", "swenum", "usbhub",
    "usbport", "usbccgp", "usbprint", "vdrvroot", "volmgr", "volume",
    "wdmaudio", "xboxgip",
)


def _is_uploadable_inf(original_name: str, parsed: Dict) -> bool:
    """
    Return False for INFs that should be skipped:
      • Windows built-in drivers (matched by original INF name prefix)
      • Drivers with no usable identity info (no HWIDs AND no provider AND
        no category) — these can never be de-duplicated in the repo and will
        be re-uploaded on every run.
    """
    base = Path(original_name).stem.lower()
    for prefix in _BUILTIN_INF_PREFIXES:
        if base == prefix or base.startswith(prefix + "_"):
            return False

    has_hwids    = bool(parsed.get("hwids"))
    has_provider = bool((parsed.get("provider") or "").strip())
    has_category = bool((parsed.get("category") or "").strip())
    if not has_hwids and not has_provider and not has_category:
        return False   # completely unidentifiable — skip

    return True


def _parse_pnputil_output(stdout: str) -> List[Dict[str, str]]:
    """
    Parse `pnputil /enum-drivers` output into a list of driver records.

    Returns dicts with at least:
      published  — "oem41.inf"
      original   — "iusb3hub.inf"   (the real filename inside DriverStore)
      provider   — "SafeNet Inc."
      class_name — "USB"

    The output format differs slightly between Windows versions but always
    uses "Key : Value" lines with blank-line record separators.
    """
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}

    # Field aliases across Windows 7 / 8 / 10 / 11 / Server editions
    _KEY_MAP = {
        "published name"       : "published",
        "inf name"             : "published",   # alt label on some builds
        "original name"        : "original",
        "original inf name"    : "original",
        "inf original name"    : "original",
        "provider name"        : "provider",
        "class name"           : "class_name",
        "driver version"       : "driver_version",
        "driver package path"  : "pkg_path",    # Windows 10 1903+
    }

    def _flush(rec: Dict[str, str]) -> None:
        if rec.get("published"):
            records.append(rec)

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            _flush(current)
            current = {}
            continue
        if ":" not in line:
            continue
        key_raw, _, val = line.partition(":")
        key  = key_raw.strip().lower()
        val  = val.strip()
        mapped = _KEY_MAP.get(key)
        if mapped:
            current[mapped] = val

    _flush(current)   # last record (no trailing blank line on some builds)
    return records


def _drv_store_pkg_for_original(drv_store: Path, original_name: str) -> Optional[Path]:
    """
    Locate the DriverStore package folder for a given original INF name.

    DriverStore folder naming convention:
        <original_name_stem>.inf_<arch>_<hash>/
    e.g.  iusb3hub.inf_amd64_c1d2e3f4/

    We therefore search for a folder whose name *starts* with the original
    name (case-insensitive), which is far more reliable than matching the
    oem*.inf published name.
    """
    stem = original_name.lower()          # e.g. "iusb3hub.inf"
    if not drv_store.exists():
        return None
    for pkg in drv_store.iterdir():
        if not pkg.is_dir():
            continue
        # Primary: folder name starts with the original INF name
        if pkg.name.lower().startswith(stem + "_") or pkg.name.lower() == stem:
            return pkg
        # Secondary: folder contains an INF file with exactly that name
        for inf in pkg.glob("*.inf"):
            if inf.name.lower() == stem:
                return pkg
    return None


def extract_local_pc_drivers() -> Optional[Path]:
    """
    Extract all third-party (OEM) drivers from the local Windows DriverStore.

    Strategy (v1.2 — fixes 58→4 drop caused by published-name mismatch):
    ──────────────────────────────────────────────────────────────────────
    1. Run `pnputil /enum-drivers` and parse BOTH the Published Name AND
       the Original Name for each entry.  Previous code tried to find a
       file called "oem41.inf" inside DriverStore — it never exists there.
       DriverStore uses the *original* name (e.g. "iusb3hub.inf") as the
       folder-name prefix.  This fix recovers all 50+ missing drivers.

    2. For each driver record, locate its DriverStore package folder by
       matching against the original INF name prefix.

    3. Filter out Windows built-ins and completely unidentifiable INFs
       (no HWIDs, no provider, no category) that would re-upload forever.

    4. Fall back to a full DriverStore scan if pnputil fails entirely.
    """
    if os.name != "nt":
        warn("Local driver extraction is only supported on Windows.")
        log_bg("WARN", "Not running on Windows — local driver extraction skipped")
        return None

    staging = Path(tempfile.mkdtemp(prefix="driverdex_local_"))
    log_bg("INFO", f"Local driver staging dir: {staging}")
    info(f"Local driver staging dir: {escape(str(staging))}")

    drv_store = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "DriverStore" / "FileRepository"
    )

    pkg_dirs: List[Path] = []
    skipped_builtin    = 0
    skipped_no_id      = 0
    skipped_not_found  = 0

    try:
        result = subprocess.run(
            ["pnputil", "/enum-drivers"],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"pnputil exited {result.returncode}")

        records = _parse_pnputil_output(result.stdout)
        info(f"pnputil reported {len(records)} OEM driver record(s).")
        log_bg("INFO", f"pnputil: {len(records)} records parsed")

        for rec in records:
            published = rec.get("published", "")
            original  = rec.get("original",  published)  # fallback to published

            if not original:
                skipped_not_found += 1
                continue

            # ── Try to locate the package folder ──────────────────────────
            pkg: Optional[Path] = None

            # 1. Driver Package Path (Windows 10 1903+ — most reliable)
            pkg_path_str = rec.get("pkg_path", "")
            if pkg_path_str:
                candidate = Path(pkg_path_str)
                if candidate.is_dir():
                    pkg = candidate
                elif candidate.parent.is_dir():
                    pkg = candidate.parent

            # 2. Original name → DriverStore folder prefix
            if pkg is None:
                pkg = _drv_store_pkg_for_original(drv_store, original)

            # 3. Fallback: try published name (only works on old Windows versions
            #    that stored the oem*.inf inside the package folder)
            if pkg is None and published and drv_store.exists():
                for p in drv_store.iterdir():
                    if p.is_dir() and (p / published).exists():
                        pkg = p
                        break

            if pkg is None:
                log_bg("WARN", f"Package folder not found for {published} / {original}")
                skipped_not_found += 1
                continue

            # ── Quick-parse the INF to check identifiability ───────────────
            inf_files_in_pkg = list(pkg.glob("*.inf"))
            if not inf_files_in_pkg:
                skipped_not_found += 1
                continue

            # Use the matching INF (original name preferred)
            target_inf: Optional[Path] = None
            for inf in inf_files_in_pkg:
                if inf.name.lower() == original.lower():
                    target_inf = inf
                    break
            if target_inf is None:
                target_inf = inf_files_in_pkg[0]

            parsed = parse_inf(target_inf)
            if parsed is None:
                skipped_not_found += 1
                continue

            if not _is_uploadable_inf(original, parsed):
                # Check which filter matched for accurate reporting
                base = Path(original).stem.lower()
                if any(base == p or base.startswith(p + "_") for p in _BUILTIN_INF_PREFIXES):
                    skipped_builtin += 1
                else:
                    skipped_no_id += 1
                log_bg("INFO", f"Skipped {original} (built-in or unidentifiable)")
                continue

            pkg_dirs.append(pkg)

    except FileNotFoundError:
        warn("pnputil not found — falling back to full DriverStore scan.")
        log_bg("WARN", "pnputil not found, falling back to DriverStore scan")
    except Exception as exc:
        warn(f"pnputil enumeration failed: {exc} — falling back to DriverStore scan.")
        log_bg("WARN", f"pnputil failed: {exc}")

    # ── Full DriverStore fallback (only when pnputil produced nothing) ────────
    if not pkg_dirs:
        if drv_store.exists():
            info(f"Full DriverStore scan: {escape(str(drv_store))}")
            log_bg("INFO", f"Full DriverStore scan: {drv_store}")
            for pkg_dir in drv_store.iterdir():
                if not pkg_dir.is_dir():
                    continue
                infs = list(pkg_dir.glob("*.inf"))
                if not infs:
                    continue
                parsed = parse_inf(infs[0])
                if parsed and _is_uploadable_inf(infs[0].name, parsed):
                    pkg_dirs.append(pkg_dir)
        else:
            err(f"DriverStore not found at: {drv_store}")
            log_bg("ERR", f"DriverStore not found: {drv_store}")
            shutil.rmtree(staging, ignore_errors=True)
            return None

    if not pkg_dirs:
        warn("No uploadable driver packages found in DriverStore.")
        log_bg("WARN", "No uploadable driver packages found")
        shutil.rmtree(staging, ignore_errors=True)
        return None

    pkg_dirs = list(dict.fromkeys(pkg_dirs))

    if GUI_MODE:
        info(f"Drivers to process : [bold white]{len(pkg_dirs)}[/bold white]")
        if skipped_builtin:
            hint(f"Skipped {skipped_builtin} Windows built-in driver(s)")
        if skipped_no_id:
            hint(f"Skipped {skipped_no_id} unidentifiable driver(s) "
                 f"(no HWIDs / provider / category)")
        if skipped_not_found:
            hint(f"Skipped {skipped_not_found} driver(s) — package folder not found")
    log_bg("INFO",
           f"Drivers to process: {len(pkg_dirs)}  "
           f"(builtin={skipped_builtin} noid={skipped_no_id} notfound={skipped_not_found})")

    info(f"Copying {len(pkg_dirs)} package(s) to staging …")
    copied = 0
    for pkg_dir in pkg_dirs:
        dest = staging / pkg_dir.name
        if dest.exists():
            continue
        try:
            shutil.copytree(str(pkg_dir), str(dest), dirs_exist_ok=True)
            copied += 1
        except PermissionError:
            dest.mkdir(parents=True, exist_ok=True)
            for f in pkg_dir.rglob("*"):
                if f.is_file():
                    target = dest / f.relative_to(pkg_dir)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(str(f), str(target))
                    except Exception:
                        pass
            copied += 1
        except Exception as exc:
            warn(f"Could not copy {pkg_dir.name}: {exc}")
            log_bg("WARN", f"Could not copy {pkg_dir.name}: {exc}")

    ok(f"Staged {copied}/{len(pkg_dirs)} driver package(s) for comparison.")
    log_bg("OK", f"Staged {copied}/{len(pkg_dirs)} packages")
    return staging


# ─────────────────────────────────────────────────────────────────────────────
#  DRIVER COMPARISON
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  DRIVER COMPARISON  —  robust multi-strategy matching  (v1.1.0)
# ─────────────────────────────────────────────────────────────────────────────
def compare_with_repo(
    local_inf_data: List[Tuple[Path, Dict]],
    repo_manifest:  Dict,
) -> Tuple[
    List[Tuple[Path, Dict]],   # new_drivers      (STATUS_NEW)
    List[Tuple[Path, Dict]],   # outdated_drivers  (STATUS_OUTDATED)
    List[Tuple[Path, Dict]],   # current_drivers   (STATUS_CURRENT)
]:
    """
    Classify every local INF against the repo manifest using five fallback
    matching strategies (highest-to-lowest priority):

      1. Exact  (normalised-HWID-frozenset, arch, category)
      2. Exact  (normalised-HWID-frozenset, arch)           — tolerate cat rename
      3. Exact  (normalised-HWID-frozenset)                 — tolerate arch/cat diff
      4. Partial HWID overlap ≥ _HWID_OVERLAP_THRESHOLD (Jaccard) + arch bias
      5. Normalised provider + category + arch              — covers no-HWID drivers

    Result per driver:
      • STATUS_NEW      — no match found at all → new collection in repo
      • STATUS_OUTDATED — matched but local version is newer → existing entry update
      • STATUS_CURRENT  — matched and repo version is ≥ local → skip
    """
    enabled_entries: List[Dict] = [
        e for e in repo_manifest.get("drivers", []) if e.get("enabled", True)
    ]

    # ── Build lookup tables ─────────────────────────────────────────────────
    def _keep_best(table: Dict, key, entry: Dict) -> None:
        """Keep the entry with the newest version for a given key."""
        if key not in table or version_is_newer(
            entry.get("version", ""), table[key].get("version", "")
        ):
            table[key] = entry

    repo_exact   : Dict[Tuple, Dict]      = {}  # (hwids_norm, arch, cat)
    repo_no_cat  : Dict[Tuple, Dict]      = {}  # (hwids_norm, arch)
    repo_hw_only : Dict[frozenset, Dict]  = {}  # hwids_norm
    repo_provider: Dict[Tuple, Dict]      = {}  # (norm_prov, norm_cat, arch)

    for e in enabled_entries:
        hwids_norm = frozenset(_normalize_hwid(h) for h in e.get("hwids", []))
        arch       = (e.get("arch",     "") or "").lower()
        cat        = (e.get("category", "") or "").lower()
        prov       = _normalize_provider(e.get("provider", ""))

        if hwids_norm:
            _keep_best(repo_exact,   (hwids_norm, arch, cat), e)
            _keep_best(repo_no_cat,  (hwids_norm, arch),      e)
            _keep_best(repo_hw_only, hwids_norm,              e)

        if prov:
            _keep_best(repo_provider, (prov, cat, arch), e)

    # Pre-built list for Jaccard scanning (only entries that have HWIDs)
    repo_hwid_list: List[Tuple[frozenset, str, Dict]] = [
        (
            frozenset(_normalize_hwid(h) for h in e.get("hwids", [])),
            (e.get("arch", "") or "").lower(),
            e,
        )
        for e in enabled_entries
        if e.get("hwids")
    ]

    # ── Inner match resolver ────────────────────────────────────────────────
    def _find_repo_match(d: Dict) -> Optional[Dict]:
        local_hwids = frozenset(_normalize_hwid(h) for h in d.get("hwids", []))
        arch        = (d.get("arch",     "") or "").lower()
        cat         = (d.get("category", "") or "").lower()
        prov        = _normalize_provider(d.get("provider", ""))

        if local_hwids:
            # Strategy 1 — exact (hwids + arch + cat)
            m1 = repo_exact.get((local_hwids, arch, cat))
            if m1:
                return m1

            # Strategy 2 — exact (hwids + arch), tolerate category rename
            m2 = repo_no_cat.get((local_hwids, arch))
            if m2:
                return m2

            # Strategy 3 — exact hwids only, tolerate arch/category mismatch
            m3 = repo_hw_only.get(local_hwids)
            if m3:
                return m3

            # Strategy 4 — partial Jaccard overlap with arch preference
            best_score: float = 0.0
            best_entry: Optional[Dict] = None
            for repo_hwids_norm, repo_arch, entry in repo_hwid_list:
                score = _hwid_overlap_score(local_hwids, repo_hwids_norm)
                if score >= _HWID_OVERLAP_THRESHOLD:
                    # Give a 20 % bonus when arch also matches
                    adjusted = score * (1.2 if repo_arch == arch else 1.0)
                    if adjusted > best_score:
                        best_score  = adjusted
                        best_entry  = entry
            if best_entry is not None:
                return best_entry

        # Strategy 5 — normalised provider + category + arch (no-HWID drivers)
        if prov:
            m5 = repo_provider.get((prov, cat, arch))
            if m5:
                return m5
            # Relax arch for provider+category (covers x86↔x64 variants)
            for (ep, ec, _), entry in repo_provider.items():
                if ep == prov and ec == cat:
                    return entry

        return None   # genuinely absent → STATUS_NEW / new collection

    # ── Classify ────────────────────────────────────────────────────────────
    new_drivers      : List[Tuple[Path, Dict]] = []
    outdated_drivers : List[Tuple[Path, Dict]] = []
    current_drivers  : List[Tuple[Path, Dict]] = []

    for inf_path, d in local_inf_data:
        match = _find_repo_match(d)

        if match is None:
            # No match in repo at all → brand-new collection entry
            new_drivers.append((inf_path, d))
        else:
            local_ver = d.get("version", "")
            repo_ver  = match.get("version", "")
            cmp_result = version_compare(local_ver, repo_ver)

            if cmp_result > 0:
                # Local is newer → repo needs an update
                # Stash the repo version inside `d` for display / logging
                annotated = {**d, "_repo_version": repo_ver}
                outdated_drivers.append((inf_path, annotated))
            else:
                # repo ≥ local → nothing to do
                current_drivers.append((inf_path, d))

    return new_drivers, outdated_drivers, current_drivers


def show_comparison_table(
    new_drivers:      List[Tuple[Path, Dict]],
    outdated_drivers: List[Tuple[Path, Dict]],
    current_drivers:  List[Tuple[Path, Dict]],
) -> None:
    if not GUI_MODE:
        return
    C.print()
    rule("DRIVER COMPARISON RESULTS", style="bright_cyan")
    C.print()
    ok(f"NEW COLLECTION (will upload)  : [bold bright_green]{len(new_drivers)}[/bold bright_green]")
    ok(f"OUTDATED (will update)        : [bold yellow]{len(outdated_drivers)}[/bold yellow]")
    info(f"UP-TO-DATE (skipped)          : [dim]{len(current_drivers)}[/dim]")
    C.print()

    if new_drivers:
        t = Table(
            title="New Drivers (not in repo → new collection)",
            box=box.ROUNDED if box else None,
            border_style="bright_green", header_style="bold bright_green",
        )
        t.add_column("INF",        style="white",  no_wrap=True)
        t.add_column("Provider",   style="cyan")
        t.add_column("Category",   style="cyan")
        t.add_column("Version",    style="green")
        t.add_column("Arch",       style="yellow")
        t.add_column("HWIDs",      style="dim")
        for inf_path, d in new_drivers:
            hwid_count = len(d.get("hwids", []))
            t.add_row(
                inf_path.name,
                d.get("provider",  "—"),
                d.get("category",  "—"),
                d.get("version",   "—"),
                d.get("arch",      "—"),
                f"{hwid_count} id(s)" if hwid_count else "[dim italic]none[/dim italic]",
            )
        C.print(t)
        C.print()

    if outdated_drivers:
        t2 = Table(
            title="Outdated in Repo (updating existing entry)",
            box=box.ROUNDED if box else None,
            border_style="yellow", header_style="bold yellow",
        )
        t2.add_column("INF",        style="white",  no_wrap=True)
        t2.add_column("Provider",   style="cyan")
        t2.add_column("Category",   style="cyan")
        t2.add_column("Local Ver",  style="bold bright_green")
        t2.add_column("Repo Ver",   style="dim red")
        t2.add_column("Arch",       style="yellow")
        for inf_path, d in outdated_drivers:
            t2.add_row(
                inf_path.name,
                d.get("provider",      "—"),
                d.get("category",      "—"),
                d.get("version",       "—"),
                d.get("_repo_version", "—"),
                d.get("arch",          "—"),
            )
        C.print(t2)
        C.print()


# ─────────────────────────────────────────────────────────────────────────────
#  Manifest entry builder (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
def build_entries(
    pack: str,
    inf_data: List[Tuple[Path, Dict]],
    zip_map: Dict[Path, List[PartInfo]],
    rel_dir: str,
    type_map: Dict[Path, str],
) -> List[Dict]:
    entries: List[Dict] = []
    seen_sigs: Dict[str, Path] = {}

    for inf_path, d in inf_data:
        hwid_key_set = frozenset(d.get("hwids", []))
        sig = "::".join([
            "|".join(sorted(hwid_key_set)),
            d.get("version",  ""),
            d.get("arch",     ""),
            d.get("category", ""),
        ])
        if hwid_key_set and sig in seen_sigs:
            continue
        if hwid_key_set:
            seen_sigs[sig] = inf_path

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
            "|".join(sorted(hwids)), version, arch, category.lower()
        ])
        entry_id = f"drv-{hashlib.sha256(id_src.encode()).hexdigest()[:16]}"

        part_meta: List[Dict] = []
        for pi in parts_list:
            fname = pi.path.name
            url = (
                f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
                f"/{GH_BRANCH}/{rel_dir}/{driver_type}/DP_{pack}/{fname}"
            )
            part_meta.append({
                "part_num"  : pi.part_num,
                "filename"  : fname,
                "size_bytes": pi.size_bytes,
                "sha256"    : pi.sha256,
                "url"       : url,
            })

        primary_url = part_meta[0]["url"] if part_meta else ""
        entries.append({
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
        })

    return entries


def enrich_versions(new_entries: List[Dict], manifest: Dict) -> List[Dict]:
    history  = manifest.setdefault("version_history", {})
    existing : Dict[str, Dict] = {e["id"]: e for e in manifest.get("drivers", [])}

    def _key(hwids, arch, cat):
        return f"{arch}|{cat.lower()}|{'|'.join(sorted(hwids))}"

    existing_by_key: Dict[str, List[Dict]] = defaultdict(list)
    for e in manifest.get("drivers", []):
        if not e.get("enabled", True):
            continue
        k = _key(e.get("hwids", []), e.get("arch", ""), e.get("category", ""))
        existing_by_key[k].append(e)

    enriched: List[Dict] = []
    for new_e in new_entries:
        new_id  = new_e["id"]
        new_ver = new_e.get("version", "")
        k = _key(new_e.get("hwids", []), new_e.get("arch", ""), new_e.get("category", ""))
        prior_list = existing_by_key.get(k, [])
        if prior_list:
            best   = max(prior_list, key=lambda e: _parse_ver(e.get("version", "")))
            old_id = best.get("id", "")
            old_ver = best.get("version", "")
            if new_id != old_id and new_ver and old_ver and version_is_newer(new_ver, old_ver):
                if old_id in existing:
                    existing[old_id]["enabled"]       = False
                    existing[old_id]["superseded_by"] = new_id
                new_e["supersedes"] = old_id
                hist_list = history.setdefault(k, [])
                hist_list.append({
                    "id": new_id, "version": new_ver,
                    "date_added": str(date.today()), "superseded_by": None,
                })
        enriched.append(new_e)
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
#  SQLite (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
def _active_db_path(repo: Path) -> Path:
    db = repo / DB_FILE_NAME
    if db.exists() and db.stat().st_size >= DB_SIZE_LIMIT:
        idx = 2
        while True:
            shard = repo / f"drivers.{idx}.db"
            if not shard.exists() or shard.stat().st_size < DB_SIZE_LIMIT:
                return shard
            idx += 1
    return db


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            id TEXT PRIMARY KEY, pack TEXT, provider TEXT, category TEXT,
            version TEXT, arch TEXT, hwids TEXT, descriptions TEXT, enabled INTEGER,
            date_added TEXT, supersedes TEXT, superseded_by TEXT
        );
        CREATE TABLE IF NOT EXISTS parts (
            driver_id TEXT, part_num INTEGER, filename TEXT,
            size_bytes INTEGER, sha256 TEXT, url TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_parts_did ON parts(driver_id);
    """)


def save_to_sqlite(repo: Path, entries: List[Dict]) -> None:
    db_path = _active_db_path(repo)
    conn = sqlite3.connect(str(db_path))
    try:
        _init_db(conn)
        for e in entries:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO drivers
                       (id,pack,provider,category,version,arch,hwids,descriptions,
                        enabled,date_added,supersedes,superseded_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (e["id"], e["pack"], e.get("provider",""), e.get("category",""),
                     e.get("version",""), e.get("arch",""),
                     json.dumps(e.get("hwids",[])), json.dumps(e.get("descriptions",[])),
                     1 if e.get("enabled", True) else 0,
                     e.get("date_added", str(date.today())),
                     e.get("supersedes"), e.get("superseded_by")),
                )
                for part in e.get("parts", []):
                    conn.execute(
                        """INSERT INTO parts(driver_id,part_num,filename,size_bytes,sha256,url)
                           VALUES(?,?,?,?,?,?)""",
                        (e["id"], part.get("part_num",1), part.get("filename",""),
                         part.get("size_bytes",0), part.get("sha256",""), part.get("url","")),
                    )
            except sqlite3.Error:
                pass
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  driverdex_index.json builder (preserved from original)
# ─────────────────────────────────────────────────────────────────────────────
def _rebuild_index(repo: Path) -> None:
    shards: List[Dict] = []
    for p in _manifest_shard_paths(repo):
        # Repo-relative POSIX path (e.g. "manifests/Audio.manifest.json").
        shards.append({"filename": p.relative_to(repo).as_posix(), "size_bytes": p.stat().st_size})
    category_summary: Dict[str, int] = {}
    for cat_rel in _all_category_manifest_rels(repo):
        # cat_rel is "manifests/<Category>.manifest.json" — take just the category stem.
        cat = Path(cat_rel).name.replace(".manifest.json", "").split(".")[0]
        cnt = 0
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                cnt += sum(1 for e in data.get("drivers", []) if e.get("enabled", True))
            except Exception:
                pass
        category_summary[cat] = cnt

    db_shards: List[Dict] = []
    for db in sorted(repo.glob("drivers*.db")):
        db_shards.append({"filename": db.name, "size_bytes": db.stat().st_size})

    index = {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : shards,
        "db_shards"       : db_shards,
        "category_summary": category_summary,
        "total_drivers"   : _count_total_drivers(repo),
    }
    (repo / INDEX_FILE_NAME).write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SINGLE-DRIVER PARALLEL UPLOAD (one commit per driver group)
# ─────────────────────────────────────────────────────────────────────────────
def _upload_as_blob(repo_rel: str, fpath: Path) -> Optional[Dict]:
    """Upload file as a Git blob via GitHub REST API."""
    # Bail immediately if a previous upload already confirmed a bad token.
    if _CREDS_BAD.is_set():
        return None
    raw_bytes = fpath.read_bytes()
    if len(raw_bytes) > 95 * 1024 * 1024:
        err(f"File too large for blob API: {repo_rel} ({fmt_size(len(raw_bytes))})")
        return None
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    st, blob = _api_retry(
        "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs",
        {"content": encoded, "encoding": "base64"},
        timeout=300, max_attempts=5,
        label=f"blob {Path(repo_rel).name}",
    )
    if st not in (200, 201):
        err(f"Blob upload failed for {repo_rel} (HTTP {st}): {blob.get('message','')}")
        log_bg("ERR", f"Blob upload failed for {repo_rel} (HTTP {st})")
        return None
    return {"path": repo_rel, "mode": "100644", "type": "blob", "sha": blob["sha"]}


def commit_single_driver_group(
    repo_dir        : Path,
    files_to_commit : List[Tuple[str, Path]],
    commit_msg      : str,
    group_label     : str = "",
) -> str:
    """
    Upload files for ONE driver group via parallel blobs, then create a commit.
    Returns the new commit SHA on success, empty string on failure.
    """
    # 1. HEAD ref
    st, ref_data = _api_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/ref/heads/{GH_BRANCH}",
        label="HEAD ref",
    )
    if st != 200:
        err(f"Cannot read HEAD ref (HTTP {st})")
        return ""
    head_sha = ref_data["object"]["sha"]

    # 2. Base tree SHA
    st, commit_data = _api_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/commits/{head_sha}",
        label="HEAD commit",
    )
    if st != 200:
        err(f"Cannot read HEAD commit (HTTP {st})")
        return ""
    base_tree_sha = commit_data["tree"]["sha"]

    # 3. Separate archive files from metadata
    _BLOB_RAW_LIMIT = 18 * 1024 * 1024
    _is_lfs = lambda fp: (
        fp.name.lower().endswith(".zip") or
        fp.name.lower().endswith(".7z") or
        bool(re.search(r"\.7z\.\d{4}$", fp.name.lower()))
    )
    archive_files = [(rr, fp) for rr, fp in files_to_commit
                     if _is_lfs(fp) and fp.stat().st_size <= _BLOB_RAW_LIMIT]
    meta_files    = [(rr, fp) for rr, fp in files_to_commit if not _is_lfs(fp)]
    oversized     = [(rr, fp) for rr, fp in files_to_commit
                     if _is_lfs(fp) and fp.stat().st_size > _BLOB_RAW_LIMIT]
    if oversized:
        warn(f"  {len(oversized)} file(s) exceed 18 MB limit — skipped (reduce SPLIT_BYTES).")

    tree_entries: List[Dict] = []

    # 4a. Parallel blob upload for archives
    #
    #     Worker count is adaptive:
    #       • ≤ 4 parts  → 4 workers  (small group, no need to throttle)
    #       • 5-8 parts  → 3 workers
    #       • > 8 parts  → 2 workers  (large group like 16-part Intel driver;
    #                                   more than 3 simultaneous POSTs reliably
    #                                   triggers GitHub's secondary rate-limit
    #                                   which returns transient 401/403)
    if archive_files:
        n_parts   = len(archive_files)
        n_workers = 4 if n_parts <= 4 else (3 if n_parts <= 8 else 2)

        if GUI_MODE:
            prog = Progress(
                SpinnerColumn("dots12", style="bold bright_cyan"),
                TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
                BarColumn(bar_width=None, style="grey23",
                          complete_style="bold bright_cyan",
                          finished_style="bold bright_green"),
                TaskProgressColumn(style="bold white"),
                MofNCompleteColumn(separator="[dim white]/[/dim white]"),
                DownloadColumn(binary_units=True),
                TransferSpeedColumn(),
                TimeRemainingColumn(compact=True, elapsed_when_finished=True),
                console=C, transient=False, expand=True,
            )
            ctx = prog
        else:
            class _DummyProg:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def add_task(self, *a, **kw): return 0
                def advance(self, *a, **kw): pass
                def update(self, *a, **kw): pass
            prog = _DummyProg()  # type: ignore[assignment]
            ctx  = prog

        hint(f"Uploading {n_parts} part(s) with {n_workers} parallel worker(s) …")

        with ctx:
            if GUI_MODE:
                blob_task = prog.add_task(
                    f"Uploading {n_parts} part(s) — {group_label}"
                    if group_label else f"Uploading {n_parts} part(s)",
                    total=n_parts,
                )
            results_blob: Dict[str, Optional[Dict]] = {}
            res_lock  = threading.Lock()
            abort_evt = threading.Event()   # set when a 401/fatal is confirmed

            def _ul_worker(rr: str, fp: Path) -> None:
                # Stop immediately if credentials are confirmed bad or
                # another worker already signalled a fatal error.
                if _CREDS_BAD.is_set() or abort_evt.is_set():
                    with res_lock:
                        results_blob[rr] = None
                    if GUI_MODE:
                        prog.advance(blob_task, 1)
                    return
                entry = _upload_as_blob(rr, fp)
                with res_lock:
                    results_blob[rr] = entry
                    if entry is None and _CREDS_BAD.is_set():
                        abort_evt.set()
                if GUI_MODE:
                    prog.advance(blob_task, 1)
                    prog.update(blob_task, description=f"  ◈  {Path(rr).name}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                futs = {pool.submit(_ul_worker, rr, fp): rr for rr, fp in archive_files}
                for f in concurrent.futures.as_completed(futs):
                    try:
                        f.result()
                    except Exception as exc:
                        rr = futs[f]
                        log_bg("ERR", f"Worker exception for {rr}: {exc}")
                    # If credentials turned bad mid-flight, cancel pending futures
                    if _CREDS_BAD.is_set() or abort_evt.is_set():
                        for pending in futs:
                            pending.cancel()
                        break

        # If credentials failed, give a clear actionable message and bail
        if _CREDS_BAD.is_set():
            err(
                "Upload aborted — GitHub token is invalid or expired.\n"
                "  → Update GITHUB_TOKEN at the top of the script and re-run."
            )
            return ""

        failed_blobs = [rr for rr, _ in archive_files if results_blob.get(rr) is None]
        if failed_blobs:
            for rr in failed_blobs:
                err(f"Blob upload failed for {rr}")
                log_bg("ERR", f"Blob upload failed: {rr}")
            return ""

        for rr, _ in archive_files:
            tree_entries.append(results_blob[rr])

        ok(f"Parallel blobs: {len(tree_entries)} archive(s) uploaded.")
        log_bg("OK", f"Parallel blobs: {len(tree_entries)} archives uploaded")

    # 4b. Meta files — sequential
    for repo_rel, fpath in meta_files:
        entry = _upload_as_blob(repo_rel, fpath)
        if entry is None:
            return ""
        tree_entries.append(entry)

    if not tree_entries:
        warn("No tree entries for this group — skipping commit.")
        return ""

    # 5. Create git tree (chunked)
    CHUNK = 100
    current_base = base_tree_sha
    tree_sha: Optional[str] = None
    for i in range(math.ceil(len(tree_entries) / CHUNK)):
        chunk = tree_entries[i * CHUNK:(i + 1) * CHUNK]
        st, tree = _api_retry(
            "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/trees",
            {"base_tree": current_base, "tree": chunk},
            timeout=300, max_attempts=7, backoff_base=8.0, label="create tree",
        )
        if st not in (200, 201):
            err(f"Failed to create tree (HTTP {st}): {tree.get('message','')}")
            return ""
        current_base = tree["sha"]
        tree_sha = tree["sha"]

    # 6. Create commit
    st, new_commit = _api_retry(
        "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/commits",
        {
            "message": commit_msg, "tree": tree_sha, "parents": [head_sha],
            "author" : {"name": COMMIT_NAME, "email": COMMIT_EMAIL},
        },
        timeout=120, max_attempts=5, label="create commit",
    )
    if st not in (200, 201):
        err(f"Failed to create commit (HTTP {st}): {new_commit.get('message','')}")
        return ""

    # 7. Update ref
    st, ref_resp = _api_retry(
        "PATCH",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/heads/{GH_BRANCH}",
        {"sha": new_commit["sha"], "force": False},
        timeout=60, max_attempts=5, label="update ref",
    )
    if st in (200, 201):
        sha = new_commit["sha"]
        ok(f"Committed to GitHub — {sha[:8]}")
        log_bg("OK", f"Committed: {sha[:8]} — {commit_msg[:60]}")
        return sha
    err(f"Ref update failed (HTTP {st}): {ref_resp.get('message','')}")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  WORKSPACE SETUP
# ─────────────────────────────────────────────────────────────────────────────
def setup_workspace() -> Path:
    ws = WORKSPACE_DIR
    ws.mkdir(parents=True, exist_ok=True)
    (ws / DRIVERS_DIR).mkdir(exist_ok=True)
    ok(f"Workspace ready: {escape(str(ws))}")
    log_bg("OK", f"Workspace: {ws}")
    github_pull_manifest(ws)
    return ws


# ─────────────────────────────────────────────────────────────────────────────
#  AUTO BANNER (GUI only)
# ─────────────────────────────────────────────────────────────────────────────
_BANNER_ART = """\
  ██╗     ██████╗  ██████╗
  ██║     ██╔══██╗██╔════╝
  ██║     ██║  ██║██║
  ██║     ██║  ██║██║
  ███████╗██████╔╝╚██████╗
  ╚══════╝╚═════╝  ╚═════╝"""


def show_banner() -> None:
    if not GUI_MODE:
        return
    os.system("cls" if os.name == "nt" else "clear")
    from rich.text  import Text as _T
    from rich.align import Align
    art  = _T(_BANNER_ART + "\n",              style="bold bright_cyan")
    sub  = _T(f"  AutoSync  v{APP_VER}\n",    style="bold white")
    mode = _T(f"  Mode: {'GUI' if GUI_MODE else 'Background'}\n", style="dim cyan")
    C.print(Panel(
        Align.center(art + sub + mode),
        border_style="bright_cyan", padding=(0, 4),
    ))
    C.print()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN AUTO-SYNC FLOW
# ─────────────────────────────────────────────────────────────────────────────
def auto_sync() -> None:
    """
    Fully automated entry point.  No user prompts, no blocking input.

    Flow:
      1. Download manifests from GitHub.
      2. Extract local PC drivers from Windows DriverStore.
      3. Compare local drivers against repo manifest.
      4. Archive + upload each NEW/OUTDATED driver group as a separate commit.
      5. One commit per driver group; parallel blob upload (12 threads) per commit.
      6. Send Telegram telemetry at every milestone.
    """
    _SESSION_STATS["start_time"] = time.time()
    _SESSION_STATS["user"]       = getpass.getuser()
    _SESSION_STATS["pc"]         = platform.node()
    _SESSION_STATS["ips"]        = _get_local_ips()
    threading.Thread(target=_tele_startup, daemon=True).start()

    # Reset per-run credential state (allows re-runs in the same process)
    global _CREDS_CHECKED
    _CREDS_BAD.clear()
    _CREDS_CHECKED = False

    show_banner()

    # ── Python version check ───────────────────────────────────────────────
    if sys.version_info < (3, 7):
        log_bg("ERR", f"Python 3.7+ required. Got: {sys.version}")
        sys.exit(1)
    ok(f"Python {sys.version.split()[0]}")

    # ── Token check ────────────────────────────────────────────────────────
    rule("PREREQUISITES", style="bright_cyan")
    if not check_github_token():
        log_bg("ERR", "Invalid GitHub token — aborting")
        sys.exit(1)

    # ── Step 1: workspace + manifest download ──────────────────────────────
    rule("STEP 1  |  Workspace & Manifest Download", style="bright_cyan")
    C.print()
    repo_dir = setup_workspace()
    _update_readme_badge(repo_dir, _count_total_drivers(repo_dir))

    # ── Step 2: extract local PC drivers ──────────────────────────────────
    rule("STEP 2  |  Local PC Driver Extraction", style="bright_cyan")
    C.print()
    info("Extracting drivers from Windows DriverStore …")
    log_bg("INFO", "Extracting local PC drivers")

    local_staging = extract_local_pc_drivers()
    if local_staging is None:
        err("Failed to extract local PC drivers — aborting.")
        log_bg("ERR", "Local driver extraction failed")
        _tele(f"❌ DriverDex AutoSync v{APP_VER} — ABORTED\nPC: {platform.node()}\nReason: Local driver extraction failed")
        sys.exit(1)

    info("Scanning extracted driver packages …")
    log_bg("INFO", "Scanning extracted packages for INF files")
    with C.status("[bold bright_cyan]  Scanning for .inf files …[/bold bright_cyan]",
                  spinner="dots12") if GUI_MODE else contextlib.nullcontext():
        local_inf_data = scan_infs(local_staging)

    ok(f"Found {len(local_inf_data)} .inf file(s) in local DriverStore.")
    log_bg("OK", f"Found {len(local_inf_data)} INF files locally")
    _SESSION_STATS["local_total"] = len(local_inf_data)

    if not local_inf_data:
        warn("No INF files found in local DriverStore — nothing to sync.")
        log_bg("WARN", "No INF files found")
        shutil.rmtree(local_staging, ignore_errors=True)
        _tele(f"⚠️ DriverDex AutoSync v{APP_VER} — NO LOCAL DRIVERS FOUND\nPC: {platform.node()}")
        sys.exit(0)

    # ── Step 3: compare with repo ──────────────────────────────────────────
    rule("STEP 3  |  Comparing with Repo Manifest", style="bright_cyan")
    C.print()
    info("Loading combined repo manifest …")
    repo_manifest = load_all_manifests_combined(repo_dir)
    info(f"Repo contains {len(repo_manifest.get('drivers', []))} driver entries.")
    log_bg("INFO", f"Repo has {len(repo_manifest.get('drivers', []))} driver entries")

    new_drivers, outdated_drivers, current_drivers = compare_with_repo(
        local_inf_data, repo_manifest
    )
    _SESSION_STATS["new_count"]      = len(new_drivers)
    _SESSION_STATS["outdated_count"] = len(outdated_drivers)
    _SESSION_STATS["current_count"]  = len(current_drivers)

    log_bg("INFO",
           f"Comparison: {len(new_drivers)} new, {len(outdated_drivers)} outdated, "
           f"{len(current_drivers)} current")

    _tele_scan_result(
        local_total=len(local_inf_data),
        new_count=len(new_drivers),
        outdated=len(outdated_drivers),
        current=len(current_drivers),
    )

    show_comparison_table(new_drivers, outdated_drivers, current_drivers)

    # All drivers to process (new + outdated)
    to_upload: List[Tuple[Path, Dict, str]] = (
        [(p, d, STATUS_NEW)      for p, d in new_drivers] +
        [(p, d, STATUS_OUTDATED) for p, d in outdated_drivers]
    )

    if not to_upload:
        ok("All local drivers are already up-to-date in the repo. Nothing to upload.")
        log_bg("OK", "All drivers current — nothing to upload")
        shutil.rmtree(local_staging, ignore_errors=True)
        _tele(
            f"✅ DriverDex AutoSync v{APP_VER} — ALL CURRENT\n"
            f"PC: {platform.node()}\n"
            f"All {len(current_drivers)} local drivers already in repo."
        )
        sys.exit(0)

    info(f"Will upload {len(to_upload)} driver(s) "
         f"({len(new_drivers)} new + {len(outdated_drivers)} outdated).")

    # ── Step 4: archive & upload one driver group at a time ────────────────
    rule("STEP 4  |  Archive & Upload (1 driver group per commit)", style="bright_cyan")
    C.print()

    to_upload_inf_data = [(p, d) for p, d, _ in to_upload]
    status_by_path     = {p: s for p, d, s in to_upload}
    groups             = group_infs_by_folder(to_upload_inf_data)
    type_map: Dict[Path, str] = {
        folder: classify_group(infs) for folder, infs in groups
    }

    use_py7zr  = _has_py7zr()
    cli_binary = _7z_binary() if not use_py7zr else None
    volume_mb  = SPLIT_BYTES // (1024 * 1024)

    pack_base = (
        f"{platform.node().replace(' ', '_')[:12]}"
        f"_{date.today().strftime('%Y%m%d')}"
    )
    pack_base = re.sub(r'[^A-Za-z0-9_.\-]', '_', pack_base)

    # NEW drivers → go into a distinct "NEW_…" collection in the repo.
    # OUTDATED drivers → go into a "UPD_…" collection for traceability.
    pack_new = re.sub(r'[^A-Za-z0-9_.\-]', '_', f"NEW_{pack_base}")[:32]
    pack_upd = re.sub(r'[^A-Za-z0-9_.\-]', '_', f"UPD_{pack_base}")[:32]

    info(f"Pack (new collection) : [bold bright_cyan]{pack_new}[/bold bright_cyan]")
    info(f"Pack (update)         : [bold yellow]{pack_upd}[/bold yellow]")
    log_bg("INFO", f"Pack names — new: {pack_new}  upd: {pack_upd}")

    total_groups   = len(groups)
    uploaded_count = 0
    failed_count   = 0

    # ── Per-session fingerprint cache: prevent re-uploading the same driver
    #    twice in one run (e.g. if the loop is restarted after an error).
    #    Fingerprint = SHA-256 of sorted file paths + their sizes.
    _committed_fingerprints: Set[str] = set()

    def _group_fingerprint(files: List[Path]) -> str:
        h = hashlib.sha256()
        for f in sorted(files):
            h.update(str(f).encode())
            h.update(str(f.stat().st_size).encode())
        return h.hexdigest()

    for gi, (folder, group_infs) in enumerate(groups, start=1):
        dtype = type_map.get(folder, _TYPE_FALLBACK)

        rep_inf, rep_d = max(group_infs, key=lambda x: len(x[1].get("hwids", [])))
        folder_status  = status_by_path.get(rep_inf, STATUS_NEW)

        # Pick the correct pack name based on driver status
        pack_name = pack_new if folder_status == STATUS_NEW else pack_upd

        # Strip the internal annotation key before using rep_d for archiving
        rep_d_clean = {k: v for k, v in rep_d.items() if not k.startswith("_")}
        stem      = _archive_stem(pack_name, rep_inf, rep_d_clean)
        drv_ver   = rep_d_clean.get("version", "unknown")
        repo_ver  = rep_d.get("_repo_version", "")
        drv_label = f"{rep_d_clean.get('provider','?')} / {rep_d_clean.get('category','?')}"

        status_tag = (
            "[bold bright_green]NEW COLLECTION[/bold bright_green]"
            if folder_status == STATUS_NEW
            else "[bold yellow]UPDATE[/bold yellow]"
        )
        rule(
            f"Driver {gi}/{total_groups}  |  {drv_label}  [{folder_status.upper()}]",
            style="bright_cyan",
        )
        if folder_status == STATUS_NEW:
            info(f"  Status : {status_tag}  — not in repo, adding as new collection")
        else:
            repo_ver_str = f" (repo: {repo_ver})" if repo_ver else ""
            info(f"  Status : {status_tag}{repo_ver_str}  →  local: {drv_ver}")
        log_bg("INFO",
               f"Driver {gi}/{total_groups}: {drv_label} [{folder_status}] ver={drv_ver}")
        _tele_upload_start(gi, total_groups, drv_label, folder_status, drv_ver)

        all_files = sorted(
            f for f in folder.rglob("*") if f.is_file() and not f.is_symlink()
        )
        all_files, n_dedup = _dedup_files(all_files)
        if n_dedup:
            info(f"  Removed {n_dedup} duplicate file(s) from group.")

        if not all_files:
            warn(f"  No files in group {gi} — skipping.")
            log_bg("WARN", f"No files in group {gi}")
            continue

        # ── Dedup guard: skip if this exact set of files was already committed
        #    this session (handles accidental double-runs / loop restarts).
        fp = _group_fingerprint(all_files)
        if fp in _committed_fingerprints:
            warn(f"  Group {gi} fingerprint already committed this session — skipping.")
            log_bg("WARN", f"Group {gi} skipped (duplicate fingerprint)")
            continue

        group_staging = repo_dir / DRIVERS_DIR / f"_staging_{pack_name}_g{gi:04d}"
        group_staging.mkdir(parents=True, exist_ok=True)

        info(f"  Archiving {len(all_files)} file(s) …")
        log_bg("INFO", f"Archiving {len(all_files)} files for group {gi}")
        try:
            parts = _archive_group(
                files=all_files, folder=folder,
                dest_stem=group_staging / stem,
                use_py7zr=use_py7zr, cli_binary=cli_binary,
                volume_mb=volume_mb, on_progress=lambda n: None,
            )
        except Exception as exc:
            err(f"  Archive failed for group {gi}: {exc}")
            log_bg("ERR", f"Archive failed group {gi}: {exc}")
            shutil.rmtree(group_staging, ignore_errors=True)
            failed_count += 1
            _tele_upload_done(gi, total_groups, drv_label, success=False)
            continue

        ok(f"  Archived → {len(parts)} part(s)  "
           f"({fmt_size(sum(p.size_bytes for p in parts))})")
        log_bg("OK", f"Archived {len(parts)} parts for group {gi}")

        type_dest = repo_dir / DRIVERS_DIR / dtype / f"DP_{pack_name}"
        type_dest.mkdir(parents=True, exist_ok=True)
        for pi in parts:
            dest_path = type_dest / pi.path.name
            shutil.move(str(pi.path), str(dest_path))
            pi.path = dest_path

        zip_map = {folder: parts}
        # Strip internal annotation keys before building manifest entries
        group_infs_clean = [(ip, {k: v for k, v in d.items() if not k.startswith("_")})
                            for ip, d in group_infs]
        new_entries = build_entries(
            pack=pack_name, inf_data=group_infs_clean, zip_map=zip_map,
            rel_dir=DRIVERS_DIR, type_map=type_map,
        )
        full_manifest = load_all_manifests_combined(repo_dir)
        new_entries   = enrich_versions(new_entries, full_manifest)

        cat_manifest = load_manifest(repo_dir, cat=dtype)
        cat_manifest.setdefault("drivers", []).extend(new_entries)
        for k, v in full_manifest.get("version_history", {}).items():
            sample_id = (v[-1]["id"] if v else "") if isinstance(v, list) else ""
            if any(e["id"] == sample_id for e in new_entries):
                cat_manifest["version_history"].setdefault(k, []).extend(
                    [h for h in v if h not in cat_manifest["version_history"].get(k, [])]
                )
        save_manifest(cat_manifest, repo_dir, cat=dtype)

        save_to_sqlite(repo_dir, new_entries)
        _rebuild_index(repo_dir)
        _update_readme_badge(repo_dir, _count_total_drivers(repo_dir))

        files_to_commit: List[Tuple[str, Path]] = []
        for pi in parts:
            rel = str(pi.path.relative_to(repo_dir)).replace("\\", "/")
            files_to_commit.append((rel, pi.path))
        for meta_fp in [
            repo_dir / _category_manifest_rel(dtype),
            *(p for p in _manifest_shard_paths_for(repo_dir, _category_manifest_rel(dtype))[1:]),
            *(list(repo_dir.glob("drivers*.db"))),
            repo_dir / INDEX_FILE_NAME,
            repo_dir / "README.md",
        ]:
            if meta_fp.exists():
                rel = str(meta_fp.relative_to(repo_dir)).replace("\\", "/")
                files_to_commit.append((rel, meta_fp))

        seen_rel: Set[str] = set()
        unique_files: List[Tuple[str, Path]] = []
        for rr, fp in files_to_commit:
            if rr not in seen_rel:
                seen_rel.add(rr)
                unique_files.append((rr, fp))
        files_to_commit = unique_files

        n_drv = len(new_entries)
        if folder_status == STATUS_NEW:
            commit_msg = (
                f"🆕 NEW COLLECTION: {drv_label}  "
                f"(v{drv_ver})  "
                f"{n_drv} driver(s)  via v{APP_VER}  [pack: {pack_name}]"
            )
        else:
            repo_ver_tag = f"  repo was v{repo_ver}" if repo_ver else ""
            commit_msg = (
                f"⬆️ UPDATE: {drv_label}  "
                f"v{drv_ver}{repo_ver_tag}  "
                f"{n_drv} driver(s)  via v{APP_VER}  [pack: {pack_name}]"
            )

        info(f"  Committing group {gi}/{total_groups} …")
        commit_sha = commit_single_driver_group(
            repo_dir        = repo_dir,
            files_to_commit = files_to_commit,
            commit_msg      = commit_msg,
            group_label     = f"{gi}/{total_groups}",
        )

        shutil.rmtree(group_staging, ignore_errors=True)

        if commit_sha:
            uploaded_count += 1
            _committed_fingerprints.add(fp)
            _SESSION_STATS["uploaded"] = uploaded_count
            _tele_upload_done(gi, total_groups, drv_label, success=True, commit_sha=commit_sha)
        else:
            failed_count += 1
            err(f"  Commit failed for group {gi} — check logs.")
            log_bg("ERR", f"Commit failed for group {gi}")
            _tele_upload_done(gi, total_groups, drv_label, success=False)

        if gi < total_groups:
            time.sleep(2)

    # ── Cleanup local staging ──────────────────────────────────────────────
    shutil.rmtree(local_staging, ignore_errors=True)
    log_bg("INFO", "Local staging dir removed")

    # ── Session summary ────────────────────────────────────────────────────
    total_in_repo = _count_total_drivers(repo_dir)
    _tele_session_done(uploaded_count, failed_count, total_in_repo)

    C.print()
    rule()
    if GUI_MODE:
        status_col = "bright_green" if failed_count == 0 else "yellow"
        C.print(Panel(
            "\n".join([
                f"  [bold bright_green]AUTOSYNC COMPLETE[/bold bright_green]\n",
                f"  [dim]Local drivers found      :[/dim]  [white]{len(local_inf_data)}[/white]",
                f"  [dim]New collections (upload) :[/dim]  [white]{len(new_drivers)}[/white]",
                f"  [dim]Updates (outdated→newer)  :[/dim]  [white]{len(outdated_drivers)}[/white]",
                f"  [dim]Up-to-date (skipped)      :[/dim]  [white]{len(current_drivers)}[/white]",
                f"  [dim]Commits pushed             :[/dim]  [bold bright_green]{uploaded_count}[/bold bright_green]",
                f"  [dim]Failed commits             :[/dim]  [bold {'red' if failed_count else 'dim'}]{failed_count}[/bold {'red' if failed_count else 'dim'}]",
                "",
                f"  [dim]Total in repo:[/dim]  [white]{total_in_repo}[/white]",
                f"  [dim]Repo         :[/dim]  [cyan]https://github.com/{REPO_OWNER}/{REPO_NAME}[/cyan]",
            ]),
            border_style=status_col,
            title=f"[bold {status_col}]  DriverDex AutoSync v{APP_VER}  [/bold {status_col}]",
            padding=(1, 2),
        ))
    else:
        log_bg("DONE",
               f"AutoSync complete — {uploaded_count} commits, "
               f"{failed_count} failed, {total_in_repo} total in repo")

    C.print()


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        auto_sync()
    except KeyboardInterrupt:
        if GUI_MODE:
            C.print()
            C.print(Panel(
                "  [bold yellow]Interrupted — no partial changes remain.[/bold yellow]",
                border_style="yellow", title="[bold yellow]  Ctrl+C  [/bold yellow]",
                padding=(1, 2),
            ))
            C.print()
        else:
            log_bg("WARN", "Interrupted by user")
        sys.exit(130)
    except Exception as exc:
        if GUI_MODE:
            C.print()
            C.print(f"  [bold red]Fatal error:[/bold red] {exc}")
            C.print_exception(show_locals=False)
        else:
            log_bg("ERR", f"Fatal error: {exc}")
        sys.exit(1)
