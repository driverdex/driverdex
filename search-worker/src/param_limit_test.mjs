// Regression test for the "500 on every free-text search" bug: D1 caps bound
// parameters at 100 per statement, and the original attachHwidCounts() built
// one WHERE driver_id IN (?, ?, ..., ?) with one parameter PER candidate row
// -- fine for a 25-row page, fatal for the up-to-5000-row candidate pool a
// free-text search pulls. This harness fakes just enough of D1's query
// semantics to run the real handlers against a synthetic multi-thousand-row
// dataset and asserts none of them ever bind more than 100 parameters --
// exactly the check that would have caught this before it shipped.

import {
  D1_MAX_PARAMS,
  chunk,
  buildFilter,
  fetchCandidates,
  browseDrivers,
  fetchDriversByIds,
  handleSearch,
} from "./index.js";

// ----------------------------------------------------------------------------
// Minimal fake D1: enough WHERE/ORDER BY/LIMIT semantics to run this file's
// actual SQL shapes against in-memory arrays, plus a hard bind() cap that
// throws exactly like real D1 does past 100 parameters.
// ----------------------------------------------------------------------------

function makeMockD1(drivers, hwids) {
  const hwidCount = new Map();
  for (const h of hwids) hwidCount.set(h.driver_id, (hwidCount.get(h.driver_id) || 0) + 1);

  function parseWhereClauses(sql) {
    const m = sql.match(/WHERE\s+([\s\S]*?)(?:\bORDER BY\b|\bLIMIT\b|\bGROUP BY\b|$)/i);
    if (!m) return [];
    return m[1]
      .split(/\bAND\b/i)
      .map((s) => s.trim())
      .filter(Boolean)
      .map((c) => {
        if (/enabled\s*=\s*1/i.test(c)) return { type: "enabled", value: 1 };
        if (/enabled\s*=\s*0/i.test(c)) return { type: "enabled", value: 0 };
        let mm;
        if ((mm = c.match(/(category|provider|arch)\s*=\s*\?/i))) return { type: "eq", field: mm[1] };
        if (/driver_id\s*=\s*\?\d*/i.test(c)) return { type: "eq", field: "driver_id" };
        if (/hwid_string\s*=\s*\?\d*/i.test(c)) return { type: "eq", field: "hwid_string" };
        if (/driver_id\s+LIKE\s+\?\d*/i.test(c)) return { type: "like", field: "driver_id" };
        if ((mm = c.match(/driver_id\s+IN\s*\(([^)]*)\)/i))) return { type: "in", field: "driver_id", n: mm[1].split(",").length };
        throw new Error("mock D1: unhandled WHERE clause: " + c);
      });
  }

  function paramsConsumed(clauses) {
    return clauses.reduce((sum, c) => sum + (c.type === "in" ? c.n : c.type === "enabled" ? 0 : 1), 0);
  }

  function matchRow(clauses, params, row) {
    let pi = 0;
    for (const c of clauses) {
      if (c.type === "enabled") {
        if (Number(!!row.enabled) !== c.value) return false;
      } else if (c.type === "eq") {
        const val = params[pi++];
        if (String(row[c.field]).toLowerCase() !== String(val).toLowerCase()) return false;
      } else if (c.type === "like") {
        const val = String(params[pi++]).replace(/%$/, "").toLowerCase();
        if (!String(row[c.field]).toLowerCase().startsWith(val)) return false;
      } else if (c.type === "in") {
        const set = params.slice(pi, pi + c.n).map((v) => String(v).toLowerCase());
        pi += c.n;
        if (!set.includes(String(row[c.field]).toLowerCase())) return false;
      }
    }
    return true;
  }

  function execute(sql, params) {
    const fromHwids = /FROM HWIDs/i.test(sql) && !/FROM Drivers/i.test(sql);
    const source = fromHwids ? hwids : drivers;
    const clauses = parseWhereClauses(sql);
    const consumed = paramsConsumed(clauses);
    let rows = source.filter((row) => matchRow(clauses, params, row));

    if (/SELECT COUNT\(\*\)/i.test(sql)) return [{ n: rows.length }];

    if (!fromHwids) {
      rows = rows.map((r) => ({ ...r, hwid_count: hwidCount.get(r.driver_id) || 0 }));
    } else if (/DISTINCT driver_id/i.test(sql)) {
      rows = [...new Map(rows.map((r) => [r.driver_id, r])).values()];
    }

    if (/ORDER BY d\.updated_at DESC/i.test(sql)) {
      rows = rows.slice().sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    }
    if (/ORDER BY d\.display_name/i.test(sql)) {
      rows = rows.slice().sort((a, b) => String(a.display_name || "").localeCompare(String(b.display_name || "")));
    }

    const trailing = params.slice(consumed);
    if (/LIMIT \?\s*OFFSET \?/i.test(sql)) {
      const [limit, offset] = trailing;
      rows = rows.slice(offset, offset + limit);
    } else if (/LIMIT \?/i.test(sql)) {
      const [limit] = trailing;
      rows = rows.slice(0, limit);
    }
    return rows;
  }

  return {
    prepare(sql) {
      return {
        bind(...params) {
          if (params.length > D1_MAX_PARAMS) {
            throw new Error(
              `D1_ERROR: too many SQL variables (simulated: ${params.length} bound params, D1's real cap is ${D1_MAX_PARAMS})`
            );
          }
          return {
            async all() {
              return { results: execute(sql, params) };
            },
            async first() {
              return execute(sql, params)[0] || null;
            },
          };
        },
      };
    },
  };
}

// ----------------------------------------------------------------------------
// Synthetic dataset: 5000 drivers (enough to blow past CANDIDATE_CAP's old
// bug), plus 300 HWIDs sharing one generic vendor-level compatible ID -- the
// exact shape that made fetchDriversByIds's old IN(...) explode too.
// ----------------------------------------------------------------------------

function makeDataset() {
  const drivers = [];
  for (let i = 0; i < 5000; i++) {
    const isNvidia = i % 100 === 0; // 50 NVIDIA entries scattered through 5000 rows
    drivers.push({
      driver_id: `drv-${i.toString(16).padStart(8, "0")}`,
      display_name: isNvidia ? `NVIDIA GeForce Model ${i}` : `Generic Device ${i}`,
      provider: isNvidia ? "NVIDIA" : "Acme",
      category: "Display",
      driver_type: "GPU",
      pack: `pack_${i}`,
      version: "1.0.0",
      arch: "x64",
      descriptions: isNvidia ? `["NVIDIA GeForce Model ${i}"]` : `["Generic Device ${i}"]`,
      os_targets: '["Windows 10/11"]',
      primary_url: "https://example.com/x.zip",
      zip_parts: 1,
      date_added: "2026-01-01",
      updated_at: "2026-01-01",
      enabled: 1,
      supersedes: null,
      superseded_by: null,
      source_manifest: "Display.manifest.json",
    });
  }

  // 300 driver_ids all sharing one generic HWID -- simulates a vendor-only
  // compatible ID that matches far more than D1_MAX_PARAMS drivers at once.
  const hwids = [];
  for (let i = 0; i < 300; i++) {
    hwids.push({ hwid_string: "PCI\\VEN_10DE", driver_id: drivers[i].driver_id });
  }

  return { drivers, hwids };
}

function assert(cond, msg) {
  if (!cond) throw new Error("FAIL: " + msg);
  console.log("ok  -", msg);
}

async function main() {
  const { drivers, hwids } = makeDataset();
  const env = { DB: makeMockD1(drivers, hwids), PUBLIC_BASE_URL: "https://example.workers.dev" };

  // 1. fetchCandidates against 5000 rows -- this is the exact call that used
  //    to throw "too many SQL variables" via attachHwidCounts().
  const filter = buildFilter(new URL("https://x/?"));
  const candidates = await fetchCandidates(env, filter, 5000);
  assert(candidates.length === 5000, "fetchCandidates returns all 5000 synthetic rows without throwing");
  assert(candidates.every((r) => typeof r.hwid_count === "number"), "every candidate row got a hwid_count");

  // 2. browseDrivers (facet-only browsing, small page) still works.
  const browsed = await browseDrivers(env, filter, 1, 25);
  assert(browsed.results.length === 25 && browsed.total === 5000, "browseDrivers paginates correctly over 5000 rows");

  // 3. fetchDriversByIds with 300 ids (more than D1_MAX_PARAMS) -- this is
  //    the exact shape a generic HWID match now produces; must chunk.
  const ids300 = hwids.map((h) => h.driver_id);
  const byIds = await fetchDriversByIds(env, ids300, false);
  assert(byIds.length === 300, "fetchDriversByIds correctly returns all 300 matches by chunking, not one giant IN(...)");
  assert(byIds[0].driver_id === ids300[0], "fetchDriversByIds preserves the caller's original ordering across chunks");

  // 4. Full handleSearch flow for a free-text query over the whole 5000-row
  //    table -- this is precisely the request that was 500-ing in production.
  const searchUrl = new URL("https://x/api/search?q=nvidia&page=1&pageSize=25");
  const res = await handleSearch(env, searchUrl);
  assert(res.status === 200, "handleSearch('nvidia') over 5000 rows returns 200, not 500");
  const body = await res.json();
  assert(body.mode === "text", "free-text query is routed to text mode");
  assert(body.total === 50, "all 50 NVIDIA rows are found among 5000 candidates");
  assert(body.results.length === 25, "page 1 returns a full page of 25");
  assert(
    body.results.every((r) => r.provider === "NVIDIA"),
    "every result on the page is actually an NVIDIA row"
  );

  // 5. chunk() itself, sanity check.
  assert(chunk([1, 2, 3, 4, 5], 2).length === 3, "chunk() splits into ceil(n/size) batches");
  assert(chunk([], 100).length === 0, "chunk() of an empty array is an empty array of batches");

  console.log("\nAll D1 bound-parameter regression tests passed.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
