# Grant (or refresh) SSH access for one public key on this Windows box, idempotently,
# then prove it works. Re-run any time; it never duplicates a key and it self-verifies.
#
#   .\scripts\windows-grant-ssh.ps1 -PubKey "ssh-ed25519 AAAA... you@host"
#   .\scripts\windows-grant-ssh.ps1 -PubKey "ssh-ed25519 ..." -User hangar-ops   # a standard (non-admin) account
#
# Why this exists: the Administrators override file (administrators_authorized_keys) that
# sshd forces on admin users is picky about ACLs and line endings, and hand-editing it fails
# silently. This script picks the file sshd will actually read for the target account, removes
# any existing key with the SAME fingerprint (so it is safe to re-run), writes LF/no-BOM, locks
# the ACLs sshd demands, then verifies the key is in the file and prints sshd's effective
# key-file path plus any Match/Allow/Deny rules that gate connections. Run it elevated.
param(
  [Parameter(Mandatory)][string]$PubKey,
  [string]$User = $env:USERNAME,
  [switch]$SkipVerify
)
$ErrorActionPreference = 'Stop'
$PubKey = $PubKey.Trim()

function Get-LineFingerprint([string]$line) {
  if (-not $line.Trim()) { return $null }
  $t = [IO.Path]::GetTempFileName()
  try { [IO.File]::WriteAllText($t, $line.Trim() + "`n"); (ssh-keygen -lf $t 2>$null).Split(' ')[1] }
  finally { Remove-Item $t -ErrorAction SilentlyContinue }
}

$fp = Get-LineFingerprint $PubKey
if (-not $fp) { throw "Not a valid public key line: $PubKey" }

# Which file does sshd read for this account? Admins use the shared override; everyone else their own.
# SID lookup so it works on any Windows language.
$adminsName = (New-Object Security.Principal.SecurityIdentifier([Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null)).Translate([Security.Principal.NTAccount]).Value.Split('\')[-1]
$isTargetAdmin = ((net localgroup $adminsName) -join "`n") -match ('(?im)^\s*' + [regex]::Escape($User) + '$')
if ($isTargetAdmin) {
  $f = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
} else {
  $f = "C:\Users\$User\.ssh\authorized_keys"
  New-Item -ItemType Directory -Force (Split-Path $f) | Out-Null
}

# Rebuild: keep every key except one already matching this fingerprint, then append the fresh copy.
$existing = if (Test-Path $f) { @(Get-Content $f) | Where-Object { $_.Trim() } } else { @() }
$kept = @()
foreach ($l in $existing) { if ((Get-LineFingerprint $l) -ne $fp) { $kept += $l } }
$all = $kept + $PubKey
[System.IO.File]::WriteAllBytes($f, [Text.Encoding]::ASCII.GetBytes(($all -join "`n") + "`n"))

# sshd refuses the file unless ownership/ACLs are exactly what it wants.
if ($isTargetAdmin) {
  icacls $f /inheritance:r /grant "SYSTEM:(F)" "BUILTIN\Administrators:(F)" | Out-Null
} else {
  icacls $f /inheritance:r /grant "SYSTEM:(F)" "$env:USERDOMAIN\$User:(F)" | Out-Null
}

Write-Host "installed $fp -> $f ($($all.Count) key(s))"

if ($SkipVerify) { return }

# Structural proof (a real login test needs the laptop holding the private key).
$back = @(Get-Content $f) | Where-Object { (Get-LineFingerprint $_) -eq $fp }
if ($back.Count) { Write-Host "verified: $fp present in $f" -ForegroundColor Green }
else { Write-Host "VERIFY FAILED: fingerprint not found after write" -ForegroundColor Yellow }

# Reveal what sshd actually reads, and any source/user rule that could reject a client.
Write-Host "--- sshd effective settings ---"
& sshd -f (Join-Path $env:ProgramData 'ssh\sshd_config') -T 2>$null |
  Select-String -Pattern '^(authorizedkeysfile|pubkeyauthentication|passwordauthentication|permitrootlogin|allowusers|allowgroups|denyusers|denygroups)' |
  ForEach-Object { Write-Host "  $_" }
Write-Host "--- Match / Allow / Deny lines in sshd_config (evaluate per connection) ---"
Select-String -Path (Join-Path $env:ProgramData 'ssh\sshd_config') -Pattern '^\s*(Match|AllowUsers|AllowGroups|DenyUsers|DenyGroups)' |
  ForEach-Object { Write-Host "  $($_.LineNumber): $($_.Line)" }
