@echo off
rem Double-click to start Jarvis. Starts Ollama if it isn't running yet.
cd /d "%~dp0"
tasklist /fi "imagename eq ollama.exe" | find /i "ollama.exe" >nul || start "" /min ollama serve
timeout /t 3 /nobreak >nul
".venv\Scripts\jarvis.exe" %*
pause
