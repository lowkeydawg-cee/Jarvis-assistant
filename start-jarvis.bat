@echo off
rem Start Jarvis in voice mode. Double-click this file, or run it from any terminal.
rem Pass extra flags through, e.g.  start-jarvis.bat --text
cd /d "%~dp0"
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
rem Make sure Ollama is up (it starts with Windows, but may have been closed).
curl -s -o nul http://localhost:11434/api/tags || (
  echo Starting Ollama...
  start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
  timeout /t 10 /nobreak >nul
)
echo Starting Jarvis. It takes a minute or two to load on this laptop; wait for "[listening]".
".venv\Scripts\jarvis.exe" %*
echo.
pause
