<#
.SYNOPSIS
    Safely update STRATA-TUI in its Git checkout.
.DESCRIPTION
    Fast-forward pulls this repository, validates the TUI and its helper modules,
    and leaves local configuration (manager.env, manager-models.json) untouched.
    No Strata source or installed files are modified.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $Repo
try {
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only failed (exit $LASTEXITCODE)" }

    $Python = "python"
    if ($env:STRATA_ROOT -and (Test-Path (Join-Path $env:STRATA_ROOT ".venv\Scripts\python.exe"))) {
        $Python = Join-Path $env:STRATA_ROOT ".venv\Scripts\python.exe"
    }
    elseif (Test-Path (Join-Path $Repo ".venv\Scripts\python.exe")) {
        $Python = Join-Path $Repo ".venv\Scripts\python.exe"
    }
    & $Python -m py_compile (Join-Path $Repo "STRATA-TUI.py") (Join-Path $Repo "manager_identity.py")
    if ($LASTEXITCODE -ne 0) { throw "Python validation failed (exit $LASTEXITCODE)" }

    Write-Host "STRATA-TUI updated and validated. Local configuration was left untouched."
}
finally {
    Pop-Location
}
