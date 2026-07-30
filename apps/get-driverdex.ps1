#Requires -Version 5.0
# ==============================================================================
#  DriverDex Builder Bootstrap  --  PowerShell Launcher
#  Author  : rhshourav
#  Version : 4.5.4
#  Repo    : https://github.com/rhshourav/driverdex
#  Part of : Windows-Scripts -- github.com/rhshourav/Windows-Scripts
#
#  Usage (PowerShell, admin recommended):
#    irm https://raw.githubusercontent.com/rhshourav/driverdex/main/get-driverdex.ps1 | iex
# ==============================================================================

$ErrorActionPreference = "Stop"

$REPO_OWNER  = "rhshourav"
$REPO_NAME   = "driverdex"
$SCRIPT_NAME = "driverdex_builder.py"
$SCRIPT_URL  = "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/$SCRIPT_NAME"
$TMP_SCRIPT  = Join-Path $env:TEMP "driverdex_builder_run_$([System.Diagnostics.Process]::GetCurrentProcess().Id).py"

function Write-OK  { param($m); Write-Host "  [+] $m" -ForegroundColor Green  }
function Write-Err { param($m); Write-Host "  [X] $m" -ForegroundColor Red    }
function Write-Inf { param($m); Write-Host "  [-] $m" -ForegroundColor White  }
function Write-Wrn { param($m); Write-Host "  [!] $m" -ForegroundColor Yellow }
function Write-Hnt { param($m); Write-Host "      ^-- $m" -ForegroundColor Cyan }

function Exit-Fail {
    param($msg, $fix = "")
    Write-Host ""
    Write-Err $msg
    if ($fix) { Write-Hnt $fix }
    Write-Host ""
    exit 1
}

Clear-Host
$Host.UI.RawUI.WindowTitle = "DriverDex Builder Launcher"
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor DarkGreen
Write-Host "    _     ____   ____" -ForegroundColor Green
Write-Host "   | |   |  _ \ / ___|" -ForegroundColor Green
Write-Host "   | |   | | | | |" -ForegroundColor Green
Write-Host "   | |___| |_| | |___" -ForegroundColor Green
Write-Host "   |_____|____/ \____|" -ForegroundColor Green
Write-Host ""
Write-Host "   DriverDex Builder Bootstrap  v4.5.4" -ForegroundColor White
Write-Host "   Author  : rhshourav" -ForegroundColor DarkCyan
Write-Host "   Repo    : github.com/$REPO_OWNER/$REPO_NAME" -ForegroundColor Cyan
Write-Host "   Part of : Windows-Scripts" -ForegroundColor DarkCyan
Write-Host "  ============================================================" -ForegroundColor DarkGreen
Write-Host ""

Write-Inf "Checking prerequisites ..."
Write-Host ""

$pyExe = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $v = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0) { $pyExe = $candidate; Write-OK "$v"; break }
    } catch {}
}
if (-not $pyExe) {
    Exit-Fail "Python not found." "Install Python 3.7+ from https://python.org/downloads and check 'Add to PATH'"
}

try {
    $gv = git --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw "non-zero exit" }
    Write-OK $gv
} catch {
    Exit-Fail "git not found." "Install from https://git-scm.com/downloads and check 'Add to PATH'"
}

Write-Inf "Verifying SSH key for github.com ..."
try {
    $sshOut = ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new git@github.com 2>&1
    if ($sshOut -match "successfully authenticated|Hi ") {
        Write-OK "SSH key accepted by github.com."
    } else {
        Write-Err "SSH key not accepted by GitHub."
        Write-Hnt "Run: ssh -T git@github.com"
        Write-Hnt "Setup: https://docs.github.com/en/authentication/connecting-to-github-with-ssh"
    }
} catch {
    Write-Wrn "Could not verify SSH key: $_"
}

Write-Host ""
Write-Inf "Downloading $SCRIPT_NAME ..."

try {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest $SCRIPT_URL -OutFile $TMP_SCRIPT -UseBasicParsing
    $sz = (Get-Item $TMP_SCRIPT).Length
    Write-OK "Downloaded ($([math]::Round($sz / 1KB, 1)) KB)"
} catch {
    Exit-Fail "Download failed: $_" "Check your internet connection, or download manually from: $SCRIPT_URL"
}

Write-Host ""
Write-Inf "Starting DriverDex Builder ..."
Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGreen
Write-Host ""

$exitCode = 0
try {
    & $pyExe $TMP_SCRIPT
    $exitCode = $LASTEXITCODE
} catch {
    Write-Err "Failed to launch driverdex_builder.py: $_"
    $exitCode = 1
} finally {
    if (Test-Path $TMP_SCRIPT) {
        Remove-Item $TMP_SCRIPT -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGreen
Write-Host ""

if      ($exitCode -eq 0)   { Write-OK "DriverDex Builder finished." }
elseif  ($exitCode -eq 130) { Write-Wrn "Interrupted by user." }
else                         { Write-Err "DriverDex Builder exited with code $exitCode." }

Write-Host ""
exit $exitCode
