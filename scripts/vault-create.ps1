#Requires -RunAsAdministrator
<#
.SYNOPSIS
  One-time setup: creates a BitLocker-encrypted VHDX and mounts it at <repo>\data.
.EXAMPLE
  .\scripts\vault-create.ps1 -SizeGB 50
.NOTES
  Paste a password with right-click (or Ctrl+Shift+V). Each character shows as one "*".
#>
param(
    [int]$SizeGB = 50,
    [securestring]$Password   # for automated tests; normally you are prompted
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\vault-common.ps1"

if (Test-Path $VaultFile) { throw "Vault already exists: $VaultFile" }
$existing = Get-ChildItem -Force $MountPath -ErrorAction SilentlyContinue | Where-Object Name -ne "README.txt"
if ($existing) { throw "$MountPath must be empty (found: $($existing.Name -join ', ')). Move those files away first." }

if (-not $Password) {
    $Password = Read-Host "New vault password (min. $MinPasswordLength characters)" -AsSecureString
    $repeat = Read-Host "Repeat password" -AsSecureString
    if ((ConvertFrom-Secure $Password) -cne (ConvertFrom-Secure $repeat)) { throw "Passwords do not match. Nothing was created." }
}
$plain = ConvertFrom-Secure $Password
if ($plain.Length -lt $MinPasswordLength) {
    throw "Password has only $($plain.Length) character(s), minimum is $MinPasswordLength. If you pasted it, use right-click to paste. Nothing was created."
}
if ($plain -match '[\x00-\x1F]') { throw "Password contains control characters (paste went wrong?). Nothing was created." }
Write-Host "Password accepted ($($plain.Length) characters)."
$plain = $null

try {
    # 1. Create an expandable VHDX (grows as needed, up to SizeGB)
    New-Item -ItemType Directory -Force (Split-Path $VaultFile) | Out-Null
    $dp = New-TemporaryFile
    "create vdisk file=`"$VaultFile`" maximum=$($SizeGB * 1024) type=expandable" | Set-Content $dp -Encoding ascii
    diskpart /s $dp | Out-Null
    Remove-Item $dp
    if (-not (Test-Path $VaultFile)) { throw "diskpart failed to create $VaultFile" }

    # 2. Attach, partition, format (no drive letter)
    $disk = Mount-DiskImage -ImagePath $VaultFile -NoDriveLetter -PassThru | Get-Disk
    $disk | Initialize-Disk -PartitionStyle GPT
    $disk | New-Partition -UseMaximumSize | Format-Volume -FileSystem NTFS -NewFileSystemLabel "Vault" -Confirm:$false | Out-Null

    # 3. BitLocker with password (XTS-AES-256)
    $vol = Get-VaultVolumePath
    Enable-BitLocker -MountPoint $vol -EncryptionMethod XtsAes256 -UsedSpaceOnly -PasswordProtector -Password $Password | Out-Null

    # 4. Mount into <repo>\data and move the README inside
    $readme = Join-Path $MountPath "README.txt"
    $keep = if (Test-Path $readme) { Get-Content $readme -Raw } else { $null }
    if (Test-Path $readme) { Remove-Item $readme }
    Confirm-MountPoint
    if ($keep) { Set-Content (Join-Path $MountPath "README.txt") $keep }
    Set-Content (Join-Path $MountPath $Marker) "vault"
}
catch {
    Write-Warning "Setup failed, cleaning up: $_"
    if ($keep -and -not (Test-Path (Join-Path $MountPath "README.txt"))) {
        try { Get-VaultPartition | Remove-PartitionAccessPath -AccessPath "$MountPath\" -ErrorAction SilentlyContinue } catch {}
        Set-Content (Join-Path $MountPath "README.txt") $keep -ErrorAction SilentlyContinue
    }
    if (Test-Path $VaultFile) {
        Dismount-DiskImage -ImagePath $VaultFile -ErrorAction SilentlyContinue | Out-Null
        Remove-Item $VaultFile -Force -ErrorAction SilentlyContinue
    }
    throw
}

Write-Host "Vault created: $VaultFile"
Write-Host "Mounted (unlocked) at: $MountPath"
Write-Host "Everything in the vault can be rebuilt from your .odt except overrides.yaml and"
Write-Host "people merges - keep a copy of those if you forget the vault password."
