<#
.SYNOPSIS
  Looks for unprotected leftovers of a password-protected document.
.DESCRIPTION
  Checks the LibreOffice backup and recovery folders, stray copies of the document, stale lock and
  temp files, secrets in the PowerShell history, and the LibreOffice settings that create such copies.

  Read-only by default. It never reads document text: for ODF files it only looks for an encryption
  entry in the manifest, for other files it only reads the first 8 bytes to recognise the type.
  Works in Windows PowerShell 5.1 and PowerShell 7.
.PARAMETER Name
  Base name of the protected document (default: taken from the DOC_PATH environment variable).
.PARAMETER Delete
  Ask, file by file, whether to delete each unprotected file in the LibreOffice backup folders.
.EXAMPLE
  .\scripts\check-leftovers.ps1
.EXAMPLE
  .\scripts\check-leftovers.ps1 -Name notes -Delete
#>
param(
    [string]$Name,
    [switch]$Delete
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

if (-not $Name -and $env:DOC_PATH) { $Name = [IO.Path]::GetFileNameWithoutExtension($env:DOC_PATH) }
$attention = 0

function Write-Section([string]$Title) { Write-Host ""; Write-Host "== $Title" -ForegroundColor Cyan }
function Write-Ok([string]$Text)    { Write-Host "  [OK]    $Text" -ForegroundColor Green }
function Write-Check([string]$Text) { Write-Host "  [CHECK] $Text" -ForegroundColor Yellow; $script:attention++ }
function Write-Info([string]$Text)  { Write-Host "  [info]  $Text" -ForegroundColor Gray }

function Get-Protection([string]$Path) {
    # ENCRYPTED, PLAIN or UNVERIFIED - without reading any document text.
    $zip = $null
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($Path)
        $manifest = $zip.Entries | Where-Object { $_.FullName -eq "META-INF/manifest.xml" } | Select-Object -First 1
        if (-not $manifest) { return "PLAIN" }   # a zip without ODF manifest (docx, xlsx, pptx) is not password protected
        $reader = New-Object IO.StreamReader($manifest.Open())
        try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
        if ($text -match "encryption-data") { return "ENCRYPTED" }
        return "PLAIN"
    }
    catch [System.IO.InvalidDataException] {
        # not a zip: look at the first bytes only
        try {
            $head = New-Object byte[] 8
            $fs = [IO.File]::OpenRead($Path)
            try { [void]$fs.Read($head, 0, 8) } finally { $fs.Dispose() }
            if ([Text.Encoding]::ASCII.GetString($head).StartsWith("{\rtf")) { return "PLAIN" }   # RTF cannot be encrypted
            return "UNVERIFIED"                                                                  # old binary Office, text, other
        }
        catch { return "UNVERIFIED" }
    }
    catch { return "UNVERIFIED" }
    finally { if ($zip) { $zip.Dispose() } }
}

function Get-LoSetting($Xml, [string]$ItemPath, [string]$PropName) {
    if (-not $Xml) { return $null }
    $xpath = "//item[@*[local-name()='path' and .='$ItemPath']]/prop[@*[local-name()='name' and .='$PropName']]/value"
    $node = $Xml.SelectSingleNode($xpath)
    if ($node) { return $node.InnerText }
    return $null
}

$lo = Join-Path $env:APPDATA "LibreOffice\4\user"

# 1. LibreOffice backup and recovery copies ---------------------------------------------------------
Write-Section "LibreOffice backup and recovery copies"
$files = @()
foreach ($sub in "backup", "autorecovery") {
    $dir = Join-Path $lo $sub
    if (Test-Path $dir) { $files += @(Get-ChildItem $dir -Recurse -File -Force -ErrorAction SilentlyContinue) }
}
$rows = @()
foreach ($f in $files) {
    $rows += [pscustomobject]@{
        Path = $f.FullName; Name = $f.Name; KB = [math]::Round($f.Length / 1KB); Date = $f.LastWriteTime
        Protection = Get-Protection $f.FullName
        Mine = [bool]($Name -and $f.Name -like "*$Name*")
    }
}
if ($rows.Count -eq 0) {
    Write-Ok "no backup or recovery files"
}
else {
    $groups = $rows | Group-Object Protection | ForEach-Object { "{0} {1}" -f $_.Count, $_.Name }
    Write-Info ("{0} file(s): {1}" -f $rows.Count, ($groups -join ", "))
    $mineBad = @($rows | Where-Object { $_.Mine -and $_.Protection -ne "ENCRYPTED" })
    if ($mineBad.Count -gt 0) {
        Write-Check ("{0} copy/copies of '{1}' without encryption:" -f $mineBad.Count, $Name)
        $mineBad | ForEach-Object { Write-Host ("            {0:yyyy-MM-dd} {1,7} KB {2,-10} {3}" -f $_.Date, $_.KB, $_.Protection, $_.Name) }
    }
    elseif ($Name) {
        Write-Ok "all copies of '$Name' are encrypted"
    }
    $others = @($rows | Where-Object { -not $_.Mine -and $_.Protection -ne "ENCRYPTED" })
    if ($others.Count -gt 0) {
        Write-Check ("{0} unprotected copy/copies of other documents (they may contain personal data):" -f $others.Count)
        $others | Sort-Object Date | Select-Object -First 25 | ForEach-Object {
            Write-Host ("            {0:yyyy-MM-dd} {1,7} KB {2,-10} {3}" -f $_.Date, $_.KB, $_.Protection, $_.Name) }
        if ($others.Count -gt 25) { Write-Host ("            ... and {0} more" -f ($others.Count - 25)) }
    }
    if ($Delete) {
        foreach ($r in @($rows | Where-Object { $_.Protection -ne "ENCRYPTED" })) {
            $answer = Read-Host ("Delete '{0}' ({1})? [y/N]" -f $r.Name, $r.Protection)
            if ($answer -eq "y") { Remove-Item -LiteralPath $r.Path -Force; Write-Host "  deleted" }
        }
    }
}

# 2. Stray copies of the document and stale lock files -----------------------------------------------
Write-Section "Stray copies and lock files in your user folders"
$roots = @()
foreach ($sub in "Downloads", "Desktop", "Documents", "OneDrive", "Private") {
    $p = Join-Path $env:USERPROFILE $sub
    if (Test-Path $p) { $roots += $p }
}
$roots += $env:USERPROFILE
$found = @()
foreach ($root in ($roots | Select-Object -Unique)) {
    $depth = 3
    if ($root -eq $env:USERPROFILE) { $depth = 1 }
    $found += @(Get-ChildItem $root -Recurse -Depth $depth -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName.Substring($env:USERPROFILE.Length) -notmatch '\\(AppData|\.git|\.venv|node_modules|\.claude)\\' })
}
$found = $found | Sort-Object FullName -Unique
$self = $null
if ($env:DOC_PATH -and (Test-Path -LiteralPath $env:DOC_PATH)) { $self = (Resolve-Path -LiteralPath $env:DOC_PATH).Path }
if ($Name) {
    $copies = @($found | Where-Object { $_.Name -like "*$Name*" -and $_.Name -notlike ".~lock.*" -and $_.FullName -ne $self })
    if ($copies.Count -eq 0) { Write-Ok "no other copies of '$Name' found" }
    foreach ($c in $copies) {
        $prot = Get-Protection $c.FullName
        $line = "{0:yyyy-MM-dd} {1,7} MB {2,-10} {3}" -f $c.LastWriteTime, [math]::Round($c.Length / 1MB, 1), $prot, $c.FullName
        if ($prot -eq "ENCRYPTED") { Write-Info "other copy: $line (encrypted, but is it still needed?)" }
        else { Write-Check "other copy: $line" }
    }
}
else {
    Write-Info "no document name given (use -Name or set DOC_PATH): stray-copy search skipped"
}
$stale = @($found | Where-Object { $_.Name -like ".~lock.*" -and $_.LastWriteTime -lt (Get-Date).AddDays(-1) })
if ($stale.Count -gt 0) { Write-Check ("{0} stale LibreOffice lock file(s): {1}" -f $stale.Count, (($stale | ForEach-Object { $_.FullName }) -join "; ")) }
else { Write-Ok "no stale lock files" }

# 3. LibreOffice temp leftovers ------------------------------------------------------------------------
Write-Section "Temp folder"
$tmp = @(Get-ChildItem $env:TEMP -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "lu*.tmp" -and $_.LastWriteTime -lt (Get-Date).AddHours(-2) })
if ($tmp.Count -gt 0) { Write-Check ("{0} old LibreOffice temp item(s) in {1} (close LibreOffice, then delete them)" -f $tmp.Count, $env:TEMP) }
else { Write-Ok "no old LibreOffice temp items" }

# 4. Secrets in the shell history ------------------------------------------------------------------------
Write-Section "PowerShell history"
$histPath = $null
try { $histPath = (Get-PSReadLineOption).HistorySavePath } catch { }
if (-not $histPath) { $histPath = Join-Path $env:APPDATA "Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt" }
if ($histPath -and (Test-Path $histPath)) {
    $hits = @(Select-String -Path $histPath -Pattern 'sk-ant-[A-Za-z0-9_\-]{10,}', '(?i)(pass(wor[dt])|kennwort)\s*[=:]\s*[^\s$]' -ErrorAction SilentlyContinue)
    if ($hits.Count -gt 0) { Write-Check ("{0} history line(s) look like an API key or password. Edit or delete: {1}" -f $hits.Count, $histPath) }
    else { Write-Ok "no API keys or passwords in the history" }
}
else { Write-Info "no history file found" }

# 5. LibreOffice settings -----------------------------------------------------------------------------------
Write-Section "LibreOffice settings"
$xcu = Join-Path $lo "registrymodifications.xcu"
$xml = $null
if (Test-Path $xcu) { $xml = New-Object System.Xml.XmlDocument; $xml.Load($xcu) }
if (-not $xml) {
    Write-Info "no LibreOffice profile found at $lo"
}
else {
    $backup = Get-LoSetting $xml "/org.openoffice.Office.Common/Save/Document" "CreateBackup"
    if ($backup -eq "false") { Write-Ok "'Always create backup copy' is off" }
    else { Write-Check "'Always create backup copy' is on (Tools > Options > Load/Save > General)" }

    $auto = Get-LoSetting $xml "/org.openoffice.Office.Recovery/AutoSave" "Enabled"
    if ($auto -eq "false") { Write-Ok "AutoRecovery is off" }
    else { Write-Info "AutoRecovery is on: recovery copies of open documents are written to the backup folder" }

    $pick = Get-LoSetting $xml "/org.openoffice.Office.Common/History" "PickListSize"
    if ($pick -eq "0") { Write-Ok "recent-documents list is disabled (PickListSize = 0)" }
    else { Write-Info "recent-documents list remembers file names (Expert Configuration: PickListSize = 0 disables it)" }
}

Write-Host ""
if ($attention -gt 0) { Write-Host "$attention item(s) need your attention." -ForegroundColor Yellow; exit 1 }
Write-Host "Nothing to worry about." -ForegroundColor Green
exit 0
