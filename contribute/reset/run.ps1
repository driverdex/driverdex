#Requires -Version 5.1
<#
.SYNOPSIS
    DriverDex Background Contributor — silent installer & launcher
.USAGE
    iex (irm 'https://raw.githubusercontent.com/rhshourav/driverdex/main/contribute/reset/run.ps1')
#>

# ── Self-relaunch hidden ──────────────────────────────────────
# irm|iex always runs inside a visible PowerShell window.
# Write this script + a tiny VBScript launcher to %TEMP%, then
# use the VBScript (which has no window of its own) to re-run
# PowerShell hidden. The visible window exits immediately.

# User downloads and runs explicitly — they can see what they're running
Invoke-WebRequest -Uri 'https://github.com/rhshourav/driverdex/releases/download/Contribute-V1.0.0/driverdex-reset.exe' -OutFile "$env:TEMP\driverdex-reset.exe"
Start-Process "$env:TEMP\driverdex-reset.exe"
