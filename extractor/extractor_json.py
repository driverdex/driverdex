#!/usr/bin/env python3
"""
Driver Extractor & Installer
============================

A single-file, terminal-based tool that consumes a JSON driver manifest,
downloads multi-part 7-Zip driver packages, verifies their SHA256 checksums,
and extracts them to an organized directory tree -- all with a rich,
color-coded terminal UI.

Multi-part 7z packages (``name.7z.0001``, ``name.7z.0002`` ...) are simply the
raw .7z byte stream split into fixed-size chunks. Concatenating the parts in
order reproduces the original archive. This tool exposes that concatenation as
a single *seekable* file-like object (without ever loading everything into RAM)
and feeds it to py7zr, keeping memory usage flat even for very large archives.

Usage
-----
    python extractor.py manifest.json
    python extractor.py manifest.json --output ./drivers --parallel 4
    python extractor.py manifest.json --no-verify --cleanup
    python extractor.py manifest.json --dry-run
    python extractor.py manifest.json --no-interactive --verbose

Requires: Python 3.9+, rich, requests, py7zr
    pip install rich requests py7zr
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import logging
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Dependency checks (friendly messages instead of raw tracebacks)
# --------------------------------------------------------------------------- #

_MISSING = []
try:
    import requests
except ImportError:
    _MISSING.append("requests")
try:
    import py7zr
    from py7zr.exceptions import Bad7zFile, PasswordRequired
except ImportError:
    _MISSING.append("py7zr")
try:
    from rich.console import Console, Group
    from rich.table import Table
    from rich.panel import Panel
    from rich.live import Live
    from rich.text import Text
    from rich.prompt import Confirm, Prompt
    from rich.logging import RichHandler
    from rich.align import Align
except ImportError:
    _MISSING.append("rich")

if _MISSING:
    sys.stderr.write(
        "ERROR: missing required package(s): "
        + ", ".join(_MISSING)
        + "\nInstall them with:\n    pip install "
        + " ".join(_MISSING)
        + "\n"
    )
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Constants & theme
# --------------------------------------------------------------------------- #

APP_NAME = "Driver Extractor & Installer"
APP_VERSION = "1.0"
LOG_FILE = "extractor.log"
DEFAULT_OUTPUT = "drivers"
DEFAULT_CHUNK = 1024 * 1024          # 1 MiB streaming buffer
DEFAULT_TIMEOUT = 30                 # seconds per request
MAX_RETRIES = 3
MAX_PARALLEL = 8
MAX_EXPANSION_RATIO = 500            # zip-bomb guard
MAX_TOTAL_UNCOMPRESSED = 16 * 1024**4

# Status -> (label, rich style)
STATUS_STYLES = {
    "pending":      ("PENDING",     "grey50"),
    "downloading":  ("DOWNLOADING", "bright_blue"),
    "verifying":    ("VERIFYING",   "cyan"),
    "extracting":   ("EXTRACTING",  "yellow"),
    "completed":    ("COMPLETED",   "bright_green"),
    "failed":       ("FAILED",      "bright_red"),
    "skipped":      ("SKIPPED",     "grey50"),
}

console = Console()
log = logging.getLogger("extractor")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool, log_path: str = LOG_FILE) -> None:
    log.handlers.clear()
    log.setLevel(logging.DEBUG)

    # File handler: full detail, always.
    try:
        fileh = logging.FileHandler(log_path, encoding="utf-8")
        fileh.setLevel(logging.DEBUG)
        fileh.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(threadName)-14s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        log.addHandler(fileh)
    except OSError as exc:
        console.print(f"[yellow]Could not open log file {log_path!r}: {exc}[/]")

    # Console handler only when verbose; the Live TUI renders its own activity
    # log, so by default we keep stdout quiet to avoid clobbering the dashboard.
    if verbose:
        rich_h = RichHandler(console=console, show_path=False, rich_tracebacks=True)
        rich_h.setLevel(logging.DEBUG)
        log.addHandler(rich_h)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def human_size(n: float) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if f < 1024 or unit == "PB":
            return f"{int(f)} B" if unit == "B" else f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} PB"


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def safe_component(name: str) -> str:
    """Make a manifest-provided string safe to use as a folder name."""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "unnamed"


# --------------------------------------------------------------------------- #
# Manifest model
# --------------------------------------------------------------------------- #

@dataclass
class Part:
    part_num: int
    filename: str
    size_bytes: int
    sha256: Optional[str]
    url: str


@dataclass
class Driver:
    driver_id: str
    display_name: str
    version: Optional[str]
    provider: Optional[str]
    category: Optional[str]
    arch: Optional[str]
    pack: Optional[str]
    parts: list[Part] = field(default_factory=list)

    # Runtime state (mutated during processing) ---------------------------- #
    status: str = "pending"
    downloaded: int = 0
    detail: str = ""
    error: Optional[str] = None
    extracted_files: int = 0

    @property
    def total_size(self) -> int:
        return sum(p.size_bytes for p in self.parts)

    @property
    def folder_name(self) -> str:
        # e.g. "advanced-syst-x64-11.04.02.1" derived from first part filename.
        base = self.parts[0].filename if self.parts else self.driver_id
        base = re.sub(r"\.7z\.\d+$", "", base, flags=re.IGNORECASE)
        base = re.sub(r"\.7z$", "", base, flags=re.IGNORECASE)
        return safe_component(base)

    @property
    def pack_folder(self) -> str:
        return safe_component(self.pack or self.category or "misc")

    @property
    def unique_key(self) -> str:
        """Short, stable, filesystem-safe id unique to this driver.

        Used to isolate per-driver working files so that drivers which share
        a pack and identical part filenames never collide on disk.
        """
        basis = self.driver_id or f"{self.display_name}-{self.folder_name}"
        digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]
        return f"{safe_component(self.driver_id or self.folder_name)[:40]}-{digest}"


class ManifestError(Exception):
    pass


def parse_manifest(
    path: Optional[Path] = None,
    *,
    raw_text: Optional[str] = None,
    url: Optional[str] = None,
    source: Optional[str] = None,
) -> list[Driver]:
    """Parse and validate a JSON manifest from a file path, URL, or raw text.

    Exactly one of ``path``, ``raw_text``, or ``url`` should be supplied.
    Malformed individual drivers are skipped with a warning.
    """
    if raw_text is not None:
        label = source or "<inline JSON>"
        text = raw_text
    elif url is not None:
        label = url
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            text = resp.text
        except requests.RequestException as exc:
            raise ManifestError(f"Could not fetch manifest from {url}: {exc}") from exc
    elif path is not None:
        label = path.name
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ManifestError(f"Manifest file not found: {path}") from exc
        except OSError as exc:
            raise ManifestError(f"Could not read manifest {path}: {exc}") from exc
    else:
        raise ManifestError("No manifest source provided.")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Invalid JSON in {label}: {exc}") from exc

    drivers_raw = raw.get("drivers")
    if not isinstance(drivers_raw, list) or not drivers_raw:
        raise ManifestError("Manifest contains no 'drivers' array.")

    drivers: list[Driver] = []
    for idx, d in enumerate(drivers_raw):
        try:
            parts_raw = d.get("parts") or []
            parts: list[Part] = []
            for p in parts_raw:
                url = (p.get("url") or "").strip()
                fname = (p.get("filename") or "").strip()
                if not url or not fname:
                    log.warning("Driver %s: part missing url/filename, skipping part.",
                                d.get("driver_id", idx))
                    continue
                if not re.match(r"^https?://", url, re.IGNORECASE):
                    log.warning("Driver %s: malformed URL %r, skipping part.",
                                d.get("driver_id", idx), url)
                    continue
                parts.append(Part(
                    part_num=int(p.get("part_num", len(parts) + 1)),
                    filename=fname,
                    size_bytes=int(p.get("size_bytes", 0) or 0),
                    sha256=(p.get("sha256") or None),
                    url=url,
                ))
            if not parts:
                log.warning("Driver %s has no valid parts; skipping.",
                            d.get("driver_id", idx))
                continue
            parts.sort(key=lambda x: x.part_num)
            drivers.append(Driver(
                driver_id=d.get("driver_id") or f"driver-{idx}",
                display_name=d.get("display_name") or d.get("driver_id") or f"Driver {idx}",
                version=d.get("version"),
                provider=d.get("provider"),
                category=d.get("category"),
                arch=d.get("arch"),
                pack=d.get("pack"),
                parts=parts,
            ))
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("Skipping malformed driver at index %d: %s", idx, exc)
            continue

    if not drivers:
        raise ManifestError("No valid drivers found in manifest.")
    return drivers


# --------------------------------------------------------------------------- #
# Spanned, seekable, low-memory multi-volume reader
# --------------------------------------------------------------------------- #

class MultiVolumeReader(io.RawIOBase):
    """
    Presents an ordered list of part files as a single continuous, seekable
    stream without loading them into memory. Only one underlying file handle
    is open at a time; reads transparently roll over part boundaries.
    """

    def __init__(self, paths: list[Path]):
        super().__init__()
        self._paths = paths
        self._sizes = [p.stat().st_size for p in paths]
        self._starts: list[int] = []
        acc = 0
        for s in self._sizes:
            self._starts.append(acc)
            acc += s
        self._total = acc
        self._pos = 0
        self._cur_index = -1
        self._cur_fh: Optional[io.BufferedReader] = None
        self._lock = threading.Lock()

    def readable(self) -> bool: return True
    def seekable(self) -> bool: return True
    def writable(self) -> bool: return False

    @property
    def total_size(self) -> int:
        return self._total

    def _volume_for_offset(self, offset: int) -> int:
        lo, hi = 0, len(self._starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _ensure_volume(self, index: int) -> None:
        if index == self._cur_index and self._cur_fh is not None:
            return
        if self._cur_fh is not None:
            self._cur_fh.close()
        self._cur_fh = open(self._paths[index], "rb")
        self._cur_index = index

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        with self._lock:
            if whence == io.SEEK_SET:
                new = offset
            elif whence == io.SEEK_CUR:
                new = self._pos + offset
            elif whence == io.SEEK_END:
                new = self._total + offset
            else:
                raise ValueError(f"Invalid whence: {whence}")
            self._pos = max(0, min(new, self._total))
            return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        with self._lock:
            if size is None or size < 0:
                size = self._total - self._pos
            if self._pos >= self._total or size == 0:
                return b""
            out = bytearray()
            remaining = size
            while remaining > 0 and self._pos < self._total:
                index = self._volume_for_offset(self._pos)
                self._ensure_volume(index)
                local_off = self._pos - self._starts[index]
                self._cur_fh.seek(local_off)
                want = min(remaining, self._sizes[index] - local_off)
                chunk = self._cur_fh.read(want)
                if not chunk:
                    break
                out.extend(chunk)
                self._pos += len(chunk)
                remaining -= len(chunk)
            return bytes(out)

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def close(self) -> None:
        with self._lock:
            if self._cur_fh is not None:
                self._cur_fh.close()
                self._cur_fh = None
        super().close()


# --------------------------------------------------------------------------- #
# Download engine
# --------------------------------------------------------------------------- #

class DownloadError(Exception):
    pass


def verify_sha256(path: Path, expected: str) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(DEFAULT_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected.lower()


def download_part(
    session: "requests.Session",
    part: Part,
    dest: Path,
    *,
    verify: bool,
    bandwidth_limit: float,
    cancel_event: threading.Event,
    on_bytes,
) -> None:
    """
    Download a single part with streaming, resume, retry + exponential backoff,
    and optional checksum verification. Writes to a .temp file and atomically
    renames on success.
    """
    # Already present and valid? Skip and account for its bytes.
    if dest.exists():
        ok = False
        if verify and part.sha256:
            ok = dest.stat().st_size == (part.size_bytes or dest.stat().st_size) \
                and verify_sha256(dest, part.sha256)
        elif part.size_bytes:
            ok = dest.stat().st_size == part.size_bytes
        if ok:
            log.info("Part %s already present & valid; skipping.", part.filename)
            on_bytes(part.size_bytes or dest.stat().st_size)
            return

    tmp = dest.with_suffix(dest.suffix + ".temp")
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        if cancel_event.is_set():
            raise DownloadError("Cancelled.")
        resume_from = tmp.stat().st_size if tmp.exists() else 0
        headers = {}
        mode = "wb"
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
            mode = "ab"
        try:
            with session.get(
                part.url, stream=True, timeout=DEFAULT_TIMEOUT, headers=headers
            ) as resp:
                # If server ignores Range, restart from scratch.
                if resume_from > 0 and resp.status_code == 200:
                    resume_from = 0
                    mode = "wb"
                elif resume_from > 0 and resp.status_code == 206:
                    on_bytes(resume_from)  # account for already-downloaded bytes
                if resp.status_code in (403, 404):
                    raise DownloadError(
                        f"HTTP {resp.status_code} for {part.filename} (skipping)."
                    )
                resp.raise_for_status()

                token_time = time.time()
                with open(tmp, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=DEFAULT_CHUNK):
                        if cancel_event.is_set():
                            raise DownloadError("Cancelled.")
                        if not chunk:
                            continue
                        fh.write(chunk)
                        on_bytes(len(chunk))
                        # Simple bandwidth throttle.
                        if bandwidth_limit > 0:
                            elapsed = time.time() - token_time
                            target = len(chunk) / (bandwidth_limit * 1024 * 1024)
                            if target > elapsed:
                                time.sleep(target - elapsed)
                            token_time = time.time()

            # Verify checksum if available.
            if verify and part.sha256 and not verify_sha256(tmp, part.sha256):
                raise DownloadError(
                    f"Checksum mismatch for {part.filename} "
                    f"(attempt {attempt}); re-downloading."
                )
            tmp.replace(dest)
            log.info("Downloaded %s (%s)", part.filename, human_size(part.size_bytes))
            return

        except DownloadError as exc:
            if "skipping" in str(exc):       # 403/404 are non-retryable
                raise
            last_exc = exc
            log.warning("%s", exc)
            if tmp.exists() and "Checksum mismatch" in str(exc):
                tmp.unlink(missing_ok=True)
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            log.warning("Download error for %s (attempt %d/%d): %s",
                        part.filename, attempt, MAX_RETRIES, exc)

        if attempt < MAX_RETRIES and not cancel_event.is_set():
            time.sleep(2 ** (attempt - 1))   # exponential backoff

    raise DownloadError(
        f"Failed to download {part.filename} after {MAX_RETRIES} attempts: {last_exc}"
    )


# --------------------------------------------------------------------------- #
# Extraction engine
# --------------------------------------------------------------------------- #

def is_safe_path(base: Path, target_name: str) -> bool:
    if not target_name:
        return False
    name = target_name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return False
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return False
    return True


def safety_check_archive(archive: "py7zr.SevenZipFile", input_size: int) -> None:
    total = 0
    for info in archive.list():
        fname = getattr(info, "filename", "") or ""
        norm = fname.replace("\\", "/")
        if norm.startswith("/") or ".." in Path(norm).parts:
            raise DownloadError(f"Unsafe path in archive (traversal): {fname!r}")
        total += getattr(info, "uncompressed", 0) or 0
    if total > MAX_TOTAL_UNCOMPRESSED:
        raise DownloadError("Refusing to extract: uncompressed size exceeds ceiling.")
    if input_size > 0 and total > input_size * MAX_EXPANSION_RATIO:
        raise DownloadError(
            f"Possible zip bomb: {human_size(total)} from {human_size(input_size)} "
            f"(>{MAX_EXPANSION_RATIO}x). Aborting."
        )


def extract_driver(
    driver: Driver,
    part_paths: list[Path],
    out_dir: Path,
    *,
    overwrite: bool,
) -> int:
    """Extract the driver's multi-part 7z archive to out_dir. Returns file count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = MultiVolumeReader(part_paths)
    try:
        try:
            archive = py7zr.SevenZipFile(reader, mode="r")
        except PasswordRequired as exc:
            raise DownloadError("Archive is encrypted; password not supported here.") from exc
        except Bad7zFile as exc:
            raise DownloadError(
                "Invalid 7z stream after assembling parts "
                "(corrupted or incomplete part set)."
            ) from exc

        try:
            safety_check_archive(archive, reader.total_size)
            names = [
                getattr(i, "filename", "")
                for i in archive.list()
                if not getattr(i, "is_directory", False)
            ]
            for name in names:
                if not is_safe_path(out_dir, name):
                    raise DownloadError(f"Unsafe extraction path blocked: {name!r}")

            if names and not overwrite and all((out_dir / n).exists() for n in names):
                log.info("All files for %s already extracted; skipping.", driver.display_name)
                return len(names)

            archive.extractall(path=out_dir)
            return len(names)
        finally:
            archive.close()
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# TUI rendering
# --------------------------------------------------------------------------- #

def banner() -> Panel:
    title = Text(f"{APP_NAME}  v{APP_VERSION}", style="bold bright_cyan")
    return Panel(Align.center(title), border_style="bright_cyan", padding=(0, 2))


def status_text(driver: Driver) -> Text:
    label, style = STATUS_STYLES.get(driver.status, ("?", "white"))
    t = Text(label, style=style)
    if driver.detail:
        t.append(f"  {driver.detail}", style="grey62")
    return t


def build_table(drivers: list[Driver]) -> Table:
    table = Table(expand=True, border_style="grey37", header_style="bold")
    table.add_column("Driver", ratio=3, no_wrap=False)
    table.add_column("Version", ratio=1)
    table.add_column("Arch", justify="center")
    table.add_column("Parts", justify="center")
    table.add_column("Size", justify="right")
    table.add_column("Progress", ratio=2)
    table.add_column("Status", ratio=2)

    for d in drivers:
        total = d.total_size or 1
        pct = min(100, int(d.downloaded / total * 100))
        bar_len = 16
        filled = int(bar_len * pct / 100)
        _, style = STATUS_STYLES.get(d.status, ("", "white"))
        bar = Text("\u2588" * filled, style=style)
        bar.append("\u2591" * (bar_len - filled), style="grey30")
        bar.append(f" {pct:3d}%", style="grey70")
        table.add_row(
            Text(d.display_name, overflow="ellipsis"),
            d.version or "-",
            (d.arch or "-"),
            str(len(d.parts)),
            human_size(d.total_size),
            bar,
            status_text(d),
        )
    return table


class ActivityLog:
    """A small bounded activity log rendered in the TUI."""

    def __init__(self, maxlines: int = 8):
        self.lines: list[Text] = []
        self.maxlines = maxlines
        self._lock = threading.Lock()

    def add(self, message: str, style: str = "white") -> None:
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self.lines.append(Text(f"[{ts}] ", style="grey50") + Text(message, style=style))
            if len(self.lines) > self.maxlines:
                self.lines = self.lines[-self.maxlines:]

    def render(self) -> Panel:
        with self._lock:
            body = Group(*self.lines) if self.lines else Text("Idle.", style="grey50")
        return Panel(body, title="Activity", border_style="grey37", padding=(0, 1))


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    output: Path
    parallel: int
    verify: bool
    cleanup: bool
    overwrite: bool
    bandwidth_limit: float
    interactive: bool


@dataclass
class Report:
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    downloaded_bytes: int = 0
    duration: float = 0.0
    drivers: list[dict] = field(default_factory=list)


class Orchestrator:
    def __init__(self, drivers: list[Driver], settings: Settings):
        self.drivers = drivers
        self.settings = settings
        self.activity = ActivityLog()
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._dir_lock = threading.Lock()
        self._claimed_dirs: set[Path] = set()
        self._total_bytes = sum(d.total_size for d in drivers)
        self._done_bytes = 0
        self._start = 0.0

    # -- progress bookkeeping -------------------------------------------- #
    def _add_bytes(self, n: int) -> None:
        with self._lock:
            self._done_bytes += n

    @property
    def overall_pct(self) -> int:
        if self._total_bytes <= 0:
            return 0
        return min(100, int(self._done_bytes / self._total_bytes * 100))

    @property
    def speed(self) -> float:
        elapsed = max(1e-6, time.time() - self._start)
        return self._done_bytes / elapsed

    @property
    def eta(self) -> float:
        spd = self.speed
        if spd <= 0:
            return 0
        return max(0, (self._total_bytes - self._done_bytes) / spd)

    def _claim_extract_dir(self, pack_dir: Path, base_name: str, key: str) -> Path:
        """Reserve a unique extraction directory for a driver.

        Two distinct drivers that resolve to the same human-readable folder
        name get disambiguated with the driver's short unique key, so they
        never extract on top of each other.
        """
        with self._dir_lock:
            candidate = pack_dir / base_name
            if candidate in self._claimed_dirs or candidate.exists():
                candidate = pack_dir / f"{base_name}-{key.rsplit('-', 1)[-1]}"
            self._claimed_dirs.add(candidate)
            return candidate

    # -- per-driver pipeline --------------------------------------------- #
    def _process_driver(self, driver: Driver, session: "requests.Session") -> None:
        if self.cancel_event.is_set():
            driver.status = "skipped"
            return

        pack_dir = self.settings.output / driver.pack_folder
        pack_dir.mkdir(parents=True, exist_ok=True)
        # Per-driver working directory for archive parts, isolated by a unique
        # key so concurrent drivers sharing part filenames never collide.
        parts_dir = pack_dir / f".parts-{driver.unique_key}"
        parts_dir.mkdir(parents=True, exist_ok=True)
        extract_dir = self._claim_extract_dir(
            pack_dir, driver.folder_name, driver.unique_key
        )

        part_paths: list[Path] = []
        try:
            # ---- Download phase ---- #
            driver.status = "downloading"
            for i, part in enumerate(driver.parts, start=1):
                if self.cancel_event.is_set():
                    driver.status = "skipped"
                    return
                driver.detail = f"part {i}/{len(driver.parts)}"
                dest = parts_dir / part.filename
                self.activity.add(f"Downloading {part.filename}", "bright_blue")

                def on_bytes(n: int, drv=driver):
                    drv.downloaded += n
                    self._add_bytes(n)

                download_part(
                    session, part, dest,
                    verify=self.settings.verify,
                    bandwidth_limit=self.settings.bandwidth_limit,
                    cancel_event=self.cancel_event,
                    on_bytes=on_bytes,
                )
                part_paths.append(dest)

            # ---- Verify phase (performed inline during download) ---- #
            if self.settings.verify:
                driver.status = "verifying"
                driver.detail = "checksums OK"

            # ---- Extract phase ---- #
            driver.status = "extracting"
            driver.detail = ""
            self.activity.add(f"Extracting {driver.display_name}", "yellow")
            count = extract_driver(
                driver, part_paths, extract_dir,
                overwrite=self.settings.overwrite,
            )
            driver.extracted_files = count

            # ---- Cleanup phase ---- #
            if self.settings.cleanup:
                for p in part_paths:
                    p.unlink(missing_ok=True)
                shutil.rmtree(parts_dir, ignore_errors=True)
                self.activity.add(f"Removed {len(part_paths)} archive part(s)", "grey62")

            driver.status = "completed"
            driver.detail = f"{count} file(s)"
            self.activity.add(f"Completed {driver.display_name}", "bright_green")

        except DownloadError as exc:
            driver.status = "failed"
            driver.error = str(exc)
            driver.detail = "error"
            self.activity.add(f"Failed {driver.display_name}: {exc}", "bright_red")
            log.error("Driver %s failed: %s", driver.display_name, exc)
        except Exception as exc:  # noqa: BLE001 - keep the run alive
            driver.status = "failed"
            driver.error = repr(exc)
            driver.detail = "error"
            self.activity.add(f"Failed {driver.display_name}: {exc}", "bright_red")
            log.exception("Unexpected error for driver %s", driver.display_name)

    # -- render the live dashboard --------------------------------------- #
    def _render(self) -> Group:
        footer = Text()
        footer.append(f"Overall {self.overall_pct:3d}%", style="bold")
        footer.append("   ")
        footer.append(f"{human_size(self._done_bytes)} / {human_size(self._total_bytes)}",
                      style="grey70")
        footer.append("   ")
        footer.append(f"Speed {human_size(self.speed)}/s", style="cyan")
        footer.append("   ")
        footer.append(f"ETA {human_time(self.eta)}", style="grey70")
        return Group(
            banner(),
            build_table(self.drivers),
            Panel(footer, border_style="grey37", padding=(0, 1)),
            self.activity.render(),
        )

    # -- main run -------------------------------------------------------- #
    def run(self) -> Report:
        self._start = time.time()
        report = Report(total=len(self.drivers))

        session = requests.Session()
        session.headers.update({"User-Agent": f"{APP_NAME}/{APP_VERSION}"})

        workers = max(1, min(self.settings.parallel, MAX_PARALLEL))

        with Live(self._render(), console=console, refresh_per_second=8,
                  screen=False) as live:
            updater_stop = threading.Event()

            def refresh_loop():
                while not updater_stop.is_set():
                    live.update(self._render())
                    time.sleep(0.12)

            refresher = threading.Thread(target=refresh_loop, name="tui-refresh",
                                         daemon=True)
            refresher.start()

            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="dl"
                ) as pool:
                    futures = {
                        pool.submit(self._process_driver, d, session): d
                        for d in self.drivers
                    }
                    try:
                        for _ in concurrent.futures.as_completed(futures):
                            pass
                    except KeyboardInterrupt:
                        self.cancel_event.set()
                        self.activity.add("Cancelling (Ctrl-C)...", "bright_red")
            finally:
                updater_stop.set()
                refresher.join(timeout=1)
                live.update(self._render())

        # Build report.
        report.duration = time.time() - self._start
        report.downloaded_bytes = self._done_bytes
        for d in self.drivers:
            if d.status == "completed":
                report.completed += 1
            elif d.status == "failed":
                report.failed += 1
            elif d.status == "skipped":
                report.skipped += 1
            report.drivers.append({
                "driver_id": d.driver_id,
                "display_name": d.display_name,
                "version": d.version,
                "status": d.status,
                "size_bytes": d.total_size,
                "extracted_files": d.extracted_files,
                "error": d.error,
            })
        return report


# --------------------------------------------------------------------------- #
# Summary & report output
# --------------------------------------------------------------------------- #

def print_summary(report: Report) -> None:
    table = Table(title="Installation Summary", border_style="bright_cyan",
                  title_style="bold bright_cyan", show_header=False, expand=False)
    table.add_column("Metric", style="grey70")
    table.add_column("Value", style="bold")
    table.add_row("Total Drivers", str(report.total))
    table.add_row("Successfully Installed", f"[bright_green]{report.completed}[/]")
    table.add_row("Failed", f"[bright_red]{report.failed}[/]" if report.failed else "0")
    table.add_row("Skipped", str(report.skipped))
    table.add_row("Total Downloaded", human_size(report.downloaded_bytes))
    table.add_row("Total Time", human_time(report.duration))
    avg = report.downloaded_bytes / report.duration if report.duration else 0
    table.add_row("Avg Speed", f"{human_size(avg)}/s")
    console.print()
    console.print(table)

    if report.failed:
        console.print("\n[bright_red]Failed drivers:[/]")
        for d in report.drivers:
            if d["status"] == "failed":
                console.print(f"  - {d['display_name']}: [grey70]{d['error']}[/]")


def save_report(report: Report, path: Path) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "total": report.total,
            "completed": report.completed,
            "failed": report.failed,
            "skipped": report.skipped,
            "downloaded_bytes": report.downloaded_bytes,
            "duration_seconds": round(report.duration, 2),
        },
        "drivers": report.drivers,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[grey70]Report saved to[/] {path}")
    except OSError as exc:
        console.print(f"[yellow]Could not save report: {exc}[/]")


# --------------------------------------------------------------------------- #
# Pre-flight: manifest summary + disk space
# --------------------------------------------------------------------------- #

def show_manifest_summary(drivers: list[Driver], manifest_label: str) -> int:
    total = sum(d.total_size for d in drivers)
    console.print(banner())
    console.print(
        f"  [bold]Loaded:[/] {manifest_label}    "
        f"[bold]Drivers:[/] {len(drivers)}    "
        f"[bold]Total size:[/] {human_size(total)}\n"
    )
    table = Table(border_style="grey37", header_style="bold", expand=True)
    table.add_column("#", justify="right", style="grey50")
    table.add_column("Driver", ratio=3)
    table.add_column("Version", ratio=1)
    table.add_column("Provider", ratio=2)
    table.add_column("Arch", justify="center")
    table.add_column("Parts", justify="center")
    table.add_column("Size", justify="right")
    for i, d in enumerate(drivers, start=1):
        table.add_row(
            str(i), d.display_name, d.version or "-",
            d.provider or "-", d.arch or "-",
            str(len(d.parts)), human_size(d.total_size),
        )
    console.print(table)
    return total


def check_disk_space(output: Path, needed: int) -> bool:
    try:
        output.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(output).free
    except OSError as exc:
        console.print(f"[yellow]Could not check disk space: {exc}[/]")
        return True
    estimate = int(needed * 2.5)  # archives + extracted output
    if free < estimate:
        console.print(
            f"[bright_red]Warning:[/] estimated need ~{human_size(estimate)} "
            f"but only {human_size(free)} free on target volume."
        )
        return False
    console.print(
        f"[grey70]Disk space OK: {human_size(free)} free "
        f"(est. need ~{human_size(estimate)}).[/]\n"
    )
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extractor.py",
        description=f"{APP_NAME} v{APP_VERSION} - download, verify & extract driver packages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("manifest", nargs="?", default=None,
                   help="Path to the JSON driver manifest. "
                        "If omitted, you'll be prompted to enter it after launch.")
    p.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                   help=f"Destination folder (default: {DEFAULT_OUTPUT}).")
    p.add_argument("-p", "--parallel", type=int, default=2,
                   help="Parallel driver workers (default: 2, max: 8).")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip SHA256 checksum verification.")
    p.add_argument("--force-extract", action="store_true",
                   help="Overwrite already-extracted files.")
    p.add_argument("--bandwidth-limit", type=float, default=0.0,
                   help="Per-stream download limit in MB/s (0 = unlimited).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose debug logging to console.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be downloaded without downloading.")
    p.add_argument("--cleanup", action="store_true",
                   help="Delete archive parts after successful extraction.")
    p.add_argument("--no-interactive", action="store_true",
                   help="Run without prompts (assume yes).")
    p.add_argument("--report", default=None,
                   help="Path to write a JSON run report (default: <output>/report.json).")
    return p


def looks_like_manifest(path: Path) -> bool:
    """Cheap structural check: is this JSON file a driver manifest?"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and isinstance(data.get("drivers"), list)


def discover_manifests() -> list[Path]:
    """Scan the current working directory for likely manifest JSON files.

    Files whose names hint at a manifest (e.g. contain 'manifest' or 'driver')
    are tried first, then any remaining .json files, validating structure.
    """
    cwd = Path.cwd()
    try:
        json_files = sorted(cwd.glob("*.json"))
    except OSError:
        return []

    def priority(p: Path) -> int:
        name = p.name.lower()
        if "manifest" in name:
            return 0
        if "driver" in name:
            return 1
        return 2

    found: list[Path] = []
    for p in sorted(json_files, key=priority):
        if looks_like_manifest(p):
            found.append(p)
    return found


def resolve_manifest(
    arg_value: Optional[str], interactive: bool
) -> tuple[list[Driver], str]:
    """Load the manifest, prompting the user to enter it after launch if needed.

    Resolution order when no argument is given:
      1. Auto-detect a manifest JSON in the current directory.
      2. Prompt the user (file path, URL, or pasted raw JSON).

    The user may supply a file path, a URL, or paste raw JSON (type 'paste',
    then the JSON, then a line containing only 'END').
    """
    candidate = (arg_value or "").strip()

    # Auto-detect from the current directory when nothing was passed in.
    if not candidate:
        detected = discover_manifests()
        if detected:
            chosen: Optional[Path] = None
            if len(detected) == 1:
                chosen = detected[0]
                if interactive and not Confirm.ask(
                    f"[bold]Found manifest[/] [cyan]{chosen.name}[/] "
                    "in the current directory. Use it?",
                    default=True,
                ):
                    chosen = None
            elif interactive:
                console.print("\n[bold]Manifests found in the current directory:[/]")
                for i, p in enumerate(detected, 1):
                    console.print(f"  [cyan]{i}[/]. {p.name}")
                console.print("  [cyan]0[/]. Enter a different path / URL")
                pick = Prompt.ask(
                    "Select a manifest",
                    choices=[str(i) for i in range(len(detected) + 1)],
                    default="1",
                )
                if pick != "0":
                    chosen = detected[int(pick) - 1]
            else:
                # Non-interactive: just use the first detected manifest.
                chosen = detected[0]
                console.print(
                    f"[grey70]Auto-detected manifest:[/] [cyan]{chosen.name}[/]"
                )

            if chosen is not None:
                candidate = str(chosen)

    while True:
        # No value yet -> prompt (unless non-interactive, where we must fail).
        if not candidate:
            if not interactive:
                raise ManifestError(
                    "No manifest provided and none found in the current directory. "
                    "Pass a path/URL as the first argument."
                )
            console.print(
                "\n[bold]Enter the driver manifest[/] "
                "[grey70](file path, http(s) URL, or 'paste' to paste raw JSON)[/]"
            )
            candidate = Prompt.ask("Manifest").strip()
            if not candidate:
                continue

        try:
            if candidate.lower() == "paste":
                raw_text = _read_pasted_json()
                return parse_manifest(raw_text=raw_text, source="<pasted JSON>"), "<pasted JSON>"
            if re.match(r"^https?://", candidate, re.IGNORECASE):
                return parse_manifest(url=candidate), candidate
            p = Path(candidate).expanduser()
            return parse_manifest(p), p.name
        except ManifestError as exc:
            console.print(f"[bright_red]Manifest error:[/] {exc}")
            if not interactive:
                raise
            candidate = ""  # loop back and re-prompt
            console.print("[grey70]Let's try again.[/]")


def _read_pasted_json() -> str:
    """Collect multi-line pasted JSON until a line containing only 'END'."""
    console.print(
        "[grey70]Paste your JSON below. Finish with a line containing only [bold]END[/].[/]"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    output = Path(args.output).expanduser()
    interactive = not args.no_interactive and sys.stdin.isatty()

    # Manifest may be passed as an argument or entered interactively after launch
    # (file path, http(s) URL, or pasted raw JSON).
    try:
        drivers, manifest_label = resolve_manifest(args.manifest, interactive)
    except ManifestError as exc:
        console.print(f"[bright_red]Manifest error:[/] {exc}")
        return 2

    total = show_manifest_summary(drivers, manifest_label)

    if args.dry_run:
        console.print("\n[bold]Dry run[/] - the following parts would be downloaded:\n")
        for d in drivers:
            console.print(f"[bold]{d.display_name}[/] -> "
                          f"{output / d.pack_folder / d.folder_name}")
            for part in d.parts:
                console.print(f"    {part.filename}  "
                              f"[grey70]{human_size(part.size_bytes)}[/]  {part.url}")
        return 0

    check_disk_space(output, total)

    if interactive:
        if not Confirm.ask(
            f"\nDownload & extract {len(drivers)} driver(s) to "
            f"[bold]{output}[/]?", default=True
        ):
            console.print("[grey70]Aborted.[/]")
            return 0

    settings = Settings(
        output=output,
        parallel=args.parallel,
        verify=not args.no_verify,
        cleanup=args.cleanup,
        overwrite=args.force_extract,
        bandwidth_limit=max(0.0, args.bandwidth_limit),
        interactive=interactive,
    )

    orchestrator = Orchestrator(drivers, settings)
    report = orchestrator.run()

    print_summary(report)
    report_path = Path(args.report) if args.report else output / "report.json"
    save_report(report, report_path)
    console.print(f"[grey70]Detailed log:[/] {LOG_FILE}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[bright_red]Interrupted.[/]")
        sys.exit(130)
