# One-click setup for Jarvis on Windows. Double-click setup.bat, or run:
#   powershell -ExecutionPolicy Bypass -File setup.ps1            # asks before installing anything
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Yes       # yes to every question
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -NoPrompt  # no questions; skips optional steps
#
# Safe to run again: it keeps your config.yaml, keys and models, and only adds what's missing.

param([switch]$Yes, [switch]$NoPrompt)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Note($text) { Write-Host "   $text" }
function Ask($question) {
    if ($Yes) { return $true }
    if ($NoPrompt) { return $false }
    $a = Read-Host "   $question [Y/n]"
    return ($a -eq "" -or $a -match "^[yY]")
}
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "Jarvis setup" -ForegroundColor White

# ---- 1. Python 3.10 or newer --------------------------------------------------------
Step "Python"
$Py = $null
foreach ($c in @("py -3", "python")) {
    try {
        $v = & ([scriptblock]::Create("$c -c `"import sys; print('%d.%d' % sys.version_info[:2])`"")) 2>$null
        if ($v -and [version]$v -ge [version]"3.10") { $Py = $c; break }
    } catch {}
}
if (-not $Py) {
    if ((Have winget) -and (Ask "Python 3.10+ isn't installed. Install Python 3.12 now with winget?")) {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $Py = "py -3"
    } else {
        Write-Host "   Install Python 3.10 or newer from https://www.python.org/downloads/ (tick 'Add to PATH'), then run setup again." -ForegroundColor Yellow
        exit 1
    }
}
Ok "Python $(& ([scriptblock]::Create("$Py --version")))"

# ---- 2. Ollama (the local brain, notes search and fallback) --------------------------
Step "Ollama"
$OllamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
if (-not (Have ollama) -and -not (Test-Path $OllamaExe)) {
    if ((Have winget) -and (Ask "Ollama isn't installed. Install it now with winget (about 1 GB)?")) {
        winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    } else {
        Note "Skipped. Get it from https://ollama.com/download, then run setup again for the models."
    }
}
if (Have ollama) { $Ollama = "ollama" } elseif (Test-Path $OllamaExe) { $Ollama = $OllamaExe } else { $Ollama = $null }
if ($Ollama) {
    try { Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing -TimeoutSec 3 | Out-Null }
    catch { Start-Process (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe") -ErrorAction SilentlyContinue; Start-Sleep 8 }
    Ok "Ollama is installed"
}

# ---- 3. Jarvis itself ---------------------------------------------------------------
Step "Installing Jarvis (a few minutes the first time)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & ([scriptblock]::Create("$Py -m venv .venv"))
}
$VPy = Join-Path $Root ".venv\Scripts\python.exe"
& $VPy -m pip install --upgrade pip -q
& $VPy -m pip install -e ".[all,voiceid]" -q
if ($LASTEXITCODE -ne 0) {
    Note "The full install failed; trying without the heavier extras."
    & $VPy -m pip install -e ".[voice,mcp,brain,proactive]" -q
}
Ok "Jarvis installed in .venv"

Step "Voices"
& $VPy scripts\download_voice.py | Out-Null
& $VPy scripts\download_speaker_model.py | Out-Null
Ok "Speech voice and speaker-recognition model ready"

# ---- 4. A config that fits this laptop ------------------------------------------------
Step "Configuration"
$RamGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$Model = if ($RamGB -ge 12) { "llama3.1:8b" } else { "llama3.2:3b" }
if (-not (Test-Path "config.yaml")) {
    $cfg = Get-Content "config.example.yaml" -Raw
    $cfg = $cfg -replace "(?m)^  model: llama3\.1:8b.*$", "  model: $Model                 # chosen for $RamGB GB of RAM"
    $notes = @("Documents", "Desktop", "OneDrive\Documents") | ForEach-Object { Join-Path $HOME $_ } | Where-Object { Test-Path $_ }
    $folders = ($notes | ForEach-Object { "    - " + ($_ -replace "\\", "/") }) -join "`n"
    $cfg = $cfg -replace "(?m)^    - C:/Users/SAINT/Documents/Notes\r?\n    - C:/Users/SAINT/code", $folders
    $cfg = $cfg -replace "(?m)^  git_repos:\r?\n    - C:/Users/SAINT/code/jarvis", "  git_repos: []"
    $cfg = $cfg -replace "(?m)^(\s+vision:\s*\r?\n\s+enabled:) true", '$1 false'
    Set-Content "config.yaml" $cfg -Encoding utf8
    Ok "Made config.yaml: local model $Model, notes from $($notes.Count) folder(s)"
} else {
    $m = Select-String -Path "config.yaml" -Pattern "^\s+model:\s*(\S+)" | Select-Object -First 1
    if ($m) { $Model = $m.Matches[0].Groups[1].Value }
    Ok "Kept your existing config.yaml (local model $Model)"
}

# ---- 5. AI models -------------------------------------------------------------------------
if ($Ollama) {
    Step "AI models (several GB the first time)"
    foreach ($name in @($Model, "nomic-embed-text")) {
        & $Ollama pull $name
        if ($LASTEXITCODE -eq 0) { Ok $name } else { Note "Couldn't pull $name. Run: ollama pull $name" }
    }
}

# ---- 6. Optional: the fast cloud brain -----------------------------------------------
Step "Cloud brain (optional, free)"
if ([Environment]::GetEnvironmentVariable("GROQ_API_KEY", "User")) {
    Ok "A Groq key is already saved"
} elseif (Ask "Add a free Groq key now? Answers become about 1 second instead of 5 to 60 (get one at console.groq.com/keys)") {
    $k = Read-Host "   Paste your Groq key (nothing shows while you paste)" -AsSecureString
    $plain = [Net.NetworkCredential]::new("", $k).Password.Trim()
    if ($plain) { [Environment]::SetEnvironmentVariable("GROQ_API_KEY", $plain, "User"); $env:GROQ_API_KEY = $plain; Ok "Key saved" }
    Remove-Variable k, plain
} else {
    Note "Skipped. Jarvis runs fully on this laptop. Add a key later with docs/SETUP-KEYS.md."
}

# ---- 7. Start with Windows ----------------------------------------------------------------
Step "Start with Windows"
if (Get-ScheduledTask -TaskName "Jarvis Assistant" -ErrorAction SilentlyContinue) {
    Ok "Already starts when you sign in"
} elseif (Ask "Start Jarvis automatically when you sign in?") {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\install_startup.ps1")
} else {
    Note "Skipped. Start it any time with start-jarvis-web.bat."
}

# ---- 8. Check and start --------------------------------------------------------------
Step "Health check"
& (Join-Path $Root ".venv\Scripts\jarvis.exe") --doctor
Write-Host "`nAll set. Start Jarvis with start-jarvis-web.bat (it opens in your browser)." -ForegroundColor Green
Write-Host "Phone, Telegram and alerts: docs/PHONE.md. Gmail: docs/EMAIL.md." -ForegroundColor Green
if (-not $NoPrompt -and (Ask "Start Jarvis now?")) {
    Start-Process (Join-Path $Root "start-jarvis-web.bat")
}
