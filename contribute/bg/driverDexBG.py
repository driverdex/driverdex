#!/usr/bin/env python3
# ==============================================================================
#  DriverDex Contribute  --  Driver & Installer Manifest Builder / GitHub Uploader
#  Author  : rhshourav
#  Version : 1.2.0  (background/silent build)
#  Repo    : https://github.com/rhshourav/driverdex
#  Part of : Windows-Scripts  --  github.com/rhshourav/Windows-Scripts
# ==============================================================================

from __future__ import annotations

# ── Silent background bootstrap ───────────────────────────────────────────────
# Must run before any import that touches stdout/stderr.
import os as _os, sys as _sys, io as _io, datetime as _dt

# Module-level log file handle — set by _go_silent() before anything else runs.
# Other parts of the module use _LOG_FILE / _LOG_PATH to write directly.
_LOG_FILE = None   # type: ignore[assignment]
_LOG_PATH: str = ""

def _go_silent() -> None:
    """Detach from any console and redirect all output to a log file."""
    global _LOG_FILE, _LOG_PATH

    # 1. On Windows, release the console window immediately so no black box
    #    flickers on screen, even if launched from Explorer.
    if _os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()          # type: ignore[attr-defined]
        except Exception:
            pass

    # 2. Route all stdout / stderr to a rotating log file in TEMP.
    try:
        _base    = _sys.executable if getattr(_sys, "frozen", False) else __file__
        _tmp     = _os.environ.get("TEMP", _os.environ.get("TMP",
                       _os.path.dirname(_os.path.abspath(_base))))
        _LOG_PATH = _os.path.join(_tmp, "driverdex_bg.log")
        _LOG_FILE = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
        _sys.stdout = _LOG_FILE
        _sys.stderr = _LOG_FILE
        # ── startup banner ────────────────────────────────────────────────────
        _ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _LOG_FILE.write(f"\n{'='*64}\n")
        _LOG_FILE.write(f"  DriverDex BG  v1.2.0  —  STARTED  {_ts}\n")
        _LOG_FILE.write(f"  Log file : {_LOG_PATH}\n")
        _LOG_FILE.write(f"  Python   : {_sys.version.split()[0]}  |  "
                        f"Platform : {_os.name}\n")
        _LOG_FILE.write(f"{'='*64}\n\n")
        _LOG_FILE.flush()
    except Exception as _ex:
        # Absolute last resort — discard everything rather than crash.
        _null     = _io.StringIO()
        _sys.stdout = _null
        _sys.stderr = _null

_go_silent()

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
import subprocess
import threading
import time
import traceback
import urllib.request
import urllib.error
import urllib.parse
import concurrent.futures
import getpass
import platform
import socket
from collections  import Counter, defaultdict
from pathlib      import Path
from datetime     import date, datetime, timezone
from typing       import Callable, Dict, List, Optional, Set, Tuple

# ── auto-install required packages ───────────────────────────────────────────
def _ensure(pkg: str, import_name: str = "") -> None:
    name = import_name or pkg
    try:
        __import__(name)
    except ImportError:
        # NOTE: _NO_WINDOW is defined later in the module (line ~924) so we
        # cannot reference it here — on a fresh PC this function runs before
        # that assignment and would raise NameError. Use getattr() instead so
        # it works at any point during startup, with a safe fallback of 0.
        _cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "-q"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_cflags,
        )

_ensure("rich")
_ensure("py7zr")
_ensure("multivolumefile")
_ensure("certifi")

from rich.console  import Console, Group as RichGroup
from rich.panel    import Panel
from rich.table    import Table
from rich.live     import Live
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    TimeRemainingColumn, TaskProgressColumn, DownloadColumn,
    TransferSpeedColumn, MofNCompleteColumn,
)
from rich.prompt   import Prompt, Confirm
from rich.text     import Text
from rich.rule     import Rule
from rich.align    import Align
from rich          import box
from rich.markup   import escape

# ── PyInstaller-safe base directory ──────────────────────────────────────────
def _get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

_APP_DIR = _get_app_dir()

# ── constants ─────────────────────────────────────────────────────────────────
REPO_OWNER          = "rhshourav"
REPO_NAME           = "driverdex"
GH_BRANCH           = "main"
MANIFEST_DIR        = "manifests"                                   # all manifests live here
INSTALLER_MANIFEST_REL = f"{MANIFEST_DIR}/installers.manifest.json"
DRIVERS_DIR         = "drivers"
SPLIT_BYTES         = 15 * 1024 * 1024
SCHEMA_VER          = "3.0"
APP_VER             = "1.2.0"
COMMIT_EMAIL        = "driverdex-contribute@noreply.local"
COMMIT_NAME         = "DriverDex-Contribute"

API_BASE            = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
LFS_BATCH_URL = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git/info/lfs/objects/batch"
)

WORKSPACE_DIR       = Path(os.environ.get("TEMP", os.environ.get("TMP", str(_APP_DIR)))) / "driverdex_workspace"

MANIFEST_SIZE_LIMIT = 15 * 1024 * 1024
INDEX_FILE_NAME     = "ldc_index.json"

BADGE_MARKER_START  = "<!-- DRIVERDEX_DRIVER_BADGE_START -->"
BADGE_MARKER_END    = "<!-- DRIVERDEX_DRIVER_BADGE_END -->"

BASE_RAW_URL = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{DRIVERS_DIR}"
)
MANIFEST_RAW_BASE = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main"
)

C = Console(
    highlight=False,
    no_color=True,
    file=_LOG_FILE if _LOG_FILE is not None else open(os.devnull, "w"),
    stderr=False,
)

# ── Direct log writer (bypasses Rich — always reaches the file) ───────────────
def _log(msg: str) -> None:
    """Write a plain-text timestamped line directly to the log file.

    Used for startup events, errors, and fatal exceptions where we cannot rely
    on the Rich Console (e.g. before it is initialised, or in except blocks).
    Safe to call at any point in the process lifetime.
    """
    try:
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        if _LOG_FILE is not None:
            _LOG_FILE.write(line)
            _LOG_FILE.flush()
        else:
            print(line, end="", flush=True)
    except Exception:
        pass

# ── SSL context helper (handles systems with missing/broken CA stores) ─────────
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


# ── Config file path (optional, non-secret settings only — never a token) ─────
# This file is NOT used for credentials. It may carry optional DriverDex tuning
# keys (driverdex_base_url, driverdex_cache_ttl, …) read by _dd_load_config().
_CONFIG_FILE = Path(os.environ.get("TEMP", os.environ.get("TMP", str(_APP_DIR)))) / "driverdex_config.json"


# ── GitHub token — encrypted-only, in-memory cache ───────────────────────────
# The token is obtained ONLY from the encrypted split-secret scheme
# (_dd_bootstrap_token): the KDF salt + passphrase are compiled into the build
# and the AES-256-GCM ciphertext is fetched from the repo. The decrypted value
# lives in memory for the process lifetime — it is never written to disk, an
# environment variable, or any config file. GITHUB_TOKEN mirrors the cache for
# any code that reads it directly.
GITHUB_TOKEN = ""
_TOKEN_CACHE: List[str] = [""]          # mutable in-memory cache (no disk writes)
_TOKEN_LOCK  = threading.Lock()
# Set when the encrypted token is rejected (HTTP 401) and cannot be recovered;
# parallel upload worker threads watch this to stop immediately instead of
# each looping on 401.
_TOKEN_REFRESH_DECLINED = threading.Event()


def _load_token() -> str:
    """
    Return the GitHub token, decrypting it from the embedded/encrypted source
    on first use and caching it in memory only.

    The encrypted split-secret scheme (salt + passphrase compiled into the
    build, AES-256-GCM ciphertext fetched from the repo) is the SINGLE source
    of truth. There is no environment-variable, config-file, or interactive
    fallback. Returns "" if the token could not be decrypted (handled by
    check_github_token()).

    Safe to call on every API request — after the first successful decrypt the
    cached in-memory value is returned with no further disk or network access.
    """
    global GITHUB_TOKEN
    if _TOKEN_CACHE[0]:
        return _TOKEN_CACHE[0]
    with _TOKEN_LOCK:
        if _TOKEN_CACHE[0]:
            return _TOKEN_CACHE[0]
        tok = _dd_bootstrap_token(quiet=True) or ""
        _TOKEN_CACHE[0] = tok
        GITHUB_TOKEN = tok
        return tok


def _refresh_github_token(label: str = "") -> bool:
    """
    Recover from an HTTP 401 during a GitHub API call.

    Because the token comes only from the encrypted split-secret scheme, the
    sole recovery path is to re-fetch and re-decrypt the key file from the repo
    (in case the encrypted key was rotated since this process started). No
    prompt is ever shown. Returns True if a non-empty token was recovered.

    On permanent failure _TOKEN_REFRESH_DECLINED is set so all parallel upload
    threads stop immediately instead of each looping on 401.
    """
    global GITHUB_TOKEN
    if _TOKEN_REFRESH_DECLINED.is_set():
        return False
    with _TOKEN_LOCK:
        _TOKEN_CACHE[0] = ""
        new_tok = _dd_bootstrap_token(quiet=True) or ""
        _TOKEN_CACHE[0] = new_tok
        GITHUB_TOKEN = new_tok
    if new_tok:
        ok("Re-decrypted GitHub token from encrypted key file"
           + (f" [dim]({escape(label)})[/dim]" if label else "") + " — resuming …")
        return True
    _TOKEN_REFRESH_DECLINED.set()
    err("HTTP 401 — the embedded GitHub token was rejected and could not be "
        "recovered. Rotate the encrypted key file with tools/make_token_blob.py "
        "and rebuild the .exe.")
    return False


# ── Telemetry ───────────────────────────────────────────────────────────────  ─
_TELEMETRY_URL   = "https://cryocore.rhshourav02.workers.dev/message"
_TELEMETRY_TOKEN = "shourav"

_TELEMETRY_MILESTONES = {
    "STEP 2 — SCAN COMPLETE"       : "📋 Drivers scanned — ready to upload",
    "STEP 6 — ARCHIVE COMPLETE"    : "📦 Compression done — moving to upload",
    "STEP 4 — PACK STATS"          : "📊 Pack stats calculated",
    "STEP 3 — PREFLIGHT AUDIT DONE": "🔍 Pre-flight audit passed",
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
                        "User-Agent"  : "Python-DriverDex-Contribute",
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
            f"[DriverDex Contribute v{APP_VER}] {friendly}",
            f"PC: {pc}  Pack: {pack}",
        ]
        if extra:
            lines.append(extra)
    else:
        lines = [f"[DriverDex Contribute v{APP_VER}] {step}  |  PC: {pc}  Pack: {pack}"]
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
        f"\U0001f7e2 DriverDex Contribute v{APP_VER} \u2014 STARTED\n"
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
        f"[DriverDex Contribute v{APP_VER}] {status}\n"
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
        f"[DriverDex Contribute v{APP_VER}] \U0001f3c1 SESSION COMPLETE\n"
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
                capture_output=True, timeout=60, creationflags=_NO_WINDOW,
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
                / f"_ldc_del_{src.name}_{int(time.time())}"
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
    pass  # background build — no UI


# ── Logging & output helpers ──────────────────────────────────────────────────
def log(_level: str, _msg: str) -> None:
    pass


def rule(title: str = "", style: str = "bright_cyan") -> None:
    if title:
        C.print(Rule(f"[bold {style}] {title} [/bold {style}]", style=f"dim {style}"))
    else:
        C.print(Rule(style="dim cyan"))


def ok(msg: str) -> None:
    _log(f"[OK]   {msg}")
    C.print(f"  [bold bright_green]✓[/bold bright_green]  {msg}")

def warn(msg: str) -> None:
    _log(f"[WARN] {msg}")
    C.print(f"  [bold yellow]⚠[/bold yellow]  [yellow]{msg}[/yellow]")

def err(msg: str) -> None:
    _log(f"[ERR]  {msg}")
    C.print(f"  [bold bright_red]✗[/bold bright_red]  [bright_red]{msg}[/bright_red]")

def info(msg: str) -> None:
    _log(f"[INFO] {msg}")
    C.print(f"  [dim cyan]◈[/dim cyan]  [white]{msg}[/white]")

def hint(msg: str) -> None:
    _log(f"       {msg}")
    C.print(f"      [dim]↳  {msg}[/dim]")


def die(msg: str, fix: str = "", code: int = 1) -> None:
    _log(f"[DIE]  {msg}" + (f" | fix: {fix}" if fix else ""))
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
) -> Tuple[int, Dict]:
    # Always call _load_token() so a cross-thread refresh or a config-file
    # update is picked up without relying on the stale module-level variable.
    tok = token or _load_token()
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    headers: Dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _urlopen_ssl_safe(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"message": str(exc)}
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
) -> Tuple[int, Dict]:
    """
    Robust caller with:
      * Exponential back-off + jitter on 429/5xx/network errors
      * Retry-After header honoured on 429
      * One-shot token refresh on 401
      * 403 (missing scope) → immediate permanent failure, not retried
    """
    tag = f"[{label}] " if label else ""
    token_refreshed = False
    last_st, last_resp = 0, {}

    for attempt in range(1, max_attempts + 1):
        # Always pick up the current global token so a cross-thread refresh
        # is automatically used on the next attempt.
        st, resp = _api(method, path, data=data, token=token or _load_token(), timeout=timeout)
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


# ── GitHub token check ────────────────────────────────────────────────────────
def check_github_token() -> bool:
    global GITHUB_TOKEN
    C.print()

    # The token is resolved ONLY from the encrypted split-secret scheme: the
    # salt + passphrase embedded in this build decrypt the ciphertext fetched
    # from the repo. Nothing is read from the environment, a config file, or an
    # interactive prompt — this lets an ordinary end-user run the distributed
    # .exe and contribute drivers without ever owning a personal token.
    tok = _load_token()
    if not tok:
        err("No usable GitHub token.")
        hint("This build could not decrypt an embedded write token. Either it "
             "still carries placeholder secrets, or the encrypted key file could "
             "not be fetched/decrypted. Rotate the key file with "
             "tools/make_token_blob.py and rebuild the .exe.")
        return False

    GITHUB_TOKEN = tok

    with C.status("[bold bright_cyan]  Verifying GitHub token …[/bold bright_cyan]", spinner="dots12"):
        try:
            status, resp = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}")
        except KeyboardInterrupt:
            C.print()
            warn("Token check interrupted.")
            return False
    if status == 200:
        ok(f"GitHub token accepted — repo: {resp.get('full_name', REPO_OWNER+'/'+REPO_NAME)}")
        return True
    if status == 401:
        err("Embedded GitHub token is invalid or expired — rotate the encrypted key file.")
    elif status == 403:
        err("Embedded GitHub token lacks required permissions (needs 'repo' scope).")
    elif status == 404:
        err(f"Repo {REPO_OWNER}/{REPO_NAME} not found.")
    else:
        err(f"GitHub API returned HTTP {status}: {resp.get('message', '')}")
    return False


# ── Workspace setup ───────────────────────────────────────────────────────────
def setup_workspace() -> Path:
    ws = WORKSPACE_DIR
    ws.mkdir(parents=True, exist_ok=True)
    (ws / DRIVERS_DIR).mkdir(exist_ok=True)
    ok(f"Workspace ready: {escape(str(ws))}")

    # No local credential file to protect — the GitHub token never touches disk
    # (it is decrypted into memory from the encrypted key file at runtime).
    github_pull_rebase(ws)
    return ws


# ── Administrator elevation (Windows) ─────────────────────────────────────────
def _is_windows() -> bool:
    return os.name == "nt" or platform.system().lower().startswith("win")


def is_admin() -> bool:
    """Return True if the current process has administrative rights."""
    if not _is_windows():
        # On non-Windows we treat root (uid 0) as 'admin'; driver export is a
        # no-op there but this keeps the flow testable.
        try:
            return os.geteuid() == 0          # type: ignore[attr-defined]
        except AttributeError:
            return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())   # type: ignore[attr-defined]
    except Exception:
        return False


def ensure_admin() -> None:
    """Relaunch the current process elevated (UAC) if not already admin.

    When the relaunch is triggered the *current* (non-elevated) process exits
    immediately; the elevated copy continues the work. If elevation is declined
    or fails we keep running unelevated so the user still gets a clear message
    from the extraction step.
    """
    if is_admin():
        ok("Running with administrator privileges.")
        return
    if not _is_windows():
        warn("Not running as root — driver export may be limited.")
        return

    info("Administrator rights are required to export installed drivers — requesting elevation …")
    try:
        import ctypes
        # Re-run this same script/exe with the same arguments, elevated.
        if getattr(sys, "frozen", False):
            target = sys.executable
            params = subprocess.list2cmdline(sys.argv[1:])
        else:
            target = sys.executable
            params = subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *sys.argv[1:]])

        rc = ctypes.windll.shell32.ShellExecuteW(   # type: ignore[attr-defined]
            None, "runas", target, params, None, 1)
        # ShellExecuteW returns a value > 32 on success.
        if int(rc) > 32:
            info("Elevated instance launched — closing this window.")
            sys.exit(0)
        warn("Elevation request was declined — continuing without admin rights "
             "(driver export will likely be incomplete).")
    except Exception as exc:
        warn(f"Could not elevate automatically ({exc}) — continuing unelevated.")


# ── Local driver extraction (DISM + pnputil) ──────────────────────────────────
# Suppress any console window that child processes (DISM, pnputil, 7z) would
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
    ok(f"Extraction complete — {total_inf} .inf file(s) collected in {escape(str(dest))}")
    return dest


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
    driver_date = ""
    m = _VER_RE.search(txt)
    if m:
        raw = _resolve(m.group(1).strip(), strings)
        parts = [p.strip() for p in raw.split(",", 1)]
        if len(parts) == 2:
            # Canonical "MM/DD/YYYY,version" form.
            driver_date = parts[0].strip()
            version = parts[1].strip()
        else:
            # Some INFs only carry one token — detect whether it's a date.
            only = parts[0].strip()
            if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", only):
                driver_date = only
            else:
                version = only

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


def scan_infs(folder: Path) -> List[Tuple[Path, Dict]]:
    results: List[Tuple[Path, Dict]] = []
    for inf in sorted(folder.rglob("*.inf")):
        d = parse_inf(inf)
        if d:
            results.append((inf, d))
    return results


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


# ── Checksum helpers ───────────────────────────────────────────────────────  ──
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


def _7z_binary() -> Optional[str]:
    for candidate in ("7z", "7za", "7zz"):
        try:
            result = subprocess.run([candidate, "i"], capture_output=True, timeout=5,
                                    creationflags=_NO_WINDOW)
            if result.returncode == 0:
                return candidate
        except Exception:
            pass
    return None


# ── File deduplication ────────────────────────────────────────────────────────
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                                creationflags=_NO_WINDOW)
        if result.returncode != 0:
            raise RuntimeError(f"7z CLI failed (rc={result.returncode}):\n{result.stderr.strip()}")
    except KeyboardInterrupt:
        for p in dest_parent.glob(dest_stem.name + ".7z*"):
            try:
                p.unlink()
            except Exception:
                pass
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

    all_files_by_group: Dict[Path, List[Path]] = {}
    total_bytes = 0
    total_dedup_removed = 0
    for folder, _ in groups:
        raw_files = sorted(p for p in folder.rglob("*") if p.is_file())
        unique, removed = _dedup_files(raw_files)
        all_files_by_group[folder] = unique
        total_bytes += sum(f.stat().st_size for f in unique)
        total_dedup_removed += removed

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

    all_files_by_group: Dict[Path, List[Path]] = {}
    total_bytes = 0
    for folder, _ in groups:
        raw = sorted(p for p in folder.rglob("*") if p.is_file())
        unique, removed = _dedup_files(raw)
        all_files_by_group[folder] = unique
        total_bytes += sum(f.stat().st_size for f in unique)

    use_py7zr  = _has_py7zr()
    cli_binary = None if use_py7zr else _7z_binary()
    volume_mb  = max(1, SPLIT_BYTES // (1024 * 1024))
    group_idx  : Dict[Path, int] = {folder: i+1 for i, (folder, _) in enumerate(groups)}

    zip_map          : Dict[Path, List[PartInfo]] = {}
    pack_dir_by_type : Dict[str, Path]            = {}
    total_verified   = 0

    lfs_batch_url = LFS_BATCH_URL

    def _upload_lfs_file(repo_rel: str, fpath: Path) -> bool:
        raw_bytes = fpath.read_bytes()
        oid  = hashlib.sha256(raw_bytes).hexdigest()
        size = len(raw_bytes)

        lfs_headers: Dict[str, str] = {
            "Content-Type": "application/vnd.git-lfs+json",
            "Accept"      : "application/vnd.git-lfs+json",
        }
        if GITHUB_TOKEN:
            lfs_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        dl_payload = json.dumps({
            "operation": "download", "transfers": ["basic"],
            "ref": {"name": f"refs/heads/{GH_BRANCH}"},
            "objects": [{"oid": oid, "size": size}],
        }).encode("utf-8")

        def _lfs_batch_download_ok() -> bool:
            try:
                vreq = urllib.request.Request(lfs_batch_url, data=dl_payload,
                                              headers=lfs_headers, method="POST")
                with _urlopen_ssl_safe(vreq, timeout=60) as vresp:
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
                with _urlopen_ssl_safe(req, timeout=60) as resp:
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
                    with _urlopen_ssl_safe(ul_req, timeout=300) as ul_resp:
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
        return _archive_group(
            files=files, folder=folder, dest_stem=dest_stem,
            use_py7zr=use_py7zr, cli_binary=cli_binary,
            volume_mb=volume_mb, on_progress=lambda n: None,
        )

    C.print()
    info(
        f"Pipeline mode: archive → upload → archive → …  |  "
        f"{len(groups)} group(s)  |  Volume: {fmt_size(SPLIT_BYTES)}"
    )
    C.print()

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


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _scan_exe_files(folder: Path) -> List["InstallerPackage"]:
    dir_to_exes: Dict[Path, List[Path]] = defaultdict(list)
    for p in sorted(folder.rglob("*.exe")):
        if p.is_file():
            dir_to_exes[p.parent].append(p)

    packages: List[InstallerPackage] = []
    seen_dirs: set = set()

    for parent_dir, exes in sorted(dir_to_exes.items()):
        if parent_dir in seen_dirs:
            continue

        if parent_dir == folder:
            for exe in exes:
                packages.append(InstallerPackage(exe_path=exe, pkg_dir=None))
        else:
            seen_dirs.add(parent_dir)
            for d, sub_exes in dir_to_exes.items():
                try:
                    d.relative_to(parent_dir)
                    seen_dirs.add(d)
                except ValueError:
                    pass

            rep_exe = max(exes, key=lambda p: p.stat().st_size)
            all_sub_exes = [
                e for d, es in dir_to_exes.items()
                for e in es
                if _is_subpath(d, parent_dir)
            ]
            packages.append(InstallerPackage(
                exe_path=rep_exe,
                pkg_dir=parent_dir,
                all_exes=all_sub_exes or exes,
            ))

    return packages


# ── REST API error diagnosis ──────────────────────────────────────────────────
def diagnose_api_error(status: int, resp: Dict) -> str:
    msg = resp.get("message", "")
    if status == 401:
        return "GitHub token is invalid or expired. Regenerate at https://github.com/settings/tokens"
    if status == 403:
        if "rate limit" in msg.lower():
            return "GitHub API rate limit exceeded. Wait ~1 hour or use an authenticated token."
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


# ── GitHub REST — manifest sync ──────────────────────────  ────────────────────
def _load_remote_index(workspace: Path) -> Optional[Dict]:
    result = _download_file_from_github(INDEX_FILE_NAME, workspace / INDEX_FILE_NAME)
    if result != "ok":
        return None
    try:
        return json.loads((workspace / INDEX_FILE_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def github_pull_skeleton(workspace: Path) -> None:
    """Pull only the lightweight repo *skeleton* needed to decide what to do:

        • ldc_index.json   — the catalog index (category list + shard map + counts)
        • the installer manifest + README badge

    The catalog is described entirely by the JSON manifests (and the live
    Cloudflare DriverDex database); there is no longer a separate SQLite
    drivers.db to download.

    Category driver manifests are deliberately NOT downloaded here. They are
    fetched lazily and selectively by github_pull_category_manifests() once the
    driver folder has been scanned and classified, so we never download
    manifests for categories the current pack doesn't actually touch.
    """
    with C.status("[bold bright_cyan]  Syncing catalog index from GitHub …[/bold bright_cyan]",
                  spinner="dots12"):
        remote_index = _load_remote_index(workspace)

        _download_file_from_github(INSTALLER_MANIFEST_REL, workspace / INSTALLER_MANIFEST_REL)
        _download_file_from_github("README.md", workspace / "README.md")

    if remote_index and remote_index.get("category_summary"):
        cats  = remote_index["category_summary"]
        total = sum(cats.values())
        ok(f"Index synced — {len(cats)} categor(y/ies), {total} driver(s) catalogued remotely.")
        info("Matching category manifests are downloaded on demand, only for the drivers you contribute.")
    elif remote_index:
        ok("Index synced — repo has no catalogued drivers yet.")
    else:
        ok("No remote index yet — starting fresh.")


def _manifest_category_key(filename: str) -> str:
    """Sanitized category stem for a manifest shard filename.

    'manifests/Audio.manifest.json'   -> 'Audio'
    'manifests/Audio.manifest.2.json' -> 'Audio'
    """
    name = filename.split("/")[-1]
    return re.sub(r"\.manifest(\.\d+)?\.json$", "", name)


def github_pull_category_manifests(workspace: Path, categories) -> None:
    """Download ONLY the manifest shards for the given driver categories.

    Uses ldc_index.json (already synced by github_pull_skeleton) to resolve the
    exact shard filenames per category and to skip categories that have no
    remote drivers to compare against. Falls back to probing the canonical
    shard names when the index is missing or doesn't list a category yet.

    Existing remote manifests for categories NOT in `categories` are left
    untouched: the push uses GitHub's base_tree, so files absent locally are
    preserved in the repo, and _save_index() merges with the synced index.
    """
    cats = {c for c in (categories or set()) if c}
    if not cats:
        return

    remote_index = _load_index(workspace)
    shard_list   = remote_index.get("manifest_shards") or []
    have_index   = any(s.get("size_bytes", 0) > 0 for s in shard_list)

    pulled_rels: List[str] = []
    skipped: List[str]     = []

    with C.status("[bold bright_cyan]  Fetching matching category manifests …[/bold bright_cyan]",
                  spinner="dots12"):
        for cat in sorted(cats):
            base_rel = _category_manifest_rel(cat)              # manifests/Audio.manifest.json
            stem     = base_rel.replace(".manifest.json", "")
            cat_key  = _manifest_category_key(base_rel)

            # Shard filenames for this category as listed in the index.
            cat_shards = [
                str(s.get("filename", "")) for s in shard_list
                if _manifest_category_key(str(s.get("filename", ""))) == cat_key
                and s.get("size_bytes", 0) > 0
            ]

            if cat_shards:
                for sname in cat_shards:
                    r = _download_file_from_github(sname, workspace / sname)
                    if r == "ok":
                        pulled_rels.append(sname)
                    elif r == "error":
                        warn(f"Could not download manifest shard {escape(sname)} — skipping.")
            elif have_index:
                # Index is populated but doesn't list this category → nothing
                # remote to compare against; every driver in it is treated as new.
                skipped.append(cat)
            else:
                # No usable index — probe the canonical shard names directly.
                r = _download_file_from_github(base_rel, workspace / base_rel)
                if r == "ok":
                    pulled_rels.append(base_rel)
                    for idx in range(2, 50):
                        shard_r = f"{stem}.manifest.{idx}.json"
                        sr = _download_file_from_github(shard_r, workspace / shard_r)
                        if sr == "ok":
                            pulled_rels.append(shard_r)
                        elif sr == "not_found":
                            break
                        elif sr == "error":
                            warn(f"Could not download manifest shard {escape(shard_r)} — skipping.")

        # Drop any non-JSON / empty downloads so the audit doesn't choke on them.
        for rel in list(pulled_rels):
            local = workspace / rel
            if not local.exists():
                continue
            raw_bytes = local.read_bytes()
            if not raw_bytes or raw_bytes[:1] in (b"<", b"\n", b"\r\n"):
                local.unlink(missing_ok=True)
                pulled_rels.remove(rel)
                warn(f"{escape(rel)} contained non-JSON content — removed.")
                continue
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
                if "drivers" not in data:
                    raise ValueError("Missing 'drivers' key")
            except (json.JSONDecodeError, ValueError) as exc:
                local.unlink(missing_ok=True)
                pulled_rels.remove(rel)
                warn(f"{escape(rel)} is not valid JSON: {escape(str(exc))} — removed.")

    if pulled_rels:
        ok(f"Downloaded {len(pulled_rels)} manifest shard(s) for matching categor(y/ies): "
           + ", ".join(sorted(cats)))
    else:
        ok(f"No existing remote manifests for {', '.join(sorted(cats))} — these drivers are all new.")
    if skipped:
        info(f"Skipped (no remote drivers catalogued yet): {', '.join(skipped)}")


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
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_load_token()}"
    req = urllib.request.Request(lfs_url, data=payload, headers=headers, method="POST")
    try:
        with _urlopen_ssl_safe(req, timeout=60) as resp:
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
        with _urlopen_ssl_safe(dl_req, timeout=120) as dl_resp:
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
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(raw_url, headers=headers)
    try:
        with _urlopen_ssl_safe(req, timeout=60) as resp:
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
            repo_rel_path, oid=lfs_info["oid"], size=int(lfs_info["size"]), local_dest=local_dest
        )
    probe = data.lstrip()
    if not probe:
        warn(f"Empty response for {repo_rel_path}     skipping.")
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
        github_pull_skeleton(workspace)
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


# ── Upload mode selector ──────────────────────────────────────────────────────
UPLOAD_MODE_PARALLEL  = "parallel"
UPLOAD_MODE_PIPELINE  = "pipeline"
UPLOAD_MODE_LFS_AGGR  = "lfs_aggressive"


def _select_upload_mode() -> str:
    return UPLOAD_MODE_PARALLEL  # background build — always parallel, no prompt


# ── GitHub commit + push ──────────────────────────────────────────────────────
def github_commit_push(
    workspace            : Path,
    commit_msg           : str,
    upload_mode          : str = UPLOAD_MODE_PARALLEL,
    skip_archive_upload  : bool = False,
) -> bool:
    # ── 1. HEAD ref ───────────────────────────────────────────────────────────
    with C.status("[bold bright_cyan]  Fetching HEAD ref …[/bold bright_cyan]", spinner="dots12"):
        st, ref_data = _api_with_retry(
            "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/ref/heads/{GH_BRANCH}", label="HEAD ref"
        )
    if st != 200:
        err(f"Cannot read HEAD ref (HTTP {st}): {ref_data.get('message', '')}")
        hint(diagnose_api_error(st, ref_data))
        return False
    head_sha = ref_data["object"]["sha"]

    # ── 2. Base tree SHA ──────────────────────────────────────────────────────
    st, commit_data = _api_with_retry(
        "GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/commits/{head_sha}", label="HEAD commit"
    )
    if st != 200:
        err(f"Cannot read HEAD commit (HTTP {st})")
        return False
    base_tree_sha = commit_data["tree"]["sha"]

    # ── 3. Collect files ──────────────────────────────────────────────────────
    #  Only commit repo-tracked artifacts: the split archives under drivers/,
    #  the manifests, and top-level metadata (README, index).  NEVER walk the
    #  temporary extraction / download / staging directories — those hold the
    #  RAW, un-split driver payloads (multi-hundred-MB .dll/.sys/.msi files) and
    #  would otherwise leak into the commit and blow past the blob size limit.
    def _is_excluded(rel_parts: Tuple[str, ...]) -> bool:
        for part in rel_parts:
            p = part.lower()
            if p in ("extracted_drivers", "driverdex_downloads", ".git"):
                return True
            if p.startswith("_staging"):       # drivers/<Type>/_staging_DP_* etc.
                return True
        return False

    files: List[Tuple[str, Path]] = []
    for fpath in sorted(workspace.rglob("*")):
        if not fpath.is_file():
            continue
        try:
            rel = fpath.relative_to(workspace)
        except ValueError:
            continue
        if _is_excluded(rel.parts):
            continue
        repo_rel = str(rel).replace("\\", "/")
        files.append((repo_rel, fpath))

    if not files:
        warn("Workspace is empty — nothing to upload.")
        return True

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

    if upload_mode == UPLOAD_MODE_LFS_AGGR:
        zip_files  = files[:]
        meta_files = []
    else:
        zip_files  = [(rr, fp) for rr, fp in files if _is_lfs_file(fp)]
        meta_files = [(rr, fp) for rr, fp in files if not _is_lfs_file(fp)]

    already_uploaded_rrs: set = set()
    if skip_archive_upload:
        already_uploaded_rrs = {
            rr for rr, fp in zip_files
            if rr.startswith(f"{DRIVERS_DIR}/") and _is_lfs_file(fp)
        }
        if already_uploaded_rrs:
            info(
                f"Pipeline: skipping re-upload of {len(already_uploaded_rrs)} "
                f"archive(s) already in LFS — writing pointer blobs only."
            )

    mode_label = {
        UPLOAD_MODE_PARALLEL : "Parallel blobs (12 threads)",
        UPLOAD_MODE_PIPELINE : "Pipeline REST (archive→upload interleaved)",
        UPLOAD_MODE_LFS_AGGR : "LFS aggressive (all via LFS)",
    }.get(upload_mode, upload_mode)

    info(
        f"Upload mode: [bold bright_cyan]{mode_label}[/bold bright_cyan]  |  "
        f"{len(zip_files)} archive(s) via blob API  ·  {len(meta_files)} meta file(s) via blob API"
    )
    C.print()

    # ── 4a. Upload archives via blob API ──────────────────────────────────────
    lfs_tree_entries: List[Dict] = []

    def _upload_as_blob(repo_rel: str, fpath: Path) -> Optional[Dict]:
        """
        Upload file content as a plain Git blob via the GitHub REST API.
        Key improvements over v5.3.2:
          * 403 (missing repo scope) → immediate skip with clear diagnosis; no retry loop
          * 401 → one-shot token refresh via _api_with_retry, then inline retry
          * Memory guard: rejects files > 95 MB before reading into RAM
          * Returns None immediately if user already declined token refresh
        """
        # Fast-fail: user already said "no" — don't even try the request.
        if _TOKEN_REFRESH_DECLINED.is_set():
            return None

        fsize = fpath.stat().st_size
        if fsize > 95 * 1024 * 1024:
            err(
                f"File too large for blob API: {repo_rel} "
                f"({fmt_size(fsize)} > 95 MB). Reduce SPLIT_BYTES."
            )
            return None

        raw_bytes = fpath.read_bytes()
        encoded   = base64.b64encode(raw_bytes).decode("ascii")

        st, blob = _api_with_retry(
            "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs",
            {"content": encoded, "encoding": "base64"},
            timeout=300, max_attempts=5,
            label=f"blob {Path(repo_rel).name}",
        )

        # ── 403: missing 'repo' scope — permanent failure, skip immediately ───
        if st == 403:
            err(
                f"Blob upload failed for {repo_rel} (HTTP 403): "
                f"{blob.get('message', 'Resource not accessible by personal access token')}"
            )
            hint("Token lacks required permissions (needs 'repo' scope with push access).")
            return None

        # ── 401: _api_with_retry already attempted a one-shot refresh.
        # If it still returns 401 here, the user declined — propagate as abort.
        if st == 401:
            if _refresh_github_token(label=f"blob {Path(repo_rel).name}"):
                st, blob = _api_with_retry(
                    "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs",
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
        lfs_total_bytes = sum(fp.stat().st_size for _, fp in zip_files)
        lfs_prog = Progress(
            SpinnerColumn("dots12", style="bold bright_cyan"),
            TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
            BarColumn(bar_width=None, style="grey23", complete_style="bold bright_cyan",
                      finished_style="bold bright_green"),
            TaskProgressColumn(style="bold white"),
            MofNCompleteColumn(separator="[dim white]/[/dim white]"),
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

            # ── Per-run upload-abort flag ──────────────────────────────────────
            # Set by the first thread that detects the user declined a token
            # refresh, so all other threads stop immediately instead of each
            # hitting 401 and re-triggering the (now no-op) prompt.
            # NOTE: 403 (missing scope) is a permanent configuration error —
            # we do NOT abort on 403; we collect them and report all at once.
            _upload_abort = threading.Event()
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
                # If every failure is a 403 scope issue, the token is wrong —
                # surface a single actionable error instead of a wall of per-file noise.
                n_fail = len(failed_blobs)
                n_total = len(zip_files)
                if n_fail == n_total:
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
            console=C, transient=False, expand=True,
        )
        with meta_prog:
            meta_task = meta_prog.add_task("Metadata blobs", total=len(meta_files))
            for repo_rel, fpath in meta_files:
                meta_prog.update(meta_task, description=f"  ◈  blob  {Path(repo_rel).name}")
                entry = _upload_as_blob(repo_rel, fpath)
                if entry is None:
                    err(f"Failed to create blob for {repo_rel}")
                    return False
                blob_tree_entries.append(entry)
                meta_prog.advance(meta_task, 1)

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

    for chunk_i in range(n_chunks):
        chunk = tree_entries[chunk_i * TREE_CHUNK_SIZE : (chunk_i + 1) * TREE_CHUNK_SIZE]
        chunk_label = f"tree chunk {chunk_i + 1}/{n_chunks}"
        with C.status(
            f"[bold bright_cyan]  Creating {chunk_label} …[/bold bright_cyan]",
            spinner="dots12",
        ):
            st, tree = _api_with_retry(
                "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/trees",
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

    # ── 6. Create commit ──────────────────────────────────────────────────────
    with C.status("[bold bright_cyan]  Creating commit …[/bold bright_cyan]", spinner="dots12"):
        st, new_commit = _api_with_retry(
            "POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/commits",
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
            f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/heads/{GH_BRANCH}",
            {"sha": new_commit["sha"], "force": False},
            timeout=60, max_attempts=5, label="update ref",
        )
    if st in (200, 201):
        ok(f"Pushed to GitHub — commit {new_commit['sha'][:8]}")
        return True
    err(f"Ref update failed (HTTP {st}): {ref_resp.get('message', '')}")
    hint(diagnose_api_error(st, ref_resp))
    return False


# ── Version comparison ────────────────────────────────────────────────────────
def _parse_ver(ver_str: str) -> Tuple[int, ...]:
    clean = re.sub(r'^\d{1,2}/\d{1,2}/\d{4}\s*,\s*', '', ver_str.strip())
    parts = re.split(r'[.,\-]', clean)
    ints: List[int] = [int(p.strip()) for p in parts if p.strip().isdigit()]
    return tuple(ints) if ints else (0,)


def version_is_newer(new_ver: str, old_ver: str) -> bool:
    return _parse_ver(new_ver) > _parse_ver(old_ver)


# ── Category-manifest naming ──────────────────────────────────────────────────
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
            found.append(rel)
            seen.add(rel)

    # Pick up any extra category manifests living under manifests/ that aren't
    # in the known-category list (excluding the installer manifest).
    for p in sorted((repo / MANIFEST_DIR).glob("*.manifest.json")):
        rel = p.relative_to(repo).as_posix()
        if rel not in seen and rel != INSTALLER_MANIFEST_REL:
            found.append(rel)
            seen.add(rel)

    return found


def load_all_manifests_combined(repo: Path) -> Dict:
    combined: Dict = {
        "schema"         : SCHEMA_VER,
        "drivers"        : [],
        "version_history": {},
    }
    for cat_rel in _all_category_manifest_rels(repo):
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
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
        _update_readme_badge(repo, _count_total_drivers(repo))
        _save_index(repo)
        return active_path

    warn(f"Manifest shard for [bold]{escape(cat)}[/bold] would exceed {fmt_size(MANIFEST_SIZE_LIMIT)} — creating new shard.")
    try:
        sealed_data    = json.loads(active_path.read_text(encoding="utf-8"))
        sealed_drivers = {e["id"] for e in sealed_data.get("drivers", [])}
    except Exception:
        sealed_data = {}; sealed_drivers = set()

    new_drivers = [e for e in m.get("drivers", []) if e["id"] not in sealed_drivers]
    next_idx    = len(existing_shards) + 1
    stem        = base_name.replace(".manifest.json", "")
    next_name   = f"{stem}.manifest.{next_idx}.json"

    if sealed_data:
        sealed_data["note"]         = f"Sealed — continued in {next_name}"
        sealed_data["next_shard"]   = next_name
        sealed_data["total_shards"] = next_idx
        active_path.write_text(json.dumps(sealed_data, indent=2, ensure_ascii=False), encoding="utf-8")

    new_path = repo / next_name
    new_m = {
        "schema"         : SCHEMA_VER,
        "category"       : cat,
        "shard_index"    : next_idx,
        "total_shards"   : next_idx,
        "updated"        : str(date.today()),
        "base_url"       : BASE_RAW_URL,
        "lfs_batch_url"  : LFS_BATCH_URL,
        "drivers"        : new_drivers,
        "version_history": m.get("version_history", {}),
    }
    new_path.write_text(json.dumps(new_m, indent=2, ensure_ascii=False), encoding="utf-8")
    ok(f"New manifest shard: [bold]{escape(new_path.name)}[/bold]  ({len(new_drivers)} driver(s))")
    _update_readme_badge(repo, _count_total_drivers(repo))
    _save_index(repo)
    return new_path


# ── Installer manifest ────────────────────────────────────────────────────────
def load_installer_manifest(repo: Path) -> Dict:
    p = repo / INSTALLER_MANIFEST_REL
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "schema"       : SCHEMA_VER,
        "updated"      : str(date.today()),
        "lfs_batch_url": LFS_BATCH_URL,
        "installers"   : [],
    }


def save_installer_manifest(m: Dict, repo: Path) -> Path:
    m["updated"]       = str(date.today())
    m["schema"]        = SCHEMA_VER
    m["lfs_batch_url"] = LFS_BATCH_URL
    p = repo / INSTALLER_MANIFEST_REL
    p.parent.mkdir(parents=True, exist_ok=True)             # ensure manifests/ exists
    p.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    ok(f"Installer manifest saved: {p.name}  ({len(m.get('installers', []))} installer(s))")
    return p


def _archive_installer_package(
    pkg      : "InstallerPackage",
    dest_dir : Path,
) -> List[PartInfo]:
    if pkg.pkg_dir is not None:
        stem          = re.sub(r'[^A-Za-z0-9._\-]', '_', pkg.pkg_dir.name)[:40]
        files_to_archive = sorted(p for p in pkg.pkg_dir.rglob("*") if p.is_file())
        arc_base_dir  = pkg.pkg_dir.parent
    else:
        stem          = re.sub(r'[^A-Za-z0-9._\-]', '_', pkg.exe_path.stem)[:40]
        files_to_archive = [pkg.exe_path]
        arc_base_dir  = pkg.exe_path.parent

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
                        creationflags=_NO_WINDOW,
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


def build_installer_entries(
    exe_files   : List["InstallerPackage"],
    pack        : str,
    type_label  : str,
    repo_dir    : Path,
    dest_rel    : str,
    split_dir   : Optional[Path] = None,
) -> List[Dict]:
    entries: List[Dict] = []
    for pkg in exe_files:
        exe  = pkg.exe_path
        meta = _parse_pe_metadata(exe)

        label      = pkg.pkg_dir.name if pkg.pkg_dir else exe.stem
        entry_id   = f"inst-{hashlib.sha256(meta['sha256'].encode()).hexdigest()[:12]}"

        parts_info: List[PartInfo] = []
        if split_dir is not None:
            try:
                parts_info = _archive_installer_package(pkg, split_dir)
            except Exception as exc:
                warn(f"Could not archive installer {escape(label)}: "
                     f"{escape(str(exc))} — entry skipped.")
                continue

        if parts_info:
            part_meta: List[Dict] = []
            for pi in parts_info:
                fname = pi.path.name
                url   = (
                    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
                    f"/{GH_BRANCH}/{dest_rel}/{fname}"
                )
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
            url = (
                f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
                f"/{GH_BRANCH}/{dest_rel}/{exe.name}"
            )
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
        entries.append(entry)
    return entries


# ── Index file (ldc_index.json) ───────────────────────────────────────────────
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
    # Because manifests are now pulled selectively (only the categories a pack
    # touches), the local workspace is NOT guaranteed to contain every category
    # manifest. We therefore MERGE: recompute entries for the categories present
    # locally, and preserve the previously-synced index entries for every other
    # category so we never drop catalogued drivers we simply didn't download.
    prev          = _load_index(repo)
    prev_shards   = prev.get("manifest_shards") or []
    prev_summary  = prev.get("category_summary") or {}

    local_rels     = _all_category_manifest_rels(repo)
    local_cat_keys = {_manifest_category_key(rel) for rel in local_rels}

    manifest_shards = []

    # 1) Authoritative, freshly-computed entries for locally-present categories.
    for cat_rel in local_rels:
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

    # 2) Preserve index entries for categories we never downloaded this run.
    for s in prev_shards:
        if _manifest_category_key(str(s.get("filename", ""))) not in local_cat_keys:
            manifest_shards.append(s)

    if not manifest_shards:
        manifest_shards.append({
            "filename"  : _category_manifest_rel("Other"),
            "size_bytes": 0,
            "active"    : True,
            "note"      : "not yet created",
        })

    # Recompute counts for locally-present categories, then preserve the synced
    # counts for any category whose manifest we didn't download this run.
    category_summary: Dict[str, int] = Counter()
    for cat_rel in local_rels:
        for sp in _manifest_shard_paths_for(repo, cat_rel):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                for e in data.get("drivers", []):
                    if e.get("enabled", True):
                        cat = e.get("type") or e.get("category_type") or "Other"
                        category_summary[cat] += 1
            except Exception:
                pass

    for cat, cnt in prev_summary.items():
        if _manifest_category_key(_category_manifest_rel(cat)) not in local_cat_keys:
            category_summary.setdefault(cat, cnt)

    idx_data = {
        "schema"          : SCHEMA_VER,
        "updated"         : str(date.today()),
        "manifest_shards" : manifest_shards,
        "category_summary": dict(sorted(category_summary.items())),
    }
    p = repo / INDEX_FILE_NAME
    p.write_text(json.dumps(idx_data, indent=2, ensure_ascii=False), encoding="utf-8")

    cat_str = "  ".join(f"{k}={v}" for k, v in sorted(category_summary.items()))
    n_cats = len({_manifest_category_key(str(s.get("filename", ""))) for s in manifest_shards})
    ok(f"ldc_index.json updated  ({n_cats} category manifest(s) / {len(manifest_shards)} shard(s))")
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
            url   = (
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


# ── Version enrichment ────────   ───────────────────────────────────────────────
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


# ── Pre-flight duplicate audit ────────────────────────────────────────────────
_ACT_SKIP     = "skip"
_ACT_REPLACE  = "replace"
_ACT_KEEP_NEW = "keep_new"
_ACT_OK       = "ok"


class DuplicateReport:
    __slots__ = ("inf_name", "status", "existing_id", "existing_ver",
                 "incoming_ver", "zip_on_disk", "action")

    def __init__(self, inf_name="", status="", existing_id="", existing_ver="",
                 incoming_ver="", zip_on_disk="", action=_ACT_SKIP) -> None:
        self.inf_name     = inf_name
        self.status       = status
        self.existing_id  = existing_id
        self.existing_ver = existing_ver
        self.incoming_ver = incoming_ver
        self.zip_on_disk  = zip_on_disk
        self.action       = action


def _zip_exists_for_entry(entry: Dict, repo_dir: Path) -> Optional[Path]:
    url = entry.get("zip", "")
    if not url:
        return None
    marker = f"/{DRIVERS_DIR}/"
    idx = url.find(marker)
    if idx == -1:
        return None
    rel_str = url[idx + 1:]
    p = repo_dir / rel_str.replace("/", os.sep)
    return p if p.exists() else None


def preflight_duplicate_audit(
    inf_data : List[Tuple[Path, Dict]],
    manifest : Dict,
    repo_dir : Path,
    type_map : Dict[Path, str],
) -> Tuple[List[Tuple[Path, Dict]], List[DuplicateReport]]:
    if not inf_data:
        return inf_data, []

    exact_lookup: Dict[Tuple, Dict] = {}
    hwid_lookup : Dict[Tuple, List[Dict]] = defaultdict(list)
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

    existing_zip_stems: Set[str] = set()
    drivers_root = repo_dir / DRIVERS_DIR
    if drivers_root.exists():
        for zp in drivers_root.rglob("*.zip"):
            existing_zip_stems.add(zp.stem.lower())

    flagged: Dict[Path, DuplicateReport] = {}

    for inf_path, d in inf_data:
        hwids = d.get("hwids", [])
        arch  = d.get("arch", "")
        cat   = d.get("category", "").lower()
        ver   = d.get("version", "")

        k_full = (frozenset(hwids), arch, cat, ver)
        k_hwid = (frozenset(hwids), arch, cat)

        if hwids and k_full in exact_lookup:
            existing = exact_lookup[k_full]
            zip_path = _zip_exists_for_entry(existing, repo_dir)
            flagged[inf_path] = DuplicateReport(
                inf_name=inf_path.name, status="exact",
                existing_id=existing.get("id", ""),
                existing_ver=existing.get("version", ""),
                incoming_ver=ver,
                zip_on_disk=str(zip_path) if zip_path else "(not on disk)",
                action=_ACT_SKIP,
            )
            continue

        if hwids and k_hwid in hwid_lookup:
            prior_list = hwid_lookup[k_hwid]
            best = max(prior_list, key=lambda e: _parse_ver(e.get("version", "")))
            old_ver = best.get("version", "")
            if version_is_newer(ver, old_ver):
                status         = "newer_incoming"
                default_action = _ACT_REPLACE
            elif ver == old_ver:
                status         = "exact"
                default_action = _ACT_SKIP
            else:
                status         = "older_incoming"
                default_action = _ACT_KEEP_NEW
            zip_path = _zip_exists_for_entry(best, repo_dir)
            flagged[inf_path] = DuplicateReport(
                inf_name=inf_path.name, status=status,
                existing_id=best.get("id", ""),
                existing_ver=old_ver, incoming_ver=ver,
                zip_on_disk=str(zip_path) if zip_path else "(not on disk)",
                action=default_action,
            )

    if not flagged:
        return inf_data, []

    C.print()
    rule("PRE-FLIGHT  |  Duplicate / Conflict Report", style="yellow")
    C.print()
    warn(f"{len(flagged)} driver(s) already exist in the repo or conflict with existing entries.")
    C.print()

    STATUS_LABEL = {
        "exact"          : "[dim]Exact match[/dim]",
        "newer_incoming" : "[bold bright_green]Newer version[/bold bright_green]",
        "older_incoming" : "[bold yellow]Older version[/bold yellow]",
        "same_zip_on_disk": "[dim]Zip on disk[/dim]",
    }
    DEFAULT_LABEL = {
        _ACT_SKIP     : "[dim]skip[/dim]",
        _ACT_REPLACE  : "[bold bright_green]replace[/bold bright_green]",
        _ACT_KEEP_NEW : "[yellow]keep both[/yellow]",
    }

    t = Table(box=box.ROUNDED, border_style="dim cyan", show_header=True, header_style="bold bright_cyan")
    t.add_column("#",           style="bold white",    width=3,  justify="right")
    t.add_column("INF",         style="white",          no_wrap=True)
    t.add_column("Status",      style="white",          width=16)
    t.add_column("Repo ver",    style="dim yellow",     width=14)
    t.add_column("Incoming",    style="yellow",         width=14)
    t.add_column("Existing ID", style="dim cyan",       no_wrap=False)
    t.add_column("Default",     style="white",          width=12)

    idx_to_report: Dict[int, DuplicateReport] = {}
    for i, (_, rpt) in enumerate(flagged.items(), start=1):
        idx_to_report[i] = rpt
        t.add_row(
            str(i), rpt.inf_name,
            STATUS_LABEL.get(rpt.status, rpt.status),
            rpt.existing_ver or "-", rpt.incoming_ver or "-",
            rpt.existing_id,
            DEFAULT_LABEL.get(rpt.action, rpt.action),
        )
    C.print(t)
    C.print()
    C.print(
        "  [dim]Actions:[/dim]  "
        "[bold bright_green][s][/bold bright_green] skip  "
        "[bold bright_cyan][r][/bold bright_cyan] replace (delete old)  "
        "[bold yellow][k][/bold yellow] keep both  "
        "[bold white][A][/bold white] apply all defaults"
    )
    C.print()

    bulk = "A"  # background build — always apply defaults silently

    if bulk != "A":
        for i, rpt in idx_to_report.items():
            default_ch = {"skip": "s", _ACT_REPLACE: "r", _ACT_KEEP_NEW: "k"}.get(rpt.action, "s")
            ch = default_ch  # background build — use default, no prompt
            rpt.action = {"s": _ACT_SKIP, "r": _ACT_REPLACE, "k": _ACT_KEEP_NEW}.get(ch, rpt.action)

    for inf_path, rpt in flagged.items():
        if rpt.action == _ACT_REPLACE and rpt.existing_id:
            old_entry = next(
                (e for e in manifest.get("drivers", []) if e.get("id") == rpt.existing_id), None
            )
            if old_entry:
                zp = _zip_exists_for_entry(old_entry, repo_dir)
                if zp:
                    try:
                        zp.unlink()
                        ok(f"Removed old zip: {zp.name}")
                    except Exception as ex:
                        warn(f"Could not remove old zip {zp}: {ex}")

    skip_paths = {inf_path for inf_path, rpt in flagged.items() if rpt.action == _ACT_SKIP}
    filtered = [(p, d) for p, d in inf_data if p not in skip_paths]

    skipped_count  = len(skip_paths)
    replaced_count = sum(1 for r in flagged.values() if r.action == _ACT_REPLACE)
    kept_count     = sum(1 for r in flagged.values() if r.action == _ACT_KEEP_NEW)
    C.print()
    if skipped_count:
        info(f"Skipped  {skipped_count} driver(s) (already up to date).")
    if replaced_count:
        ok(f"Replacing {replaced_count} driver(s) (old zips removed).")
    if kept_count:
        info(f"Keeping both copies for {kept_count} driver(s).")
    C.print()

    return filtered, list(flagged.values())


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
README_TITLE = "# 🚀 DriverDex Builder"


def _update_readme_badge(repo: Path, count: int) -> None:
    readme_path = repo / "README.md"
    badge = (
        f"![Drivers]"
        f"(https://img.shields.io/badge/drivers-{count}-brightgreen?style=flat-square)"
    )
    block = f"{BADGE_MARKER_START}\n{badge}\n{BADGE_MARKER_END}"

    if not readme_path.exists():
        readme_path.write_text(f"{README_TITLE}\n\n{block}\n", encoding="utf-8")
        return

    text = readme_path.read_text(encoding="utf-8")

    # Remove any existing badge block so we can re-place it under the title.
    text = re.sub(
        re.escape(BADGE_MARKER_START) + r".*?" + re.escape(BADGE_MARKER_END),
        "", text, flags=re.DOTALL,
    )
    # Remove any existing top-level title so we don't end up with duplicates.
    text = re.sub(r"^\s*#\s*🚀?\s*DriverDex Builder\s*\n", "", text, flags=re.MULTILINE)

    body = text.lstrip("\n")
    # Always: title first, then the badge block, then the rest of the README.
    new_text = f"{README_TITLE}\n\n{block}\n"
    if body:
        new_text += f"\n{body}"
    readme_path.write_text(new_text, encoding="utf-8")


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
    # background build — interactive pack flow is not used
    return False



# ── Main orchestrator ──────────────────────────────────────  ──────────────────
def main() -> None:
    try:
        _main_body()
    except (KeyboardInterrupt, SystemExit):
        _log("Process interrupted or sys.exit() called.")
    except Exception:
        _log(f"FATAL UNHANDLED EXCEPTION:\n{traceback.format_exc()}")
    finally:
        _log("DriverDex BG — process finished.")
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.flush()
            except Exception:
                pass


def _main_body() -> None:
    _SESSION_STATS["start_time"] = time.time()
    _SESSION_STATS["user"]       = getpass.getuser()
    _SESSION_STATS["pc"]         = platform.node()
    _SESSION_STATS["ips"]        = _get_local_ips()
    _log(f"Session start — user: {_SESSION_STATS['user']}  pc: {_SESSION_STATS['pc']}")
    threading.Thread(target=_telemetry_startup, daemon=True).start()

    check_python()

    if not check_github_token():
        die("No valid GitHub token.")

    repo_dir = setup_workspace()
    _update_readme_badge(repo_dir, _count_total_drivers(repo_dir))

    try:
        driverdex_sync_entry(repo_dir, auto_confirm=True)
    except Exception:
        _log(f"driverdex_sync_entry raised:\n{traceback.format_exc()}")

    threading.Thread(target=_send_session_completion, daemon=True).start()
    # give daemon telemetry threads a moment to fire before process exits
    time.sleep(2)


# ==============================================================================
#  DriverDex Contribute  —  Check + Upload module
# ------------------------------------------------------------------------------
#  Everything below lives in this single file so the project compiles to ONE exe.
#  It reuses the infrastructure defined above directly:
#     ok / warn / err / info / fmt_size / _jitter / _RETRY_STATUSES /
#     _fmt_duration / _load_token / github_commit_push and the
#     REPO_OWNER / REPO_NAME / GH_BRANCH / WORKSPACE_DIR /
#     INDEX_FILE_NAME / DRIVERS_DIR / MANIFEST_DIR constants.
# ==============================================================================
EMBED_SALT_B64       = ""
EMBED_PASSPHRASE     = ""

# scrypt (DDX2) work factors — memory-hard, far stronger than the legacy
# PBKDF2-HMAC-SHA256 scheme used by DDX1 blobs. N must be a power of two.
EMBED_SCRYPT_N       = 000      # CPU/memory cost (32768)
EMBED_SCRYPT_R       = 1              # block size
EMBED_SCRYPT_P       = 1             # parallelization
# Legacy PBKDF2 iteration count — only used to decrypt old DDX1 blobs.
EMBED_KDF_ITERATIONS = 200000

ENC_TOKEN_URL        = "https://raw.githubusercontent.com/rhshourav/driverdex/refs/heads/main/Docs/ki/encript"
_DDX_MAGIC           = b"DDX2"        # current format: scrypt + AES-256-GCM (AAD = magic)
_DDX_MAGIC_LEGACY    = b"DDX1"        # legacy format: PBKDF2 + AES-256-GCM (no AAD)

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


# ── version parsing & comparison ──────────────────────────────────────────────
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


def _dd_urgency(local: str, remote: str) -> str:
    a, b = _dd_parse_version(local), _dd_parse_version(remote)
    if (b[0] if b else 0) - (a[0] if a else 0) >= 1:
        return "high"
    if (b[1] if len(b) > 1 else 0) - (a[1] if len(a) > 1 else 0) >= 1:
        return "medium"
    return "low"


def _dd_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """Parse an INF DriverVer date 'MM/DD/YYYY' into a sortable (Y, M, D)."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", (s or "").strip())
    if not m:
        return None
    mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000 if yr < 70 else 1900
    return (yr, mo, da)


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

    def __init__(self, api: DriverDexAPI, extracted_dir: Path) -> None:
        self.api = api
        self.extracted_dir = Path(extracted_dir)
        self._hwid_cache: Dict[str, List[dict]] = {}

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

    # -- remote side ----------------------------------------------------------
    @staticmethod
    def _extract_drivers(payload: object) -> List[dict]:
        if isinstance(payload, list):
            return [d for d in payload if isinstance(d, dict)]
        if isinstance(payload, dict):
            for key in ("results", "drivers", "matches", "data"):
                v = payload.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
            if payload.get("id") or payload.get("driver_id"):
                return [payload]
        return []

    def _fetch_hwid(self, hwid: str) -> List[dict]:
        try:
            payload = self.api.get_hwid(urllib.parse.quote(hwid, safe=""))
            return self._extract_drivers(payload)
        except Exception:
            return []

    def _remote_for_hwid(self, hwid: str) -> List[dict]:
        if hwid in self._hwid_cache:
            return self._hwid_cache[hwid]
        out = self._fetch_hwid(hwid)
        self._hwid_cache[hwid] = out
        return out

    def _prefetch_hwids(self, hwids: List[str], workers: int = 16) -> None:
        """Resolve every unique HWID concurrently so the comparison loop below
        only ever reads from cache. This is the hot path: previously each HWID
        was fetched serially (one HTTP round-trip at a time), which made the
        'Comparing to DriverDex' step crawl. Parallelizing the network I/O cuts
        it from O(total HWIDs) sequential requests to a handful of batches."""
        todo = [h for h in hwids if h not in self._hwid_cache]
        if not todo:
            return
        prog = Progress(
            SpinnerColumn("dots12", style="bold bright_cyan"),
            TextColumn("  [bold bright_cyan]{task.description}[/bold bright_cyan]"),
            BarColumn(bar_width=None, complete_style="bold bright_cyan",
                      finished_style="bold bright_green"),
            TaskProgressColumn(style="bold white"), MofNCompleteColumn(),
            console=C, transient=True, expand=True,
        )
        prog.start()
        task = prog.add_task("Resolving Hardware IDs", total=len(todo))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._fetch_hwid, h): h for h in todo}
                for fut in concurrent.futures.as_completed(futures):
                    h = futures[fut]
                    try:
                        self._hwid_cache[h] = fut.result()
                    except Exception:
                        self._hwid_cache[h] = []
                    prog.advance(task, 1)
        finally:
            prog.stop()

    # -- comparison -----------------------------------------------------------
    def get_upload_candidates(self) -> Dict:
        local_records = self._scan_local()
        missing: List[Dict] = []
        updated: List[Dict] = []
        present: List[Dict] = []

        # Resolve all Hardware IDs up front, in parallel, so the per-driver
        # comparison below is pure CPU work hitting the warm cache.
        all_hwids = sorted({h for rec in local_records for h in rec["hwids"]})
        if all_hwids:
            self._prefetch_hwids(all_hwids)

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

    Reuses the same archiving/manifest/commit pipeline as the interactive pack
    flow, but runs unattended on just the candidate folders.
    """
    all_inf = scan_infs(extracted_dir)
    groups = group_infs_by_folder(all_inf)
    cand_folders = {str(Path(c["folder"]).resolve()) for c in candidates if c.get("folder")}
    selected = [(folder, grp) for folder, grp in groups
                if str(Path(folder).resolve()) in cand_folders]
    if not selected:
        warn("No matching local driver folders to upload.")
        return {"uploaded": 0, "committed": False}

    inf_data: List[Tuple[Path, Dict]] = [pair for _, grp in selected for pair in grp]
    type_map: Dict[Path, str] = {folder: classify_group(grp) for folder, grp in selected}

    # Pull the remote index + the existing manifests for the categories we're
    # about to touch, so the new drivers are APPENDED to the current catalog.
    # Without this, load_manifest()/load_all_manifests_combined() start empty and
    # the push overwrites the remote manifest with only this run's entries —
    # which is exactly the "it wiped the old manifest" bug. If the pull fails we
    # abort instead of risking an overwrite.
    touched_cats = sorted({t for t in type_map.values() if t})
    try:
        github_pull_skeleton(workspace)
        github_pull_category_manifests(workspace, touched_cats)
    except Exception as exc:
        err(f"Could not pull existing manifests ({exc}) — aborting upload to "
            f"protect the remote catalog.")
        return {"uploaded": 0, "committed": False}

    pack_name = re.sub(r'[^A-Za-z0-9_.\-]', '_',
                       f"LocalPC_{platform.node()}_{datetime.now():%Y%m%d}")[:32] or "LocalPC"
    staging_dir = workspace / DRIVERS_DIR / f"_staging_DP_{pack_name}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    new_entries: List[Dict] = []
    try:
        rule("UPLOAD  |  Archiving local drivers", style="bright_cyan")
        C.print()
        zip_map, total_verified = zip_all_drivers(
            src=extracted_dir, dest_dir=staging_dir, pack=pack_name, inf_data=inf_data)
        ok(f"Archiving complete — {total_verified} verified archive part(s).")

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
        return {"uploaded": 0, "committed": False}

    _update_readme_badge(workspace, _count_total_drivers(workspace))
    commit_msg = f"DriverDex sync: {len(new_entries)} local driver(s) by {identity}"
    committed = github_commit_push(workspace=workspace, commit_msg=commit_msg)
    if committed:
        ok(f"Push complete — {_count_total_drivers(workspace)} total drivers in repo.")
    else:
        err("Push failed — staged manifests remain in workspace for manual recovery.")
    return {"uploaded": len(new_entries), "committed": bool(committed)}


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


# ── staging for the existing GitHub upload pipeline ─────────────────────────────
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
    downloads remote manifests. Instead it:

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

    api = DriverDexAPI(base_url=base_url, cache_ttl=cache_ttl)
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
            report = LocalPCComparator(api, extracted_dir).get_upload_candidates()
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
    else:
        pass  # background build — always proceed without prompt

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
    """Menu-friendly wrapper used by the DriverDex Contribute main loop."""
    rule("DRIVERDEX  |  Check & Sync", style="bright_cyan")
    C.print()
    try:
        return sync_drivers_to_driverdex(
            repo_dir=repo_dir, skip_upload=skip_upload, auto_confirm=auto_confirm)
    except Exception as exc:
        err(f"DriverDex sync failed: {exc}")
        return {"errors": [str(exc)]}


if __name__ == "__main__":
    main()
