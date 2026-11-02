#Requires -RunAsAdministrator
<# .SYNOPSIS  Attach and unlock the vault at <repo>\data. It closes by itself after -AutoCloseMinutes
              (default 120, 0 = never); while a Python process of the project runs, it waits for it. #>
param(
    [securestring]$Password,          # for automated tests; normally you are prompted
    [int]$AutoCloseMinutes = 120
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\vault-common.ps1"

if (-not (Test-Path $VaultFile)) { throw "No vault at $VaultFile. Run vault-create.ps1 first." }
if (-not (Get-DiskImage -ImagePath $VaultFile).Attached) {
    Mount-DiskImage -ImagePath $VaultFile -NoDriveLetter | Out-Null
}
$vol = Get-VaultVolumePath
if ((Get-BitLockerVolume -MountPoint $vol).LockStatus -eq "Locked") {
    if (-not $Password) { $Password = Read-Host "Vault password" -AsSecureString }
    Unlock-BitLocker -MountPoint $vol -Password $Password | Out-Null
}
Confirm-MountPoint
if (-not (Test-Path (Join-Path $MountPath $Marker))) { throw "Mounted, but marker file missing in $MountPath." }
Write-Host "Vault open at $MountPath"
if ($AutoCloseMinutes -gt 0) {
    $at = Register-VaultAutoClose $AutoCloseMinutes
    Write-Host ("Closes by itself at {0:HH:mm} (later while a project job still runs). Close now: scripts\vault-close.ps1" -f $at)
}
else {
    Unregister-VaultAutoClose
    Write-Host "No automatic close. Close it with scripts\vault-close.ps1"
}
