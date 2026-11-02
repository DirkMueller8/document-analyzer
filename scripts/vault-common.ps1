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

$AutoCloseTask = "document-analyzer vault auto-close"

function Get-AutoCloseCommand {
    # Runs as SYSTEM, so the logic lives inside the task (admin-protected), not in a user-writable script.
    # Skips a run while a Python process from the project's venv is alive (it may be writing to the vault).
    $vault = $VaultFile.Replace("'", "''")
    $venv  = (Join-Path $RepoRoot "backend\.venv").Replace("'", "''")
    $task  = $AutoCloseTask.Replace("'", "''")
    @"
`$ErrorActionPreference = 'Stop'
`$vault = '$vault'; `$venv = '$venv'; `$task = '$task'
try { `$img = Get-DiskImage -ImagePath `$vault } catch { `$img = `$null }
if (-not `$img -or -not `$img.Attached) { Unregister-ScheduledTask -TaskName `$task -Confirm:`$false; return }
`$busy = Get-CimInstance Win32_Process | Where-Object { ("`$(`$_.ExecutablePath) `$(`$_.CommandLine)").IndexOf(`$venv, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
if (`$busy) { return }
`$vol = (`$img | Get-Disk | Get-Partition | Where-Object Type -eq 'Basic').AccessPaths | Where-Object { `$_ -like '*Volume{*' } | Select-Object -First 1
Lock-BitLocker -MountPoint `$vol -ForceDismount | Out-Null
Dismount-DiskImage -ImagePath `$vault | Out-Null
Unregister-ScheduledTask -TaskName `$task -Confirm:`$false
"@
}

function Register-VaultAutoClose([int]$Minutes) {
    # One task: first try after $Minutes, then every 15 minutes while a project job keeps the vault busy.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes((Get-AutoCloseCommand)))
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded"
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($Minutes) `
        -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 24)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $AutoCloseTask -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Force | Out-Null
    (Get-Date).AddMinutes($Minutes)
}

function Unregister-VaultAutoClose {
    if (Get-ScheduledTask -TaskName $AutoCloseTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $AutoCloseTask -Confirm:$false
    }
}

function ConvertFrom-Secure([securestring]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
