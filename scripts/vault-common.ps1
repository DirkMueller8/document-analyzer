# Shared settings for the vault scripts.
$RepoRoot  = Resolve-Path (Join-Path $PSScriptRoot "..")
$MountPath = if ($env:VAULT_MOUNT) { $env:VAULT_MOUNT } else { Join-Path $RepoRoot "data" }
# Keep the vault file OUTSIDE the repo (and outside OneDrive/cloud-synced folders).
$VaultFile = if ($env:VAULT_PATH) { $env:VAULT_PATH } else { Join-Path $env:USERPROFILE "Vaults\vault.vhdx" }
$Marker    = ".vault"   # file inside the vault; its presence means "vault is open"
$MinPasswordLength = 12

function Get-VaultPartition {
    Get-DiskImage -ImagePath $VaultFile | Get-Disk | Get-Partition | Where-Object Type -eq "Basic"
}

function Get-VaultVolumePath {
    # Volume GUID path (\\?\Volume{guid}\) - works for BitLocker cmdlets without a drive letter
    $path = (Get-VaultPartition).AccessPaths | Where-Object { $_ -like '*Volume{*' } | Select-Object -First 1
    if (-not $path) { throw "Could not find the vault volume. Is $VaultFile attached?" }
    $path
}

function Confirm-MountPoint {
    $part = Get-VaultPartition
    $target = "$MountPath\"
    if ($part.AccessPaths -notcontains $target) {
        if (-not (Test-Path $MountPath)) { New-Item -ItemType Directory $MountPath | Out-Null }
        $part | Add-PartitionAccessPath -AccessPath $target
    }
}

function ConvertFrom-Secure([securestring]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
