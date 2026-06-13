@echo off
title LMStudioClaw
cd /d "%~dp0"

REM --- Stop any running instance so we relaunch cleanly (frees the web port) ---
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*lmstudioclaw.cli*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

REM --- Build the React frontend so the served UI is always current ---
where npm >nul 2>&1
if %ERRORLEVEL%==0 (
  pushd frontend
  if not exist node_modules (
    echo Installing frontend dependencies...
    call npm install
  )
  echo Building UI...
  call npm run build
  popd
) else (
  echo npm not found on PATH - serving the existing build in lmstudioclaw\web\static.
)

REM --- Launch the controller: it serves the API, WebSockets, and the built React UI ---
start "" "%~dp0venv\Scripts\pythonw.exe" -m lmstudioclaw.cli
