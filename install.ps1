# Install (or update) Hangar on Windows, from PowerShell:
#
#   irm https://raw.githubusercontent.com/jobski/Hangar/main/install.ps1 | iex
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
    $repo = if ($env:HANGAR_REPO) { $env:HANGAR_REPO } else { 'jobski/Hangar' }
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
    function AuthGit {
        if (-not $env:GITHUB_TOKEN) { & git @args; return }
        $keys = @('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0')
        $saved = @{}
        foreach ($key in $keys) { $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }
        try {
            $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$($env:GITHUB_TOKEN)"))
            $env:GIT_CONFIG_COUNT = '1'
            $env:GIT_CONFIG_KEY_0 = 'http.extraHeader'
            $env:GIT_CONFIG_VALUE_0 = "Authorization: Basic $b64"
            & git @args
        } finally {
            foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
        }
    }
    # the Hangar on the port is this install when it accepts this install's token (each install makes its own);
    # the listening process is the base Python, not the venv's, so its command line cannot tell
    function Ours {
        $python = Join-Path $dir '.venv\Scripts\python.exe'
        if (-not (Test-Path $python)) { return $false }
        $probe = @'
import os, sys, urllib.request
try:
    from manager import config
    cfg = config.load()
    port = int(os.environ.get("HANGAR_PORT") or cfg["manager"]["port"])
    token = os.environ.get("LLM_MANAGER_TOKEN") or (cfg["manager"]["data_dir"] / "token.txt").read_text().strip()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/jobs?limit=1", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
'@
        $preference = $ErrorActionPreference
        Push-Location $dir
        try { $ErrorActionPreference = 'Continue'; & $python -c $probe 2>$null; return $LASTEXITCODE -eq 0 }
        finally { $ErrorActionPreference = $preference; Pop-Location }
    }
    $restart = $false
    if (Test-Path (Join-Path $dir '.git')) {
        Say "Updating $dir to $ref"
        $restart = Ours
        AuthGit -C $dir fetch --quiet origin $ref
        if ($LASTEXITCODE -ne 0) { throw "could not fetch $ref from $repo" }
        git -C $dir show-ref --verify --quiet "refs/heads/$ref"
        $hasRef = $LASTEXITCODE -eq 0
        if (-not $hasRef) { git -C $dir show-ref --verify --quiet "refs/tags/$ref"; $hasRef = $LASTEXITCODE -eq 0 }
        if ($hasRef) { git -C $dir checkout --quiet $ref }
        else { git -C $dir checkout --quiet -b $ref FETCH_HEAD }
        if ($LASTEXITCODE -ne 0) { throw "cannot check out $ref; kept local commits and edits in $dir" }
        if (git -C $dir rev-list 'FETCH_HEAD..HEAD') { throw "local commits in $dir; kept them and did not update" }
        git -C $dir merge --quiet --ff-only FETCH_HEAD
        if ($LASTEXITCODE -ne 0) {
            throw "local commits or edits in $dir; kept them and did not update"
        }
    } elseif (Test-Path $dir) {
        throw "$dir exists and is not a Hangar checkout; set HANGAR_DIR to install somewhere else"
    } else {
        Say "Downloading Hangar ($ref) into $dir"
        AuthGit clone --quiet --branch $ref "https://github.com/$repo.git" $dir
        if ($LASTEXITCODE -ne 0) { throw "could not clone $repo (private repo? set GITHUB_TOKEN)" }
    }

    # ---- Python environment, config.yaml, token
    $env:HANGAR_PYTHON = ($py -join ' ')
    & (Join-Path $dir 'scripts\setup.ps1')
    Push-Location $dir
    try { $port = & (Join-Path $dir '.venv\Scripts\python.exe') -m manager.local --port }
    finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the manager port' }

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
        Push-Location $dir
        try { & (Join-Path $dir '.venv\Scripts\python.exe') -m manager.local --stop-manager }
        finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw 'Cannot safely stop the installed manager' }
    }
    if (-not $env:HANGAR_NO_START -or $restart) {
        & (Join-Path $dir 'scripts\start.ps1')
        for ($i = 0; $i -lt 30; $i++) {
            try { Invoke-WebRequest "http://127.0.0.1:$port/api/ping" -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
        }
        if (-not $env:HANGAR_NO_AUTOSTART) {
            Push-Location $dir
            try { & (Join-Path $dir '.venv\Scripts\python.exe') -m manager.local autostart on }
            finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { Warn 'could not turn on start with Windows; try: hangar autostart on' }
        }
    }

    $token = (Get-Content (Join-Path $dir 'data\token.txt') -ErrorAction SilentlyContinue | Select-Object -First 1)
    Say "Hangar is installed in $dir"
    Write-Host "    Dashboard: http://localhost:$port/?token=$token"
    Write-Host "    Settings:  $dir\config.yaml   (apply with: scripts\stop.ps1; scripts\start.ps1)"
    Write-Host "    Remove:    & '$dir\uninstall.ps1'"
}
