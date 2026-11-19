@echo off
title MultiConnect SELFTEST
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
echo Full pipeline test on THIS PC with a fake chat site (2-3 min). Touches nothing real.
echo Add "--mode mini" to watch the browser windows.
"%MC_PY%" -m multiconnect.cli selftest %*
pause
