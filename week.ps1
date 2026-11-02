<#
.SYNOPSIS  Weekly report of the daily plan (data\planning\tagesplan.md) against the goals (data\planning\goals.yaml).
           Vault must be open. Example goals: config\goals.example.yaml.
.EXAMPLE   .\week.ps1
.EXAMPLE   .\week.ps1 --date 2027-01-15 --weeks 8
.EXAMPLE   .\week.ps1 --no-notes                 # without the written notes, e.g. to show it to someone
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m planning.week @args }
finally { Pop-Location }
