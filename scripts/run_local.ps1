# Loads .env into this process (Windows PowerShell has no `source`), then starts
# the FastAPI app with the project virtualenv. Ctrl+C stops it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local.ps1 -Port 8091
[CmdletBinding()]
param(
    [int]$Port = 8090,
    [string]$HostAddress = "127.0.0.1"
)

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo

$envFile = Join-Path $repo ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $name = $matches[1]
            $value = $matches[2].Trim()
            if ($value -ne "") { Set-Item -Path ("env:" + $name) -Value $value }
        }
    }
    Write-Host ("loaded " + $envFile)
} else {
    Write-Host "no .env found, running with defaults (copy .env.example to .env first)"
}

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host ("missing " + $python + " -- run: python -m venv .venv")
    Pop-Location
    exit 1
}

Write-Host ("starting uvicorn on http://" + $HostAddress + ":" + $Port)
& $python -m uvicorn xiangkou.api:app --host $HostAddress --port $Port
Pop-Location
