/**
 * site/src/index.js
 * ----------------------------------------------------------------------------
 * Thin same-origin proxy in front of the static UI (./dist, served via
 * Workers Static Assets) and the driverdex-check search API (a separate Worker,
 * reached over a zero-hop Service Binding -- see [[services]] in
 * wrangler.toml). Because `run_worker_first = ["/api/*", "/i/*"]` in this
 * Worker's asset config, every /api/* and /i/* request reaches this fetch
 * handler before the static-asset router ever sees it; everything else falls
 * straight through to env.ASSETS untouched.
 *
 * /i/:token is the install-link endpoint: it looks up the ephemeral KV token,
 * deletes it on read (single-use), then renders a PowerShell install script
 * from the live D1 row and returns it as text/plain for `irm <url> | iex`.
 * ----------------------------------------------------------------------------
 */

import { buildInstallScript, buildBulkInstallScript, expiredScript } from "./install-script.js";

const PS_HEADERS = { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" };

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Install-link fetch: matched before the API proxy and the asset fallthrough.
    // Token is exactly 32 lowercase hex chars (crypto.randomUUID() with dashes
    // stripped); anything else falls through to the SPA 404 handling.
    const installMatch = url.pathname.match(/^\/i\/([a-f0-9]{32})$/);
    if (installMatch && request.method === "GET") {
      return await handleInstallFetch(installMatch[1], env);
    }

    if (url.pathname.startsWith("/api/")) {
      // Forward as-is (path + query + method); the search worker owns its
      // own response shape, status codes, and CORS headers.
      return env.SEARCH_API.fetch(request);
    }

    // Not an /api/* or /i/* request -- serve the static UI.
    return env.ASSETS.fetch(request);
  },
};

/**
 * GET /i/:token
 *
 * 1. Look the token up in KV.
 * 2. If missing/expired -> return an "expired" PS1 with status 410.
 * 3. Otherwise delete it IMMEDIATELY (single-use; closes the TTL replay window)
 *    and render the script from the *live* D1 row so a DB fix made seconds ago
 *    is reflected even on a link minted just before it.
 */
async function handleInstallFetch(token, env) {
  if (!env.INSTALL_LINKS) {
    return new Response(expiredScript(), { status: 503, headers: PS_HEADERS });
  }

  const key = `install:${token}`;
  const raw = await env.INSTALL_LINKS.get(key);
  if (!raw) {
    return new Response(expiredScript(), { status: 410, headers: PS_HEADERS });
  }

  // Single-use: delete before doing any further work.
  await env.INSTALL_LINKS.delete(key);

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return new Response(expiredScript(), { status: 410, headers: PS_HEADERS });
  }

  // Bulk token: a list of driver_ids minted from the cart "Install all" flow.
  // Render a single multi-package script from the live D1 rows.
  if (Array.isArray(payload.driver_ids) && payload.driver_ids.length > 0) {
    return await renderBulkScript(payload.driver_ids, env);
  }

  const driverId = payload.driver_id;
  if (!driverId) {
    return new Response(expiredScript(), { status: 410, headers: PS_HEADERS });
  }

  const driver = await env.DB.prepare(
    `SELECT driver_id, display_name, provider, category, version, arch, primary_url
       FROM Drivers WHERE driver_id = ?1`
  )
    .bind(driverId)
    .first();

  if (!driver) {
    return new Response(
      `Write-Host "This driver no longer exists in the DriverDex index." -ForegroundColor Red`,
      { status: 410, headers: PS_HEADERS }
    );
  }

  const { results: parts } = await env.DB.prepare(
    `SELECT part_num, filename, url, sha256
       FROM DriverParts WHERE driver_id = ?1 ORDER BY part_num`
  )
    .bind(driverId)
    .all();

  const script = buildInstallScript(driver, parts);
  return new Response(script, { status: 200, headers: PS_HEADERS });
}

/**
 * Render a bulk install script for a list of driver_ids. Each driver row +
 * its DriverParts are fetched fresh from D1 (so a recent DB fix is reflected),
 * disabled/missing drivers are silently dropped, and the surviving set is
 * handed to buildBulkInstallScript(). If nothing survives, return an expired
 * script so the user regenerates rather than running an empty installer.
 */
async function renderBulkScript(driverIds, env) {
  const items = [];
  for (const driverId of driverIds) {
    const driver = await env.DB.prepare(
      `SELECT driver_id, display_name, provider, category, version, arch, primary_url, enabled
         FROM Drivers WHERE driver_id = ?1`
    )
      .bind(driverId)
      .first();

    if (!driver || !driver.enabled) continue;

    const { results: parts } = await env.DB.prepare(
      `SELECT part_num, filename, url, sha256
         FROM DriverParts WHERE driver_id = ?1 ORDER BY part_num`
    )
      .bind(driverId)
      .all();

    items.push({ driver, parts });
  }

  if (items.length === 0) {
    return new Response(
      `Write-Host "None of the selected drivers are still available in the DriverDex index." -ForegroundColor Red`,
      { status: 410, headers: PS_HEADERS }
    );
  }

  return new Response(buildBulkInstallScript(items), { status: 200, headers: PS_HEADERS });
}
