@echo off
rem Fake chat site for testing the whole pipeline without real accounts (provider: mock).
title MultiConnect MOCKCHAT
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
"%MC_PY%" -m multiconnect.cli mockchat %*
