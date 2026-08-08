#!/usr/bin/env python3
"""
migrate_to_d1.py
=================
Streams `<Category>.manifest[.N].json` driver manifests (as produced by
DriverDex-Builder) into batched, size-capped SQL files suitable for ingestion via
`wrangler d1 execute --remote --file=...` against a Cloudflare D1 database.

Source shape (per entry inside the "drivers" array of a manifest file):
    {
      "id": "drv-<sha256:16>", "pack": "...", "type": "...", "provider": "...",
      "category": "...", "version": "...", "arch": "...",
      "hwids": [...], "compatible_ids": [...], "descriptions": [...],
      "os_targets": [...], "zip": "<url>", "zip_parts": N,
      "parts": [{"part_num","filename","size_bytes","sha256","url"}, ...],
      "date_added": "...", "enabled": true|false,
      "supersedes": id|null, "superseded_by": id|null, "notes": "..."
    }

`installers.manifest.json` (and any `installers.manifest.N.json` shards) use
an entirely different shape -- EXE/installer metadata, no HWIDs -- and are
deliberately skipped by this script. See --include-installers-warning if you
want a heads-up logged whenever one is encountered (on by default).

Design goals
------------
- Low memory footprint: manifests are streamed with `ijson`, never loaded
  whole into memory, so multi-GB manifests are safe to process.
- Zero duplicates: rows are emitted as idempotent upserts (`INSERT ... ON
  CONFLICT DO UPDATE` for Drivers; delete-then-INSERT for child tables),
  keyed on the unique keys defined in schema.sql (driver_id /
  hwid_string+driver_id / driver_id+part_num) -- re-running this script
  against the same manifests is always safe and never creates duplicate rows.
- Chunked, size-capped output: chunk files roll over once both `--batch-size`
  (default 5000 statements) and `--max-chunk-mb` (default 40MB) are reached,
  staying well under Wrangler/D1's per-request execution limits. Chunks
  contain plain statements with NO explicit SQL transaction control --
  D1 (Durable Objects SQLite) rejects client-issued `BEGIN TRANSACTION` /
  `SAVEPOINT` outright ("please use the state.storage.transaction() ...
  APIs instead"), and doesn't need them anyway: each
  `wrangler d1 execute --remote --file=...` call already applies its whole
  file atomically on D1's side.
- Deterministic child replacement: rather than relying on D1's FK CASCADE
  support (which varies by version), every driver's HWIDs and DriverParts
  rows are explicitly DELETEd and re-inserted on each sync. This guarantees
  a "perfect mirror" for partial removals (e.g. a dropped HWID, or a
  re-archived package with a different part count) even without cascade.
- Fail-safe: malformed manifests/entries are logged and skipped, never crash
  the run. A non-zero exit code is only used for unrecoverable conditions
  (no input found, or every record was rejected).
- Optional mirror cleanup: with --clean, the script queries the live D1
  database (via `wrangler d1 execute --json`) and diffs known driver_ids
  per source manifest, emitting DELETE statements for anything that no
  longer exists locally -- including whole manifests that were deleted.

NEW in this version
-------------------
- --min-date / --since: skip drivers whose date_added is older than the given
  ISO-8601 date. When combined with --clean this gives you an incremental
  "only sync new drivers added since <last run>" mode.
- --force-enabled: override enabled=false in the manifest and insert/upsert
  every driver as enabled=1. Useful when newly-imported manifests incorrectly
  flag entries as disabled.
- --debug-ids: comma-separated list of driver_ids to emit extra logging for,
  handy when a specific newly-added driver still doesn't show up after a sync.
- --dry-run: generate SQL to stdout/files but do NOT apply them to D1.

Usage
-----
    python migrate_to_d1.py --input-dir . --output-dir ./d1_import
    python migrate_to_d1.py --input-dir . --output-dir ./d1_import \\
        --clean --db-name driverdex-index --commit-sha "$GITHUB_SHA"

    # Only process drivers added in the last 7 days:
    python migrate_to_d1.py --input-dir . --output-dir ./d1_import \\
        --since 2025-01-01 --db-name driverdex-index --clean

A note on upserts
------------------
Drivers rows use `INSERT ... ON CONFLICT(driver_id) DO UPDATE SET ...`,
which SQLite/D1 both support and which leaves `created_at` untouched on
re-sync while still refreshing every other column (`updated_at` included)
-- so re-running this script against the same manifests is fully
idempotent and never duplicates a row, and the audit trail stays intact.
HWIDs and DriverParts use plain INSERT, because their parent driver's
children are explicitly DELETEd immediately beforehand (see
build_children_delete) -- there is nothing left to conflict with, and the
UNIQUE constraints on each table are a backstop against duplicates even so.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Set

try:
    import ijson
except ImportError:  # pragma: no cover - surfaced clearly at runtime instead
    ijson = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_to_d1")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    manifests_seen: int = 0
    manifests_skipped_nondriver: int = 0
    manifests_failed: int = 0
    drivers_ok: int = 0
    drivers_skipped: int = 0
    drivers_skipped_date_filter: int = 0
    drivers_null_enabled: int = 0
    hwids_ok: int = 0
    hwids_skipped: int = 0
    parts_ok: int = 0
    parts_skipped: int = 0
    chunks_written: int = 0

    def summary(self) -> str:
        date_note = (
            f" (date-filtered={self.drivers_skipped_date_filter})"
            if self.drivers_skipped_date_filter else ""
        )
        null_enabled_note = (
            f" (null-enabled normalized to true={self.drivers_null_enabled})"
            if self.drivers_null_enabled else ""
        )
        return (
            f"manifests={self.manifests_seen} "
            f"(non-driver skipped={self.manifests_skipped_nondriver}, failed={self.manifests_failed}) | "
            f"drivers ok={self.drivers_ok} skipped={self.drivers_skipped}{date_note}{null_enabled_note} | "
            f"hwids ok={self.hwids_ok} skipped={self.hwids_skipped} | "
            f"parts ok={self.parts_ok} skipped={self.parts_skipped} | "
            f"chunks={self.chunks_written}"
        )


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def sql_escape(value) -> str:
    """Render a Python value as a literal safe for a flat .sql file.

    Chunk files are executed via `wrangler d1 execute --file`, i.e. as plain
    text SQL, not parameterized queries -- every value must be escaped here.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        value = json.dumps(value, separators=(",", ":"))
    text = str(value).replace("'", "''")
    return f"'{text}'"


_DRIVER_COLUMNS = (
    "driver_id", "pack", "driver_type", "provider", "category", "version",
    "arch", "display_name", "descriptions", "os_targets", "primary_url",
    "zip_parts", "date_added", "enabled", "supersedes", "superseded_by",
    "notes", "source_manifest", "updated_at",
)


def build_driver_upsert(row: dict) -> str:
    """True upsert keyed on the unique driver_id.

    Uses INSERT ... ON CONFLICT DO UPDATE rather than INSERT OR REPLACE so
    that (a) created_at is preserved across re-syncs instead of being reset
    to "now" on every run (INSERT OR REPLACE is a DELETE+INSERT under the
    hood), and (b) the AUTOINCREMENT surrogate `id` is never invalidated by
    a delete/insert cycle. This is also a stronger duplicate guard: a second
    sync of the same manifest is guaranteed to UPDATE the existing row,
    never insert a second one, even under retries or out-of-order chunk
    re-application.
    """
    values = ", ".join(sql_escape(row.get(c)) for c in _DRIVER_COLUMNS)
    update_cols = [c for c in _DRIVER_COLUMNS if c != "driver_id"]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO Drivers ({', '.join(_DRIVER_COLUMNS)}) "
        f"VALUES ({values}) "
        f"ON CONFLICT(driver_id) DO UPDATE SET {set_clause};"
    )


def build_children_delete(table: str, driver_id: str) -> str:
    """Clear all existing child rows (HWIDs / DriverParts) for a driver
    before re-inserting the fresh set from the manifest -- see module
    docstring re: not relying on FK CASCADE for this.
    """
    return f"DELETE FROM {table} WHERE driver_id = {sql_escape(driver_id)};"


def build_hwid_upsert(hwid_string: str, driver_id: str, is_generic: bool) -> str:
    cols = ("hwid_string", "driver_id", "is_generic")
    values = ", ".join(sql_escape(v) for v in (hwid_string, driver_id, is_generic))
    return f"INSERT INTO HWIDs ({', '.join(cols)}) VALUES ({values});"


def build_part_upsert(driver_id: str, part: dict) -> str:
    cols = ("driver_id", "part_num", "filename", "size_bytes", "sha256", "url")
    values = ", ".join(sql_escape(v) for v in (
        driver_id, part.get("part_num"), part.get("filename"),
        part.get("size_bytes"), part.get("sha256"), part.get("url"),
    ))
    return f"INSERT INTO DriverParts ({', '.join(cols)}) VALUES ({values});"


# ---------------------------------------------------------------------------
# Chunked / batched SQL writer
# ---------------------------------------------------------------------------

class ChunkWriter:
    """Writes plain statements (NO explicit transaction-control SQL), rolling
    over to a new file once the current file approaches `max_bytes`.
    """

    def __init__(self, output_dir: Path, base_name: str, batch_size: int, max_bytes: int):
        self.output_dir = output_dir
        self.base_name = base_name
        self.batch_size = batch_size
        self.max_bytes = max_bytes

        self.part_num = 1
        self.batch_count = 0
        self.current_size = 0
        self.total_statements = 0
        self.chunk_paths: list[Path] = []
        self._file = None
        self._open_new_file()

    def _open_new_file(self) -> None:
        path = self.output_dir / f"{self.base_name}_{self.part_num}.sql"
        self._file = path.open("w", encoding="utf-8")
        self.chunk_paths.append(path)
        self.current_size = 0
        self.batch_count = 0
        self._raw(
            f"-- Auto-generated by migrate_to_d1.py "
            f"({datetime.now(timezone.utc).isoformat()}) -- part {self.part_num}\n"
        )

    def _raw(self, text: str) -> None:
        self._file.write(text)
        self.current_size += len(text.encode("utf-8"))

    def write_driver_block(self, statements: list[str]) -> None:
        """Write all statements for a single driver atomically w.r.t. chunk
        boundaries.

        The critical invariant: a driver's DELETE-then-INSERT sequence for
        HWIDs and DriverParts must never be split across two chunk files.
        If the DELETE lands in chunk N but the INSERTs spill into chunk N+1,
        a re-sync against a DB that already has rows for that driver will hit
        the UNIQUE constraint on HWIDs(hwid_string, driver_id) because the
        DELETE in the previous chunk already committed before N+1 is applied --
        except on a *retry* where chunk N is skipped (already applied) and
        chunk N+1 is re-run, the DELETE never executes and the stale rows
        collide with the fresh INSERTs.

        Strategy: check whether the entire block would overflow the current
        chunk *before* writing any of it.  If it would, roll over first so
        the whole block lands in a single fresh chunk.  This keeps every
        driver's upsert + delete + insert sequence atomic within one file.
        """
        if not statements:
            return

        block_bytes = sum(len((s + "\n").encode("utf-8")) for s in statements)
        block_count = len(statements)

        # If adding this block would push us over either threshold, and the
        # current chunk is non-empty, roll over *before* writing anything.
        # (If the chunk is already empty the block simply goes in as-is; a
        # single driver block that exceeds max_bytes on its own is unavoidable
        # and handled gracefully -- it just produces an oversized chunk.)
        if self.batch_count > 0 and (
            self.batch_count + block_count >= self.batch_size
            or self.current_size + block_bytes >= self.max_bytes
        ):
            self._file.close()
            self.part_num += 1
            self._open_new_file()

        for stmt in statements:
            self._raw(stmt + "\n")
        self.batch_count += block_count
        self.total_statements += block_count

    def close(self) -> list[Path]:
        self._file.close()
        return self.chunk_paths


# ---------------------------------------------------------------------------
# Manifest streaming + validation
# ---------------------------------------------------------------------------

def _looks_like_installer_manifest(filename: str) -> bool:
    return filename.startswith("installers.manifest")


def stream_drivers(path: Path) -> Iterator[dict]:
    """Stream `drivers[]` entries from a manifest file one at a time using
    ijson, so the file is never fully materialized in memory.
    """
    if ijson is None:
        raise RuntimeError("ijson is required for streaming parsing. Install with: pip install ijson")
    with path.open("rb") as fh:
        try:
            yield from ijson.items(fh, "drivers.item")
        except ijson.JSONError as exc:
            raise ValueError(f"Malformed JSON in {path}: {exc}") from exc


def validate_driver(entry: dict, manifest_name: str, idx: int) -> bool:
    if not entry.get("id"):
        log.warning("Skipping driver #%d in %s: missing required field 'id'", idx, manifest_name)
        return False
    hwids = entry.get("hwids")
    if hwids is not None and not isinstance(hwids, list):
        log.warning("Skipping driver #%d (%s) in %s: 'hwids' must be a list",
                     idx, entry.get("id"), manifest_name)
        return False
    if not hwids:
        log.warning("Driver #%d (%s) in %s has no hwids -- keeping the row, "
                     "but it won't be reachable via HWID lookup.",
                     idx, entry.get("id"), manifest_name)
    return True


def validate_part(part, driver_id: str, manifest_name: str) -> bool:
    if not isinstance(part, dict) or not part.get("filename") or not part.get("url"):
        log.warning("Skipping malformed archive part for driver '%s' in %s",
                     driver_id, manifest_name)
        return False
    return True


# ---------------------------------------------------------------------------
# Date filter helper
# ---------------------------------------------------------------------------

def parse_min_date(min_date_str: Optional[str]) -> Optional[str]:
    """Normalise --since / --min-date value to a plain ISO-8601 date string
    (YYYY-MM-DD) or None.  Accepts full datetime strings too -- only the
    date portion is used for the comparison so millisecond-precision
    timestamps in date_added fields compare cleanly.
    """
    if not min_date_str:
        return None
    # Accept both "2025-06-01" and "2025-06-01T00:00:00Z"
    return min_date_str[:10]


def driver_passes_date_filter(entry: dict, min_date: Optional[str]) -> bool:
    """Return True if the driver should be included (no filter, or date_added
    is >= min_date).  Drivers with a missing or unparseable date_added are
    ALWAYS included so a bad date field never silently drops a driver.
    """
    if not min_date:
        return True
    date_added = entry.get("date_added")
    if not date_added or not isinstance(date_added, str):
        return True  # don't filter out drivers with no date
    # Only compare the YYYY-MM-DD prefix -- avoids timezone/time mismatches
    return date_added[:10] >= min_date


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def discover_manifests(input_dir: Path, pattern: str) -> list[Path]:
    return sorted(p for p in input_dir.rglob(pattern) if p.is_file())


def derive_display_name(entry: dict) -> str:
    descriptions = entry.get("descriptions") or []
    if descriptions and isinstance(descriptions, list) and descriptions[0]:
        return str(descriptions[0])
    parts = [entry.get("provider", ""), entry.get("category", ""), entry.get("version", "")]
    return " ".join(p for p in parts if p).strip() or entry.get("id", "")


def migrate(
    input_dir: Path,
    output_dir: Path,
    pattern: str,
    batch_size: int,
    max_chunk_mb: int,
    commit_sha: Optional[str],
    min_date: Optional[str] = None,
    force_enabled: bool = False,
    debug_ids: Optional[Set[str]] = None,
) -> tuple[Stats, list[Path], dict[str, set[str]]]:
    """Run the full migration.

    Returns (stats, chunk_paths, valid_ids_by_manifest). The last value maps
    {manifest_filename: {driver_id, ...}} and is reused by --clean to know
    exactly what "currently valid" means.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stats = Stats()
    valid_ids_by_manifest: dict[str, set[str]] = {}
    debug_ids = debug_ids or set()
    now_iso = datetime.now(timezone.utc).isoformat()

    manifests = discover_manifests(input_dir, pattern)
    if not manifests:
        log.error("No manifest files matched pattern '%s' under %s", pattern, input_dir)
        return stats, [], valid_ids_by_manifest

    if min_date:
        log.info("Date filter active: only processing drivers with date_added >= %s", min_date)

    max_bytes = max_chunk_mb * 1024 * 1024
    writer = ChunkWriter(output_dir, "d1_import_part", batch_size, max_bytes)

    for manifest_path in manifests:
        manifest_name = manifest_path.name

        if _looks_like_installer_manifest(manifest_name):
            log.info("Skipping %s (installer manifest -- different schema, not synced here)", manifest_name)
            stats.manifests_skipped_nondriver += 1
            continue

        stats.manifests_seen += 1
        valid_ids_by_manifest.setdefault(manifest_name, set())
        log.info("Processing %s", manifest_name)

        driver_iter = enumerate(stream_drivers(manifest_path), start=1)
        while True:
            # Advance the iterator inside a per-entry try/except so that a
            # single malformed entry (unexpected field type, unencodable value,
            # etc.) is isolated: we log it, skip that driver, and carry on with
            # the rest of the manifest rather than aborting all remaining drivers.
            # ijson.JSONError / ValueError from a truly corrupt file are still
            # propagated by the generator and caught in the outer except below,
            # which breaks out of the loop.
            try:
                idx, entry = next(driver_iter)
            except StopIteration:
                break
            except ValueError as exc:
                # Stream-level parse failure (ijson re-raises as ValueError) --
                # the file is unrecoverable; mark it failed and move on.
                log.error(str(exc))
                stats.manifests_failed += 1
                break
            except Exception as exc:  # noqa: BLE001
                log.error("Unexpected error advancing manifest stream for %s: %s", manifest_name, exc)
                stats.manifests_failed += 1
                break

            try:
                if not isinstance(entry, dict) or not validate_driver(entry, manifest_name, idx):
                    stats.drivers_skipped += 1
                    continue

                driver_id = str(entry["id"])

                # --debug-ids: extra visibility for specific drivers
                if driver_id in debug_ids:
                    log.info(
                        "[DEBUG-ID] %s found in %s (idx=%d): enabled=%s date_added=%s hwids=%d",
                        driver_id, manifest_name, idx,
                        entry.get("enabled"), entry.get("date_added"),
                        len(entry.get("hwids") or []),
                    )

                # Date filter: skip drivers that are too old (but always include
                # the driver in valid_ids so --clean won't delete it either)
                if not driver_passes_date_filter(entry, min_date):
                    stats.drivers_skipped_date_filter += 1
                    # Still register it as valid so --clean doesn't purge it
                    valid_ids_by_manifest[manifest_name].add(driver_id)
                    continue

                # --force-enabled: override enabled=false so newly added
                # drivers don't get silently hidden by a bad manifest flag.
                #
                # Note: entry.get("enabled", True) only falls back to the
                # default when the key is completely ABSENT.  If a manifest
                # entry explicitly carries "enabled": null, .get() returns
                # None -- and bool(None) is False, silently disabling the
                # driver.  We normalise null -> True before bool() so that
                # only an explicit `false` disables a driver.
                raw_enabled = entry.get("enabled", True)
                if raw_enabled is None:
                    stats.drivers_null_enabled += 1
                    raw_enabled = True

                enabled_value = True if force_enabled else bool(raw_enabled)

                if force_enabled and not raw_enabled:
                    log.info("[force-enabled] Overriding enabled=false for driver %s in %s",
                             driver_id, manifest_name)

                row = {
                    "driver_id": driver_id,
                    "pack": entry.get("pack"),
                    "driver_type": entry.get("type"),
                    "provider": entry.get("provider"),
                    "category": entry.get("category"),
                    "version": entry.get("version"),
                    "arch": entry.get("arch"),
                    "display_name": derive_display_name(entry),
                    "descriptions": entry.get("descriptions") or [],
                    "os_targets": entry.get("os_targets") or [],
                    "primary_url": entry.get("zip"),
                    "zip_parts": entry.get("zip_parts"),
                    "date_added": entry.get("date_added"),
                    "enabled": enabled_value,
                    "supersedes": entry.get("supersedes"),
                    "superseded_by": entry.get("superseded_by"),
                    "notes": entry.get("notes"),
                    "source_manifest": manifest_name,
                    "updated_at": now_iso,
                }

                # Collect every statement for this driver into a block so
                # that write_driver_block() can guarantee the whole group
                # (upsert + DELETE-children + INSERT-children) lands in a
                # single chunk file.  Splitting across chunks is what causes
                # UNIQUE constraint failures on re-sync: if the DELETE is in
                # chunk N (already applied) and the INSERTs are in chunk N+1
                # (being re-applied after a retry), stale rows from the
                # previous sync collide with the fresh inserts.
                driver_stmts: list[str] = []
                driver_stmts.append(build_driver_upsert(row))

                # --- HWIDs ---------------------------------------------------
                driver_stmts.append(build_children_delete("HWIDs", driver_id))
                hwids_added = 0
                for hwid in entry.get("hwids") or []:
                    if not hwid or not isinstance(hwid, str):
                        log.warning("Skipping malformed HWID for driver '%s' in %s", driver_id, manifest_name)
                        stats.hwids_skipped += 1
                        continue
                    is_generic = "&" not in hwid
                    driver_stmts.append(build_hwid_upsert(hwid, driver_id, is_generic))
                    hwids_added += 1

                # --- DriverParts (multi-volume archive metadata) --------------
                driver_stmts.append(build_children_delete("DriverParts", driver_id))
                parts_added = 0
                for part in entry.get("parts") or []:
                    if not validate_part(part, driver_id, manifest_name):
                        stats.parts_skipped += 1
                        continue
                    driver_stmts.append(build_part_upsert(driver_id, part))
                    parts_added += 1

                # Atomic chunk write -- rolls over before the first statement
                # if needed, ensuring DELETE and INSERT stay in the same file.
                writer.write_driver_block(driver_stmts)
                stats.drivers_ok += 1
                stats.hwids_ok += hwids_added
                stats.parts_ok += parts_added
                valid_ids_by_manifest[manifest_name].add(driver_id)

            except Exception as exc:  # noqa: BLE001
                # Per-entry isolation: log this driver as skipped, then continue
                # iterating so the rest of the manifest is not silently discarded.
                log.error(
                    "Unexpected error processing driver #%d in %s (skipping this entry): %s",
                    idx, manifest_name, exc,
                )
                stats.drivers_skipped += 1

    # Audit row -- use write_driver_block (pre-write rollover) so a single
    # INSERT never triggers a post-write rollover that would leave a trailing
    # chunk containing only the auto-generated comment header.
    writer.write_driver_block([
        "INSERT INTO SyncLog (manifests_processed, drivers_upserted, "
        "hwids_upserted, drivers_skipped, drivers_null_enabled, commit_sha) VALUES "
        f"({stats.manifests_seen}, {stats.drivers_ok}, {stats.hwids_ok}, "
        f"{stats.drivers_skipped}, {stats.drivers_null_enabled}, {sql_escape(commit_sha)});"
    ])

    chunk_paths = writer.close()
    stats.chunks_written = len(chunk_paths)
    return stats, chunk_paths, valid_ids_by_manifest


# ---------------------------------------------------------------------------
# Optional: generate cleanup SQL by diffing against live D1
# ---------------------------------------------------------------------------

def fetch_remote_driver_manifest_pairs(db_name: str, remote: bool) -> list[tuple[str, str]]:
    """Query D1 for every (driver_id, source_manifest) currently stored."""
    cmd = [
        "npx", "wrangler", "d1", "execute", db_name,
        "--remote" if remote else "--local",
        "--command", "SELECT driver_id, source_manifest FROM Drivers;",
        "--json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        log.error("wrangler query failed: %s", result.stderr.strip())
        return []

    try:
        payload = json.loads(result.stdout)
        rows = payload[0]["results"] if payload else []
        return [(r["driver_id"], r["source_manifest"]) for r in rows]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        log.error("Could not parse wrangler --json output: %s", exc)
        return []


def generate_cleanup_sql(
    output_dir: Path,
    db_name: str,
    remote: bool,
    valid_ids_by_manifest: dict[str, set[str]],
    batch_size: int,
    max_chunk_mb: int,
) -> list[Path]:
    """Diff live D1 state against locally-known valid driver_ids and emit
    DELETE statements for anything orphaned.
    """
    log.info("Fetching current D1 state for cleanup diff...")
    remote_pairs = fetch_remote_driver_manifest_pairs(db_name, remote)
    if not remote_pairs:
        log.info("No remote rows found (or query failed) -- skipping cleanup.")
        return []

    known_manifests = set(valid_ids_by_manifest.keys())
    orphans: list[str] = []

    for driver_id, source_manifest in remote_pairs:
        if source_manifest not in known_manifests:
            orphans.append(driver_id)
        elif driver_id not in valid_ids_by_manifest[source_manifest]:
            orphans.append(driver_id)

    if not orphans:
        log.info("D1 is already a perfect mirror -- nothing to clean up.")
        return []

    log.info("Found %d orphaned driver(s) to purge.", len(orphans))
    max_bytes = max_chunk_mb * 1024 * 1024
    writer = ChunkWriter(output_dir, "d1_cleanup_part", batch_size, max_bytes)

    for driver_id in orphans:
        esc = sql_escape(driver_id)
        # Bundle all three DELETEs for one orphan into a single block so they
        # always land in the same chunk file -- consistent with write_driver_block
        # semantics and avoids write() callers that could trigger a post-write
        # rollover producing a trailing empty chunk.
        writer.write_driver_block([
            f"DELETE FROM HWIDs WHERE driver_id = {esc};",
            f"DELETE FROM DriverParts WHERE driver_id = {esc};",
            f"DELETE FROM Drivers WHERE driver_id = {esc};",
        ])

    return writer.close()


# ---------------------------------------------------------------------------
# Storage usage guardrail
# ---------------------------------------------------------------------------

D1_FREE_PLAN_LIMIT_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB


def check_storage_usage(db_name: str, warn_pct: float, fail_pct: float) -> int:
    cmd = ["npx", "wrangler", "d1", "info", db_name, "--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        log.warning("Could not fetch D1 storage info (continuing anyway): %s",
                     result.stderr.strip())
        return 0

    try:
        info = json.loads(result.stdout)
        if isinstance(info, list):
            info = info[0]
        size_bytes = info.get("file_size") or info.get("database_size") or 0
        size_bytes = int(size_bytes)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("Could not parse `wrangler d1 info --json` output (continuing anyway): %s", exc)
        return 0

    pct = (size_bytes / D1_FREE_PLAN_LIMIT_BYTES) * 100
    size_mb = size_bytes / (1024 * 1024)
    log.info("D1 storage usage: %.1f MB / %.0f MB (%.1f%% of free-plan 5GB limit)",
              size_mb, D1_FREE_PLAN_LIMIT_BYTES / (1024 * 1024), pct)

    if pct >= fail_pct:
        log.error(
            "D1 storage at %.1f%% of the 5GB free-plan limit (>= fail threshold %.0f%%). "
            "Refusing to proceed.",
            pct, fail_pct,
        )
        return 1
    if pct >= warn_pct:
        log.warning(
            "D1 storage at %.1f%% of the 5GB free-plan limit (>= warn threshold %.0f%%). "
            "Consider cleaning up stale rows or upgrading soon.",
            pct, warn_pct,
        )
    return 0


# ---------------------------------------------------------------------------
# Database auto-provisioning
# ---------------------------------------------------------------------------

def ensure_database_exists(db_name: str) -> Optional[str]:
    list_cmd = ["npx", "wrangler", "d1", "list", "--json"]
    result = subprocess.run(list_cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        try:
            databases = json.loads(result.stdout)
            for db in databases:
                if db.get("name") == db_name:
                    db_id = db.get("uuid") or db.get("database_id")
                    log.info("D1 database '%s' already exists (id=%s) -- reusing it.", db_name, db_id)
                    return db_id
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("Could not parse `wrangler d1 list --json` output: %s", exc)
    else:
        log.warning("`wrangler d1 list` failed (continuing to attempt create): %s", result.stderr.strip())

    log.info("D1 database '%s' not found -- creating it.", db_name)
    create_cmd = ["npx", "wrangler", "d1", "create", db_name]
    result = subprocess.run(create_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        if "already exists" in (result.stderr or "").lower():
            log.info("Database appears to have been created concurrently -- re-checking list.")
            result2 = subprocess.run(list_cmd, capture_output=True, text=True, check=False)
            if result2.returncode == 0:
                try:
                    for db in json.loads(result2.stdout):
                        if db.get("name") == db_name:
                            return db.get("uuid") or db.get("database_id")
                except (json.JSONDecodeError, TypeError):
                    pass
        log.error("Failed to create D1 database '%s': %s", db_name, result.stderr.strip())
        return None

    db_id = _extract_database_id_from_create_output(result.stdout)
    if db_id:
        log.info("Created D1 database '%s' (id=%s).", db_name, db_id)
        return db_id
    log.error(
        "Database may have been created, but couldn't parse its id from output:\n%s",
        result.stdout.strip(),
    )
    return None


def _extract_database_id_from_create_output(stdout: str) -> Optional[str]:
    match = re.search(r'database_id\s*=\s*"([0-9a-fA-F-]+)"', stdout)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input-dir", type=Path, default=Path("."), help="Repo root to scan for manifests.")
    p.add_argument("--output-dir", type=Path, default=Path("./d1_import"), help="Where to write .sql chunks.")
    p.add_argument("--pattern", default="*.manifest*.json", help="Glob pattern for manifest files.")
    p.add_argument("--batch-size", type=int, default=5000, help="Statements per transaction.")
    p.add_argument("--max-chunk-mb", type=int, default=40, help="Max size per .sql chunk file (MB).")
    p.add_argument("--clean", action="store_true", help="Also generate d1_cleanup_part_N.sql for obsolete rows.")
    p.add_argument("--db-name", default=None, help="D1 database name/binding (required with --clean, --ensure-db, --check-storage).")
    p.add_argument("--local", action="store_true", help="Use wrangler --local instead of --remote for --clean's query.")
    p.add_argument("--commit-sha", default=None, help="Git commit SHA to stamp into SyncLog.")
    p.add_argument("--ensure-db", action="store_true", help="Idempotently create the D1 database named --db-name if it doesn't already exist.")
    p.add_argument("--write-database-id-to", type=Path, default=None, help="Patch the resolved database_id into this wrangler.toml file.")
    p.add_argument("--check-storage", action="store_true", help="Query live D1 storage usage and warn/fail near the 5GB free-plan limit.")
    p.add_argument("--storage-warn-pct", type=float, default=80.0)
    p.add_argument("--storage-fail-pct", type=float, default=95.0)

    # ---- NEW flags for incremental / debugging ---------------------------------
    p.add_argument(
        "--since", "--min-date", dest="min_date", default=None, metavar="YYYY-MM-DD",
        help=(
            "Only sync drivers whose date_added >= this date (ISO-8601, e.g. 2025-06-01). "
            "Drivers outside the window are still registered as valid so --clean never deletes them."
        ),
    )
    p.add_argument(
        "--force-enabled", action="store_true",
        help=(
            "Override enabled=false in the manifest and upsert every driver as enabled=1. "
            "Use this when newly-imported manifests incorrectly mark entries as disabled."
        ),
    )
    p.add_argument(
        "--debug-ids", default=None, metavar="ID1,ID2,...",
        help="Comma-separated driver_ids to emit extra DEBUG logging for (visibility into exactly what was processed).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Generate SQL chunk files but do NOT apply them to D1 (prints chunk paths instead).",
    )
    # ---------------------------------------------------------------------------
    return p.parse_args(argv)


def patch_wrangler_toml_database_id(toml_path: Path, db_name: str, db_id: str) -> bool:
    if not toml_path.exists():
        log.warning("wrangler.toml not found at %s -- skipping database_id patch.", toml_path)
        return False

    text = toml_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    in_target_block = False
    block_has_matching_name = False
    out: list[str] = []
    patched = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[[d1_databases]]"):
            in_target_block = True
            block_has_matching_name = False
            out.append(line)
            continue
        if stripped.startswith("[") and not stripped.startswith("[[d1_databases]]"):
            in_target_block = False

        if in_target_block and stripped.startswith("database_name"):
            block_has_matching_name = (f'"{db_name}"' in stripped) or (f"'{db_name}'" in stripped)

        if in_target_block and block_has_matching_name and stripped.startswith("database_id"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f'{indent}database_id = "{db_id}"\n')
            patched = True
            continue

        out.append(line)

    if patched:
        toml_path.write_text("".join(out), encoding="utf-8")
        log.info("Patched database_id=%s into %s (database_name=%s).", db_id, toml_path, db_name)
    else:
        log.warning(
            "Could not find a [[d1_databases]] block with database_name = \"%s\" and a "
            "database_id line to patch in %s -- leaving the file untouched.",
            db_name, toml_path,
        )
    return patched


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.clean and not args.db_name:
        log.error("--clean requires --db-name")
        return 2
    if (args.ensure_db or args.check_storage) and not args.db_name:
        log.error("--ensure-db and --check-storage require --db-name")
        return 2

    if args.ensure_db:
        db_id = ensure_database_exists(args.db_name)
        if db_id is None:
            log.error("Could not ensure D1 database '%s' exists -- aborting before any data changes.", args.db_name)
            return 1
        if args.write_database_id_to:
            patch_wrangler_toml_database_id(args.write_database_id_to, args.db_name, db_id)

    if args.check_storage:
        rc = check_storage_usage(args.db_name, args.storage_warn_pct, args.storage_fail_pct)
        if rc != 0:
            return rc

    min_date = parse_min_date(args.min_date)
    debug_ids: Set[str] = set()
    if args.debug_ids:
        debug_ids = {s.strip() for s in args.debug_ids.split(",") if s.strip()}
        log.info("Debug-ID watch list (%d): %s", len(debug_ids), ", ".join(sorted(debug_ids)))

    stats, chunk_paths, valid_ids_by_manifest = migrate(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        pattern=args.pattern,
        batch_size=args.batch_size,
        max_chunk_mb=args.max_chunk_mb,
        commit_sha=args.commit_sha,
        min_date=min_date,
        force_enabled=args.force_enabled,
        debug_ids=debug_ids,
    )

    if not chunk_paths:
        log.error("No SQL chunks were generated -- aborting.")
        return 1

    log.info("Migration complete: %s", stats.summary())
    for path in chunk_paths:
        log.info("  -> %s (%.2f MB)", path, path.stat().st_size / (1024 * 1024))

    if args.dry_run:
        log.info("[dry-run] SQL chunks generated but NOT applied to D1.")
        return 0

    if args.clean:
        cleanup_paths = generate_cleanup_sql(
            output_dir=args.output_dir,
            db_name=args.db_name,
            remote=not args.local,
            valid_ids_by_manifest=valid_ids_by_manifest,
            batch_size=args.batch_size,
            max_chunk_mb=args.max_chunk_mb,
        )
        for path in cleanup_paths:
            log.info("  -> %s (cleanup)", path)

    if stats.drivers_ok == 0:
        log.error("Zero drivers were successfully processed across %d manifest(s).", stats.manifests_seen)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
