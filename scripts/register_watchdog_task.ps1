#
# register_watchdog_task.ps1 — one-click registration of the 5-minute
# Undermind proxy watchdog scheduled task. Needs one admin approval
# (Windows reserves scheduled-task creation for elevated processes).
#
# Right-click > Run with PowerShell, or double-click.
# Remove anytime:  Unregister-ScheduledTask -TaskName UndermindProxyWatchdog
#
$script = "C:\Users\dewayne\Downloads\undermind\scripts\undermind_proxy_watchdog.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$t1 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$t2 = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask -TaskName "UndermindProxyWatchdog" `
    -Action $action -Trigger $t1, $t2 `
    -Description "Keep the Undermind proxy (:11435) alive across crashes and reboots. No-op when already serving." `
    -Force | Out-Null

Write-Host "Registered 'UndermindProxyWatchdog':"
Get-ScheduledTask -TaskName "UndermindProxyWatchdog" |
    Format-List TaskName, State
Write-Host "Next runs: every 5 minutes and at logon."
Write-Host "To remove: Unregister-ScheduledTask -TaskName UndermindProxyWatchdog"
