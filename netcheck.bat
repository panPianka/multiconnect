@echo off
title MultiConnect NETCHECK
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
rem  netcheck.bat --listen        on the PC that should become the commander
rem  netcheck.bat 192.168.1.10    on a worker PC (IP of the commander)
"%MC_PY%" -m multiconnect.cli netcheck %*
pause
