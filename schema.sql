-- ============================================================================
-- schema.sql
-- D1 schema for the DriverDex driver mirror, modeled directly on the manifest
-- entries produced by DriverDex-Builder (see build_driver_entries() / save_manifest()
-- in driverdex-builder-rest.py).
--
-- Source shape per driver entry (inside "<Category>.manifest[.N].json"):
--   {
--     "id": "drv-<sha256:16>", "pack": "...", "type": "...", "provider": "...",
--     "category": "...", "version": "...", "arch": "...",
--     "hwids": ["PCI\\VEN_..&DEV_..", ...], "compatible_ids": [...],
--     "descriptions": [...], "os_targets": [...],
--     "zip": "<url of first volume>", "zip_parts": N,
--     "parts": [{"part_num","filename","size_bytes","sha256","url"}, ...],
--     "date_added": "...", "enabled": true|false,
--     "supersedes": id|null, "superseded_by": id|null, "notes": "..."
--   }
--
-- Design notes:
--  - Drivers.driver_id is the manifest's "id" (already a stable content
--    hash of hwids+version+arch+category) -- a natural idempotent key.
--  - DriverParts exists because driver packages are split into multi-volume
--    7z archives to stay under GitHub's 18MB blob limit; a client needs the
--    full ordered part list (with per-part sha256) to download and
--    reassemble a package, not just a single file_url.
--  - compatible_ids in the source data is *already a subset* of hwids
--    (specifically, hwids containing no "&") -- not a separate ID space.
--    Rather than duplicate rows, HWIDs.is_generic captures that same
--    distinction as a flag computed from the string itself.
--  - supersedes / superseded_by are stored as plain TEXT, not enforced
--    FOREIGN KEYs -- entries can reference an id that hasn't been inserted
--    yet within the same batch (insertion order isn't guaranteed to follow
--    the supersession chain), so a hard FK would risk spurious failures.
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Drivers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       TEXT NOT NULL UNIQUE,   -- manifest "id", e.g. "drv-a1b2c3d4e5f6..."
    pack            TEXT,
    driver_type     TEXT,                   -- manifest "type" (mapped device-type label, e.g. "GPU")
    provider        TEXT,                   -- manifest "provider" (vendor)
    category        TEXT,                   -- manifest "category" (raw INF class, e.g. "Display")
    version         TEXT,
    arch            TEXT,                   -- "x64" | "x86" | "arm64"
    display_name    TEXT,                   -- synthesized: descriptions[0], or "{provider} {category} {version}"
    descriptions    TEXT,                   -- JSON array of resolved INF device description strings
    os_targets      TEXT,                   -- JSON array, e.g. ["Windows 10/11"]
    primary_url     TEXT,                   -- manifest "zip" -- URL of the first archive volume
    zip_parts       INTEGER,
    date_added      TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,   -- 0/1; disabled entries are superseded-but-kept for history
    supersedes      TEXT,                   -- driver_id this entry replaces, or NULL
    superseded_by   TEXT,                   -- driver_id that replaced this entry, or NULL
    notes           TEXT,
    source_manifest TEXT NOT NULL,          -- originating *.manifest*.json filename, for mirror cleanup
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per archive volume. A single-volume package still gets one row
-- (zip_parts = 1) so the client always reads parts the same way.
CREATE TABLE IF NOT EXISTS DriverParts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id   TEXT NOT NULL,
    part_num    INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    size_bytes  INTEGER,
    sha256      TEXT,
    url         TEXT NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES Drivers(driver_id) ON DELETE CASCADE,
    UNIQUE (driver_id, part_num)
);

CREATE TABLE IF NOT EXISTS HWIDs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hwid_string TEXT NOT NULL,
    driver_id   TEXT NOT NULL,
    is_generic  INTEGER NOT NULL DEFAULT 0,  -- 1 if this string has no "&" (a "compatible ID" in Windows PnP terms)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (driver_id) REFERENCES Drivers(driver_id) ON DELETE CASCADE,
    UNIQUE (hwid_string, driver_id)
);

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------

-- Primary client-facing lookup: "given this hwid_string, which driver(s)?"
-- driver_id and is_generic are included in the index, so the query is
-- answered entirely from the index (a covering index) -- no extra table
-- seek -- which is what gets you sub-millisecond lookups at scale.
CREATE INDEX IF NOT EXISTS idx_hwids_lookup_covering
    ON HWIDs (hwid_string, driver_id, is_generic);

CREATE INDEX IF NOT EXISTS idx_hwids_driver_id
    ON HWIDs (driver_id);

CREATE INDEX IF NOT EXISTS idx_driverparts_driver_id
    ON DriverParts (driver_id);

-- Supports the cleanup diff (DELETE ... WHERE source_manifest = ?).
CREATE INDEX IF NOT EXISTS idx_drivers_source_manifest
    ON Drivers (source_manifest);

CREATE INDEX IF NOT EXISTS idx_drivers_category    ON Drivers (category);
CREATE INDEX IF NOT EXISTS idx_drivers_provider     ON Drivers (provider);
CREATE INDEX IF NOT EXISTS idx_drivers_pack         ON Drivers (pack);
CREATE INDEX IF NOT EXISTS idx_drivers_enabled      ON Drivers (enabled);

-- ----------------------------------------------------------------------------
-- Audit trail
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS SyncLog (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL DEFAULT (datetime('now')),
    manifests_processed INTEGER,
    drivers_upserted    INTEGER,
    hwids_upserted      INTEGER,
    drivers_skipped     INTEGER,
    drivers_null_enabled INTEGER NOT NULL DEFAULT 0,  -- count of entries whose "enabled": null
                                                       -- was normalized to enabled=1 this run
                                                       -- (see migrate_to_d1.py null-enabled fix)
    commit_sha          TEXT
);

-- NOTE: this CREATE TABLE is only reached on a brand-new database -- on an
-- *existing* D1 database, "IF NOT EXISTS" means it's a no-op and a column
-- added here will NOT retroactively appear on the live table. The
-- "Ensure SyncLog has drivers_null_enabled column" step in
-- .github/workflows/d1-sync.yml runs the matching idempotent
-- ALTER TABLE ADD COLUMN for already-existing databases.
