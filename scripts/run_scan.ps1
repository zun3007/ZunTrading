# Chạy 1 cycle thật (Telegram bật, executor auto: MT5 nếu sẵn sàng, ngược lại paper).
param([string]$TradeProfile = "day")
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m zuntrading.scanner --profile $TradeProfile --executor auto
exit $LASTEXITCODE
