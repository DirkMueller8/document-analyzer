<#
.SYNOPSIS  Milestones over the years next to the mood (vault must be open; run .\enrich.ps1 first).
           Counts only: achievements, first times, setbacks and decisions per period and per domain.
.EXAMPLE   .\milestones.ps1
.EXAMPLE   .\milestones.ps1 --from 2008 --by half
.EXAMPLE   .\milestones.ps1 --min-n 30
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m analysis.milestones_report @args }
finally { Pop-Location }
