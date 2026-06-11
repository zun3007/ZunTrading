# Verify: lint + toàn bộ unit tests. Exit 0 = sạch.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "=== ruff check ===" -ForegroundColor Cyan
python -m ruff check .
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "=== pytest ===" -ForegroundColor Cyan
python -m pytest -q
exit $LASTEXITCODE
