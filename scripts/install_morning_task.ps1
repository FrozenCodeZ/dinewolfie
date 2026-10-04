# Registers a Windows scheduled task that plans your day every morning and
# pushes it to your phone. Run from the project folder:
#     powershell -ExecutionPolicy Bypass -File scripts\install_morning_task.ps1
# Optional: -Time "7:30AM"
param(
    [string]$Time = "8:00AM",
    [string]$TaskName = "DineWolfie Morning Plan"
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\pythonw.exe"   # pythonw = no console window
if (-not (Test-Path $python)) {
    Write-Error "Can't find $python. Create the virtual environment first (README > Setup)."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $python -Argument "-m src.morning_run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: if the PC was off at 8:00, run as soon as it's back on.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "DineWolfie: plan today's SBU dining and push it to my phone (ntfy)." -Force | Out-Null

Write-Host "Scheduled '$TaskName' every day at $Time."
Write-Host "Test it right now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Logs go to data\logs\.  Remove it with:  scripts\uninstall_morning_task.ps1"
