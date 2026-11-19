# MultiConnect - developer notes (this repo is the TOOL, not the game)

The game lives in a separate folder (`game_dir` in `config/commander.yaml`). When commanding the hive,
open Claude Code **in the game folder**: it gets `CLAUDE.md`/`AGENTS.md` from `templates/commander_playbook.md`.

Language: the user writes Polish; reply in Polish. Code, comments, prompts, docs for agents: English.

## Layout
- `multiconnect/coordinator/` - FastAPI app (`app.py`), state + scheduling (`hub.py`), game folder ops
  (`gameops.py`: integrate, git, backups, assets, HANDOFF), SQLite (`db.py`), web panel (`panel/`, vanilla JS, EN/PL).
- `multiconnect/worker/` - one per PC: WebSocket client (`main.py`), shared Chromium + contexts (`browser.py`),
  agent bridge loop (`agent.py`), sandbox permissions (`sandbox.py`), YAML-driven chat driver (`providers/generic.py`).
- `multiconnect/mcp_server/server.py` - thin MCP client over `/api/c/*` (all state in the coordinator).
- `multiconnect/common/` - config, text protocol parser (`@@WRITE/@@REPLACE/@@READ/@@LIST/@@CHECK/@@RUN/@@ASK/@@DONE`),
  Godot runner, token estimate.
- `config/providers/*.yaml` - chat sites (URLs, selectors, patterns). Panel overrides are stored in the DB.
- `prompts/` - English prompt templates sent to agents. `templates/` - sandbox project + check.gd + playbook.
- `multiconnect/mockchat/` - fake chat site for end-to-end tests (`provider: mock`).

## Run / test
- `uv sync --extra dev`, `uv run pytest -q`
- `uv run multiconnect mockchat` + `commander` + `worker` with mock accounts = full local pipeline.
- `multiconnect selftest [--manual] [--keep]` = the same pipeline automated in `state/selftest/<ts>` (MC_STATE_DIR/MC_AGENTS_DIR).
- Fallbacks (see `RUNBOOK.md`, Polish): `netcheck`, `solo`, `tunnel.bat` (cloudflared), `restore-hive` (hive-*.zip =
  DB + state/secrets + config, mirrored to a worker), provider `manual` (human relay at `/manual/inbox`, served by
  `mockchat/server.py` inside every worker on 127.0.0.1:8790).
