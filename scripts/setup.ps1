# First-time setup: Python environment, config.yaml from the example, and the API token.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .venv\Scripts\python.exe)) {
    Write-Host 'Creating the Python environment (.venv) ...'
    py -3 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip -q
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -q

if (-not (Test-Path config.yaml)) {
    Copy-Item config.example.yaml config.yaml
    Write-Host 'Made config.yaml from config.example.yaml - check the paths in it.'
}
.\.venv\Scripts\python.exe -c "from manager import config; c = config.load(); print('Token:', config.token(c))"
Write-Host 'Done. Start it with scripts\start.ps1, or install-autostart.ps1 to run it at logon.'
