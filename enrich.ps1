<#
.SYNOPSIS  Per-entry extraction with the local model (vault must be open). Resumable; prints counts only.
.EXAMPLE   .\enrich.ps1 --limit 20
.EXAMPLE   .\enrich.ps1 --from 2008 --to 2010 --limit 20 --spread
.EXAMPLE   .\enrich.ps1 --mood-only                      # only the mood of every dated entry: faster, resumable
.EXAMPLE   .\enrich.ps1 --stats
.EXAMPLE   .\enrich.ps1 --show 20      # results per entry - private content, for your screen only
.EXAMPLE   .\enrich.ps1 --model qwen3:30b-a3b
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m enrich.run @args }
finally { Pop-Location }
