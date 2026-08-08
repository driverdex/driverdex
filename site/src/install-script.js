/**
 * site/src/install-script.js
 * ----------------------------------------------------------------------------
 * Pure function: (driver row + ordered DriverParts[]) -> PowerShell (.ps1) text.
 *
 * The output is what `GET /i/:token` returns as text/plain and what the user
 * pipes through `irm <url> | iex`. Everything the script needs is interpolated
 * server-side from the live D1 row — the client never supplies a URL, filename,
 * or hash. Each part is SHA256-verified before extraction, the script credits
 * `rhshourav` in its banner and footer, and the install step follows the
 * per-package logic from the spec (silent pnputil for INF-only packages, prompt
 * before a vendor setup.exe/msi, else point at Device Manager).
 *
 * SECURITY: all dynamic values are emitted as PowerShell SINGLE-quoted string
 * literals via psLit(), which doubles embedded single quotes. Single-quoted PS
 * strings are fully literal — no $variable, subexpression, or backtick-escape
 * interpolation happens inside them — so a hostile display_name/filename cannot
 * inject executable PowerShell. URLs are additionally constrained to https://.
 * ----------------------------------------------------------------------------
 */

export const EXTRACTOR_URL =
  "https://raw.githubusercontent.com/rhshourav/driverdex/refs/heads/main/extractor/extractor.exe";

const GITHUB_URL = "https://github.com/rhshourav/driverdex";
const AUTHOR = "rhshourav";

/** Git LFS batch endpoint for the public repo (download needs no auth). */
const LFS_BATCH_URL = "https://github.com/rhshourav/driverdex.git/info/lfs/objects/batch";

/**
 * Reusable PowerShell helper injected into every generated script.
 *
 * WHY: large driver archives are stored in Git LFS. Fetching their
 * raw.githubusercontent.com URL returns a tiny text "pointer" file, not the
 * binary — so a naive Invoke-WebRequest downloads the stub and the SHA256
 * check fails ("Checksum mismatch"). Get-DriverDexFile downloads the URL,
 * detects an LFS pointer, parses its oid/size, resolves the real object via
 * the LFS batch API, and re-downloads the actual binary. For non-LFS files it
 * is a no-op pass-through. Mirrors the Python client's _download_file_from_github.
 *
 * NOTE: kept free of backticks and of the "${" sequence so it survives being
 * embedded inside a JS template literal verbatim.
 */
const LFS_HELPER_PS = `$DriverDexLfsBatchUrl = '${LFS_BATCH_URL}'

function Get-DriverDexFile {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Dest
    )

    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing

    # Anything larger than 1 KB cannot be an LFS pointer — keep the binary as-is.
    $fileInfo = Get-Item -LiteralPath $Dest
    if ($fileInfo.Length -gt 1024) { return }

    $firstLine = ''
    try { $firstLine = Get-Content -LiteralPath $Dest -TotalCount 1 -ErrorAction Stop } catch { return }
    if ($firstLine -notlike 'version https://git-lfs.github.com/spec/*') { return }

    # It IS a Git LFS pointer: parse oid + size and resolve the real object.
    $oid = ''
    $size = ''
    foreach ($line in (Get-Content -LiteralPath $Dest)) {
        $trimmed = $line.Trim()
        if ($trimmed -like 'oid sha256:*') { $oid = $trimmed.Substring($trimmed.IndexOf(':') + 1).Trim() }
        elseif ($trimmed -like 'size *')   { $size = $trimmed.Substring(5).Trim() }
    }
    if (-not $oid -or -not $size) { throw 'Malformed Git LFS pointer encountered.' }

    $reqBody = @{
        operation = 'download'
        transfers = @('basic')
        objects   = @(@{ oid = $oid; size = [long]$size })
    } | ConvertTo-Json -Depth 5

    $batch = Invoke-RestMethod -Uri $DriverDexLfsBatchUrl -Method Post -Body $reqBody -ContentType 'application/vnd.git-lfs+json' -Headers @{ 'Accept' = 'application/vnd.git-lfs+json' }

    $obj = $batch.objects[0]
    if ($obj.error) { throw ('Git LFS error: ' + $obj.error.message) }
    $href = $obj.actions.download.href
    if (-not $href) { throw 'Git LFS batch API returned no download URL.' }

    $dlHeaders = @{}
    if ($obj.actions.download.header) {
        foreach ($prop in $obj.actions.download.header.PSObject.Properties) { $dlHeaders[$prop.Name] = $prop.Value }
    }
    Invoke-WebRequest -Uri $href -OutFile $Dest -UseBasicParsing -Headers $dlHeaders
}
`;

/**
 * Emit a value as a PowerShell single-quoted string literal.
 * In single-quoted PS strings the only special character is the single quote
 * itself, which is escaped by doubling it. Everything else is literal.
 */
function psLit(value) {
  const s = value == null ? "" : String(value);
  return "'" + s.replace(/'/g, "''") + "'";
}

/**
 * Only allow https:// URLs into the script. Anything else (http, data:,
 * file:, javascript:, or junk) collapses to an empty string so the download
 * step fails loudly rather than fetching something unexpected.
 */
function safeHttpsUrl(url) {
  const s = String(url || "").trim();
  return /^https:\/\//i.test(s) ? s : "";
}

/** SHA256 hex is 64 chars; normalize/validate so a bad value can't slip in. */
function safeSha256(hash) {
  const s = String(hash || "").trim().toLowerCase();
  return /^[a-f0-9]{64}$/.test(s) ? s : "";
}

/**
 * Build the full .ps1 text.
 *
 * @param {object} driver  Drivers row: driver_id, display_name, provider,
 *                         category, version, arch, primary_url
 * @param {Array}  parts   DriverParts rows ordered by part_num:
 *                         { part_num, filename, url, sha256 }
 */
export function buildInstallScript(driver, parts) {
  const provider = driver.provider || "Unknown";
  const category = driver.category || "Driver";
  const version = driver.version || "0";
  const arch = driver.arch || "x64";
  const displayName = driver.display_name || driver.driver_id;

  // Normalize parts. Fall back to the single primary_url if no part rows exist
  // (single-volume packages still usually have one DriverParts row, but be safe).
  let normParts = (parts || [])
    .map((p) => ({
      part_num: p.part_num,
      filename: String(p.filename || "").trim(),
      url: safeHttpsUrl(p.url),
      sha256: safeSha256(p.sha256),
    }))
    .filter((p) => p.filename && p.url);

  if (normParts.length === 0 && safeHttpsUrl(driver.primary_url)) {
    const url = safeHttpsUrl(driver.primary_url);
    const filename = url.split("/").pop() || "driver.7z.001";
    normParts = [{ part_num: 1, filename, url, sha256: "" }];
  }

  const firstPart = normParts[0];
  const manifestHwids = ""; // hwids not needed in the manifest body for v1

  // ---- per-part download + verify block ----------------------------------
  const partBlocks = normParts
    .map((p) => {
      const verify = p.sha256
        ? [
            `    $expected = ${psLit(p.sha256)}`,
            `    $actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()`,
            `    if ($actual -ne $expected) { throw ("Checksum mismatch on " + ${psLit(
              p.filename
            )} + " (expected " + $expected + ", got " + $actual + ")") }`,
            `    Write-Host "    verified SHA256 OK" -ForegroundColor DarkGray`,
          ].join("\n")
        : `    Write-Host "    (no checksum on record — skipping verification)" -ForegroundColor Yellow`;

      return [
        `  Write-Host "Downloading part ${String(p.part_num).padStart(2, "0")}: " -NoNewline`,
        `  Write-Host ${psLit(p.filename)} -ForegroundColor Cyan`,
        `  $dest = Join-Path $scratch ${psLit(p.filename)}`,
        `  Get-DriverDexFile -Url ${psLit(p.url)} -Dest $dest`,
        verify,
      ].join("\n");
    })
    .join("\n\n");

  // ---- assemble the full script ------------------------------------------
  return `# ============================================================
#  DriverDex Driver Installer
#  ${displayName}
#  Provider: ${provider} | Category: ${category} | Version: ${version} | Arch: ${arch}
#
#  Generated by DriverDex — ${GITHUB_URL}
#  Author: ${AUTHOR}
#  This link was single-use and is now already dead — don't bother
#  re-running it from your shell history; generate a fresh one on the site.
# ============================================================

$ErrorActionPreference = "Stop"

${LFS_HELPER_PS}
$DriverDex_Name     = ${psLit(displayName)}
$DriverDex_Provider = ${psLit(provider)}
$DriverDex_Category = ${psLit(category)}
$DriverDex_Version  = ${psLit(version)}
$DriverDex_Arch     = ${psLit(arch)}
$DriverDex_DriverId = ${psLit(driver.driver_id)}

Write-Host ""
Write-Host "============================================================" -ForegroundColor DarkYellow
Write-Host "  DriverDex Driver Installer" -ForegroundColor Yellow
Write-Host "  $DriverDex_Name" -ForegroundColor White
Write-Host "  $DriverDex_Provider | $DriverDex_Category | v$DriverDex_Version | $DriverDex_Arch" -ForegroundColor Gray
Write-Host "  ${GITHUB_URL}  —  by ${AUTHOR}" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor DarkYellow
Write-Host ""

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}
$isAdmin = Test-Admin
if (-not $isAdmin) {
    Write-Host "Not running as Administrator — the silent driver-install step will be skipped." -ForegroundColor Yellow
    Write-Host "Re-open PowerShell as Administrator if you want this script to install the driver for you." -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "About to download: $DriverDex_Name ($DriverDex_Provider, v$DriverDex_Version, $DriverDex_Arch)"
$confirm = Read-Host "Continue? [Y/n]"
if ($confirm -match '^[Nn]') { Write-Host "Cancelled."; return }

$default = Join-Path $env:USERPROFILE ("Downloads\\DriverDex-Drivers\\" + $DriverDex_Provider + "-" + $DriverDex_Category + "-" + $DriverDex_Version)
$out = Read-Host "Output folder (press Enter for default: $default)"
if ([string]::IsNullOrWhiteSpace($out)) { $out = $default }

# Organize cleanly: <chosen>\<Provider>\<Category>\<Version>\
$out = Join-Path $out (Join-Path $DriverDex_Provider (Join-Path $DriverDex_Category $DriverDex_Version))
New-Item -ItemType Directory -Force -Path $out | Out-Null

$scratch = Join-Path $env:TEMP ("driverdex-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Force -Path $scratch | Out-Null

try {
    Write-Host ""
    Write-Host "Downloading extractor..." -ForegroundColor White
    Get-DriverDexFile -Url ${psLit(
      EXTRACTOR_URL
    )} -Dest (Join-Path $scratch "extractor.exe")

    Write-Host ""
${partBlocks}

    Write-Host ""
    Write-Host "Extracting..." -ForegroundColor White
    $firstPart = Join-Path $scratch ${psLit(firstPart ? firstPart.filename : "")}
    & (Join-Path $scratch "extractor.exe") --file $firstPart --output "$out" --verify
    if ($LASTEXITCODE -ne 0) {
        $logPath = Join-Path $out "extraction.log"
        if (Test-Path $logPath) {
            Write-Host "---- extraction.log (tail) ----" -ForegroundColor Red
            Get-Content $logPath -Tail 20 | ForEach-Object { Write-Host $_ -ForegroundColor DarkRed }
        }
        throw "Extraction failed (exit code $LASTEXITCODE)"
    }

    # Traceability manifest next to the extracted files.
    $manifest = [ordered]@{
        driver_id   = $DriverDex_DriverId
        display_name= $DriverDex_Name
        provider    = $DriverDex_Provider
        category    = $DriverDex_Category
        version     = $DriverDex_Version
        arch        = $DriverDex_Arch
        source_url  = ${psLit(firstPart ? firstPart.url : "")}
        installed_on= (Get-Date).ToString("o")
        generated_by= "DriverDex (${GITHUB_URL}) — by ${AUTHOR}"
    }
    $manifest | ConvertTo-Json | Set-Content -Path (Join-Path $out "manifest.json") -Encoding UTF8

    Write-Host ""
    $infs = @(Get-ChildItem -Path $out -Filter "*.inf" -Recurse -ErrorAction SilentlyContinue)
    $installer = Get-ChildItem -Path $out -Include "setup.exe","*.msi" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1

    $doInstall = Read-Host "Install this driver now? [Y/n]"
    if ($doInstall -notmatch '^[Nn]') {
        if ($infs.Count -gt 0 -and $isAdmin) {
            Write-Host "Installing $($infs.Count) INF package(s) via pnputil..." -ForegroundColor White
            pnputil /add-driver (Join-Path $out "*.inf") /subdirs /install
        } elseif ($infs.Count -gt 0 -and -not $isAdmin) {
            Write-Host "INF drivers found, but this session is not elevated — skipping silent install." -ForegroundColor Yellow
            Write-Host "Open Device Manager > Update driver > Browse my computer, and point it to:" 
            Write-Host "  $out" -ForegroundColor Cyan
        } elseif ($installer) {
            Write-Host "Found vendor installer: $($installer.Name)" -ForegroundColor White
            $runIt = Read-Host "Run it now? [Y/n]"
            if ($runIt -notmatch '^[Nn]') { Start-Process -FilePath $installer.FullName -Wait }
        } else {
            Write-Host "No INF or vendor installer was found in the package." -ForegroundColor Yellow
            Write-Host "Open Device Manager > Update driver > Browse my computer, and point it to:"
            Write-Host "  $out" -ForegroundColor Cyan
        }
    }

    Write-Host ""
    Write-Host "Done. Files are in:" -ForegroundColor Green
    Write-Host "  $out" -ForegroundColor Green
    Write-Host ""
    Write-Host "Driver installed via DriverDex (${GITHUB_URL}) — by ${AUTHOR}" -ForegroundColor DarkYellow
}
catch {
    Write-Host ""
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "Aborted. The output folder may contain partial files." -ForegroundColor Red
}
finally {
    Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
}
`;
}

/**
 * Normalize one driver's parts the same way buildInstallScript() does, so the
 * single- and bulk-install paths stay byte-for-byte consistent per package.
 */
function normalizeParts(driver, parts) {
  let normParts = (parts || [])
    .map((p) => ({
      part_num: p.part_num,
      filename: String(p.filename || "").trim(),
      url: safeHttpsUrl(p.url),
      sha256: safeSha256(p.sha256),
    }))
    .filter((p) => p.filename && p.url);

  if (normParts.length === 0 && safeHttpsUrl(driver.primary_url)) {
    const url = safeHttpsUrl(driver.primary_url);
    const filename = url.split("/").pop() || "driver.7z.001";
    normParts = [{ part_num: 1, filename, url, sha256: "" }];
  }
  return normParts;
}

/**
 * Build a single .ps1 that installs MANY drivers (the cart "Install all" flow).
 *
 * Emits a PowerShell `$packages` array of hashtables — one per driver, each with
 * its ordered parts (filename/url/sha256) — then loops: download every part
 * (SHA256-verified when on record), extract via the shared extractor.exe into a
 * clean <root>\Provider\Category\Version folder, drop a manifest, and collect
 * INF dirs. After all packages are staged it offers a single install pass
 * (silent pnputil per INF dir when elevated, else points at Device Manager).
 *
 * Same hardening as buildInstallScript(): every dynamic value is emitted via
 * psLit() (single-quoted, doubled quotes — no interpolation) and URLs are
 * https-only, so a hostile name/filename/url cannot inject PowerShell.
 *
 * @param {Array} items  [{ driver, parts }] — driver rows + their DriverParts.
 */
export function buildBulkInstallScript(items) {
  const pkgs = (items || [])
    .map(({ driver, parts }) => {
      const normParts = normalizeParts(driver, parts);
      if (normParts.length === 0) return null; // nothing downloadable — skip
      return {
        name: driver.display_name || driver.driver_id,
        provider: driver.provider || "Unknown",
        category: driver.category || "Driver",
        version: driver.version || "0",
        arch: driver.arch || "x64",
        driverId: driver.driver_id,
        parts: normParts,
      };
    })
    .filter(Boolean);

  // Each package becomes a PowerShell hashtable literal.
  const pkgLiterals = pkgs
    .map((p) => {
      const partLiterals = p.parts
        .map(
          (part) =>
            `      @{ File = ${psLit(part.filename)}; Url = ${psLit(
              part.url
            )}; Sha = ${psLit(part.sha256)} }`
        )
        .join(",\n");
      return [
        `  @{`,
        `    Name = ${psLit(p.name)}; Provider = ${psLit(p.provider)}; Category = ${psLit(
          p.category
        )};`,
        `    Version = ${psLit(p.version)}; Arch = ${psLit(p.arch)}; DriverId = ${psLit(
          p.driverId
        )};`,
        `    First = ${psLit(p.parts[0].filename)};`,
        `    Parts = @(`,
        partLiterals,
        `    )`,
        `  }`,
      ].join("\n");
    })
    .join(",\n");

  const count = pkgs.length;

  return `# ============================================================
#  DriverDex Bulk Driver Installer
#  ${count} driver package(s)
#
#  Generated by DriverDex — ${GITHUB_URL}
#  Author: ${AUTHOR}
#  This link was single-use and is now already dead — generate a fresh one
#  from the site if you need to run it again.
# ============================================================

$ErrorActionPreference = "Stop"

${LFS_HELPER_PS}
Write-Host ""
Write-Host "============================================================" -ForegroundColor DarkYellow
Write-Host "  DriverDex Bulk Driver Installer" -ForegroundColor Yellow
Write-Host "  ${count} package(s) selected" -ForegroundColor White
Write-Host "  ${GITHUB_URL}  —  by ${AUTHOR}" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor DarkYellow
Write-Host ""

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}
$isAdmin = Test-Admin
if (-not $isAdmin) {
    Write-Host "Not running as Administrator — silent driver installs will be skipped." -ForegroundColor Yellow
    Write-Host "Re-open PowerShell as Administrator to let this script install for you." -ForegroundColor Yellow
    Write-Host ""
}

$packages = @(
${pkgLiterals}
)

Write-Host ("About to download " + $packages.Count + " driver package(s).")
$confirm = Read-Host "Continue? [Y/n]"
if ($confirm -match '^[Nn]') { Write-Host "Cancelled."; return }

$defaultRoot = Join-Path $env:USERPROFILE "Downloads\\DriverDex-Drivers"
$root = Read-Host "Output folder (press Enter for default: $defaultRoot)"
if ([string]::IsNullOrWhiteSpace($root)) { $root = $defaultRoot }
New-Item -ItemType Directory -Force -Path $root | Out-Null

$scratch = Join-Path $env:TEMP ("driverdex-bulk-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Force -Path $scratch | Out-Null

$installRoots = New-Object System.Collections.ArrayList
$failed = New-Object System.Collections.ArrayList

try {
    Write-Host ""
        Write-Host "Downloading extractor..." -ForegroundColor White
    $extractor = Join-Path $scratch "extractor.exe"
    Get-DriverDexFile -Url ${psLit(EXTRACTOR_URL)} -Dest $extractor

    $idx = 0
    foreach ($pkg in $packages) {
        $idx++
        Write-Host ""
        Write-Host ("[" + $idx + "/" + $packages.Count + "] " + $pkg.Name) -ForegroundColor Yellow
        Write-Host ("    " + $pkg.Provider + " | " + $pkg.Category + " | v" + $pkg.Version + " | " + $pkg.Arch) -ForegroundColor Gray

        try {
            $out = Join-Path $root (Join-Path $pkg.Provider (Join-Path $pkg.Category $pkg.Version))
            New-Item -ItemType Directory -Force -Path $out | Out-Null
            $pkgScratch = Join-Path $scratch ([guid]::NewGuid().ToString("N").Substring(0,8))
            New-Item -ItemType Directory -Force -Path $pkgScratch | Out-Null

            foreach ($part in $pkg.Parts) {
                Write-Host ("    downloading " + $part.File) -ForegroundColor Cyan
                $dest = Join-Path $pkgScratch $part.File
                Get-DriverDexFile -Url $part.Url -Dest $dest
                if ($part.Sha) {
                    $actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
                    if ($actual -ne $part.Sha) {
                        throw ("Checksum mismatch on " + $part.File + " (expected " + $part.Sha + ", got " + $actual + ")")
                    }
                    Write-Host "    verified SHA256 OK" -ForegroundColor DarkGray
                } else {
                    Write-Host "    (no checksum on record — skipping verification)" -ForegroundColor Yellow
                }
            }

            Write-Host "    extracting..." -ForegroundColor White
            $firstPart = Join-Path $pkgScratch $pkg.First
            & $extractor --file $firstPart --output "$out" --verify
            if ($LASTEXITCODE -ne 0) { throw ("Extraction failed (exit code " + $LASTEXITCODE + ")") }

            $manifest = [ordered]@{
                driver_id   = $pkg.DriverId
                display_name= $pkg.Name
                provider    = $pkg.Provider
                category    = $pkg.Category
                version     = $pkg.Version
                arch        = $pkg.Arch
                installed_on= (Get-Date).ToString("o")
                generated_by= "DriverDex (${GITHUB_URL}) — by ${AUTHOR}"
            }
            $manifest | ConvertTo-Json | Set-Content -Path (Join-Path $out "manifest.json") -Encoding UTF8

            [void]$installRoots.Add([pscustomobject]@{ Name = $pkg.Name; Path = $out })
            Write-Host "    staged." -ForegroundColor Green
        }
        catch {
            Write-Host ("    FAILED: " + $_.Exception.Message) -ForegroundColor Red
            [void]$failed.Add($pkg.Name)
        }
        finally {
            Remove-Item -Recurse -Force $pkgScratch -ErrorAction SilentlyContinue
        }
    }

    Write-Host ""
    Write-Host ("Staged " + $installRoots.Count + " of " + $packages.Count + " package(s) under:") -ForegroundColor Green
    Write-Host "  $root" -ForegroundColor Green
    if ($failed.Count -gt 0) {
        Write-Host ("Failed: " + ($failed -join ", ")) -ForegroundColor Red
    }

    if ($installRoots.Count -gt 0) {
        Write-Host ""
        $doInstall = Read-Host "Install all staged driver package(s) now? [Y/n]"
        if ($doInstall -notmatch '^[Nn]') {
            foreach ($entry in $installRoots) {
                $infs = @(Get-ChildItem -Path $entry.Path -Filter "*.inf" -Recurse -ErrorAction SilentlyContinue)
                if ($infs.Count -gt 0 -and $isAdmin) {
                    Write-Host ("Installing " + $entry.Name + " (" + $infs.Count + " INF package(s))...") -ForegroundColor White
                    pnputil /add-driver (Join-Path $entry.Path "*.inf") /subdirs /install
                } elseif ($infs.Count -gt 0) {
                    Write-Host ("INF drivers for " + $entry.Name + " found, but not elevated — point Device Manager at:") -ForegroundColor Yellow
                    Write-Host ("  " + $entry.Path) -ForegroundColor Cyan
                } else {
                    $installer = Get-ChildItem -Path $entry.Path -Include "setup.exe","*.msi" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($installer) {
                        Write-Host ("Vendor installer for " + $entry.Name + ": " + $installer.Name) -ForegroundColor White
                        $runIt = Read-Host "Run it now? [Y/n]"
                        if ($runIt -notmatch '^[Nn]') { Start-Process -FilePath $installer.FullName -Wait }
                    } else {
                        Write-Host ("No INF or installer found for " + $entry.Name + " — files are in:") -ForegroundColor Yellow
                        Write-Host ("  " + $entry.Path) -ForegroundColor Cyan
                    }
                }
            }
        }
    }

    Write-Host ""
    Write-Host "Done. Drivers installed via DriverDex (${GITHUB_URL}) — by ${AUTHOR}" -ForegroundColor DarkYellow
}
catch {
    Write-Host ""
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "Aborted. The output folder may contain partial files." -ForegroundColor Red
}
finally {
    Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
}
`;
}

/** PS1 returned when a token is missing, expired, or already used. */
export function expiredScript() {
  return `Write-Host ""
Write-Host "This DriverDex install link has expired or was already used." -ForegroundColor Yellow
Write-Host "Install links are single-use and live for 5 minutes." -ForegroundColor Yellow
Write-Host "Go back to the DriverDex site, find your driver, and click 'Generate Install Script' again." -ForegroundColor Yellow
Write-Host ""
Write-Host "DriverDex — ${GITHUB_URL} — by ${AUTHOR}" -ForegroundColor DarkGray
`;
}
