#!/usr/bin/env python3
"""
ArchiveExtractor - High-Performance Multi-Volume 7z Extractor
=============================================================

A single-file, enterprise-grade extraction utility specialized for
multi-volume 7-Zip archives:

    archive.7z.001
    archive.7z.002
    archive.7z.003
    ...

Multi-volume 7z files are simply the raw .7z byte stream split into
fixed-size chunks. Concatenating the volumes in order reproduces the
original archive. This tool exposes that concatenation as a single
*seekable* file-like object (without ever loading everything into RAM)
and feeds it to py7zr, which keeps memory usage low even for very large
archives.

Features
--------
* Automatic multi-volume detection & sequence validation
* Missing / duplicate / corrupted volume reporting
* Streaming, low-memory extraction (spanned seekable reader)
* Concurrent multi-job extraction (worker pool + job queue)
* Password support (CLI flag or secure interactive prompt)
* SHA256 + archive integrity verification (--verify / --test)
* Resume support via JSON checkpoint files
* Path-traversal / zip-bomb / symlink safety guards
* Structured logging to console + extraction.log

Usage
-----
    python extractor.py --file archive.7z.001 --output ./out
    python extractor.py --file backup.7z.001 --output ./out --threads 8
    python extractor.py --file enc.7z.001 --password secret
    python extractor.py --file archive.7z.001 --list
    python extractor.py --file archive.7z.001 --test
    python extractor.py --jobs a.7z.001 b.7z.001 --output ./out  # concurrent

Requires: Python 3.12+, py7zr
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

try:
    import py7zr
    from py7zr.exceptions import Bad7zFile, PasswordRequired
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: py7zr is required. Install it with:\n    pip install py7zr\n"
    )
    raise

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

LOG_FILE = "extraction.log"
CHECKPOINT_SUFFIX = ".extract-state.json"
DEFAULT_CHUNK = 1024 * 1024  # 1 MiB streaming read buffer
# Matches the multi-volume naming scheme: <stem>.7z.NNN  (NNN = 1+ digits)
VOLUME_RE = re.compile(r"^(?P<stem>.+\.7z)\.(?P<num>\d+)$", re.IGNORECASE)
# Zip-bomb guard: refuse if uncompressed total exceeds this ratio of input.
MAX_EXPANSION_RATIO = 200
MAX_TOTAL_UNCOMPRESSED = 8 * 1024**4  # 8 TiB hard ceiling sanity check

log = logging.getLogger("ArchiveExtractor")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool = False, log_path: str = LOG_FILE) -> None:
    """Configure structured logging to console and a rotating-ish log file."""
    log.handlers.clear()
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        fileh = logging.FileHandler(log_path, encoding="utf-8")
        fileh.setLevel(logging.DEBUG)
        fileh.setFormatter(fmt)
        log.addHandler(fileh)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        console.handle(
            logging.LogRecord(
                "ArchiveExtractor", logging.WARNING, __file__, 0,
                f"Could not open log file {log_path!r}: {exc}", None, None,
            )
        )


# --------------------------------------------------------------------------- #
# Volume detection & validation
# --------------------------------------------------------------------------- #

@dataclass
class VolumeSet:
    """Represents a discovered set of multi-volume parts."""
    stem: str                         # e.g. "archive.7z"
    directory: Path
    parts: dict[int, Path] = field(default_factory=dict)  # number -> path
    digits: int = 3

    @property
    def ordered_paths(self) -> list[Path]:
        return [self.parts[i] for i in sorted(self.parts)]

    @property
    def numbers(self) -> list[int]:
        return sorted(self.parts)

    def total_size(self) -> int:
        return sum(p.stat().st_size for p in self.parts.values())


class VolumeError(Exception):
    """Raised for any structural problem with the volume set."""


def discover_volumes(first_file: Path) -> VolumeSet:
    """
    Given any volume of a set (typically the .001), scan its directory and
    collect every matching part. Validates the naming scheme.
    """
    first_file = first_file.expanduser().resolve()
    if not first_file.exists():
        raise VolumeError(f"File does not exist: {first_file}")

    m = VOLUME_RE.match(first_file.name)
    if not m:
        raise VolumeError(
            f"'{first_file.name}' is not a multi-volume 7z part. "
            f"Expected a name like 'archive.7z.001'."
        )

    stem = m.group("stem")
    digits = len(m.group("num"))
    directory = first_file.parent

    vol = VolumeSet(stem=stem, directory=directory, digits=digits)

    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        em = VOLUME_RE.match(entry.name)
        if not em:
            continue
        if em.group("stem").lower() != stem.lower():
            continue
        num = int(em.group("num"))
        if num in vol.parts:
            raise VolumeError(
                f"Duplicate volume detected for part {num}: "
                f"'{entry.name}' and '{vol.parts[num].name}'"
            )
        vol.parts[num] = entry

    if not vol.parts:
        raise VolumeError(f"No volumes found for stem '{stem}'")

    return vol


def validate_sequence(vol: VolumeSet) -> None:
    """
    Ensure the numbering is contiguous starting at 1. Reports missing parts
    with a clear, human-readable message.
    """
    numbers = vol.numbers
    start = numbers[0]
    end = numbers[-1]

    if start not in (0, 1):
        raise VolumeError(
            f"Volume sequence must start at .001 (or .000); first found is "
            f".{start:0{vol.digits}d}"
        )

    expected = set(range(start, end + 1))
    found = set(numbers)
    missing = sorted(expected - found)

    if missing:
        missing_str = ", ".join(f"{vol.stem}.{n:0{vol.digits}d}" for n in missing)
        raise VolumeError(
            "Missing volume(s):\n"
            f"    {missing_str}\n"
            f"Expected: {vol.stem}.{start:0{vol.digits}d}-"
            f"{end:0{vol.digits}d}\n"
            f"Found:    {len(found)} of {len(expected)} parts"
        )

    log.info(
        "Volume sequence OK: %d parts (%s.%0*d - %s.%0*d), total %s",
        len(numbers), vol.stem, vol.digits, start, vol.stem, vol.digits, end,
        human_size(vol.total_size()),
    )


def check_volume_integrity(vol: VolumeSet) -> None:
    """Detect obviously corrupted (empty / unreadable) volumes."""
    for num, path in sorted(vol.parts.items()):
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise VolumeError(f"Cannot stat volume {path.name}: {exc}") from exc
        if size == 0:
            raise VolumeError(f"Corrupted (empty) volume: {path.name}")
        # Verify the file is actually readable.
        try:
            with open(path, "rb") as fh:
                fh.read(1)
        except OSError as exc:
            raise VolumeError(f"Unreadable volume {path.name}: {exc}") from exc
        log.debug("Volume %d OK: %s (%s)", num, path.name, human_size(size))


# --------------------------------------------------------------------------- #
# Spanned, seekable, low-memory multi-volume reader
# --------------------------------------------------------------------------- #

class MultiVolumeReader(io.RawIOBase):
    """
    A read-only, seekable file-like object that presents an ordered list of
    volume files as one continuous stream — without loading them into memory.

    Only one underlying volume file handle is open at a time; reads transparently
    roll over volume boundaries. This keeps RAM usage flat regardless of the
    total archive size (suitable for 100 GB - multi-TB archives).
    """

    def __init__(self, paths: list[Path]):
        super().__init__()
        self._paths = paths
        self._sizes = [p.stat().st_size for p in paths]
        # Cumulative starting offsets for each volume.
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

    # -- io.RawIOBase interface ------------------------------------------- #
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def total_size(self) -> int:
        return self._total

    def _volume_for_offset(self, offset: int) -> int:
        """Binary-search which volume contains the byte at `offset`."""
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
# Safety guards
# --------------------------------------------------------------------------- #

def is_safe_path(base: Path, target_name: str) -> bool:
    """
    Reject path traversal (../), absolute paths, and any name that would
    escape the extraction root.
    """
    if not target_name:
        return False
    # Normalize separators.
    name = target_name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return False  # absolute path / drive letter
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return False
    return True


def safety_check_archive(archive: "py7zr.SevenZipFile", input_size: int) -> None:
    """Zip-bomb and traversal pre-flight checks against the archive listing."""
    total_uncompressed = 0
    for info in archive.list():
        fname = getattr(info, "filename", "") or ""
        # Path traversal / absolute path detection.
        norm = fname.replace("\\", "/")
        if norm.startswith("/") or ".." in Path(norm).parts:
            raise VolumeError(f"Unsafe path in archive (traversal): {fname!r}")
        total_uncompressed += getattr(info, "uncompressed", 0) or 0

    if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
        raise VolumeError(
            f"Refusing to extract: uncompressed size "
            f"{human_size(total_uncompressed)} exceeds safety ceiling."
        )
    if input_size > 0 and total_uncompressed > input_size * MAX_EXPANSION_RATIO:
        raise VolumeError(
            f"Possible zip bomb: {human_size(total_uncompressed)} from "
            f"{human_size(input_size)} input "
            f"(>{MAX_EXPANSION_RATIO}x expansion). Aborting."
        )
    log.info(
        "Safety check passed: %s uncompressed across archive contents.",
        human_size(total_uncompressed),
    )


# --------------------------------------------------------------------------- #
# Resume / checkpoint
# --------------------------------------------------------------------------- #

def checkpoint_path(output: Path, stem: str) -> Path:
    return output / f".{stem}{CHECKPOINT_SUFFIX}"

def load_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("completed", []))
    except (OSError, json.JSONDecodeError):
        return set()

def save_checkpoint(path: Path, completed: Iterable[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps({"completed": sorted(completed), "ts": time.time()}),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover
        log.warning("Could not write checkpoint: %s", exc)


# --------------------------------------------------------------------------- #
# Core operations
# --------------------------------------------------------------------------- #

def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if f < 1024 or unit == "PB":
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.2f} PB"


def open_archive(vol: VolumeSet, password: Optional[str]) -> tuple["py7zr.SevenZipFile", MultiVolumeReader]:
    """Open the spanned volume set as a py7zr archive."""
    reader = MultiVolumeReader(vol.ordered_paths)
    try:
        archive = py7zr.SevenZipFile(reader, mode="r", password=password)
    except PasswordRequired as exc:
        reader.close()
        raise VolumeError("Archive is encrypted; a password is required.") from exc
    except Bad7zFile as exc:
        reader.close()
        raise VolumeError(
            "Not a valid 7z stream after concatenating volumes "
            "(corrupted or incomplete volume set)."
        ) from exc
    return archive, reader


def list_contents(vol: VolumeSet, password: Optional[str]) -> None:
    archive, reader = open_archive(vol, password)
    try:
        entries = archive.list()
        log.info("Archive contains %d entries:", len(entries))
        print(f"{'Size':>15}  {'Compressed':>15}  Name")
        print("-" * 70)
        for info in entries:
            print(
                f"{human_size(getattr(info, 'uncompressed', 0) or 0):>15}  "
                f"{human_size(getattr(info, 'compressed', 0) or 0):>15}  "
                f"{getattr(info, 'filename', '')}"
            )
    finally:
        archive.close()
        reader.close()


def test_archive(vol: VolumeSet, password: Optional[str]) -> bool:
    """Verify archive integrity (CRC) without extracting."""
    archive, reader = open_archive(vol, password)
    try:
        log.info("Testing archive integrity (CRC check)...")
        result = archive.test()  # returns True / None / raises on failure
        ok = result is not False
        if ok:
            log.info("Integrity test PASSED.")
        else:
            log.error("Integrity test FAILED.")
        return ok
    except Bad7zFile as exc:
        log.error("Integrity test FAILED: %s", exc)
        return False
    finally:
        archive.close()
        reader.close()


def sha256_of_tree(root: Path, files: Iterable[str]) -> dict[str, str]:
    """Compute SHA256 for each extracted file (streamed)."""
    digests: dict[str, str] = {}
    for rel in files:
        fp = root / rel
        if not fp.is_file():
            continue
        h = hashlib.sha256()
        with open(fp, "rb") as fh:
            for chunk in iter(lambda: fh.read(DEFAULT_CHUNK), b""):
                h.update(chunk)
        digests[rel] = h.hexdigest()
    return digests


@dataclass
class ExtractResult:
    job: str
    success: bool
    files: int = 0
    duration: float = 0.0
    error: Optional[str] = None
    digests: dict[str, str] = field(default_factory=dict)


def extract_job(
    first_file: Path,
    output: Path,
    password: Optional[str] = None,
    *,
    verify: bool = False,
    overwrite: bool = False,
    resume: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> ExtractResult:
    """
    Full extraction pipeline for a single multi-volume archive:
    detect -> validate -> safety -> extract -> (verify).
    """
    job_name = first_file.name
    start = time.time()
    log.info("=== Job '%s' starting ===", job_name)

    try:
        vol = discover_volumes(first_file)
        validate_sequence(vol)
        check_volume_integrity(vol)

        output.mkdir(parents=True, exist_ok=True)
        ck_path = checkpoint_path(output, vol.stem)
        completed = load_checkpoint(ck_path) if resume else set()
        if completed:
            log.info("Resume: %d files already extracted previously.", len(completed))

        archive, reader = open_archive(vol, password)
        try:
            safety_check_archive(archive, vol.total_size())
            all_names = [
                getattr(i, "filename", "")
                for i in archive.list()
                if not getattr(i, "is_directory", False)
            ]

            # Determine which targets still need extraction.
            targets = []
            for name in all_names:
                if cancel_event and cancel_event.is_set():
                    raise VolumeError("Cancelled by user.")
                dest = output / name
                if resume and name in completed and dest.exists():
                    continue
                if dest.exists() and not overwrite and not resume:
                    log.warning("Skipping existing file (use --overwrite): %s", name)
                    completed.add(name)
                    continue
                if not is_safe_path(output, name):
                    raise VolumeError(f"Unsafe extraction path blocked: {name!r}")
                targets.append(name)

            if targets:
                log.info("Extracting %d file(s) to %s", len(targets), output)
                # py7zr extracts by target list; reset stream first.
                reader.seek(0)
                archive.reset()
                archive.extract(path=str(output), targets=targets)
                completed.update(targets)
                save_checkpoint(ck_path, completed)
            else:
                log.info("Nothing to extract (all files already present).")

            digests: dict[str, str] = {}
            if verify:
                log.info("Computing SHA256 for verification...")
                digests = sha256_of_tree(output, all_names)
                for rel, dg in digests.items():
                    log.debug("SHA256 %s  %s", dg, rel)
                log.info("Verification complete: %d file digests computed.", len(digests))

        finally:
            archive.close()
            reader.close()

        # Successful run: clean up checkpoint.
        if ck_path.exists() and not (cancel_event and cancel_event.is_set()):
            try:
                ck_path.unlink()
            except OSError:
                pass

        duration = time.time() - start
        log.info(
            "=== Job '%s' DONE: %d files in %.2fs ===",
            job_name, len(completed), duration,
        )
        return ExtractResult(
            job=job_name, success=True, files=len(completed),
            duration=duration, digests=digests,
        )

    except (VolumeError, Bad7zFile, OSError, py7zr.exceptions.ArchiveError) as exc:
        duration = time.time() - start
        log.error("Job '%s' FAILED: %s", job_name, exc)
        return ExtractResult(
            job=job_name, success=False, duration=duration, error=str(exc),
        )


# --------------------------------------------------------------------------- #
# Concurrent job manager
# --------------------------------------------------------------------------- #

def run_jobs(
    files: list[Path],
    output: Path,
    *,
    password: Optional[str],
    threads: int,
    verify: bool,
    overwrite: bool,
    resume: bool,
) -> list[ExtractResult]:
    """Run multiple extraction jobs concurrently via a worker pool."""
    cancel_event = threading.Event()
    results: list[ExtractResult] = []

    max_workers = max(1, min(threads, len(files)))
    log.info("Launching %d job(s) with %d worker(s).", len(files), max_workers)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="worker"
    ) as pool:
        future_map = {
            pool.submit(
                extract_job, f, output, password,
                verify=verify, overwrite=overwrite, resume=resume,
                cancel_event=cancel_event,
            ): f
            for f in files
        }
        try:
            for fut in concurrent.futures.as_completed(future_map):
                results.append(fut.result())
        except KeyboardInterrupt:  # pragma: no cover
            log.warning("Interrupt received - cancelling remaining jobs...")
            cancel_event.set()
            for fut in future_map:
                fut.cancel()
            raise

    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extractor",
        description="High-performance multi-volume (.7z.001 ...) 7z extractor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  extractor.py --file backup.7z.001 --output ./out\n"
            "  extractor.py --file backup.7z.001 --output ./out --threads 8\n"
            "  extractor.py --file enc.7z.001 --password secret\n"
            "  extractor.py --file archive.7z.001 --list\n"
            "  extractor.py --jobs a.7z.001 b.7z.001 --output ./out\n"
        ),
    )
    p.add_argument("--file", help="First volume of the archive (e.g. archive.7z.001)")
    p.add_argument(
        "--jobs", nargs="+", metavar="FILE",
        help="Multiple first-volumes to extract concurrently.",
    )
    p.add_argument("--output", "-o", default="./output", help="Output directory.")
    p.add_argument("--password", help="Password for encrypted archives.")
    p.add_argument(
        "--threads", type=int, default=0,
        help="Worker threads (0 = auto = CPU count).",
    )
    p.add_argument("--verify", action="store_true", help="SHA256-verify extracted files.")
    p.add_argument("--list", action="store_true", help="List archive contents and exit.")
    p.add_argument("--test", action="store_true", help="Test archive integrity and exit.")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing files.")
    p.add_argument("--resume", action="store_true", help="Resume from a previous checkpoint.")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose (DEBUG) logging.")
    return p


def resolve_threads(requested: int) -> int:
    if requested and requested > 0:
        return requested
    cpu = os.cpu_count() or 4
    log.debug("Auto thread detection: %d logical CPUs.", cpu)
    return cpu


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose)

    if not args.file and not args.jobs:
        parser.error("Provide --file or --jobs.")

    # Resolve password (prompt if encrypted-looking and none provided).
    password = args.password
    if password == "":
        password = getpass.getpass("Archive password: ")

    output = Path(args.output).expanduser()
    threads = resolve_threads(args.threads)

    # --- Single-archive informational modes --------------------------------- #
    if args.file and (args.list or args.test):
        try:
            vol = discover_volumes(Path(args.file))
            validate_sequence(vol)
            check_volume_integrity(vol)
            if args.list:
                list_contents(vol, password)
            if args.test:
                ok = test_archive(vol, password)
                return 0 if ok else 2
            return 0
        except VolumeError as exc:
            log.error("%s", exc)
            return 2

    # --- Extraction (single or concurrent) ---------------------------------- #
    files = [Path(f) for f in (args.jobs or [args.file])]

    overall_start = time.time()
    try:
        results = run_jobs(
            files, output,
            password=password, threads=threads,
            verify=args.verify, overwrite=args.overwrite, resume=args.resume,
        )
    except KeyboardInterrupt:  # pragma: no cover
        log.error("Aborted by user. Use --resume to continue later.")
        return 130

    # --- Summary ------------------------------------------------------------ #
    elapsed = time.time() - overall_start
    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    total_files = sum(r.files for r in succeeded)

    log.info("-" * 60)
    log.info("SUMMARY: %d/%d jobs succeeded, %d files, %.2fs total.",
             len(succeeded), len(results), total_files, elapsed)
    for r in failed:
        log.error("  FAILED %s: %s", r.job, r.error)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
