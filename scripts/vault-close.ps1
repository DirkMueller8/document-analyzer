#Requires -RunAsAdministrator
<# .SYNOPSIS  Lock and detach the vault. Stop the backend first. #>
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\vault-common.ps1"

$img = Get-DiskImage -ImagePath $VaultFile
if (-not $img.Attached) { Write-Host "Vault is already closed."; return }
$vol = Get-VaultVolumePath
Lock-BitLocker -MountPoint $vol -ForceDismount | Out-Null
Dismount-DiskImage -ImagePath $VaultFile | Out-Null
Write-Host "Vault locked and detached."
