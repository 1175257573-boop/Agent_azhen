@echo off
rem ============================================================
rem  Atlas Console - double-click launcher (Windows)
rem  Strips host-injected env vars that break the Electron
rem  runtime (e.g. when launched from WorkBuddy / VS Code shells),
rem  then starts the app. Close the window to stop the backend.
rem ============================================================
setlocal
set "ELECTRON_RUN_AS_NODE="
set "NODE_OPTIONS="
cd /d "%~dp0"

if not exist "node_modules\electron\dist\electron.exe" (
  echo [!] Electron is not installed yet.
  echo     Run:  npm install
  echo     (in this folder, with network access)
  pause
  exit /b 1
)

"node_modules\electron\dist\electron.exe" .
endlocal
