<#
.SYNOPSIS  Review the people index (vault must be open). Output contains names - for your screen only.
.EXAMPLE   .\people.ps1 list --min 2
.EXAMPLE   .\people.ps1 suggest
.EXAMPLE   .\people.ps1 merge "Lena" "Lena B." "Lenchen"
.EXAMPLE   .\people.ps1 rename "Lena" "Lena Berg"
.EXAMPLE   .\people.ps1 ignore "Name"
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m enrich.people @args }
finally { Pop-Location }
