<#
.SYNOPSIS  Accuracy check of the extraction against entries you label by hand (vault must be open).
.EXAMPLE   .\evaluate.ps1 sample --n 30                  # writes data\enrich_labels.yaml (private, in the vault)
.EXAMPLE   .\evaluate.ps1 score                          # default model, prints figures only
.EXAMPLE   .\evaluate.ps1 score --details                # + differences per entry (private, screen only)
.EXAMPLE   .\evaluate.ps1 mood                           # model vs the moods you rated ("Stimmung = +2")
.EXAMPLE   .\evaluate.ps1 calibrate                      # would a simple stretch of the model's scale help?
.EXAMPLE   .\evaluate.ps1 achievements --details         # model vs the entries you marked ("Achievement = 2")
.EXAMPLE   .\evaluate.ps1 achievements --reviewed-from 2012 --reviewed-to 2012 --negatives 60
.EXAMPLE   .\evaluate.ps1 achievements --set validation  # once, at the very end (tuning uses the default --set tune)
#>
Push-Location (Join-Path $PSScriptRoot "backend")
try { & .\.venv\Scripts\python.exe -m enrich.evaluate @args }
finally { Pop-Location }
