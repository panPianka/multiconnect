"""HTTP/WebSocket API: workers (/ws/worker, /api/w), panel (/api/p) and Claude via MCP (/api/c)."""
from __future__ import annotations

import asyncio
import io
import json
import re
import socket
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..common.config import STATE_DIR, load_commander_config
from ..common.tokens import estimate_tokens
from .hub import ACTIVE_CHAT, SECRETS_DIR, Hub, WorkerConn

PANEL_DIR = Path(__file__).parent / "panel"
VERSION = "0.1.0"


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or load_commander_config()
    app = FastAPI(title="MultiConnect", docs_url=None, redoc_url=None)
    hub = Hub(cfg)
    app.state.hub = hub
    token = cfg["team_token"]

    @app.on_event("startup")
    async def _startup():
        asyncio.create_task(hub.loop())
        asyncio.create_task(discovery_beacon(cfg))
        hub.ev("info", "coordinator", f"Coordinator started on port {cfg['port']}")

    # ------------------------------------------------------------------ auth
    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/ping":
            t = request.headers.get("x-team-token") or request.query_params.get("token") or request.cookies.get("mc_token")
            if t != token:
                return JSONResponse({"error": "bad team token"}, status_code=401)
        resp = await call_next(request)
        if path.startswith("/api/c/") and not request.headers.get("x-from-panel"):
            # rough Claude token accounting: what Claude sends + what it reads back
            tin = int(request.headers.get("x-req-size", "0") or 0) // 4
            size = int(resp.headers.get("content-length", "0") or 0)
            hub.log_claude_tokens(tin, size // 4)
        return resp

    @app.get("/api/ping")
    async def ping():
        return {"ok": True, "name": "multiconnect", "version": VERSION}

    # ------------------------------------------------------------------ worker websocket
    @app.websocket("/ws/worker")
    async def ws_worker(ws: WebSocket):
        if ws.query_params.get("token") != token:
            await ws.close(code=4001)
            return
        await ws.accept()
        conn = None
        try:
            hello = json.loads(await ws.receive_text())
            if hello.get("type") != "hello":
                await ws.close(code=4002)
                return
            conn = WorkerConn(hello["host"], ws, ws.client.host if ws.client else "?")
            await hub.worker_connected(conn, hello)
            while True:
                m = json.loads(await ws.receive_text())
                try:
                    await hub.on_worker_message(conn, m)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    hub.ev("error", "coordinator", f"error handling {m.get('type')}: {e}", host=conn.name)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            hub.ev("warn", "host", f"worker connection error: {e}")
        finally:
            if conn:
                await hub.worker_disconnected(conn)

    # ------------------------------------------------------------------ worker http (files)
    @app.get("/api/w/bundle/{h}")
    async def get_bundle(h: str):
        p = STATE_DIR / "bundles" / f"{Path(h).name}.zip"
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="application/zip")

    @app.get("/api/w/result/{chat_id}/{version}")
    async def get_result(chat_id: str, version: int):
        rz = hub.game.result_zip(chat_id, version)
        if not rz:
            raise HTTPException(404)
        return FileResponse(rz[0], media_type="application/zip")

    @app.post("/api/w/result")
    async def post_result(meta: str = Form(...), file: UploadFile = File(...)):
        m = json.loads(meta)
        chat_id, version = m["chat_id"], int(m["version"])
        data = await file.read()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                manifest = json.loads(z.read("_meta/manifest.json"))
        except Exception as e:
            raise HTTPException(400, f"bad zip: {e}")
        dst = STATE_DIR / "results" / f"{chat_id}_v{version}.zip"
        dst.write_bytes(data)
        await hub.store_result(chat_id, version, dst, manifest)
        return {"ok": True}

    @app.post("/api/w/state/{account}")
    async def post_state(account: str, request: Request):
        state = await request.json()
        if not hub.db.one("SELECT id FROM accounts WHERE id=?", (account,)):
            raise HTTPException(404, "unknown account")
        hub.save_storage_state(account, state)
        return {"ok": True}

    @app.get("/api/w/state/{account}")
    async def get_state(account: str):
        p = SECRETS_DIR / f"{Path(account).name}.json"
        if not p.exists():
            raise HTTPException(404)
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))

    @app.get("/api/w/providers")
    async def get_providers():
        return hub.providers

    @app.get("/api/w/accounts")
    async def get_accounts():
        return hub.db.q("SELECT id, provider, owner, label, has_state FROM accounts WHERE in_config=1 ORDER BY id")

    @app.post("/api/w/screenshot/{account}")
    async def post_screenshot(account: str, request: Request):
        data = await request.body()
        d = STATE_DIR / "screens"
        d.mkdir(exist_ok=True)
        (d / f"{Path(account).name}.jpg").write_bytes(data)
        return {"ok": True}

    @app.get("/api/w/backup/{name}")
    async def get_backup(name: str):
        p = hub.game.backup_dir / Path(name).name
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="application/zip")

    # ------------------------------------------------------------------ panel
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (PANEL_DIR / "index.html").read_text(encoding="utf-8")

    app.mount("/panel", StaticFiles(directory=str(PANEL_DIR)), name="panel")

    @app.get("/api/p/state")
    async def panel_state():
        return build_state(hub)

    @app.get("/api/p/events")
    async def panel_events(after: int = 0, level: str = "", q: str = "", limit: int = 200, account: str = "",
                           host: str = "", task: str = ""):
        sql = "SELECT * FROM events WHERE id>?"
        args: list = [after]
        if level:
            sql += " AND level=?"
            args.append(level)
        for col, val in (("account", account), ("host", host), ("task", task)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if q:
            sql += " AND message LIKE ?"
            args.append(f"%{q}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 1000))
        return hub.db.q(sql, args)

    @app.get("/api/p/tokens")
    async def panel_tokens(minutes: int = 120):
        return token_stats(hub, minutes)

    @app.get("/api/p/task/{task_id}")
    async def panel_task(task_id: str):
        return task_detail(hub, task_id, full=True)

    @app.get("/api/p/chat/{chat_id}/transcript")
    async def panel_transcript(chat_id: str):
        chat = hub.db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not chat:
            raise HTTPException(404)
        if chat["host"] and chat["host"] in hub.conns:
            r = await hub.request(chat["host"], {"type": "get_transcript", "chat_id": chat_id}, timeout=10)
            if r.get("ok"):
                return {"text": r.get("text", "")}
        rz = hub.game.result_zip(chat_id)
        if rz:
            txt = hub.game.read_result_file(chat_id, rz[1].get("version", 1), "_meta/transcript.md")
            if txt:
                return {"text": txt}
        return {"text": "(transcript not available - the computer is offline)"}

    @app.post("/api/p/host/{name}")
    async def panel_host(name: str, request: Request):
        body = await request.json()
        h = hub.db.one("SELECT * FROM hosts WHERE name=?", (name,))
        if not h:
            raise HTTPException(404)
        if "delta" in body or "desired" in body:
            cur = hub.desired_for(h)
            new = int(body["desired"]) if "desired" in body else cur + int(body["delta"])
            new = max(0, min(40, new))
            hub.db.update("hosts", "name", name, desired_agents=new)
            hub.ev("info", "host", f"{name}: agents set to {new}", host=name)
        if "mode" in body:
            hub.db.update("hosts", "name", name, mode=body["mode"])
            await hub.send(name, {"type": "set_mode", "mode": body["mode"]})
        if body.get("action") in ("restart", "stop_all", "forget"):
            if body["action"] == "forget":
                hub.db.x("DELETE FROM hosts WHERE name=?", (name,))
            else:
                await hub.send(name, {"type": body["action"]})
        await hub.allocate()
        return {"ok": True}

    @app.post("/api/p/agent/{account}")
    async def panel_agent(account: str, request: Request):
        body = await request.json()
        action = body.get("action")
        acc = hub.db.one("SELECT * FROM accounts WHERE id=?", (account,))
        if not acc:
            raise HTTPException(404)
        paused = set(hub.db.kv_get("paused_accounts", []))
        if action == "pause":
            paused.add(account)
        elif action == "resume":
            paused.discard(account)
        elif action == "enable":
            hub.db.update("accounts", "id", account, enabled=1)
        elif action == "disable":
            hub.db.update("accounts", "id", account, enabled=0)
        elif action == "clear_cooldown":
            hub.db.update("accounts", "id", account, cooldown_until=0)
            if acc["host"]:
                await hub.send(acc["host"], {"type": "cooldown_over", "account": account})
        elif action == "login":
            host = body.get("host") or acc["host"] or next(iter(hub.conns), None)
            if not host:
                return {"ok": False, "error": "no computer online"}
            prov = hub.providers.get(acc["provider"])
            await hub.send(host, {"type": "login", "account": account, "provider": prov})
            return {"ok": True, "host": host}
        elif action == "start":
            host = body.get("host")
            if host:
                return {"ok": await hub.start_agent(account, host)}
        elif action in ("screenshot", "show", "reload", "restart", "stop", "hide"):
            if not acc["host"]:
                return {"ok": False, "error": "agent is not running"}
            if action == "screenshot":
                r = await hub.request(acc["host"], {"type": "screenshot", "account": account}, timeout=15)
                return r
            await hub.send(acc["host"], {"type": f"{action}_agent", "account": account})
            if action == "stop":
                hub.db.update("accounts", "id", account, status="stopped", host=None)
        hub.db.kv_set("paused_accounts", sorted(paused))
        return {"ok": True}

    @app.get("/api/p/screenshot/{account}")
    async def panel_screenshot(account: str):
        p = STATE_DIR / "screens" / f"{Path(account).name}.jpg"
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/p/provider/{pid}")
    async def panel_provider(pid: str, request: Request):
        body = await request.json()
        if pid not in hub.providers:
            raise HTTPException(404)
        if "enabled" in body:
            tog = hub.db.kv_get("provider_toggle", {}) or {}
            tog[pid] = bool(body["enabled"])
            hub.db.kv_set("provider_toggle", tog)
            hub.ev("info", "provider", f"Provider {pid} {'enabled' if body['enabled'] else 'disabled'}")
        if "selectors" in body or "settings" in body:
            ov = hub.db.kv_get("provider_overrides", {})
            cur = ov.setdefault(pid, {})
            if body.get("selectors"):
                cur.setdefault("selectors", {}).update({k: v for k, v in body["selectors"].items() if v is not None})
            for k, v in (body.get("settings") or {}).items():
                cur[k] = v
            hub.db.kv_set("provider_overrides", ov)
            hub.reload_providers()
            await hub.broadcast({"type": "providers", "providers": hub.providers})
            hub.ev("info", "provider", f"Provider {pid} config updated and pushed to all computers")
        await hub.allocate()
        return {"ok": True}

    @app.post("/api/p/selector_test")
    async def panel_selector_test(request: Request):
        body = await request.json()
        acc = hub.db.one("SELECT * FROM accounts WHERE id=?", (body.get("account", ""),))
        if not acc or not acc["host"]:
            return {"ok": False, "error": "pick a running agent"}
        return await hub.request(acc["host"], {"type": "test_selector", "account": acc["id"],
                                               "selector": body.get("selector", ""),
                                               "action": body.get("action", "highlight")}, timeout=20)

    @app.post("/api/p/task/{task_id}")
    async def panel_task_action(task_id: str, request: Request):
        body = await request.json()
        a = body.get("action")
        if a == "cancel":
            return await hub.cancel(task_id)
        if a == "reassign":
            return await hub.reassign(task_id, body.get("note", ""))
        if a == "requeue":
            hub.db.update("tasks", "id", task_id, status="queued", attempts=0, error="")
            return {"ok": True}
        if a == "priority":
            hub.db.update("tasks", "id", task_id, priority=int(body.get("value", 3)))
            return {"ok": True}
        raise HTTPException(400, "unknown action")

    @app.post("/api/p/asset/{aid}/claim")
    async def panel_asset_claim(aid: str, request: Request):
        body = await request.json()
        hub.db.update("assets", "id", aid, claimed_by=body.get("who", ""),
                      status="claimed" if body.get("who") else "requested")
        return {"ok": True}

    @app.post("/api/p/asset/{aid}/upload")
    async def panel_asset_upload(aid: str, file: UploadFile = File(...), who: str = Form("")):
        data = await file.read()
        return hub.game.deliver_asset(aid, file.filename or "", data, by=who)

    @app.post("/api/p/asset_drop")
    async def panel_asset_drop(file: UploadFile = File(...)):
        data = await file.read()
        dst = hub.game.dir / "assets_inbox" / Path(file.filename or "file.bin").name
        dst.write_bytes(data)
        await asyncio.to_thread(hub.game.scan_inbox)
        return {"ok": True}

    @app.get("/api/p/backups")
    async def panel_backups():
        return hub.game.list_backups()

    @app.post("/api/p/backup")
    async def panel_backup(request: Request):
        body = await request.json()
        if body.get("restore"):
            return await asyncio.to_thread(hub.game.restore, body["restore"])
        info = await asyncio.to_thread(hub.game.backup, "manual", True)
        try:
            hive = await asyncio.to_thread(hub.game.hive_backup)
            await hub.game.mirror(hive["name"])
            if info:
                await hub.game.mirror(info["name"])
        except Exception as e:
            hive = {"error": str(e)}
        return {"ok": True, **(info or {}), "hive": hive}

    @app.get("/api/p/backup/{name}")
    async def panel_backup_download(name: str):
        p = hub.game.backup_dir / Path(name).name
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, filename=p.name)

    @app.post("/api/p/global")
    async def panel_global(request: Request):
        body = await request.json()
        if "paused_all" in body:
            hub.db.kv_set("paused_all", bool(body["paused_all"]))
            hub.ev("warn", "coordinator", "ALL WORK PAUSED" if body["paused_all"] else "Work resumed")
        if body.get("reload_accounts"):
            hub.sync_accounts()
        if body.get("message_claude"):
            hub.ev("info", "team_message", f"Team: {body['message_claude']}", for_claude=True,
                   data={"text": body["message_claude"]})
        return {"ok": True}

    @app.get("/api/p/game/tree")
    async def panel_game_tree():
        return hub.game.tree()

    @app.get("/api/p/game/file")
    async def panel_game_file(path: str):
        t = hub.game.read_file(path)
        if t is None:
            raise HTTPException(404)
        return {"path": path, "text": t}

    @app.get("/api/p/game/raw")
    async def panel_game_raw(path: str):
        p = hub.game.safe_game_path(path)
        if not p or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-store"})

    @app.post("/api/p/game/check")
    async def panel_game_check():
        return await hub.game.check_project()

    @app.get("/api/p/diagnostics")
    async def panel_diag():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("state.json", json.dumps(build_state(hub), indent=1, default=str))
            z.writestr("events.json", json.dumps(hub.db.q("SELECT * FROM events ORDER BY id DESC LIMIT 3000"), indent=1))
            z.writestr("chats.json", json.dumps(hub.db.q("SELECT * FROM chats"), indent=1))
            z.writestr("providers.json", json.dumps(hub.providers, indent=1))
            for p in (STATE_DIR / "screens").glob("*.jpg") if (STATE_DIR / "screens").exists() else []:
                z.write(p, f"screens/{p.name}")
            for p in (STATE_DIR / "logs").glob("*.log") if (STATE_DIR / "logs").exists() else []:
                z.write(p, f"logs/{p.name}")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename=diagnostics-{int(time.time())}.zip"})

    # ------------------------------------------------------------------ Claude (MCP) API
    @app.get("/api/c/overview")
    async def c_overview():
        s = build_state(hub)
        agents = s["agents"]
        by_status: dict[str, int] = {}
        for a in agents:
            by_status[a["status"]] = by_status.get(a["status"], 0) + 1
        tasks = hub.db.q("SELECT id,title,status,priority,depends_json,result_chat,result_version,error FROM tasks ORDER BY created")
        return {
            "hosts_online": [h["name"] for h in s["hosts"] if h["online"]],
            "agents": by_status, "agents_total": len(agents),
            "agents_by_provider": s["provider_counts"],
            "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"],
                       "deps": json.loads(t["depends_json"]),
                       **({"error": t["error"][:200]} if t["error"] else {})} for t in tasks],
            "open_questions": s["questions"],
            "assets_missing": [a for a in s["assets"] if a["status"] != "delivered"],
            "tokens_total": s["tokens"]["total"],
            "contract_version": hub.db.kv_get("contract_version", 0),
            "game_dir": str(hub.game.dir),
            "paused": hub.db.kv_get("paused_all", False),
            "unseen_events": hub.db.one("SELECT COUNT(*) n FROM events WHERE for_claude=1 AND id>?",
                                        (hub.db.kv_get("claude_cursor", 0),))["n"],
        }

    @app.get("/api/c/contract")
    async def c_contract_get():
        return {"text": hub.game.get_contract(), "version": hub.db.kv_get("contract_version", 0)}

    @app.post("/api/c/contract")
    async def c_contract_set(request: Request):
        body = await request.json()
        return {"version": hub.game.set_contract(body["text"])}

    @app.post("/api/c/dispatch")
    async def c_dispatch(request: Request):
        body = await request.json()
        res = hub.dispatch(body.get("tasks", []))
        await hub.schedule()
        return res

    @app.get("/api/c/events")
    async def c_events(timeout: float = 120, peek: bool = False):
        timeout = max(0, min(timeout, 600))
        cursor = int(hub.db.kv_get("claude_cursor", 0))
        deadline = time.time() + timeout

        def fetch():
            return hub.db.q("SELECT id,ts,kind,code,task,chat,account,message,data_json FROM events "
                            "WHERE for_claude=1 AND id>? ORDER BY id LIMIT 50", (cursor,))
        rows = fetch()
        while not rows and time.time() < deadline:
            try:
                async with hub.claude_cond:
                    await asyncio.wait_for(hub.claude_cond.wait(), timeout=min(5, max(0.1, deadline - time.time())))
            except asyncio.TimeoutError:
                pass
            rows = fetch()
        if rows and not peek:
            hub.db.kv_set("claude_cursor", rows[-1]["id"])
        for r in rows:
            r["data"] = json.loads(r.pop("data_json") or "{}")
        active = hub.db.one(f"SELECT COUNT(*) n FROM chats WHERE status IN {ACTIVE_CHAT}")["n"]
        queued = hub.db.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','waiting','revision')")["n"]
        return {"events": rows, "active_chats": active, "queued_tasks": queued}

    @app.get("/api/c/task/{task_id}")
    async def c_task(task_id: str, full: bool = False):
        return task_detail(hub, task_id, full=full)

    @app.get("/api/c/report/{task_id}")
    async def c_report(task_id: str, chat: str = "", version: int = 0, level: str = "short"):
        task = hub.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise HTTPException(404, "unknown task")
        chat = chat or task["result_chat"]
        if not chat:
            return {"error": "no result yet", "status": task["status"]}
        rz = hub.game.result_zip(chat, version or None)
        if not rz:
            return {"error": "no result"}
        _, manifest = rz
        report = hub.game.read_result_file(chat, manifest["version"], "REPORT.md") or "(no REPORT.md)"
        if level == "short":
            keep = []
            for sec in ("Summary", "Files", "Public API", "Integration", "Assets needed", "Known limitations"):
                m = re.search(rf"^##\s*{sec}[^\n]*\n(.*?)(?=^##\s|\Z)", report, re.M | re.S | re.I)
                if m and m.group(1).strip():
                    keep.append(f"## {sec}\n{m.group(1).strip()}")
            report = "\n\n".join(keep) or report[:3000]
        return {"task": task_id, "chat": chat, "version": manifest["version"], "check_ok": manifest.get("check_ok"),
                "check_errors": manifest.get("check_errors", [])[:10], "files": manifest.get("files", []),
                "rounds": manifest.get("rounds"), "agent": manifest.get("account"), "report": report,
                "warnings": manifest.get("warnings", [])[:10]}

    @app.get("/api/c/file")
    async def c_file(task_id: str = "", path: str = "", chat: str = "", version: int = 0, source: str = "result"):
        if source == "game":
            t = hub.game.read_file(path)
            if t is None:
                raise HTTPException(404, "file not found in game")
            return {"path": path, "text": t}
        task = hub.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        chat = chat or (task or {}).get("result_chat")
        if not chat:
            raise HTTPException(404, "no result")
        rz = hub.game.result_zip(chat, version or None)
        if not rz:
            raise HTTPException(404, "no result")
        t = hub.game.read_result_file(chat, rz[1]["version"], path)
        if t is None:
            raise HTTPException(404, f"{path} not in result; files: {rz[1].get('files')}")
        return {"path": path, "text": t}

    @app.post("/api/c/game/file")
    async def c_game_write(request: Request):
        body = await request.json()
        ok = hub.game.write_file(body["path"], body["text"])
        return {"ok": ok}

    @app.get("/api/c/game/tree")
    async def c_game_tree():
        return [{"path": f["path"], "size": f["size"], "owner": f["owner"]} for f in hub.game.tree()]

    @app.post("/api/c/followup")
    async def c_followup(request: Request):
        b = await request.json()
        return await hub.followup(b["task_id"], b["message"], b.get("chat_id"))

    @app.post("/api/c/answer")
    async def c_answer(request: Request):
        b = await request.json()
        return await hub.answer(b.get("question_id"), b["answer"], b.get("chat_id"))

    @app.post("/api/c/task_action")
    async def c_task_action(request: Request):
        b = await request.json()
        if b["action"] == "cancel":
            return await hub.cancel(b["task_id"])
        if b["action"] == "reassign":
            return await hub.reassign(b["task_id"], b.get("note", ""))
        raise HTTPException(400)

    @app.post("/api/c/integrate")
    async def c_integrate(request: Request):
        b = await request.json()
        return await hub.game.integrate(b["task_id"], b.get("chat_id"), b.get("version"), bool(b.get("force")),
                                        bool(b.get("include_tests")))

    @app.post("/api/c/check")
    async def c_check(request: Request):
        b = await request.json()
        return await hub.game.check_project(b.get("files"))

    @app.post("/api/c/run")
    async def c_run(request: Request):
        b = await request.json()
        return await hub.game.run_scene(b["scene"], float(b.get("seconds", 5)))

    @app.post("/api/c/assets")
    async def c_assets(request: Request):
        b = await request.json()
        res = hub.game.request_assets(b.get("assets", []))
        return {"requested": res}

    @app.get("/api/c/assets")
    async def c_assets_list():
        return hub.db.q("SELECT id,name,path,kind,spec,status,width,height,frames,notes FROM assets ORDER BY id")

    @app.post("/api/c/notes")
    async def c_notes(request: Request):
        b = await request.json()
        if b.get("decision"):
            d = hub.db.kv_get("decisions", [])
            d.append({"ts": time.time(), "text": b["decision"]})
            hub.db.kv_set("decisions", d[-300:])
        if b.get("brief") is not None:
            hub.db.kv_set("game_brief", b["brief"])
        if b.get("next_steps") is not None:
            hub.db.kv_set("next_steps", b["next_steps"])
        hub.game.write_hive_docs()
        return {"ok": True}

    @app.post("/api/c/snapshot")
    async def c_snapshot(request: Request):
        b = await request.json()
        hub.game.write_hive_docs()
        info = await asyncio.to_thread(hub.game.backup, b.get("label", "snapshot"), True)
        commit = hub.game._git_commit(b.get("label", "snapshot"))
        return {"ok": True, "backup": info, "commit": commit, "handoff": str(hub.game.dir / "_hive" / "HANDOFF.md")}

    @app.post("/api/c/message_team")
    async def c_message_team(request: Request):
        b = await request.json()
        hub.ev("info", "claude_message", b["text"], data={"urgent": bool(b.get("urgent"))})
        msgs = hub.db.kv_get("claude_messages", [])
        msgs.append({"ts": time.time(), "text": b["text"], "urgent": bool(b.get("urgent"))})
        hub.db.kv_set("claude_messages", msgs[-100:])
        return {"ok": True}

    @app.get("/api/c/agents")
    async def c_agents():
        return [{k: a[k] for k in ("id", "provider", "status", "host", "tasks_done", "tasks_failed", "current_chat")}
                for a in hub.db.q("SELECT * FROM accounts WHERE in_config=1 ORDER BY id")]

    return app


# ---------------------------------------------------------------------- views
def build_state(hub: Hub) -> dict:
    db = hub.db
    now = time.time()
    hosts = db.q("SELECT * FROM hosts ORDER BY name")
    for h in hosts:
        h["sys"] = json.loads(h.pop("sys_json") or "{}")
        h["desired"] = hub.desired_for(h)
        h["connected"] = h["name"] in hub.conns
        h["since_seen"] = now - h["last_seen"] if h["last_seen"] else None
    paused = set(db.kv_get("paused_accounts", []))
    agents = db.q("SELECT * FROM accounts WHERE in_config=1 ORDER BY id")
    chats_active = {c["id"]: c for c in db.q("SELECT c.*, t.title FROM chats c LEFT JOIN tasks t ON t.id=c.task_id "
                                              f"WHERE c.status IN {ACTIVE_CHAT}")}
    provider_counts: dict[str, int] = {}
    for a in agents:
        a["paused"] = a["id"] in paused
        a["idle_for"] = (now - a["last_work_at"]) if a["status"] == "idle" and a["last_work_at"] else None
        a["status_for"] = now - a["status_since"] if a["status_since"] else None
        a["cooldown_left"] = max(0, a["cooldown_until"] - now) if a["cooldown_until"] else 0
        a["provider_enabled"] = hub.provider_enabled(a["provider"])
        c = chats_active.get(a["current_chat"] or "")
        a["chat"] = {"id": c["id"], "task": c["task_id"], "title": c["title"], "rounds": c["rounds"],
                     "ctx": c["ctx_tokens"], "status": c["status"], "url": c["url"]} if c else None
        prov = hub.providers.get(a["provider"], {})
        a["ctx_limit"] = prov.get("context_tokens", 64000)
        if a["host"]:
            provider_counts[a["provider"]] = provider_counts.get(a["provider"], 0) + 1
    tasks = db.q("SELECT id,title,status,priority,depends_json,result_chat,result_version,attempts,error,created,"
                 "updated,redundancy FROM tasks ORDER BY created")
    for t in tasks:
        t["deps"] = json.loads(t.pop("depends_json") or "[]")
        t["agents"] = [c["account_id"] for c in db.q("SELECT account_id FROM chats WHERE task_id=? ORDER BY started", (t["id"],))]
    tok = db.one("SELECT COALESCE(SUM(tin),0) tin, COALESCE(SUM(tout),0) tout FROM token_log WHERE account!='claude'")
    tok_c = db.one("SELECT COALESCE(SUM(tin),0) tin, COALESCE(SUM(tout),0) tout FROM token_log WHERE account='claude'")
    last_min = db.one("SELECT COALESCE(SUM(tin+tout),0) n FROM token_log WHERE ts>? AND account!='claude'", (now - 60,))
    providers = []
    for pid, p in sorted(hub.providers.items()):
        providers.append({"id": pid, "enabled": hub.provider_enabled(pid), "context_tokens": p.get("context_tokens"),
                          "notes": p.get("notes", ""), "selectors": p.get("selectors", {}),
                          "home_url": p.get("home_url") or p.get("new_chat_url"),
                          "accounts": sum(1 for a in agents if a["provider"] == pid)})
    return {
        "now": now, "uptime": now - hub.started, "version": VERSION,
        "paused_all": db.kv_get("paused_all", False),
        "hosts": hosts, "agents": agents, "tasks": tasks, "providers": providers, "provider_counts": provider_counts,
        "assets": db.q("SELECT * FROM assets ORDER BY priority, id"),
        "questions": db.q("SELECT id,chat_id,task_id,account_id,question,created FROM questions WHERE answered=0"),
        "tokens": {"in": tok["tin"], "out": tok["tout"], "total": tok["tin"] + tok["tout"],
                   "claude": tok_c["tin"] + tok_c["tout"], "per_min": last_min["n"]},
        "claude_last_seen": db.kv_get("claude_last_seen", 0),
        "claude_messages": db.kv_get("claude_messages", [])[-20:],
        "last_backup": (hub.game.list_backups() or [None])[0],
        "last_check": db.kv_get("last_check", {}),
        "events": db.q("SELECT * FROM events ORDER BY id DESC LIMIT 60"),
        "godot": hub.game.godot.path,
        "game_dir": str(hub.game.dir),
        "default_agents": hub.cfg["default_agents_per_host"],
    }


def token_stats(hub: Hub, minutes: int) -> dict:
    db = hub.db
    now = time.time()
    since = now - minutes * 60
    rows = db.q("SELECT CAST((ts-?)/60 AS INTEGER) b, account, SUM(tin) tin, SUM(tout) tout FROM token_log "
                "WHERE ts>? GROUP BY b, account", (since, since))
    series = [0] * (minutes + 1)
    series_c = [0] * (minutes + 1)
    for r in rows:
        b = min(minutes, max(0, r["b"]))
        if r["account"] == "claude":
            series_c[b] += r["tin"] + r["tout"]
        else:
            series[b] += r["tin"] + r["tout"]
    per_agent = db.q("SELECT id, provider, tokens_in, tokens_out, tasks_done, rounds_total FROM accounts "
                     "WHERE in_config=1 ORDER BY tokens_in+tokens_out DESC")
    per_provider = db.q("SELECT provider, SUM(tin) tin, SUM(tout) tout FROM token_log GROUP BY provider ORDER BY SUM(tin+tout) DESC")
    per_host = db.q("SELECT host, SUM(tin) tin, SUM(tout) tout FROM token_log GROUP BY host ORDER BY SUM(tin+tout) DESC")
    top_tasks = db.q("SELECT task_id, SUM(tokens_in+tokens_out) total, SUM(rounds) rounds FROM chats "
                     "GROUP BY task_id ORDER BY total DESC LIMIT 8")
    done = db.one("SELECT COUNT(*) n, COALESCE(SUM(tokens_in+tokens_out),0) t FROM chats WHERE status='done'")
    return {"minutes": minutes, "series": series, "series_claude": series_c, "per_agent": per_agent,
            "per_provider": per_provider, "per_host": per_host, "top_tasks": top_tasks,
            "avg_per_done": (done["t"] // done["n"]) if done["n"] else 0}


def task_detail(hub: Hub, task_id: str, full: bool = False) -> dict:
    db = hub.db
    t = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise HTTPException(404, "unknown task")
    t["spec"] = json.loads(t.pop("spec_json"))
    t["deps"] = json.loads(t.pop("depends_json"))
    t["chats"] = db.q("SELECT id,account_id,host,url,status,rounds,tokens_in,tokens_out,ctx_tokens,started,finished,"
                      "result_version,error FROM chats WHERE task_id=? ORDER BY started", (task_id,))
    t["results"] = [{"chat": r["chat_id"], "version": r["version"],
                     **{k: v for k, v in json.loads(r["manifest_json"]).items() if k in ("files", "check_ok", "rounds", "summary")}}
                    for r in db.q("SELECT * FROM results WHERE task_id=? ORDER BY created", (task_id,))]
    t["questions"] = db.q("SELECT * FROM questions WHERE task_id=?", (task_id,))
    if full and t["result_chat"]:
        t["report"] = hub.game.read_result_file(t["result_chat"], t["result_version"], "REPORT.md")
    return t


async def discovery_beacon(cfg: dict) -> None:
    """UDP broadcast so workers can find the coordinator without typing an IP."""
    port = int(cfg.get("discovery_port", 8766))
    msg = json.dumps({"mc": 1, "port": int(cfg["port"]), "name": socket.gethostname()}).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setblocking(False)
    while True:
        try:
            s.sendto(msg, ("255.255.255.255", port))
        except OSError:
            pass
        await asyncio.sleep(2)
