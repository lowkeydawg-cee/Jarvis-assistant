@echo off
rem Start Jarvis in your browser. Double-click this file.
rem It opens http://localhost:8765 in your default browser. Edge has the most natural voices.
cd /d "%~dp0"
rem Ollama is the private fallback brain. Start it if it isn't running.
curl -s -o nul http://localhost:11434/api/tags || (
  echo Starting Ollama...
  start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
  timeout /t 5 /nobreak >nul
)
if "%GROQ_API_KEY%"=="" echo No GROQ_API_KEY set, so Jarvis will think locally. See README for the free key.
".venv\Scripts\jarvis.exe" --web %*
echo.
pause
