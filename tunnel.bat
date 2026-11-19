@echo off
title MultiConnect TUNNEL (plan C)
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
rem Plan C when the LAN blocks connections TO the commander: an outbound-only internet tunnel.
rem Run on the COMMANDER PC next to commander.bat. Workers then use the printed https URL:
rem     set MC_COORDINATOR=https://xxxx.trycloudflare.com  ^&  worker.bat
rem (or put it into coordinator_url in config\worker.yaml). The team token still protects everything.
set "CF=%MC_HOME%\.tools\cloudflared.exe"
if not exist "%CF%" if exist "%MC_HOME%\offline\cloudflared.exe" copy /y "%MC_HOME%\offline\cloudflared.exe" "%CF%" >nul
if not exist "%CF%" (
  echo Downloading cloudflared ^(official Cloudflare release, ~60 MB^) ...
  mkdir "%MC_HOME%\.tools" 2>nul
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%CF%'"
)
if not exist "%CF%" ( echo !! cloudflared missing. Plan D: solo.bat on every PC. & pause & exit /b 1 )
set PORT=8765
if not "%1"=="" set PORT=%1
echo.
echo Look for a line with https://....trycloudflare.com below and give that URL to the worker PCs.
echo.
"%CF%" tunnel --no-autoupdate --url http://127.0.0.1:%PORT%
pause
