# Đăng ký/gỡ Windows Task Scheduler cho bot chạy 24/7.
#   .\scripts\install_task.ps1            → đăng ký 3 task (scan day 15', scan swing 4h, report 21:00)
#   .\scripts\install_task.ps1 -Unregister → gỡ cả 3
param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$python = (Get-Command python).Source
$names = @("ZunTrading-Scan-Day", "ZunTrading-Scan-Swing", "ZunTrading-Report", "ZunTrading-Sync", "ZunTrading-Weekly")

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

# Align vào ranh giới nến: quét NGAY SAU khi nến đóng (+30s cho data source kịp),
# không phải thời điểm ngẫu nhiên theo giờ cài — signal sớm tối đa ~14 phút so với lệch pha.
$now = Get-Date
$dayStart = $now.Date.AddHours($now.Hour).AddMinutes([math]::Floor($now.Minute / 15) * 15).AddMinutes(15).AddSeconds(30)
$swingStart = $now.Date.AddHours([math]::Floor($now.Hour / 4) * 4).AddHours(4).AddMinutes(1)

Register-ZunTask "ZunTrading-Scan-Day" "-m zuntrading.scanner --profile day --executor auto" `
    (New-ScheduledTaskTrigger -Once -At $dayStart -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650))

Register-ZunTask "ZunTrading-Scan-Swing" "-m zuntrading.scanner --profile swing --executor auto" `
    (New-ScheduledTaskTrigger -Once -At $swingStart -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 3650))

Register-ZunTask "ZunTrading-Report" "-m zuntrading.reporter" `
    (New-ScheduledTaskTrigger -Daily -At "21:00")

# Sync 5': chốt outcome + breakeven/trailing — không scan, không tốn não
Register-ZunTask "ZunTrading-Sync" "-m zuntrading.scanner --sync-only --executor auto" `
    (New-ScheduledTaskTrigger -Once -At $now.AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650))

# Não họp tổng kết tuần — Chủ nhật 20:00, 1 call Opus, chỉ ĐỀ XUẤT (không tự đổi config)
Register-ZunTask "ZunTrading-Weekly" "-m zuntrading.reporter --weekly" `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "20:00")

Write-Host ""
Write-Host "Xong. Kiểm tra: Get-ScheduledTask -TaskName 'ZunTrading-*'" -ForegroundColor Green
Write-Host "Bot sẽ tự chạy mỗi 15 phút. Xem log tại logs\zuntrading.log"
