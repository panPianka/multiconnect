@echo off
setlocal EnableDelayedExpansion
title MultiConnect - GitHub setup (one time)
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
rem One-time: GitHub CLI (portable, no admin) -> login -> git identity -> first commit -> PRIVATE repo -> push.
rem Usage: github_setup.bat [repo-name]      (default: multiconnect)
set "REPO=%~1"
if "%REPO%"=="" set "REPO=multiconnect"

rem ---------------------------------------------------------------- 1. gh (GitHub CLI)
set "GH="
for /f "delims=" %%G in ('where gh 2^>nul') do if not defined GH set "GH=%%G"
if not defined GH if exist "%MC_HOME%\.tools\gh\bin\gh.exe" set "GH=%MC_HOME%\.tools\gh\bin\gh.exe"
if not defined GH (
  echo [1/5] Downloading GitHub CLI ^(official cli/cli release, portable^) ...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; $r=Invoke-RestMethod 'https://api.github.com/repos/cli/cli/releases/latest'; $a=$r.assets | ? name -like '*windows_amd64.zip' | select -First 1; Invoke-WebRequest $a.browser_download_url -OutFile '%MC_HOME%\.tools\gh.zip'; Expand-Archive -Force '%MC_HOME%\.tools\gh.zip' '%MC_HOME%\.tools\gh'; Remove-Item '%MC_HOME%\.tools\gh.zip'"
  if exist "%MC_HOME%\.tools\gh\bin\gh.exe" set "GH=%MC_HOME%\.tools\gh\bin\gh.exe"
)
if not defined GH ( echo !! GitHub CLI missing. Use the web way in docs\01_GITHUB.md. & pause & exit /b 1 )
echo [1/5] gh: %GH%

rem ---------------------------------------------------------------- 2. login
"%GH%" auth status >nul 2>nul
if errorlevel 1 (
  echo [2/5] Log in to GitHub - a code appears, the browser opens, paste the code there.
  "%GH%" auth login --hostname github.com --git-protocol https --web
  "%GH%" auth status >nul 2>nul || ( echo !! Login failed. & pause & exit /b 1 )
) else ( echo [2/5] Already logged in to GitHub. )
"%GH%" auth setup-git >nul 2>nul

rem ---------------------------------------------------------------- 3. git identity (this repo only)
git config user.name >nul 2>nul
if errorlevel 1 (
  set /p "GN=[3/5] Your name for commits: "
  git config user.name "!GN!"
)
git config user.email >nul 2>nul
if errorlevel 1 (
  set /p "GE=      Your e-mail for commits (or the noreply address from GitHub settings): "
  git config user.email "!GE!"
)

rem ---------------------------------------------------------------- 4. first commit on main
git rev-parse --verify HEAD >nul 2>nul
if errorlevel 1 (
  git checkout -B main >nul 2>nul
  git add -A
  echo [4/5] Files that go to GitHub ^(login data in state\ and config\*.yaml are ignored^):
  git status --short
  git commit -q -m "MultiConnect: initial version" || ( echo !! commit failed & pause & exit /b 1 )
) else ( echo [4/5] Repo already has commits. )

rem ---------------------------------------------------------------- 5. private repo + push
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [5/5] Creating PRIVATE repo %REPO% on GitHub and pushing ...
  "%GH%" repo create "%REPO%" --private --source . --remote origin --push || ( echo !! repo create failed ^(name taken?^) - try: github_setup.bat other-name & pause & exit /b 1 )
) else (
  echo [5/5] Remote exists - pushing ...
  git push -u origin HEAD
)
echo.
"%GH%" repo view --json url -q .url
echo Done. Add teammates: GitHub repo ^> Settings ^> Collaborators. Release: make_release.bat --publish
pause
