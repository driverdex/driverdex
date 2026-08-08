-- ============================================================================
-- clean_db.sql
-- Manual / fallback mirror-cleanup template.
--
-- In normal operation, cleanup is fully automated: migrate_to_d1.py run with
-- --clean diffs the live D1 state against the manifests currently in the
-- repo and writes ready-to-run d1_cleanup_part_N.sql files (see
-- generate_cleanup_sql() in that script). You should not need this file
-- for routine syncs.
--
-- This is a manual fallback for situations like recovering from a bad sync,
-- or purging rows tied to a manifest you deleted outside of CI. It works via
-- a staging-table diff, so you never have to hand-type DELETE statements.
--
-- USAGE
-- -----
-- 1. Below, INSERT every driver_id that SHOULD currently exist (e.g. export
--    these from your manifests). This can be generated programmatically;
--    it doesn't have to be typed by hand.
--
--      INSERT INTO KnownDriverIDs (driver_id) VALUES ('nvidia-552.44-win11');
--      INSERT INTO KnownDriverIDs (driver_id) VALUES ('amd-24.3.1-win11');
--      -- ... one line per driver_id you currently trust ...
--
-- 2. Run the whole file:
--      wrangler d1 execute driverdex-index --remote --file=clean_db.sql
--
-- 3. Anything in Drivers/HWIDs NOT present in KnownDriverIDs is deleted.
-- ============================================================================

-- NOTE: no explicit BEGIN/COMMIT here -- D1 (Durable Objects SQLite) rejects
-- client-issued BEGIN TRANSACTION / SAVEPOINT statements outright ("please
-- use the state.storage.transaction() ... APIs instead"), and doesn't need
-- them: `wrangler d1 execute --remote --file=...` already applies this
-- entire file as a single atomic batch on D1's side.

CREATE TEMP TABLE IF NOT EXISTS KnownDriverIDs (
    driver_id TEXT PRIMARY KEY
);

-- >>> Insert your trusted/current driver_id list here, above this line <<<

-- Children first -- defensive, doesn't rely on FK CASCADE support.
DELETE FROM HWIDs
WHERE driver_id NOT IN (SELECT driver_id FROM KnownDriverIDs);

DELETE FROM DriverParts
WHERE driver_id NOT IN (SELECT driver_id FROM KnownDriverIDs);

DELETE FROM Drivers
WHERE driver_id NOT IN (SELECT driver_id FROM KnownDriverIDs);

DROP TABLE KnownDriverIDs;
