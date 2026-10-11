# Install (or update) Hangar on Windows, from PowerShell:
#
#   irm https://raw.githubusercontent.com/jobski/hangar/main/install.ps1 | iex
#
# A private fork works too: set $env:GITHUB_TOKEN to a token that can read it (and HANGAR_REPO to the fork).
#
# Settings (environment variables, set before running):
#   HANGAR_DIR=$HOME\hangar   where it goes          HANGAR_REF=main         branch or tag to install
#   HANGAR_NO_START=1         do not start it         HANGAR_NO_AUTOSTART=1   do not start it with Windows
#   HANGAR_NO_PATH=1          do not put `hangar` on your PATH
# Running it again updates an existing install in place; config.yaml, profiles and stats are kept.
& {
    $ErrorActionPreference = 'Stop'
    $repo = if ($env:HANGAR_REPO) { $env:HANGAR_REPO } else { 'jobski/hangar' }
    $ref = if ($env:HANGAR_REF) { $env:HANGAR_REF } else { 'main' }
    $dir = if ($env:HANGAR_DIR) { $env:HANGAR_DIR } else { Join-Path $HOME 'hangar' }
    $port = if ($env:HANGAR_PORT) { $env:HANGAR_PORT } else { 9000 }
    function Say($m) { Write-Host "==> $m" -ForegroundColor Green }
    function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }

    # ---- what we need
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git is missing. Install it with: winget install Git.Git (then open a new PowerShell)' }
    $py = $null
    foreach ($c in @(@('py', '-3'), @('python'))) {
        if (Get-Command $c[0] -ErrorAction SilentlyContinue) {
            $args0 = @($c | Select-Object -Skip 1)
            & $c[0] @args0 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>$null
            if ($LASTEXITCODE -eq 0) { $py = $c; break }
        }
    }
    if (-not $py) { throw 'Python 3.10 or newer is needed. Install it with: winget install Python.Python.3.12 (then open a new PowerShell)' }

    # ---- get the code (a token in GITHUB_TOKEN reaches a private repo; it is not stored in the clone)
    $auth = @()
    if ($env:GITHUB_TOKEN) {
        $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$($env:GITHUB_TOKEN)"))
        $auth = @('-c', "http.extraHeader=Authorization: Basic $b64")
    }
    # the Hangar on the port is this install when it accepts this install's token (each install makes its own);
    # the listening process is the base Python, not the venv's, so its command line cannot tell
    function Ours {
        $tf = Join-Path $dir 'data\token.txt'
        if (-not (Test-Path $tf)) { return $false }
        $t = (Get-Content $tf | Select-Object -First 1).Trim()
        try { Invoke-WebRequest "http://127.0.0.1:$port/api/jobs?limit=1" -Headers @{Authorization = "Bearer $t"} -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
    }
    $restart = $false
    if (Test-Path (Join-Path $dir '.git')) {
        Say "Updating $dir to $ref"
        $restart = Ours
        git @auth -C $dir fetch --quiet origin $ref
        git -C $dir checkout --quiet $ref 2>$null
        if ($LASTEXITCODE -ne 0) { git -C $dir checkout --quiet -B $ref "origin/$ref" }
        git -C $dir merge --quiet --ff-only "origin/$ref"
        if ($LASTEXITCODE -ne 0) { Warn "local changes in $dir; kept them and did not update" }
    } elseif (Test-Path $dir) {
        throw "$dir exists and is not a Hangar checkout; set HANGAR_DIR to install somewhere else"
    } else {
        Say "Downloading Hangar ($ref) into $dir"
        git @auth clone --quiet --branch $ref "https://github.com/$repo.git" $dir
        if ($LASTEXITCODE -ne 0) { throw "could not clone $repo (private repo? set GITHUB_TOKEN)" }
    }

    # ---- Python environment, config.yaml, token
    $env:HANGAR_PYTHON = ($py -join ' ')
    & (Join-Path $dir 'scripts\setup.ps1')

    # ---- `hangar` on the PATH (hangar.cmd is in the install folder)
    if (-not $env:HANGAR_NO_PATH) {
        $user = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not (($user -split ';') -contains $dir)) {
            [Environment]::SetEnvironmentVariable('Path', (($user.TrimEnd(';'), $dir) -join ';').TrimStart(';'), 'User')
            Warn 'Open a new terminal to use the hangar command'
        }
        $env:Path = "$env:Path;$dir"
    }

    # ---- start it, and with Windows
    if ($restart) {
        Say 'Restarting Hangar to run the update (engines keep running)'
        $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
        for ($i = 0; $i -lt 15 -and (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
    }
    if (-not $env:HANGAR_NO_START -or $restart) {
        & (Join-Path $dir 'scripts\start.ps1')
        for ($i = 0; $i -lt 30; $i++) {
            try { Invoke-WebRequest "http://127.0.0.1:$port/api/ping" -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
        }
        if (-not $env:HANGAR_NO_AUTOSTART) {
            & (Join-Path $dir 'hangar.cmd') autostart on
            if ($LASTEXITCODE -ne 0) { Warn 'could not turn on start with Windows; try: hangar autostart on' }
        }
    }

    $token = (Get-Content (Join-Path $dir 'data\token.txt') -ErrorAction SilentlyContinue | Select-Object -First 1)
    Say "Hangar is installed in $dir"
    Write-Host "    Dashboard: http://localhost:$port/?token=$token"
    Write-Host "    Settings:  $dir\config.yaml   (apply with: scripts\stop.ps1; scripts\start.ps1)"
    Write-Host "    Remove:    & '$dir\uninstall.ps1'"
}
