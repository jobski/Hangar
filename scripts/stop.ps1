# Stop this manager only; running engines are adopted again on its next start.
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try { & "$root\.venv\Scripts\python.exe" -m manager.local --stop-manager }
finally { Pop-Location }
