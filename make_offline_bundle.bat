@echo off
rem Builds offline\ so setup.bat works on PCs WITHOUT internet:
rem   offline\uv.exe, offline\godot.zip, and a filled .tools\uv-cache + .tools\python + .browsers.
rem Copy the WHOLE tool folder (with .tools, .browsers, offline) to a USB stick / network share.
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
mkdir offline 2>nul
where uv >nul 2>nul && for /f "delims=" %%U in ('where uv') do copy /y "%%U" offline\ >nul
if exist "%UV_INSTALL_DIR%\uvx.exe" copy /y "%UV_INSTALL_DIR%\uvx.exe" offline\ >nul
rem Python must live INSIDE the folder (.tools\python) or the USB copy has none.
uv python install 3.12
if errorlevel 1 ( echo !! uv python install failed - need internet. & pause & exit /b 1 )
findstr /i /c:"%MC_HOME%\.tools\python" .venv\pyvenv.cfg >nul 2>nul
if errorlevel 1 (
  echo Rebuilding .venv on the portable Python ...
  rmdir /s /q .venv 2>nul
)
uv sync --python 3.12
if errorlevel 1 ( echo !! uv sync failed. & pause & exit /b 1 )
rem NOTE: never run 'uv cache prune --ci' here - it deletes the downloaded wheels the offline install needs.
rem prove the offline path works: rebuild the venv from the cache only (this is what a USB copy does)
rmdir /s /q .venv 2>nul
uv sync --frozen --python 3.12 --offline
if errorlevel 1 ( echo !! Offline reinstall from the cache FAILED - the bundle would not work offline. & pause & exit /b 1 )
"%MC_PY%" -m playwright install chromium
if not exist offline\godot.zip (
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://github.com/godotengine/godot/releases/download/%GODOT_VERSION%/Godot_v%GODOT_VERSION%_win64.exe.zip' -OutFile '%MC_HOME%\offline\godot.zip'"
)
if not exist offline\cloudflared.exe (
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%MC_HOME%\offline\cloudflared.exe'"
)
rem test the copy works: selftest uses only local files
"%MC_PY%" -m multiconnect.cli selftest
echo.
echo Offline bundle ready. Copy this whole folder (it is big: Python + Chromium + Godot).
echo Do NOT copy state\ or config\accounts.yaml to untrusted places (login cookies live in state\secrets).
pause
