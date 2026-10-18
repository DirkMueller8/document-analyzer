<#
.SYNOPSIS  Weight statistics from the database (vault must be open). Works from any folder.
.EXAMPLE   .\weight.ps1
.EXAMPLE   .\weight.ps1 --csv ..\data\weight.csv
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m analysis.weight @args }
finally { Pop-Location }
