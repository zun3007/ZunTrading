# Dry-run 1 cycle: data thật + brain thật, KHÔNG Telegram, executor paper.
param([string]$TradeProfile = "day")
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m zuntrading.scanner --profile $TradeProfile --dry-run
exit $LASTEXITCODE
