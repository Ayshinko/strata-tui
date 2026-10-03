<#
.SYNOPSIS
    Install STRATA-TUI into the Strata checkout and create the Start Menu
    shortcut "Strata".

.DESCRIPTION
    Copies exactly two files into the Strata checkout:

        STRATA-TUI.py
        STRATA-TUI.bat

    It never modifies tracked Strata code (setup.py, serve/, tools/, ...).
    The Start Menu shortcut points at the runtime STRATA-TUI.bat, so it keeps
    working after every future TUI update.
#>
[CmdletBinding()]
param(
    [string]$StrataDir = "C:\Users\matri\strata"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path

$src = @(
    Join-Path $Repo "STRATA-TUI.py",
    Join-Path $Repo "STRATA-TUI.bat"
)

if (-not (Test-Path $StrataDir)) {
    throw "Strata checkout not found: $StrataDir"
}

foreach ($f in $src) {
    if (-not (Test-Path $f)) {
        throw "Missing file in the repository: $f"
    }
}

Write-Host "Installing STRATA-TUI into $StrataDir ..."
foreach ($f in $src) {
    Copy-Item -LiteralPath $f -Destination (Join-Path $StrataDir (Split-Path -Leaf $f)) -Force
}

# --- Start Menu shortcut "Strata" -> runtime STRATA-TUI.bat -----------------
$WshShell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnkPath = Join-Path $startMenu "Strata.lnk"
$lnk = $WshShell.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $StrataDir "STRATA-TUI.bat"
$lnk.WorkingDirectory = $StrataDir
$lnk.Description = "Strata terminal launcher (optional, minimal)"
$lnk.Save()

Write-Host "Done."
Write-Host "  files : $StrataDir\STRATA-TUI.py, $StrataDir\STRATA-TUI.bat"
Write-Host "  menu  : $lnkPath"