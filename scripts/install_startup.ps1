# Start Jarvis automatically when you sign in to Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Mode voice   # old terminal voice mode
#
# By default it starts the browser app (start-jarvis-web.bat) about 20 seconds after you sign in, so
# Ollama has time to start first, and opens Jarvis in your browser. The window starts minimized in the
# taskbar as "Jarvis": close it to stop Jarvis. If Jarvis is already running, nothing new starts.

param(
    [switch]$Remove,
    [ValidateSet("web", "phone", "voice")]
    [string]$Mode = "web",
    [int]$DelaySeconds = 20
)

$TaskName = "Jarvis Assistant"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the $TaskName startup task."
    exit 0
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root ".venv\Scripts\jarvis.exe"))) {
    Write-Error "Couldn't find .venv\Scripts\jarvis.exe in $Root. Set up the virtual environment first."
    exit 1
}
$Launcher = @{ web = "start-jarvis-web.bat"; phone = "start-jarvis-phone.bat"; voice = "start-jarvis.bat" }[$Mode]
$Bat = Join-Path $Root $Launcher
if (-not (Test-Path $Bat)) {
    Write-Error "Couldn't find $Bat."
    exit 1
}

# Skip starting if a Jarvis is already answering on its port (for example after a quick sign-out and in).
$Check = "powershell -NoProfile -Command `"try { Invoke-WebRequest http://127.0.0.1:8765/api/status -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 1 } catch { exit 0 }`""
$Action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c $Check && start `"Jarvis`" /min `"$Bat`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger.Delay = "PT$($DelaySeconds)S"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "Jarvis ($Mode mode) will now start about $DelaySeconds seconds after you sign in. Remove it with -Remove."
