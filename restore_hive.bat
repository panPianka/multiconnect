@echo off
title MultiConnect restore hive (move the commander to this PC)
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
call "%MC_HOME%\scripts\_ensure_py.bat" || ( pause & exit /b 1 )
rem Restores DB + logins + config from the newest hive-*.zip (mirror_backups\ or backup_dir). Add --game to also unpack the game.
"%MC_PY%" -m multiconnect.cli restore-hive %*
pause
