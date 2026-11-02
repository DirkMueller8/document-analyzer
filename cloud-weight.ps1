<#
.SYNOPSIS  Optional cloud analyst: Claude designs and interprets a weight chart from MONTHLY AGGREGATES only
           (mean, min, max, number of days; no text). It shows exactly what would be sent, asks for your
           approval, and logs every request in the vault (data\egress.log). Cancelling sends nothing.
           Needs the vault open and an Anthropic API key: ANTHROPIC_API_KEY, or you are asked (not stored).
           Result: data\cloud_weight.png and data\cloud_weight.md
.EXAMPLE   .\cloud-weight.ps1
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m privacy.cloud_chart @args }
finally { Pop-Location }
