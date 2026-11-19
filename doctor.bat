@echo off
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
"%MC_PY%" -m multiconnect.cli doctor
pause
