# Đăng ký/gỡ Windows Task Scheduler cho bot chạy 24/7.
#   .\scripts\install_task.ps1            → đăng ký 3 task (scan day 15', scan swing 4h, report 21:00)
#   .\scripts\install_task.ps1 -Unregister → gỡ cả 3
param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$python = (Get-Command python).Source
$names = @("ZunTrading-Scan-Day", "ZunTrading-Scan-Swing", "ZunTrading-Report")

if ($Unregister) {
    foreach ($n in $names) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Đã gỡ $n"
    }
    exit 0
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 14)

function Register-ZunTask($name, $args_, $trigger) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $args_ -WorkingDirectory $repo
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "Đã đăng ký $name"
}

$now = Get-Date
Register-ZunTask "ZunTrading-Scan-Day" "-m zuntrading.scanner --profile day --executor auto" `
    (New-ScheduledTaskTrigger -Once -At $now -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650))

Register-ZunTask "ZunTrading-Scan-Swing" "-m zuntrading.scanner --profile swing --executor auto" `
    (New-ScheduledTaskTrigger -Once -At $now -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 3650))

Register-ZunTask "ZunTrading-Report" "-m zuntrading.reporter" `
    (New-ScheduledTaskTrigger -Daily -At "21:00")

Write-Host ""
Write-Host "Xong. Kiểm tra: Get-ScheduledTask -TaskName 'ZunTrading-*'" -ForegroundColor Green
Write-Host "Bot sẽ tự chạy mỗi 15 phút. Xem log tại logs\zuntrading.log"
