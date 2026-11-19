"""MCP server for Claude Code / Codex. A thin client: all state lives in the
coordinator, so any commander (another Claude account, Codex, another PC) can take over."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..common.config import load_commander_config

_cfg = load_commander_config()
BASE = (os.environ.get("MC_COORDINATOR") or f"http://127.0.0.1:{_cfg['port']}").rstrip("/")
TOKEN = os.environ.get("MC_TOKEN") or _cfg["team_token"]

mcp = FastMCP("multiconnect", instructions=(
    "MultiConnect hive: you are the lead engineer commanding many weak browser-chat coding agents. "
    "Split work into SMALL tasks (1-3 files, <200 lines), define interfaces in the contract, dispatch, "
    "then use wait_for_events and review REPORTS (not code). Write all tasks/contract/code in English. "
    "Read the game folder's _hive/HANDOFF.md when taking over."))


async def _call(method: str, path: str, body: Any = None, params: dict | None = None, timeout: float = 60) -> Any:
    data = json.dumps(body) if body is not None else None
    headers = {"x-team-token": TOKEN, "content-type": "application/json", "x-req-size": str(len(data or ""))}
    async with httpx.AsyncClient(timeout=timeout) as c:
        try:
            r = await c.request(method, BASE + path, content=data, params=params, headers=headers)
        except httpx.ConnectError:
            return {"error": f"Coordinator not reachable at {BASE}. Start it with commander.bat."}
    try:
        out = r.json()
    except Exception:
        out = {"error": r.text[:2000]}
    if r.status_code >= 400 and isinstance(out, dict):
        out.setdefault("http_status", r.status_code)
    return out


def _j(x: Any) -> str:
    return json.dumps(x, indent=1, ensure_ascii=False, default=str)


@mcp.tool()
async def hive_overview() -> str:
    """Compact status of the whole hive: computers, agents per status/provider, every task with status,
    open agent questions, missing assets, token total, number of unseen events. Call this first."""
    return _j(await _call("GET", "/api/c/overview"))


@mcp.tool()
async def get_contract() -> str:
    """Read the architecture contract (_hive/contract.md) that every agent receives."""
    return _j(await _call("GET", "/api/c/contract"))


@mcp.tool()
async def set_contract(text: str) -> str:
    """Replace the architecture contract (markdown, English). Include: folder layout, autoloads and their API,
    class names, signals, node/scene structure, groups, collision layers, input actions, coding conventions.
    Every agent gets it in full, so keep it precise and compact (ideally < 2500 words)."""
    return _j(await _call("POST", "/api/c/contract", {"text": text}))


@mcp.tool()
async def dispatch_tasks(tasks: list[dict]) -> str:
    """Queue tasks for agents. Each task dict:
    {id?: "T010", title, goal (3-10 sentences), files_to_create: [paths], files_to_modify?: [paths],
     interface?: exact signatures to implement, context_files?: [paths to @@READ], assets?: [descriptions],
     acceptance?: [criteria], depends_on?: [task ids that must be INTEGRATED first], priority?: 1(high)-5,
     provider_pref?: ["chatgpt","deepseek",...], redundancy?: 1-3 (parallel attempts), notes?, max_rounds?}
    Keep each task tiny (1-3 files). Independent tasks run in parallel on all free agents."""
    return _j(await _call("POST", "/api/c/dispatch", {"tasks": tasks}))


@mcp.tool()
async def wait_for_events(timeout_s: int = 180) -> str:
    """Block until something needs you (task finished/failed, agent question, asset delivered, team message)
    or the timeout passes. Returns the batch of new events (each is delivered once). Use this instead of polling."""
    return _j(await _call("GET", "/api/c/events", params={"timeout": timeout_s}, timeout=timeout_s + 30))


@mcp.tool()
async def task_status(task_id: str) -> str:
    """Details of one task: spec, status, all chats (agent, rounds, tokens), result versions, questions."""
    return _j(await _call("GET", f"/api/c/task/{task_id}"))


@mcp.tool()
async def get_report(task_id: str, level: str = "short", chat_id: str = "", version: int = 0) -> str:
    """The agent's REPORT.md for a finished task plus check result and file list.
    level="short" (Summary, Files, Public API, Integration, Assets, Limitations) or "full"."""
    return _j(await _call("GET", f"/api/c/report/{task_id}", params={"level": level, "chat": chat_id, "version": version}))


@mcp.tool()
async def get_file(path: str, task_id: str = "", source: str = "result", chat_id: str = "", version: int = 0) -> str:
    """Read one file. source="result": from a task's result (needs task_id); source="game": from the game folder.
    Only read code when a report is unclear - reports are designed to save your context."""
    r = await _call("GET", "/api/c/file", params={"task_id": task_id, "path": path, "source": source,
                                                  "chat": chat_id, "version": version})
    return r.get("text") if isinstance(r, dict) and "text" in r else _j(r)


@mcp.tool()
async def followup(task_id: str, message: str, chat_id: str = "") -> str:
    """Send a message (fix request, extra requirement, question) to the SAME chat that did the task,
    so the agent keeps its context. The agent re-delivers a new result version when done."""
    return _j(await _call("POST", "/api/c/followup", {"task_id": task_id, "message": message, "chat_id": chat_id or None}))


@mcp.tool()
async def answer_question(answer: str, question_id: int = 0, chat_id: str = "") -> str:
    """Answer an agent's @@ASK question (question_id from the event). The agent is waiting."""
    return _j(await _call("POST", "/api/c/answer", {"question_id": question_id or None, "answer": answer,
                                                    "chat_id": chat_id or None}))


@mcp.tool()
async def reassign_task(task_id: str, note: str = "") -> str:
    """Cancel current attempts and give the task to a different agent (optionally with an extra note)."""
    return _j(await _call("POST", "/api/c/task_action", {"action": "reassign", "task_id": task_id, "note": note}))


@mcp.tool()
async def cancel_task(task_id: str) -> str:
    """Cancel a task and stop its chats."""
    return _j(await _call("POST", "/api/c/task_action", {"action": "cancel", "task_id": task_id}))


@mcp.tool()
async def integrate(task_id: str, chat_id: str = "", version: int = 0, force: bool = False,
                    include_tests: bool = False) -> str:
    """Copy a task's result files into the game (git commit before/after), save its report to
    _hive/reports/, run a Godot check on the files and unblock dependent tasks.
    Refuses on file-ownership conflicts unless force=true."""
    return _j(await _call("POST", "/api/c/integrate", {"task_id": task_id, "chat_id": chat_id or None,
                                                       "version": version or None, "force": force,
                                                       "include_tests": include_tests}, timeout=400))


@mcp.tool()
async def godot_check(files: list[str] | None = None) -> str:
    """Compile-check the game with Godot headless (all .gd/.tscn, or the given files). Returns errors."""
    return _j(await _call("POST", "/api/c/check", {"files": files}, timeout=400))


@mcp.tool()
async def godot_run(scene: str, seconds: float = 5) -> str:
    """Run a scene of the game headless for N seconds and return errors + output tail."""
    return _j(await _call("POST", "/api/c/run", {"scene": scene, "seconds": seconds}, timeout=seconds + 120))


@mcp.tool()
async def request_assets(assets: list[dict]) -> str:
    """Ask the human team for art/sound. Each: {path: "assets/sprites/player_run.png", name?, kind?: image|sound|font,
    width?, height? (one frame, pixels), frames? (horizontal strip), spec: "16x16 pixel art, 6 frame run cycle...",
    priority?: 1-3, for_task?}. Images with width/height get a colored PLACEHOLDER immediately so work never blocks;
    the real file replaces it later and you get an 'asset delivered' event."""
    return _j(await _call("POST", "/api/c/assets", {"assets": assets}))


@mcp.tool()
async def list_assets() -> str:
    """All asset requests with status (requested/placeholder/claimed/delivered), sizes and warnings."""
    return _j(await _call("GET", "/api/c/assets"))


@mcp.tool()
async def game_tree() -> str:
    """List files in the game folder with the task that owns each file."""
    return _j(await _call("GET", "/api/c/game/tree"))


@mcp.tool()
async def write_game_file(path: str, text: str) -> str:
    """Write a file in the game folder (for glue code you write yourself, e.g. main scenes, autoloads).
    If you run on the commander PC you can also edit the game folder directly."""
    return _j(await _call("POST", "/api/c/game/file", {"path": path, "text": text}))


@mcp.tool()
async def set_notes(brief: str | None = None, next_steps: str | None = None, decision: str | None = None) -> str:
    """Hand-off memory. brief: the game concept; next_steps: what to do next (keep it updated!);
    decision: append one important decision. All end up in _hive/HANDOFF.md for the next commander."""
    return _j(await _call("POST", "/api/c/notes", {"brief": brief, "next_steps": next_steps, "decision": decision}))


@mcp.tool()
async def snapshot(label: str = "snapshot") -> str:
    """Force a zip backup + git commit and regenerate HANDOFF.md. Do this before your limit runs out."""
    return _j(await _call("POST", "/api/c/snapshot", {"label": label}, timeout=300))


@mcp.tool()
async def message_team(text: str, urgent: bool = False) -> str:
    """Show a message to the humans on the panel (write it in Polish). urgent=true plays an alert sound."""
    return _j(await _call("POST", "/api/c/message_team", {"text": text, "urgent": urgent}))


@mcp.tool()
async def list_agents() -> str:
    """All agent accounts with provider, status, computer and success counters."""
    return _j(await _call("GET", "/api/c/agents"))


def run_mcp() -> None:
    mcp.run()
