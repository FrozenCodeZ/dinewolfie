# Removes the DineWolfie morning task.
param([string]$TaskName = "DineWolfie Morning Plan")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed '$TaskName'."
