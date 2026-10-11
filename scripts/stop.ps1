# Stop the manager. The engines it runs keep running; it picks them up again when it starts.
$c = Get-NetTCPConnection -LocalPort 9000 -State Listen -ErrorAction SilentlyContinue
if (-not $c) { Write-Host 'The manager is not running.'; exit 0 }
Stop-Process -Id $c[0].OwningProcess -Force
Write-Host 'Hangar stopped (engines left running).'
