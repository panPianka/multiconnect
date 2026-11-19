@echo off
setlocal
title MultiConnect setup
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
echo ================================================================
echo  MultiConnect setup  (folder: %MC_HOME%)
echo  Installs uv, Python, packages, Chromium and Godot INTO this folder.
echo ================================================================

rem ---------------------------------------------------------------- 1. uv
where uv >nul 2>nul
if errorlevel 1 (
  if exist "%MC_HOME%\offline\uv.exe" (
    echo [1/5] Copying uv from offline\ ...
    mkdir "%UV_INSTALL_DIR%" 2>nul
    copy /y "%MC_HOME%\offline\uv*.exe" "%UV_INSTALL_DIR%\" >nul
  ) else (
    echo [1/5] Downloading uv ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%UV_INSTALL_DIR%'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"
  )
) else (
  echo [1/5] uv found.
)
where uv >nul 2>nul
if errorlevel 1 (
  echo !! uv could not be installed. Copy uv.exe into %UV_INSTALL_DIR% by hand and run setup again.
  pause & exit /b 1
)

rem ---------------------------------------------------------------- 2. Python + packages
echo [2/5] Python 3.12 + packages ...
uv sync --python 3.12
if errorlevel 1 (
  echo     online install failed - trying offline cache ...
  uv sync --python 3.12 --offline
  if errorlevel 1 (
    echo !! Package install failed. No internet and no offline cache. See README: offline bundle.
    pause & exit /b 1
  )
)

rem ---------------------------------------------------------------- 3. Chromium
echo [3/5] Chromium for the agents ...
dir /b "%PLAYWRIGHT_BROWSERS_PATH%\chromium-*" >nul 2>nul
if errorlevel 1 (
  "%MC_PY%" -m playwright install chromium
  if errorlevel 1 echo !! Chromium download failed. Workers can use Edge instead: set browser_channel: msedge in config\worker.yaml
) else (
  echo     already installed.
)

rem ---------------------------------------------------------------- 4. Godot
echo [4/5] Godot %GODOT_VERSION% (console build, for checking agent code) ...
"%MC_PY%" -c "from multiconnect.common.godot import find_godot; import sys; sys.exit(0 if find_godot() else 1)"
if errorlevel 1 (
  if exist "%MC_HOME%\offline\godot.zip" (
    powershell -NoProfile -Command "Expand-Archive -Force '%MC_HOME%\offline\godot.zip' '%MC_HOME%\.godot_bin'"
  ) else (
    powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; $u='https://github.com/godotengine/godot/releases/download/%GODOT_VERSION%/Godot_v%GODOT_VERSION%_win64.exe.zip'; New-Item -ItemType Directory -Force '%MC_HOME%\.godot_bin' | Out-Null; Invoke-WebRequest $u -OutFile '%MC_HOME%\.godot_bin\godot.zip'; Expand-Archive -Force '%MC_HOME%\.godot_bin\godot.zip' '%MC_HOME%\.godot_bin'; Remove-Item '%MC_HOME%\.godot_bin\godot.zip'"
  )
  "%MC_PY%" -c "from multiconnect.common.godot import find_godot; import sys; sys.exit(0 if find_godot() else 1)"
  if errorlevel 1 echo !! Godot not found. Put Godot_v4.x_win64_console.exe into %MC_HOME%\.godot_bin\ by hand.
) else (
  echo     Godot found.
)

rem ---------------------------------------------------------------- 5. config wizard
echo [5/5] Configuration wizard
"%MC_PY%" -m multiconnect.cli setup %*
echo.
"%MC_PY%" -m multiconnect.cli doctor
echo.
echo Done. Commander PC: commander.bat (+ connect_claude.bat once). Every PC: worker.bat
pause
