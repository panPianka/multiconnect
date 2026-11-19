@echo off
rem Makes sure .venv works. A copied/moved tool folder breaks the venv (absolute paths inside):
rem it is rebuilt from the local uv cache, without internet when possible.
if exist "%MC_PY%" (
  "%MC_PY%" -c "import multiconnect, playwright, fastapi, httpx" >nul 2>nul
  if not errorlevel 1 exit /b 0
)
where uv >nul 2>nul
if errorlevel 1 (
  echo !! Python environment missing and uv not found. Run setup.bat first.
  exit /b 1
)
echo Python environment missing or broken (folder copied/moved?) - repairing ...
uv sync --frozen --python 3.12 --offline >nul 2>nul
if errorlevel 1 uv sync --frozen --python 3.12
"%MC_PY%" -c "import multiconnect, playwright, fastapi, httpx" >nul 2>nul
if errorlevel 1 (
  echo !! Repair failed. Run setup.bat - needs internet or the offline\ bundle.
  exit /b 1
)
echo Repaired.
exit /b 0
