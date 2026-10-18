<#
.SYNOPSIS  Ask a question - answered by the local model. Works from any folder.
.EXAMPLE   .\ask.ps1 "Wann bin ich meinen ersten Halbmarathon gelaufen?"
.EXAMPLE   .\ask.ps1 --first "Halbmarathon"
.EXAMPLE   .\ask.ps1 "Sommer 2014" --from 2014-06 --to 2014-08
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m search.ask @args }
finally { Pop-Location }
