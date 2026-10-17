# Run the manager in the background (no window). Logs go to logs\manager.log.
# -Foreground runs it in this window instead (Ctrl+C stops it).
param([switch]$Foreground)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force logs | Out-Null
$port = & "$root\.venv\Scripts\python.exe" -m manager.local --port
if ($LASTEXITCODE -ne 0) { throw "Cannot read the manager port" }

$running = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($running) { Write-Host "Something already listens on port $port (pid $($running[0].OwningProcess))."; exit 0 }

if ($Foreground) {
    & .\.venv\Scripts\python.exe -m manager serve
} else {
    Start-Process -FilePath "$root\.venv\Scripts\pythonw.exe" -ArgumentList '-m', 'manager', 'serve' `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput "$root\logs\manager.log" -RedirectStandardError "$root\logs\manager.err.log"
    Write-Host "Hangar started: http://localhost:$port"
}
