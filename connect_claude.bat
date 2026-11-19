@echo off
rem Registers the MultiConnect MCP server in Claude Code (user scope) and writes a Codex config snippet.
call "%~dp0scripts\_env.bat"
cd /d "%MC_HOME%"
if not exist "%MC_PY%" ( echo Run setup.bat first. & pause & exit /b 1 )

where claude >nul 2>nul
if errorlevel 1 (
  echo Claude Code CLI not found in PATH - skipping. Install it, then run this again.
) else (
  claude mcp remove multiconnect -s user >nul 2>nul
  claude mcp add multiconnect -s user -- "%MC_PY%" -m multiconnect.cli mcp
  echo Claude Code: MCP server "multiconnect" registered. Check with: claude mcp list
)

set "SNIP=%MC_HOME%\codex_mcp_snippet.toml"
set "PYESC=%MC_PY:\=\\%"
> "%SNIP%" echo [mcp_servers.multiconnect]
>> "%SNIP%" echo command = "%PYESC%"
>> "%SNIP%" echo args = ["-m", "multiconnect.cli", "mcp"]
>> "%SNIP%" echo startup_timeout_sec = 30
>> "%SNIP%" echo tool_timeout_sec = 600
echo Codex: paste %SNIP% into %USERPROFILE%\.codex\config.toml
echo.
echo Then open Claude Code (or Codex) IN THE GAME FOLDER - it contains CLAUDE.md / AGENTS.md with the playbook.
pause
