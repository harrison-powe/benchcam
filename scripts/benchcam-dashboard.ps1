# ---------------------------------------------------------------------------
# BenchCam dashboard launcher (PowerShell).
# Activates the project's .venv and runs `benchcam dashboard`, which opens the
# dashboard in your default browser. Keep this window open while you work.
#
# If PowerShell blocks the script, allow it once for your user:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# ---------------------------------------------------------------------------
$ErrorActionPreference = "Stop"

# Repo root is the parent of this script's "scripts\" folder.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$activate = Join-Path $repo ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Host "Could not find .venv in $repo."
    Write-Host "Create it first:  py -3 -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -e ."
    Read-Host "Press Enter to close"
    exit 1
}

& $activate
benchcam dashboard
