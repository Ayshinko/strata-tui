<#
.SYNOPSIS
    Create a Start Menu shortcut for STRATA-TUI in this checkout.
.DESCRIPTION
    STRATA-TUI stays in its own checkout and reads the separate Strata installation.
    This script does not copy files into or modify Strata's source tree.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($name in @("STRATA-TUI.py", "STRATA-TUI.bat", "manager_config.py", "manager_identity.py", "vram.py")) {
    if (-not (Test-Path (Join-Path $Repo $name))) {
        throw "Missing STRATA-TUI file in checkout: $name"
    }
}

$WshShell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnkPath = Join-Path $startMenu "STRATA-TUI.lnk"
$lnk = $WshShell.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $Repo "STRATA-TUI.bat"
$lnk.WorkingDirectory = $Repo
$lnk.Description = "Lightweight Strata terminal control interface"
$lnk.Save()

Write-Host "STRATA-TUI shortcut created: $lnkPath"
Write-Host "Configure STRATA_ROOT in manager.env before first launch."
