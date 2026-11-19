@echo off
title MultiConnect release
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
rem Builds dist\multiconnect-vX-source.zip (+ offline.zip if make_offline_bundle.bat was run).
rem   make_release.bat              build only
rem   make_release.bat --publish    build + tag + GitHub release (needs gh login, see docs\02_RELEASE.md)
rem   make_release.bat --no-offline only the small source zip
rem   make_release.bat --pendrive   offline zip from the CURRENT files (no commit needed, never publishes)
"%MC_PY%" "%MC_HOME%\scripts\make_release.py" %*
pause
