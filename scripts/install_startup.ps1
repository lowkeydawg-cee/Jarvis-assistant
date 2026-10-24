# Start Jarvis automatically when you sign in to Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove
#
# Run from the jarvis folder after setting up .venv. Jarvis starts minimized
# in wake word mode, so it stays quiet until you say "Hey Jarvis" or a watcher
# has something worth saying.

param(
    [switch]$Remove,
    [string]$Arguments = "--wake"
)

$TaskName = "Jarvis Assistant"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the $TaskName startup task."
    exit 0
}

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Couldn't find $Python. Create the virtual environment first."
    exit 1
}

$Action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c start `"Jarvis`" /min `"$Python`" -m jarvis $Arguments" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "Jarvis will now start when you sign in. Remove it with -Remove."
