@echo off
rem Usage: login.bat <account-id>   (e.g. login.bat anna-gpt)
rem Opens a browser window; log in by hand, then close it. The login is saved on the commander.
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
if "%~1"=="" (
  set /p ACC=Account id to log in:
) else (
  set "ACC=%~1"
)
"%MC_PY%" -m multiconnect.cli login %ACC%
pause
