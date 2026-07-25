@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 找不到 .venv\Scripts\python.exe
  exit /b 1
)
".venv\Scripts\python.exe" "scripts\dev_win.py" %*
exit /b %ERRORLEVEL%
