# Start the manager when you log on to Windows (a shortcut in your Startup folder; no admin needed).
# -Remove takes it out again.
param([switch]$Remove)
$root = Split-Path -Parent $PSScriptRoot
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'Hangar.lnk'
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'LLM Manager.lnk') -ErrorAction SilentlyContinue   # the name before the rename

if ($Remove) {
    Remove-Item $link -ErrorAction SilentlyContinue
    Write-Host 'Autostart removed.'
    exit 0
}
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($link)
$s.TargetPath = 'powershell.exe'
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\start.ps1`""
$s.WorkingDirectory = $root
$s.WindowStyle = 7
$s.Description = 'Hangar'
$s.Save()
Write-Host "Autostart installed: $link"
