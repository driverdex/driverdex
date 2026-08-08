/**
 * search-worker/src/index.js
 * ----------------------------------------------------------------------------
 * The DriverDex search API (Worker name: driverdex-check).
 *
 *   GET  /api/search?q=&category=&provider=&arch=&enabledOnly=&page=&pageSize=
 *   GET  /api/driver/:driver_id
 *   GET  /api/driver/:driver_id/versions
 *   GET  /api/hwid/:hwid
 *   GET  /api/stats
 *   GET  /api/facets
 *   POST /api/install/create        { driver_id }
 *   POST /api/install/create-bulk   { driver_ids: [...] }
 *
 * ----------------------------------------------------------------------------
 * SEARCH DESIGN
 * ----------------------------------------------------------------------------
 * The single search box auto-detects three query kinds (same as the UI):
 *
 *   1. Hardware ID  ("PCI\VEN_10DE&DEV_2684", contains VEN_/DEV_/SUBSYS_/\)
 *        -> exact match against HWIDs.hwid_string, falling back through
 *           progressively shorter Windows PnP "compatible ID" forms. Exact by
 *           design: HWIDs are machine-generated, so fuzziness = wrong drivers.
 *
 *   2. Driver ID   ("drv-536c01b4...", the manifest content hash)
 *        -> exact, or prefix match on the short form shown in the UI.
 *
 *   3. Free text   ("nvidia 552 win11", "radoen rx 5700",
 *                    'provider:hid category:smartcard -superseded',
 *                    '"smart card" hidclass')
 *        -> the "search anything, any order, typo-tolerant, nested criteria"
 *           case. Handled in two stages:
 *
 *           a) SQL fetches a bounded candidate set (facet filters + enabled
 *              flag applied in WHERE -- see CANDIDATE_CAP).
 *           b) The query is parsed into CRITERIA (see parseQuery):
 *                - plain tokens        radeon 5700        (match ANY field)
 *                - field-scoped tokens provider:hid       (match ONE field)
 *                - quoted phrases      "smart card"       (contiguous match)
 *                - negations          -superseded         (must NOT match)
 *              Every positive criterion must match SOMETHING (AND across
 *              criteria, OR across the fields a criterion is allowed to touch);
 *              every negative criterion must match NOTHING. Order never
 *              matters: 'amd radeon 5700' and '5700 amd radeon' and
 *              'provider:amd 5700 radeon' all score the same set.
 *
 * Why not push this into SQL? D1 (SQLite) has no edit-distance matching, and a
 * LIKE '%token%' prefilter would exclude the typo'd rows we want to catch
 * ("radoen" never sees "Radeon"). So typo tolerance requires JS scoring over a
 * broad candidate set. D1 supports FTS5 (trigram) if this outgrows
 * CANDIDATE_CAP -- that's an addition in front of the same scoring pass, not a
 * rewrite.
 * ----------------------------------------------------------------------------
 */

// ============================================================================
// Constants
// ============================================================================

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,OPTIONS",
  "access-control-allow-headers": "content-type",
};

// How many Drivers rows we'll pull into the Worker and score in JS for a
// single free-text search. Bounded so a pathological query can't blow the CPU
// budget. If the index regularly exceeds this, add an FTS5 trigram pre-filter
// in front of the same scoring pass below.
const CANDIDATE_CAP = 5000;

const DEFAULT_PAGE_SIZE = 25;
const MAX_PAGE_SIZE = 100;

// Single-use install links (matches the "5-min expiry" copy in the UI).
const INSTALL_TTL_SECONDS = 300;

// Every field a plain (unscoped) token may match, weighted so a hit on the
// device name outranks the same token merely appearing in the OS target list.
const FIELD_WEIGHTS = {
  display_name: 1.4,
  provider: 1.2,
  driver_id: 1.15,
  category: 1.0,
  driver_type: 1.0,
  descriptions: 1.0,
  pack: 0.9,
  version: 0.85,
  arch: 0.7,
  source_manifest: 0.6,
  os_targets: 0.55,
};
const SEARCHABLE_FIELDS = Object.keys(FIELD_WEIGHTS);

// Field aliases for scoped criteria (`provider:hid`, `vendor:hid`, ...). Maps
// a user-facing key to the actual Drivers column(s) it searches. A key can map
// to several columns (e.g. "os" -> os_targets + source_manifest) so users
// don't have to know the exact schema.
const FIELD_ALIASES = {
  name: ["display_name"],
  display: ["display_name"],
  display_name: ["display_name"],
  device: ["display_name"],
  provider: ["provider"],
  vendor: ["provider"],
  publisher: ["provider"],
  mfg: ["provider"],
  manufacturer: ["provider"],
  category: ["category"],
  class: ["category"],
  type: ["driver_type"],
  driver_type: ["driver_type"],
  id: ["driver_id"],
  driver_id: ["driver_id"],
  drv: ["driver_id"],
  pack: ["pack"],
  version: ["version"],
  ver: ["version"],
  v: ["version"],
  arch: ["arch"],
  architecture: ["arch"],
  bits: ["arch"],
  desc: ["descriptions"],
  description: ["descriptions"],
  descriptions: ["descriptions"],
  os: ["os_targets", "source_manifest"],
  target: ["os_targets"],
  targets: ["os_targets"],
  manifest: ["source_manifest"],
  source: ["source_manifest"],
};

// Cap on how many criteria we'll honor per query -- keeps worst-case scoring
// cost (criteria x fields x candidates) bounded even if someone pastes a
// paragraph into the box.
const MAX_CRITERIA = 8;

// Minimum per-criterion weighted score to count as a match. Mirrors the old
// behavior (any positive score matched); kept as a named constant so the
// negation path uses the exact same threshold as the positive path.
const MATCH_THRESHOLD = 0.0001;

// ============================================================================
// Small utilities
// ============================================================================

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...CORS_HEADERS,
      ...extraHeaders,
    },
  });
}

function clampInt(raw, fallback, min, max) {
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function safeJsonParse(text, fallback) {
  if (!text) return fallback;
  try {
    const parsed = JSON.parse(text);
    return parsed == null ? fallback : parsed;
  } catch {
    return fallback;
  }
}

// ============================================================================
// Fuzzy, field-scoped, multi-criteria text matching
// ============================================================================

// Bounded Damerau-Levenshtein (optimal-string-alignment): bails out
// (returns max + 1) as soon as the true distance provably exceeds `max`, so a
// short token never pays for a full comparison against a long field. Adjacent
// transposition ("radoen" <-> "radeon") is a single edit -- that swap is the
// most common real typo, so counting it as cost-2 would miss exactly the
// typos users make most.
function boundedLevenshtein(a, b, max) {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > max) return max + 1;

  const al = a.length;
  const bl = b.length;
  let prev2 = new Array(bl + 1).fill(0);
  let prev = new Array(bl + 1);
  let curr = new Array(bl + 1);
  for (let j = 0; j <= bl; j++) prev[j] = j;

  for (let i = 1; i <= al; i++) {
    curr[0] = i;
    let rowMin = curr[0];
    const ca = a.charCodeAt(i - 1);
    for (let j = 1; j <= bl; j++) {
      const cb = b.charCodeAt(j - 1);
      const cost = ca === cb ? 0 : 1;
      let val = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost);
      if (i > 1 && j > 1 && ca === b.charCodeAt(j - 2) && a.charCodeAt(i - 2) === cb) {
        val = Math.min(val, prev2[j - 2] + 1);
      }
      curr[j] = val;
      if (val < rowMin) rowMin = val;
    }
    if (rowMin > max) return max + 1; // whole row already over budget
    const tmp = prev2;
    prev2 = prev;
    prev = curr;
    curr = tmp;
  }
  return prev[bl];
}

// How much edit-distance slack a token gets, scaled to its length: short
// tokens (<=3 chars: "amd", "rx", "x64") get none -- fuzzing them would match
// half the dictionary -- longer tokens get 1-2 chars, which is what catches
// real typos like "radoen" -> "radeon".
function typoBudget(len) {
  if (len <= 3) return 0;
  if (len <= 7) return 1;
  return 2;
}

// Score a single token against a single field's raw text. 0 = no match;
// higher = closer. Field text may be a JSON-array-as-string
// (descriptions/os_targets) -- word-splitting on non-alphanumerics treats the
// JSON punctuation as separators and still finds the real words.
function scoreTokenAgainstText(token, text) {
  if (!text) return 0;
  const hay = String(text).toLowerCase();

  if (hay === token) return 100;
  if (hay.startsWith(token)) return 92;
  if (hay.includes(token)) return 80;

  const words = hay.split(/[^a-z0-9]+/).filter(Boolean);
  const budget = typoBudget(token.length);
  let best = 0;
  for (const w of words) {
    if (w === token) return 100;
    if (w.startsWith(token) || (token.length >= 3 && token.startsWith(w))) {
      best = Math.max(best, 88);
      continue;
    }
    if (budget > 0) {
      const d = boundedLevenshtein(token, w, budget);
      if (d <= budget) best = Math.max(best, 76 - d * 10);
    }
  }
  return best;
}

// Score a multi-word phrase against a field's text. First tries the phrase as
// a contiguous substring (strongest signal); otherwise requires EVERY phrase
// word to fuzzy-match somewhere in the field, scoring the average. Lets
// '"smart card"' match "SmartCard" / "Smart Card Reader" without matching a
// row that merely has "smart" and "card" in unrelated fields.
function scorePhraseAgainstText(phrase, text) {
  if (!text) return 0;
  const hay = String(text).toLowerCase();
  const collapsedPhrase = phrase.replace(/\s+/g, " ").trim();

  if (hay.includes(collapsedPhrase)) return 100;
  // Also try with separators stripped ("smart card" vs "smartcard").
  const squished = collapsedPhrase.replace(/\s+/g, "");
  if (hay.replace(/[^a-z0-9]+/g, "").includes(squished)) return 90;

  const parts = collapsedPhrase.split(" ").filter(Boolean);
  if (parts.length === 0) return 0;
  let sum = 0;
  for (const p of parts) {
    const s = scoreTokenAgainstText(p, hay);
    if (s <= 0) return 0; // every word of the phrase must appear
    sum += s;
  }
  return (sum / parts.length) * 0.85; // slightly below a true contiguous hit
}

// Parse a free-text query string into structured criteria. Order-independent
// by construction -- criteria are collected into a flat list and every one is
// required (or forbidden, for negations) regardless of position.
//
// Grammar (whitespace-separated, quotes group):
//   token                plain fuzzy token, matches any field
//   field:token          scoped token, matches only that field's column(s)
//   field:"two words"    scoped phrase
//   "two words"          plain phrase, matches any field
//   -token / -field:x    negation (row must NOT match this)
//
// Unknown field prefixes ("foo:bar") are treated as a plain token of the whole
// "foo:bar" string, so a stray colon never silently drops a criterion.
function parseQuery(q) {
  const criteria = [];
  const seen = new Set();

  // Split respecting double quotes.
  const raw = [];
  const re = /-?(?:[a-z0-9_]+:)?"[^"]*"|\S+/gi;
  let m;
  while ((m = re.exec(q)) !== null) raw.push(m[0]);

  for (let piece of raw) {
    if (criteria.length >= MAX_CRITERIA) break;
    if (!piece) continue;

    let negate = false;
    if (piece[0] === "-" && piece.length > 1) {
      negate = true;
      piece = piece.slice(1);
    }

    // Field scope?
    let field = null;
    let value = piece;
    const colon = piece.indexOf(":");
    if (colon > 0) {
      const key = piece.slice(0, colon).toLowerCase();
      if (FIELD_ALIASES[key]) {
        field = FIELD_ALIASES[key];
        value = piece.slice(colon + 1);
      }
    }

    // Strip surrounding quotes; detect phrase (contains whitespace).
    let isPhrase = false;
    if (value.length >= 2 && value[0] === '"' && value[value.length - 1] === '"') {
      value = value.slice(1, -1).trim();
      isPhrase = true;
    }
    value = value.toLowerCase().trim();
    if (!value) continue;
    if (/\s/.test(value)) isPhrase = true;

    // Version convention: users type "v6.1.0.0" but the DB stores "6.1.0.0".
    // Strip a leading "v" only when it prefixes a digit, so "vga"/"video" are
    // untouched. Applied to plain tokens only (phrases keep their literal form).
    if (!isPhrase && /^v\d/.test(value)) value = value.slice(1);

    const key = `${negate ? "-" : ""}${field ? field.join(",") : "*"}:${isPhrase ? "p" : "t"}:${value}`;
    if (seen.has(key)) continue;
    seen.add(key);

    criteria.push({ value, field, negate, isPhrase });
  }

  return criteria;
}

// Score one candidate against every parsed criterion.
//   - positive criterion: must match somewhere in its allowed fields (AND
//     across criteria, OR across those fields); contributes its best weighted
//     score to the total.
//   - negative criterion: if it matches anywhere in its allowed fields, the
//     whole row is disqualified.
// Returns -1 for a disqualified row, otherwise the summed score (higher =
// better). An empty criteria list returns 0 (caller treats that as "browse").
function scoreCandidate(row, criteria) {
  let total = 0;

  for (const c of criteria) {
    const fields = c.field || SEARCHABLE_FIELDS;
    let best = 0;

    for (const field of fields) {
      // Scoped criteria use full field weight (1.0) so a deliberate
      // provider:amd isn't penalized by provider's default plain weight;
      // unscoped criteria keep the field-weight ranking.
      const weight = c.field ? 1 : (FIELD_WEIGHTS[field] || 1);
      const raw = c.isPhrase
        ? scorePhraseAgainstText(c.value, row[field])
        : scoreTokenAgainstText(c.value, row[field]);
      const s = raw * weight;
      if (s > best) best = s;
    }

    if (c.negate) {
      if (best > MATCH_THRESHOLD) return -1; // forbidden term present
      continue; // negations don't add to the score
    }

    if (best <= MATCH_THRESHOLD) return -1; // required term missing
    total += best;
  }

  return total;
}

// ============================================================================
// Query-mode detection (mirrors looksLikeHwid / looksLikeDriverId in the UI)
// ============================================================================

function looksLikeHwid(q) {
  return /VEN_|DEV_|SUBSYS_|\\/i.test(q);
}

function looksLikeDriverId(q) {
  return /^drv-[0-9a-f]{6,}/i.test(q.trim());
}

// "PCI\VEN_10DE&DEV_2684&SUBSYS_408A1043&REV_A1" ->
//   ["PCI\VEN_10DE&DEV_2684&SUBSYS_408A1043&REV_A1",
//    "PCI\VEN_10DE&DEV_2684&SUBSYS_408A1043",
//    "PCI\VEN_10DE&DEV_2684",
//    "PCI\VEN_10DE"]
// Mirrors how Windows resolves a full hardware ID down to progressively more
// generic compatible IDs -- we walk the same ladder so a driver registered
// only under a generic ID (or vice versa) still gets found.
function hwidTruncationLevels(hwid) {
  const sep = hwid.indexOf("\\");
  if (sep === -1) return [hwid];
  const bus = hwid.slice(0, sep);
  const segments = hwid
    .slice(sep + 1)
    .split("&")
    .filter(Boolean);
  const levels = [];
  for (let i = segments.length; i >= 1; i--) {
    levels.push(bus + "\\" + segments.slice(0, i).join("&"));
  }
  return levels.length ? levels : [hwid];
}

// ============================================================================
// D1 access helpers
// ============================================================================

// Cloudflare D1 caps bound parameters at 100 per statement. Any
// WHERE x IN (?, ?, ...) built from a data-driven id list MUST stay under
// this, so those queries are batched via chunk() rather than one giant IN(...).
const D1_MAX_PARAMS = 100;

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

// HWID-count-per-driver join. A parameter-free subquery (one GROUP BY over
// HWIDs using idx_hwids_driver_id) rather than a per-row correlated subquery
// or an IN(...) keyed by the caller's id list -- costs zero extra bound
// parameters no matter how many Drivers rows it's joined against.
const HWID_COUNT_JOIN = `
  LEFT JOIN (SELECT driver_id, COUNT(*) c FROM HWIDs GROUP BY driver_id) hc
    ON hc.driver_id = d.driver_id
`;

function toResultShape(row) {
  return {
    driver_id: row.driver_id,
    display_name: row.display_name,
    provider: row.provider,
    category: row.category,
    version: row.version,
    arch: row.arch,
    enabled: !!row.enabled,
    supersedes: row.supersedes || null,
    superseded_by: row.superseded_by || null,
    hwid_count: row.hwid_count || 0,
    primary_url: row.primary_url || null,
    zip_parts: row.zip_parts || null,
    date_added: row.date_added || null,
  };
}

// Shared WHERE clause for enabled/category/provider/arch, used by every search
// mode so "show old versions" and the three facet dropdowns behave identically
// whether or not the box has free text in it.
function buildFilter(url) {
  const category = url.searchParams.get("category") || null;
  const provider = url.searchParams.get("provider") || null;
  const arch = url.searchParams.get("arch") || null;
  const includeDisabled = url.searchParams.get("enabledOnly") === "0";

  const clauses = [];
  const params = [];
  if (!includeDisabled) clauses.push("d.enabled = 1");
  if (category) {
    clauses.push("d.category = ?");
    params.push(category);
  }
  if (provider) {
    clauses.push("d.provider = ?");
    params.push(provider);
  }
  if (arch) {
    clauses.push("d.arch = ?");
    params.push(arch);
  }

  return {
    where: clauses.length ? "WHERE " + clauses.join(" AND ") : "",
    params,
    includeDisabled,
  };
}

// SQL column expression for every searchable field, used by the prefilter.
const PREFILTER_COLUMNS = {
  display_name: "d.display_name",
  provider: "d.provider",
  category: "d.category",
  driver_type: "d.driver_type",
  pack: "d.pack",
  version: "d.version",
  arch: "d.arch",
  descriptions: "d.descriptions",
  os_targets: "d.os_targets",
};

// Build a coarse SQL prefilter from the positive criteria so we only pull rows
// that plausibly match, instead of dragging the 5000 most-recent rows into the
// worker and fuzzy-scoring all of them (which both hid older drivers and blew
// the CPU limit). Each criterion contributes a short LIKE fragment (first few
// chars) OR-ed across its columns; criteria are AND-ed together, mirroring the
// scoring pass. The fragment is intentionally short so a typo later in the word
// ("radoen") still survives the prefilter and reaches the fuzzy scorer.
function buildPrefilter(criteria) {
  const clauses = [];
  const params = [];

  for (const c of criteria) {
    if (c.negate) continue; // negations are enforced in the JS scoring pass
    const frag = c.value.slice(0, 4).replace(/[%_]/g, "");
    if (!frag) continue; // e.g. a token that was pure punctuation
    const cols = (c.field || SEARCHABLE_FIELDS)
      .map((f) => PREFILTER_COLUMNS[f])
      .filter(Boolean);
    if (!cols.length) continue;

    const like = `%${frag}%`;
    const ors = cols.map((col) => {
      params.push(like);
      return `${col} LIKE ? COLLATE NOCASE`;
    });
    clauses.push(`(${ors.join(" OR ")})`);
  }

  return { clause: clauses.join(" AND "), params };
}

async function fetchCandidates(env, filter, criteria, limit) {
  const pre = buildPrefilter(criteria);
  const clauses = [];
  if (filter.where) clauses.push(filter.where.replace(/^WHERE\s+/i, ""));
  if (pre.clause) clauses.push(pre.clause);
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";

  const sql = `
    SELECT d.driver_id, d.display_name, d.provider, d.category, d.driver_type,
           d.pack, d.version, d.arch, d.descriptions, d.os_targets,
           d.primary_url, d.zip_parts, d.date_added, d.enabled,
           d.supersedes, d.superseded_by, d.source_manifest,
           COALESCE(hc.c, 0) AS hwid_count
    FROM Drivers d
    ${HWID_COUNT_JOIN}
    ${where}
    ORDER BY d.updated_at DESC
    LIMIT ?
  `;
  const { results } = await env.DB.prepare(sql)
    .bind(...filter.params, ...pre.params, limit)
    .all();
  return results;
}

async function browseDrivers(env, filter, page, pageSize) {
  const countSql = `SELECT COUNT(*) n FROM Drivers d ${filter.where}`;
  const countRow = await env.DB.prepare(countSql).bind(...filter.params).first();
  const total = countRow ? countRow.n : 0;

  const sql = `
    SELECT d.driver_id, d.display_name, d.provider, d.category, d.version,
           d.arch, d.enabled, d.supersedes, d.superseded_by, d.primary_url,
           d.zip_parts, d.date_added,
           COALESCE(hc.c, 0) AS hwid_count
    FROM Drivers d
    ${HWID_COUNT_JOIN}
    ${filter.where}
    ORDER BY d.display_name COLLATE NOCASE ASC
    LIMIT ? OFFSET ?
  `;
  const { results } = await env.DB.prepare(sql)
    .bind(...filter.params, pageSize, (page - 1) * pageSize)
    .all();
  return { results, total };
}

// Fetches full rows for an ordered list of driver_ids and re-applies that
// order afterward (SQL IN(...) makes no ordering promise). The id list is
// data-driven, so it's chunked to stay under D1_MAX_PARAMS.
async function fetchDriversByIds(env, ids, includeDisabled) {
  if (ids.length === 0) return [];
  const enabledClause = includeDisabled ? "" : "AND d.enabled = 1";

  const batches = await Promise.all(
    chunk(ids, D1_MAX_PARAMS).map(async (batchIds) => {
      const placeholders = batchIds.map(() => "?").join(",");
      const sql = `
        SELECT d.driver_id, d.display_name, d.provider, d.category, d.version,
               d.arch, d.enabled, d.supersedes, d.superseded_by, d.primary_url,
               d.zip_parts, d.date_added,
               COALESCE(hc.c, 0) AS hwid_count
        FROM Drivers d
        ${HWID_COUNT_JOIN}
        WHERE d.driver_id IN (${placeholders}) ${enabledClause}
      `;
      const { results } = await env.DB.prepare(sql).bind(...batchIds).all();
      return results;
    })
  );
  const results = batches.flat();

  const order = new Map(ids.map((id, i) => [id, i]));
  results.sort((a, b) => (order.get(a.driver_id) ?? 0) - (order.get(b.driver_id) ?? 0));
  return results;
}

async function matchHwid(env, rawHwid) {
  const hwid = rawHwid.trim().replace(/\\+/g, "\\");
  for (const level of hwidTruncationLevels(hwid)) {
    const { results } = await env.DB.prepare(
      `SELECT DISTINCT driver_id FROM HWIDs WHERE hwid_string = ?1 COLLATE NOCASE`
    )
      .bind(level)
      .all();
    if (results.length > 0) {
      return { driverIds: results.map((r) => r.driver_id), matchedAt: level, exact: level === hwid };
    }
  }
  return { driverIds: [], matchedAt: null, exact: false };
}

async function matchDriverId(env, q, includeDisabled) {
  const query = q.trim();
  const enabledClause = includeDisabled ? "" : "AND enabled = 1";

  const exact = await env.DB.prepare(
    `SELECT driver_id FROM Drivers WHERE driver_id = ?1 COLLATE NOCASE ${enabledClause}`
  )
    .bind(query)
    .first();
  if (exact) return [exact.driver_id];

  const { results } = await env.DB.prepare(
    `SELECT driver_id FROM Drivers
     WHERE driver_id LIKE ?1 COLLATE NOCASE ${enabledClause}
     ORDER BY driver_id LIMIT 50`
  )
    .bind(query + "%")
    .all();
  return results.map((r) => r.driver_id);
}

// ============================================================================
// Route handlers
// ============================================================================

async function handleSearch(env, url) {
  const rawQ = (url.searchParams.get("q") || "").trim();
  const page = clampInt(url.searchParams.get("page"), 1, 1, 1_000_000);
  const pageSize = clampInt(url.searchParams.get("pageSize"), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE);
  const filter = buildFilter(url);

  // ---- Hardware-ID mode: exact / PnP-style compatible-ID fallback --------
  if (rawQ && looksLikeHwid(rawQ)) {
    const { driverIds, matchedAt, exact } = await matchHwid(env, rawQ);
    const rows = await fetchDriversByIds(env, driverIds, filter.includeDisabled);
    const total = rows.length;
    const pageRows = rows.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize);
    return jsonResponse({
      results: pageRows.map(toResultShape),
      total,
      page,
      pageSize,
      mode: "hwid",
      matchedAt,
      exact,
    });
  }

  // ---- Driver-ID mode: exact, or prefix if only the short form was pasted -
  if (rawQ && looksLikeDriverId(rawQ)) {
    const ids = await matchDriverId(env, rawQ, filter.includeDisabled);
    const rows = await fetchDriversByIds(env, ids, filter.includeDisabled);
    const total = rows.length;
    const pageRows = rows.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize);
    return jsonResponse({ results: pageRows.map(toResultShape), total, page, pageSize, mode: "id" });
  }

  // ---- Parse free text into criteria (tokens / scoped / phrases / negations)
  const criteria = rawQ ? parseQuery(rawQ) : [];

  // No positive criteria (empty box, or a query that was only a negation like
  // "-superseded") -> facet browse, server-side paginated. A lone negation
  // can't define a result set on its own, so we fall back to browse rather
  // than scoring the entire table.
  const hasPositive = criteria.some((c) => !c.negate);
  if (!hasPositive) {
    const { results, total } = await browseDrivers(env, filter, page, pageSize);
    return jsonResponse({ results: results.map(toResultShape), total, page, pageSize, mode: "browse" });
  }

  // ---- Free text: bounded candidate fetch + fuzzy multi-criteria scoring --
  const candidates = await fetchCandidates(env, filter, criteria, CANDIDATE_CAP);
  const scored = [];
  for (const row of candidates) {
    const s = scoreCandidate(row, criteria);
    if (s >= 0) scored.push({ row, s });
  }
  scored.sort(
    (a, b) =>
      b.s - a.s ||
      b.row.enabled - a.row.enabled ||
      String(b.row.date_added || "").localeCompare(String(a.row.date_added || "")) ||
      a.row.driver_id.localeCompare(b.row.driver_id)
  );

  const total = scored.length;
  const pageRows = scored
    .slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize)
    .map((x) => x.row);

  return jsonResponse({
    results: pageRows.map(toResultShape),
    total,
    page,
    pageSize,
    mode: "text",
    // Informational for API consumers/debugging -- UI ignores unknown fields.
    candidatesScanned: candidates.length,
    candidateCapHit: candidates.length >= CANDIDATE_CAP,
  });
}

async function handleDriverDetail(env, driverId) {
  const driver = await env.DB.prepare(`SELECT * FROM Drivers WHERE driver_id = ?1`).bind(driverId).first();
  if (!driver) return jsonResponse({ error: "Driver not found" }, 404);

  const [{ results: parts }, { results: hwidRows }] = await Promise.all([
    env.DB.prepare(
      `SELECT part_num, filename, size_bytes, sha256, url FROM DriverParts WHERE driver_id = ?1 ORDER BY part_num`
    )
      .bind(driverId)
      .all(),
    env.DB.prepare(
      `SELECT hwid_string, is_generic FROM HWIDs WHERE driver_id = ?1 ORDER BY is_generic ASC, hwid_string ASC`
    )
      .bind(driverId)
      .all(),
  ]);

  return jsonResponse({
    ...driver,
    descriptions: safeJsonParse(driver.descriptions, []),
    os_targets: safeJsonParse(driver.os_targets, []),
    parts,
    hwids: hwidRows.map((h) => ({ hwid_string: h.hwid_string, is_generic: !!h.is_generic })),
  });
}

// Walks the supersedes / superseded_by chain in both directions to collect
// every version linked (directly or transitively) to the requested driver,
// then marks whichever entry is enabled with no superseded_by as current.
async function handleDriverVersions(env, driverId) {
  const start = await env.DB.prepare(
    `SELECT driver_id FROM Drivers WHERE driver_id = ?1`
  )
    .bind(driverId)
    .first();
  if (!start) return jsonResponse({ error: "Driver not found" }, 404);

  const seen = new Set([driverId]);
  const queue = [driverId];
  while (queue.length) {
    const id = queue.pop();
    const row = await env.DB.prepare(
      `SELECT supersedes, superseded_by FROM Drivers WHERE driver_id = ?1`
    )
      .bind(id)
      .first();
    if (!row) continue;
    for (const next of [row.supersedes, row.superseded_by]) {
      if (next && !seen.has(next)) {
        seen.add(next);
        queue.push(next);
      }
    }
  }

  const ids = Array.from(seen);
  const batches = await Promise.all(
    chunk(ids, D1_MAX_PARAMS).map(async (batchIds) => {
      const placeholders = batchIds.map(() => "?").join(",");
      const { results } = await env.DB.prepare(
        `SELECT driver_id, display_name, version, date_added, enabled, superseded_by, supersedes
         FROM Drivers WHERE driver_id IN (${placeholders})`
      )
        .bind(...batchIds)
        .all();
      return results;
    })
  );
  const results = batches.flat().sort((a, b) => String(a.date_added || "").localeCompare(String(b.date_added || "")));

  const head = results.find((r) => r.enabled && !r.superseded_by) || results[results.length - 1];
  const versions = results.map((r) => ({ ...r, is_current: head != null && r.driver_id === head.driver_id }));

  return jsonResponse({ display_name: head ? head.display_name : null, versions });
}

async function handleHwidLookup(env, url, hwidParam) {
  const includeDisabled = url.searchParams.get("enabledOnly") === "0";
  const { driverIds, matchedAt, exact } = await matchHwid(env, decodeURIComponent(hwidParam));
  const rows = await fetchDriversByIds(env, driverIds, includeDisabled);
  return jsonResponse({ results: rows.map(toResultShape), total: rows.length, mode: "hwid", matchedAt, exact });
}

async function handleStats(env) {
  const [drivers, hwids, providers, noHwid, disabled, lastSync] = await Promise.all([
    env.DB.prepare(`SELECT COUNT(*) n FROM Drivers`).first(),
    env.DB.prepare(`SELECT COUNT(*) n FROM HWIDs`).first(),
    env.DB.prepare(`SELECT COUNT(DISTINCT provider) n FROM Drivers WHERE provider IS NOT NULL AND provider != ''`).first(),
    env.DB.prepare(
      `SELECT COUNT(*) n FROM Drivers d WHERE NOT EXISTS (SELECT 1 FROM HWIDs h WHERE h.driver_id = d.driver_id)`
    ).first(),
    env.DB.prepare(`SELECT COUNT(*) n FROM Drivers WHERE enabled = 0`).first(),
    env.DB.prepare(`SELECT run_at, commit_sha FROM SyncLog ORDER BY run_at DESC, id DESC LIMIT 1`).first(),
  ]);

  return jsonResponse({
    drivers: drivers.n,
    hwids: hwids.n,
    providers: providers.n,
    drivers_no_hwid: noHwid.n,
    drivers_disabled: disabled.n,
    lastSync: lastSync ? { run_at: lastSync.run_at, commit_sha: lastSync.commit_sha } : null,
  });
}

async function handleFacets(env) {
  const [categories, providers, archs] = await Promise.all([
    env.DB.prepare(
      `SELECT category, COUNT(*) n FROM Drivers
       WHERE enabled = 1 AND category IS NOT NULL AND category != ''
       GROUP BY category ORDER BY n DESC, category ASC`
    ).all(),
    env.DB.prepare(
      `SELECT provider, COUNT(*) n FROM Drivers
       WHERE enabled = 1 AND provider IS NOT NULL AND provider != ''
       GROUP BY provider ORDER BY n DESC, provider ASC`
    ).all(),
    env.DB.prepare(
      `SELECT arch, COUNT(*) n FROM Drivers
       WHERE enabled = 1 AND arch IS NOT NULL AND arch != ''
       GROUP BY arch ORDER BY n DESC, arch ASC`
    ).all(),
  ]);

  return jsonResponse({
    categories: categories.results,
    providers: providers.results,
    archs: archs.results,
  });
}

function makeToken() {
  return crypto.randomUUID().replace(/-/g, "");
}

async function handleInstallCreate(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON body" }, 400);
  }
  const driverId = body && body.driver_id;
  if (!driverId) return jsonResponse({ error: "driver_id is required" }, 400);

  const driver = await env.DB.prepare(`SELECT driver_id, enabled FROM Drivers WHERE driver_id = ?1`)
    .bind(driverId)
    .first();
  if (!driver) return jsonResponse({ error: "Driver not found" }, 404);
  if (!driver.enabled) return jsonResponse({ error: "This driver is disabled or superseded and cannot be installed" }, 410);

  const token = makeToken();
  await env.INSTALL_LINKS.put(`install:${token}`, JSON.stringify({ driver_id: driverId }), {
    expirationTtl: INSTALL_TTL_SECONDS,
  });

  const base = (env.PUBLIC_BASE_URL || "").replace(/\/+$/, "");
  return jsonResponse({
    command: `irm ${base}/i/${token} | iex`,
    expires_in_seconds: INSTALL_TTL_SECONDS,
  });
}

async function handleInstallCreateBulk(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON body" }, 400);
  }
  const ids = Array.isArray(body && body.driver_ids) ? body.driver_ids.filter(Boolean) : [];
  if (ids.length === 0) return jsonResponse({ error: "driver_ids must be a non-empty array" }, 400);

  const batches = await Promise.all(
    chunk(ids, D1_MAX_PARAMS).map(async (batchIds) => {
      const placeholders = batchIds.map(() => "?").join(",");
      const { results } = await env.DB.prepare(
        `SELECT driver_id FROM Drivers WHERE driver_id IN (${placeholders}) AND enabled = 1`
      )
        .bind(...batchIds)
        .all();
      return results;
    })
  );
  const kept = batches.flat().map((r) => r.driver_id);
  const skipped = ids.length - kept.length;
  if (kept.length === 0) return jsonResponse({ error: "None of the selected drivers are available" }, 410);

  const token = makeToken();
  await env.INSTALL_LINKS.put(`install:${token}`, JSON.stringify({ driver_ids: kept }), {
    expirationTtl: INSTALL_TTL_SECONDS,
  });

  const base = (env.PUBLIC_BASE_URL || "").replace(/\/+$/, "");
  return jsonResponse({
    command: `irm ${base}/i/${token} | iex`,
    expires_in_seconds: INSTALL_TTL_SECONDS,
    driver_count: kept.length,
    skipped,
  });
}

// ============================================================================
// Router
// ============================================================================

// Named exports of the pure search functions, so they can be unit-tested (or
// reused from a debug endpoint) without a D1 binding. Wrangler only ever calls
// the default export below.
export {
  parseQuery,
  scoreTokenAgainstText,
  scorePhraseAgainstText,
  scoreCandidate,
  hwidTruncationLevels,
  looksLikeHwid,
  looksLikeDriverId,
  chunk,
  D1_MAX_PARAMS,
  buildFilter,
  buildPrefilter,
  fetchCandidates,
  browseDrivers,
  fetchDriversByIds,
  handleSearch,
  FIELD_ALIASES,
  FIELD_WEIGHTS,
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    try {
      if (pathname === "/api/search" && request.method === "GET") {
        return await handleSearch(env, url);
      }
      if (pathname === "/api/facets" && request.method === "GET") {
        return await handleFacets(env);
      }
      if (pathname === "/api/stats" && request.method === "GET") {
        return await handleStats(env);
      }

      const versionsMatch = pathname.match(/^\/api\/driver\/([^/]+)\/versions$/);
      if (versionsMatch && request.method === "GET") {
        return await handleDriverVersions(env, decodeURIComponent(versionsMatch[1]));
      }

      const driverMatch = pathname.match(/^\/api\/driver\/([^/]+)$/);
      if (driverMatch && request.method === "GET") {
        return await handleDriverDetail(env, decodeURIComponent(driverMatch[1]));
      }

      const hwidMatch = pathname.match(/^\/api\/hwid\/(.+)$/);
      if (hwidMatch && request.method === "GET") {
        return await handleHwidLookup(env, url, hwidMatch[1]);
      }

      if (pathname === "/api/install/create" && request.method === "POST") {
        return await handleInstallCreate(request, env);
      }
      if (pathname === "/api/install/create-bulk" && request.method === "POST") {
        return await handleInstallCreateBulk(request, env);
      }

      return jsonResponse({ error: "Not found" }, 404);
    } catch (err) {
      console.error("search-worker error:", err && err.stack ? err.stack : err);
      // Surface the real cause so failures are diagnosable from the network
      // tab instead of an opaque 500. Safe to keep: it only ever runs on the
      // error path and never leaks row data.
      return jsonResponse(
        {
          error: "Internal error",
          message: err && err.message ? err.message : String(err),
          where: err && err.stack ? String(err.stack).split("\n").slice(0, 4).join("\n") : null,
        },
        500
      );
    }
  },
};
