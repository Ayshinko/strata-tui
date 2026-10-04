<#
.SYNOPSIS
    Install STRATA-TUI into a Strata checkout and create the Start Menu shortcut "Strata".

.DESCRIPTION
    Copies exactly two files into the Strata checkout:

        STRATA-TUI.py
        STRATA-TUI.bat

    It never modifies tracked Strata code (setup.py, serve/, tools/, ...).
    The single-file STRATA-TUI.py is self-contained: the custom variant (e.g.
    abliterated) logic is inlined, so no companion files are needed.
    The Start Menu shortcut points at the runtime STRATA-TUI.bat, so it keeps
    working after every future TUI update.

    The Strata checkout is found automatically (STRATA_ROOT, a "strata" folder
    next to this repository, or a prompt) - nothing is hard-coded.

.PARAMETER StrataDir
    A Strata checkout (must contain setup.py).  When omitted, detected in this
    order: $env:STRATA_ROOT, a sibling "strata" folder of this repository,
    then an interactive prompt.
#>
[CmdletBinding()]
param(
    [string]$StrataDir = ""
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path

if ([string]::IsNullOrWhiteSpace($StrataDir)) {
    if ($env:STRATA_ROOT -and (Test-Path (Join-Path $env:STRATA_ROOT "setup.py"))) {
        $StrataDir = $env:STRATA_ROOT
    }
    elseif (Test-Path (Join-Path (Split-Path $Repo -Parent) "strata")) {
        $StrataDir = Join-Path (Split-Path $Repo -Parent) "strata"
    }
    else {
        $StrataDir = Read-Host "Strata checkout path (the folder with setup.py)"
    }
}
$StrataDir = $StrataDir.TrimEnd('\', '/')
if (-not (Test-Path (Join-Path $StrataDir "setup.py"))) {
    throw "No Strata checkout at $StrataDir (setup.py not found)."
}

$src = @(
    Join-Path $Repo "STRATA-TUI.py",
    Join-Path $Repo "STRATA-TUI.bat"
)
foreach ($f in $src) {
    if (-not (Test-Path $f)) {
        throw "Missing file in the repository: $f"
    }
}

Write-Host "Installing STRATA-TUI into $StrataDir ..."
foreach ($f in $src) {
    $dst = Join-Path $StrataDir (Split-Path -Leaf $f)
    if (Test-Path $dst) { Copy-Item -LiteralPath $dst -Destination "$dst.bak" -Force }
    Copy-Item -LiteralPath $f -Destination $dst -Force
}

# --- Start Menu shortcut "Strata" -> runtime STRATA-TUI.bat -----------------
$WshShell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnkPath = Join-Path $startMenu "Strata.lnk"
$lnk = $WshShell.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $StrataDir "STRATA-TUI.bat"
$lnk.WorkingDirectory = $StrataDir
$lnk.Description = "Strata terminal launcher (optional, minimal, single-file)"
$lnk.Save()

Write-Host "Done."
Write-Host "  files : $StrataDir\STRATA-TUI.py, $StrataDir\STRATA-TUI.bat"
Write-Host "  menu  : $lnkPath"