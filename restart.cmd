@echo off
REM 终端里用这个；想完全无窗双击请用 restart.vbs
cd /d "%~dp0"
if "%~1"=="" (
  call "%~dp0scripts\dev.cmd" restart
) else (
  call "%~dp0scripts\dev.cmd" %*
)
