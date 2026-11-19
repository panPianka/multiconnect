@echo off
title MultiConnect SOLO (commander + worker on this PC)
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
rem Plan D: no network needed. Commander + worker + agents all on this PC (127.0.0.1 only).
"%MC_PY%" -m multiconnect.cli solo %*
pause
