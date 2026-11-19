@echo off
rem Shared environment: everything lives INSIDE the tool folder (portable, no admin rights).
set "MC_HOME=%~dp0.."
for %%I in ("%MC_HOME%") do set "MC_HOME=%%~fI"
set "UV_INSTALL_DIR=%MC_HOME%\.tools\uv"
set "UV_PYTHON_INSTALL_DIR=%MC_HOME%\.tools\python"
set "UV_CACHE_DIR=%MC_HOME%\.tools\uv-cache"
set "UV_NO_MODIFY_PATH=1"
rem Python itself lives in .tools\python too, so the whole folder can be copied to a USB stick
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_LINK_MODE=copy"
set "PLAYWRIGHT_BROWSERS_PATH=%MC_HOME%\.browsers"
set "PYTHONUTF8=1"
set "PATH=%UV_INSTALL_DIR%;%PATH%"
set "MC_PY=%MC_HOME%\.venv\Scripts\python.exe"
if not defined GODOT_VERSION set "GODOT_VERSION=4.3-stable"
