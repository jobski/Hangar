# Remove Hangar from Windows:
#
#   & "$HOME\hangar\uninstall.ps1"            (add -Yes to skip the question, -Purge to keep no backup)
#   irm https://raw.githubusercontent.com/jobski/hangar/main/uninstall.ps1 | iex      (asks; HANGAR_PURGE=1 for -Purge)
#
# Stops the engines it runs and the manager, removes start-with-Windows and the PATH entry, and deletes the install
# folder. Your config.yaml, profiles.yaml and data\ (token, stats) are first copied to $HOME\hangar-backup-<date>;
# -Purge skips that. Model files (models.dir) and engine folders are never touched.
param([switch]$Yes, [switch]$Purge)
& {
    $ErrorActionPreference = 'Stop'
    $dir = if ($env:HANGAR_DIR) { $env:HANGAR_DIR } else { Join-Path $HOME 'hangar' }
    $port = if ($env:HANGAR_PORT) { [int]$env:HANGAR_PORT } else { 9000 }
    $purge = $Purge -or $env:HANGAR_PURGE
    function Say($m) { Write-Host "==> $m" -ForegroundColor Green }

    if (-not (Test-Path (Join-Path $dir 'manager\__init__.py'))) { throw "No Hangar install in $dir (set HANGAR_DIR)" }
    $dir = (Resolve-Path $dir).Path
    if (-not ($Yes -or $env:HANGAR_YES)) {
        $a = Read-Host "Remove Hangar from $dir$(if ($purge) { ' including its config and stats' })? [y/N]"
        if ($a -notmatch '^(y|yes)$') { Write-Host 'Nothing removed.'; return }
    }

    # only what belongs to this install: another Hangar (a second copy, or a different port) is left alone
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($c.OwningProcess)"
        if ($proc -and ("$($proc.ExecutablePath) $($proc.CommandLine)" -like "*$dir\*")) {
            Say 'Stopping the engines and the manager'
            & (Join-Path $dir 'hangar.cmd') stop all *> $null
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            Start-Sleep 1
        }
    }

    Say 'Removing start with Windows and the PATH entry'
    $startup = [Environment]::GetFolderPath('Startup')
    $ws = New-Object -ComObject WScript.Shell
    foreach ($name in 'Hangar.lnk', 'LLM Manager.lnk') {
        $lnk = Join-Path $startup $name
        if ((Test-Path $lnk) -and ($ws.CreateShortcut($lnk).Arguments -like "*$dir\*")) { Remove-Item $lnk }
    }
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($user -and (($user -split ';') -contains $dir)) {
        [Environment]::SetEnvironmentVariable('Path', (($user -split ';' | Where-Object { $_ -and $_ -ne $dir }) -join ';'), 'User')
    }

    if (-not $purge) {
        $bk = Join-Path $HOME ("hangar-backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        New-Item -ItemType Directory -Force $bk | Out-Null
        foreach ($f in 'config.yaml', 'profiles.yaml', 'data') {
            $p = Join-Path $dir $f
            if (Test-Path $p) { Copy-Item $p $bk -Recurse }
        }
        Say "Kept your settings and stats in $bk"
    }
    Set-Location $HOME
    Remove-Item $dir -Recurse -Force
    Say 'Hangar removed. Model files and engine folders were left in place.'
}
