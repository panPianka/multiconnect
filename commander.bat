@echo off
title MultiConnect COMMANDER
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
:loop
"%MC_PY%" -m multiconnect.cli commander %*
if errorlevel 3 if not errorlevel 4 goto loop
echo Commander stopped (exit %errorlevel%). Restarting in 5 s - close this window to quit.
timeout /t 5 >nul
goto loop
