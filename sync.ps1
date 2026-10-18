<#
.SYNOPSIS  Import new/changed entries from your .odt (asks for its password) and update the search index.
           The document path comes from -Path or the user environment variable DOC_PATH, so it never
           ends up in the repository. Set it once:
               [Environment]::SetEnvironmentVariable("DOC_PATH", "C:\...\file.odt", "User")
.EXAMPLE   .\sync.ps1
.EXAMPLE   .\sync.ps1 --dry-run
#>
param([string]$Path = $env:DOC_PATH)
if (-not $Path) { Write-Host "No document path. Use -Path or set DOC_PATH (see: Get-Help .\sync.ps1)." -ForegroundColor Yellow; return }
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m ingest.run $Path @args }
finally { Pop-Location }
