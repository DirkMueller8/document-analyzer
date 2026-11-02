<#
.SYNOPSIS  Mood over the years from the database (vault must be open): your own ratings where you have them,
           else the local model's estimates. Prints a table with 95 % intervals and writes a chart.
.EXAMPLE   .\mood.ps1
.EXAMPLE   .\mood.ps1 --by quarter --from 2008 --to 2012
.EXAMPLE   .\mood.ps1 --min-n 15
.EXAMPLE   .\mood.ps1 --chart-from 1994                  # or set chart_from: 1994 in data\mood_notes.yaml
.EXAMPLE   .\mood.ps1 --no-notes                         # ignore data\mood_notes.yaml
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m analysis.mood_report @args }
finally { Pop-Location }
