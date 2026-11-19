@echo off
title MultiConnect WORKER
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
:loop
"%MC_PY%" -m multiconnect.cli worker %*
rem exit code 3 = restart requested from the panel; anything else = crash -> restart after a pause
if errorlevel 3 if not errorlevel 4 goto loop
echo Worker stopped (exit %errorlevel%). Restarting in 8 s - close this window to quit.
timeout /t 8 >nul
goto loop
