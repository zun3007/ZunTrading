# Mở dashboard local: http://127.0.0.1:8420
Set-Location (Split-Path $PSScriptRoot -Parent)
Start-Process "http://127.0.0.1:8420"
python -m zuntrading.api
