<#
.SYNOPSIS
    Update STRATA-TUI in place from this repository (the launcher only).

.DESCRIPTION
    1. git pull the latest strata-tui repository
    2. validate the new STRATA-TUI.py (py -3 -m py_compile)
    3. back up the current runtime files in the Strata checkout
    4. only after successful validation copy the new files over them
    5. if validation or the copy fails, restore the previous working runtime

    Nothing else is ever touched: setup.py, serve/, tools/, models, packs and
    configs stay exactly as they are.  The Strata checkout is detected
    automatically (STRATA_ROOT, a sibling "strata" folder, or a prompt) -
    nothing is hard-coded.

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
    Write-Host "[update] No Strata checkout at $StrataDir (setup.py not found)." -ForegroundColor Red
    exit 1
}

function Write-Step($msg) {
    Write-Host ""
    Write-Host "[update] $msg"
}

try {
    Write-Step "1. pulling the latest strata-tui repository"
    Push-Location $Repo
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "git pull failed (exit $LASTEXITCODE)"
    }
    Pop-Location

    $newPy = Join-Path $Repo "STRATA-TUI.py"
    $newBat = Join-Path $Repo "STRATA-TUI.bat"
    foreach ($f in @($newPy, $newBat)) {
        if (-not (Test-Path $f)) {
            throw "Missing file in the repository: $f"
        }
    }

    Write-Step "2. validating the new STRATA-TUI.py"
    py -3 -m py_compile $newPy
    if ($LASTEXITCODE -ne 0) {
        throw "py -3 -m py_compile failed (exit $LASTEXITCODE)"
    }

    Write-Step "3. backing up the current runtime"
    $rtPy = Join-Path $StrataDir "STRATA-TUI.py"
    $rtBat = Join-Path $StrataDir "STRATA-TUI.bat"
    foreach ($f in @($rtPy, $rtBat)) {
        if (Test-Path $f) {
            Copy-Item -LiteralPath $f -Destination "$f.bak" -Force
        }
    }

    Write-Step "4. installing the validated launcher"
    Copy-Item -LiteralPath $newPy -Destination $rtPy -Force
    Copy-Item -LiteralPath $newBat -Destination $rtBat -Force

    Write-Host ""
    Write-Host "[update] OK - STRATA-TUI is up to date."
}
catch {
    Write-Host ""
    Write-Host "[update] FAILED: $($_.Exception.Message)" -ForegroundColor Red

    # 5. restore the previous working runtime
    $rtPy = Join-Path $StrataDir "STRATA-TUI.py"
    $rtBat = Join-Path $StrataDir "STRATA-TUI.bat"
    foreach ($f in @($rtPy, $rtBat)) {
        if ((Test-Path "$f.bak") -and (Test-Path $f)) {
            Copy-Item -LiteralPath "$f.bak" -Destination $f -Force
            Write-Host "[update] restored $f from backup" -ForegroundColor Yellow
        }
    }
    exit 1
}