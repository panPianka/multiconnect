"""The Hub: live state of hosts/agents, task scheduler and worker messaging."""
from __future__ import annotations

import asyncio
import json
import secrets as pysecrets
import time
import uuid
from pathlib import Path
from typing import Any

from ..common.config import (PROMPTS_DIR, STATE_DIR, load_accounts, load_providers, resolve_path)
from ..common.tokens import estimate_tokens
from .db import DB
from .gameops import GameOps

ACTIVE_CHAT = ("assigned", "running", "question")
BUSY_AGENT = ("working", "starting", "login")
SECRETS_DIR = STATE_DIR / "secrets"


def render(template: str, **vals) -> str:
    out = template
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


class WorkerConn:
    def __init__(self, name: str, ws, ip: str):
        self.name = name
        self.ws = ws
        self.ip = ip
        self.lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(msg))


class Hub:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        self.db = DB(STATE_DIR / "multiconnect.db")
        self.game = GameOps(self)
        self.conns: dict[str, WorkerConn] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.claude_cond = asyncio.Condition()
        self.started = time.time()
        self.providers: dict[str, dict] = {}
        self.reload_providers()
        if self.db.kv_get("disabled_providers") is None:
            self.db.kv_set("disabled_providers", list(cfg.get("disabled_providers", [])))
        self.sync_accounts()
        # anything that was "live" before a coordinator restart is offline now
        self.db.x("UPDATE hosts SET online=0")
        self.db.x("UPDATE accounts SET status='offline', host=NULL, current_chat=NULL WHERE status!='offline'")

    # ------------------------------------------------------------------ config
    def reload_providers(self) -> None:
        self.providers = load_providers(self.db.kv_get("provider_overrides", {}))

    def provider_enabled(self, pid: str) -> bool:
        p = self.providers.get(pid)
        if not p:
            return False
        toggles = self.db.kv_get("provider_toggle") or {}
        if pid in toggles:  # the panel switch wins over the YAML default
            return bool(toggles[pid])
        return bool(p.get("enabled", True)) and pid not in (self.db.kv_get("disabled_providers") or [])

    def sync_accounts(self) -> list[dict]:
        accs = load_accounts(resolve_path(self.cfg["accounts_file"]))
        self.db.x("UPDATE accounts SET in_config=0")
        for a in accs:
            has_state = 1 if ((SECRETS_DIR / f"{a['id']}.json").exists()
                              or (self.providers.get(a["provider"]) or {}).get("no_login")) else 0
            if self.db.one("SELECT id FROM accounts WHERE id=?", (a["id"],)):
                self.db.update("accounts", "id", a["id"], provider=a["provider"], owner=a["owner"], label=a["label"],
                               pin_host=a["pin_host"], enabled=int(a["enabled"]), has_state=has_state, in_config=1)
            else:
                self.db.x("INSERT INTO accounts(id,provider,owner,label,pin_host,enabled,has_state,status,in_config) "
                          "VALUES(?,?,?,?,?,?,?,'offline',1)",
                          (a["id"], a["provider"], a["owner"], a["label"], a["pin_host"], int(a["enabled"]), has_state))
        return accs

    # ------------------------------------------------------------------ events
    async def notify_claude(self) -> None:
        async with self.claude_cond:
            self.claude_cond.notify_all()

    def ev(self, level: str, kind: str, message: str, for_claude: bool = False, **kw) -> None:
        self.db.event(level, kind, message, for_claude=for_claude, **kw)
        if for_claude:
            try:
                asyncio.get_running_loop().create_task(self.notify_claude())
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ workers
    async def worker_connected(self, conn: WorkerConn, hello: dict) -> None:
        name = conn.name
        old = self.conns.get(name)
        if old and old is not conn:
            try:
                await old.ws.close()
            except Exception:
                pass
        self.conns[name] = conn
        now = time.time()
        if not self.db.one("SELECT name FROM hosts WHERE name=?", (name,)):
            self.db.x("INSERT INTO hosts(name, desired_agents) VALUES(?, -1)", (name,))
        self.db.update("hosts", "name", name, ip=conn.ip, online=1, last_seen=now, connected_at=now,
                       version=hello.get("version", ""), godot=hello.get("godot") or "",
                       sys_json=json.dumps(hello.get("sys", {})))
        host = self.db.one("SELECT * FROM hosts WHERE name=?", (name,))
        mode = host.get("mode") or hello.get("mode") or ""
        # reconcile agents the worker says are still running
        running = {a["account"]: a for a in hello.get("agents", [])}
        for acc in self.db.q("SELECT * FROM accounts WHERE host=?", (name,)):
            if acc["id"] not in running:
                self.db.update("accounts", "id", acc["id"], status="offline", host=None, current_chat=None)
        for aid, a in running.items():
            self.db.update("accounts", "id", aid, host=name, status=a.get("status", "idle"),
                           current_chat=a.get("chat_id"), status_since=now)
        self.ev("info", "host", f"Computer {name} connected ({conn.ip})", host=name)
        await conn.send({"type": "welcome", "providers": self.providers, "mode": mode,
                         "max_rounds": self.cfg["max_rounds"]})

    async def worker_disconnected(self, conn: WorkerConn) -> None:
        if self.conns.get(conn.name) is conn:
            del self.conns[conn.name]
            self.db.update("hosts", "name", conn.name, online=0)
            self.ev("warn", "host", f"Computer {conn.name} disconnected", code="E_WORKER_OFFLINE", host=conn.name)

    async def send(self, host: str, msg: dict) -> bool:
        c = self.conns.get(host)
        if not c:
            return False
        try:
            await c.send(msg)
            return True
        except Exception:
            return False

    async def request(self, host: str, msg: dict, timeout: float = 20) -> dict:
        rid = uuid.uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self.pending[rid] = fut
        msg = dict(msg, req_id=rid)
        if not await self.send(host, msg):
            self.pending.pop(rid, None)
            return {"ok": False, "error": f"host {host} offline"}
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout"}
        finally:
            self.pending.pop(rid, None)

    async def broadcast(self, msg: dict) -> None:
        for name in list(self.conns):
            await self.send(name, msg)

    async def on_worker_message(self, conn: WorkerConn, m: dict) -> None:
        t = m.get("type")
        now = time.time()
        host = conn.name
        if t == "heartbeat":
            self.db.update("hosts", "name", host, last_seen=now, online=1, sys_json=json.dumps(m.get("sys", {})))
            for a in m.get("agents", []):
                acc = self.db.one("SELECT status FROM accounts WHERE id=?", (a["account"],))
                if acc and acc["status"] != a.get("status"):
                    self._set_agent_status(a["account"], a.get("status", "idle"), a.get("detail", ""), host)
        elif t == "reply":
            fut = self.pending.get(m.get("req_id", ""))
            if fut and not fut.done():
                fut.set_result(m.get("data", {}))
        elif t == "agent_status":
            self._set_agent_status(m["account"], m["status"], m.get("detail", ""), host,
                                   cooldown_until=m.get("cooldown_until"), chat_id=m.get("chat_id"))
        elif t == "chat_update":
            self._chat_update(m, host)
        elif t == "event":
            self.ev(m.get("level", "info"), m.get("kind", "agent"), m.get("message", ""), code=m.get("code", ""),
                    host=host, account=m.get("account", ""), chat=m.get("chat_id", ""), task=m.get("task_id", ""),
                    data=m.get("data"))
            if m.get("code"):
                self.db.x("UPDATE accounts SET errors=errors+1 WHERE id=?", (m.get("account", ""),))
        elif t == "question":
            await self._question(m, host)
        elif t == "chat_failed":
            await self._chat_failed(m["chat_id"], m.get("reason", ""), m.get("code", ""), host)
        elif t == "login_result":
            ok = m.get("ok")
            self.ev("info" if ok else "warn", "login", f"Login {'saved' if ok else 'failed'} for {m.get('account')}",
                    account=m.get("account", ""), host=host)
            self.sync_accounts()
        elif t == "chat_question_expired":
            self.db.x("UPDATE questions SET answered=?, answer='(expired - agent decided itself)' "
                      "WHERE chat_id=? AND answered=0", (now, m["chat_id"]))

    def _set_agent_status(self, account: str, status: str, detail: str, host: str, cooldown_until=None,
                          chat_id=None) -> None:
        now = time.time()
        fields: dict[str, Any] = {"status": status, "status_detail": detail[:500], "status_since": now, "host": host}
        if status in ("working",):
            fields["last_work_at"] = now
        if cooldown_until:
            fields["cooldown_until"] = cooldown_until
        if chat_id is not None:
            fields["current_chat"] = chat_id or None
        if status in ("offline", "stopped"):
            fields["host"] = None
            fields["current_chat"] = None
        prev = self.db.one("SELECT status, last_work_at FROM accounts WHERE id=?", (account,))
        if prev and prev["status"] == "working" and status != "working":
            fields["last_work_at"] = now
        self.db.update("accounts", "id", account, **fields)
        if status in ("logged_out", "captcha", "error"):
            code = {"logged_out": "E_LOGGED_OUT", "captcha": "E_CAPTCHA", "error": "E_AGENT"}[status]
            self.ev("error" if status == "error" else "warn", "agent", f"{account}: {status} {detail}".strip(),
                    code=code, account=account, host=host)
            if status == "logged_out":
                self.db.update("accounts", "id", account, has_state=0)
        if status == "cooldown":
            self.ev("warn", "agent", f"{account} hit a rate limit - cooling down", code="E_RATE_LIMIT",
                    account=account, host=host)

    def _chat_update(self, m: dict, host: str) -> None:
        cid = m["chat_id"]
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (cid,))
        if not chat:
            return
        tin, tout = int(m.get("tin", 0)), int(m.get("tout", 0))
        fields: dict[str, Any] = {"updated": time.time()}
        for k in ("rounds", "url", "ctx_tokens", "status"):
            if m.get(k) not in (None, ""):
                fields[k] = m[k]
        if tin or tout:
            fields["tokens_in"] = chat["tokens_in"] + tin
            fields["tokens_out"] = chat["tokens_out"] + tout
            acc = self.db.one("SELECT provider FROM accounts WHERE id=?", (chat["account_id"],))
            self.db.x("UPDATE accounts SET tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE id=?",
                      (tin, tout, chat["account_id"]))
            self.db.x("INSERT INTO token_log VALUES(?,?,?,?,?,?)",
                      (time.time(), chat["account_id"], acc["provider"] if acc else "", host, tin, tout))
        if m.get("round_inc"):
            self.db.x("UPDATE accounts SET rounds_total=rounds_total+1 WHERE id=?", (chat["account_id"],))
        self.db.update("chats", "id", cid, **fields)

    async def _question(self, m: dict, host: str) -> None:
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (m["chat_id"],))
        if not chat:
            return
        qid = self.db.x("INSERT INTO questions(chat_id,task_id,account_id,question,created) VALUES(?,?,?,?,?)",
                        (chat["id"], chat["task_id"], chat["account_id"], m.get("text", ""), time.time()))
        self.db.update("chats", "id", chat["id"], status="question")
        self.ev("info", "question", f"Agent on {chat['task_id']} asks: {m.get('text','')[:300]}", for_claude=True,
                chat=chat["id"], task=chat["task_id"], account=chat["account_id"], host=host,
                data={"question_id": qid, "question": m.get("text", "")})

    # ------------------------------------------------------------------ chats/results
    async def _chat_failed(self, chat_id: str, reason: str, code: str, host: str = "") -> None:
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not chat or chat["status"] in ("failed", "cancelled", "done"):
            return
        self.db.update("chats", "id", chat_id, status="failed", error=reason[:2000], finished=time.time())
        self.db.x("UPDATE accounts SET tasks_failed=tasks_failed+1 WHERE id=?", (chat["account_id"],))
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (chat["task_id"],))
        self.ev("warn", "chat", f"{chat_id} failed: {reason[:300]}", code=code or "E_CHAT_FAILED",
                chat=chat_id, task=chat["task_id"], account=chat["account_id"], host=host)
        if not task or task["status"] in ("cancelled", "integrated"):
            return
        active = self.db.one(f"SELECT COUNT(*) n FROM chats WHERE task_id=? AND status IN {ACTIVE_CHAT}",
                             (task["id"],))["n"]
        has_result = task["result_chat"] is not None and task["result_version"] > 0
        if active or has_result and task["status"] == "done":
            return
        if has_result:  # a follow-up on a finished task failed: keep previous result
            self.db.update("tasks", "id", task["id"], status="done", updated=time.time(),
                           error=f"follow-up failed: {reason[:500]}")
            self.ev("warn", "task", f"Follow-up on {task['id']} failed ({reason[:200]}). Previous result kept.",
                    for_claude=True, task=task["id"], chat=chat_id, code=code)
            return
        attempts = task["attempts"] + 1
        if attempts >= 3:
            self.db.update("tasks", "id", task["id"], status="failed", attempts=attempts, updated=time.time(),
                           error=reason[:2000])
            self.ev("error", "task", f"Task {task['id']} FAILED after {attempts} attempts: {reason[:300]}",
                    for_claude=True, task=task["id"], code=code or "E_TASK_FAILED")
        else:
            spec = json.loads(task["spec_json"])
            spec.setdefault("_avoid_accounts", []).append(chat["account_id"])
            self.db.update("tasks", "id", task["id"], status="queued", attempts=attempts, updated=time.time(),
                           spec_json=json.dumps(spec), error=reason[:2000])
            self.ev("info", "task", f"Task {task['id']} re-queued (attempt {attempts + 1})", task=task["id"])

    async def store_result(self, chat_id: str, version: int, zip_path: Path, manifest: dict) -> None:
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not chat:
            return
        if chat["status"] == "done" and chat["result_version"] == version and                 self.db.one("SELECT 1 FROM results WHERE chat_id=? AND version=?", (chat_id, version)):
            return  # duplicate upload (retry after a lost response): already stored and announced
        now = time.time()
        self.db.x("INSERT OR REPLACE INTO results(chat_id,version,task_id,path,manifest_json,created) VALUES(?,?,?,?,?,?)",
                  (chat_id, version, chat["task_id"], str(zip_path), json.dumps(manifest), now))
        self.db.update("chats", "id", chat_id, status="done", result_version=version, finished=now, updated=now)
        self.db.x("UPDATE accounts SET tasks_done=tasks_done+1 WHERE id=?", (chat["account_id"],))
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (chat["task_id"],))
        if not task:
            return
        self.db.update("tasks", "id", task["id"], status="done", result_chat=chat_id, result_version=version,
                       updated=now, error="")
        summary = manifest.get("summary", "")
        self.ev("info", "task_done", f"Task {task['id']} ready for review (chat {chat_id} v{version}, "
                f"{manifest.get('rounds', '?')} rounds, check {'OK' if manifest.get('check_ok') else 'FAILED'})",
                for_claude=True, task=task["id"], chat=chat_id, account=chat["account_id"],
                data={"summary": summary[:600], "files": manifest.get("files", []), "version": version,
                      "check_ok": manifest.get("check_ok")})
        self.game.write_hive_docs_soon()

    def release_waiting(self) -> list[str]:
        """Move waiting tasks whose dependencies are all integrated back to the queue."""
        integrated = {r["id"] for r in self.db.q("SELECT id FROM tasks WHERE status='integrated'")}
        out = []
        for t in self.db.q("SELECT id,depends_json FROM tasks WHERE status='waiting'"):
            if all(d in integrated for d in json.loads(t["depends_json"] or "[]")):
                self.db.update("tasks", "id", t["id"], status="queued", updated=time.time())
                self.ev("info", "task", f"Task {t['id']} unblocked (dependencies integrated)", task=t["id"])
                out.append(t["id"])
        return out

    # ------------------------------------------------------------------ Claude-facing ops
    def next_task_id(self) -> str:
        # skip ids already taken (Claude / the panel may also pick ids by hand, e.g. T001)
        n = int(self.db.kv_get("task_seq", 0))
        while True:
            n += 1
            if not self.db.one("SELECT id FROM tasks WHERE id=?", (f"T{n:03d}",)):
                break
        self.db.kv_set("task_seq", n)
        return f"T{n:03d}"

    def dispatch(self, tasks: list[dict]) -> dict:
        created, errors = [], []
        now = time.time()
        for t in tasks:
            title = (t.get("title") or "").strip()
            goal = (t.get("goal") or "").strip()
            files = t.get("files_to_create") or []
            if not title or not goal:
                errors.append({"task": t.get("id") or title, "error": "title and goal are required"})
                continue
            if not files and not t.get("files_to_modify"):
                errors.append({"task": t.get("id") or title, "error": "files_to_create or files_to_modify required"})
                continue
            tid = t.get("id") or self.next_task_id()
            if self.db.one("SELECT id FROM tasks WHERE id=?", (tid,)):
                errors.append({"task": tid, "error": "task id already exists"})
                continue
            deps = t.get("depends_on") or []
            missing = [d for d in deps if not self.db.one("SELECT id FROM tasks WHERE id=?", (d,))
                       and d not in [c["id"] for c in created]]
            if missing:
                errors.append({"task": tid, "error": f"unknown dependencies {missing}"})
                continue
            spec = {k: v for k, v in t.items() if k not in ("id",)}
            self.db.x("INSERT INTO tasks(id,title,spec_json,status,priority,depends_json,created,updated,redundancy,"
                      "pinned_account) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (tid, title, json.dumps(spec), "queued", int(t.get("priority", 3)), json.dumps(deps), now, now,
                       max(1, min(3, int(t.get("redundancy", 1)))), t.get("account")))
            created.append({"id": tid, "title": title})
            self.ev("info", "task", f"Task {tid} queued: {title}", task=tid)
        self.game.write_hive_docs_soon()
        return {"created": created, "errors": errors}

    async def followup(self, task_id: str, message: str, chat_id: str | None = None) -> dict:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return {"ok": False, "error": "unknown task"}
        if not chat_id:
            chat_id = task["result_chat"] or (self.db.one(
                "SELECT id FROM chats WHERE task_id=? ORDER BY started DESC LIMIT 1", (task_id,)) or {}).get("id")
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (chat_id,)) if chat_id else None
        if not chat:
            return {"ok": False, "error": "task has no chat yet"}
        pend = json.loads(chat["pending_json"] or "[]")
        pend.append(message)
        self.db.update("chats", "id", chat["id"], pending_json=json.dumps(pend))
        self.db.update("tasks", "id", task_id, status="revision", updated=time.time())
        acc = self.db.one("SELECT * FROM accounts WHERE id=?", (chat["account_id"],))
        where = "queued; will be sent when the agent is free"
        if not acc or acc["status"] in ("offline", "stopped", "logged_out"):
            where = (f"queued, but agent {chat['account_id']} is {acc['status'] if acc else 'unknown'}. "
                     f"Use reassign() if it does not come back.")
        await self.schedule()
        return {"ok": True, "chat_id": chat["id"], "agent": chat["account_id"], "status": where}

    async def answer(self, question_id: int | None, answer: str, chat_id: str | None = None) -> dict:
        q = None
        if question_id:
            q = self.db.one("SELECT * FROM questions WHERE id=?", (question_id,))
        elif chat_id:
            q = self.db.one("SELECT * FROM questions WHERE chat_id=? AND answered=0 ORDER BY id DESC", (chat_id,))
        if not q:
            return {"ok": False, "error": "question not found"}
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (q["chat_id"],))
        self.db.update("questions", "id", q["id"], answer=answer, answered=time.time())
        if chat and chat["host"]:
            await self.send(chat["host"], {"type": "answer", "chat_id": chat["id"], "text": answer})
            self.db.update("chats", "id", chat["id"], status="running")
        return {"ok": True}

    async def cancel(self, task_id: str) -> dict:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return {"ok": False, "error": "unknown task"}
        for c in self.db.q(f"SELECT * FROM chats WHERE task_id=? AND status IN {ACTIVE_CHAT}", (task_id,)):
            await self.send(c["host"], {"type": "cancel", "chat_id": c["id"]})
            self.db.update("chats", "id", c["id"], status="cancelled", finished=time.time())
        self.db.update("tasks", "id", task_id, status="cancelled", updated=time.time())
        return {"ok": True}

    async def reassign(self, task_id: str, note: str = "") -> dict:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return {"ok": False, "error": "unknown task"}
        avoid = []
        for c in self.db.q("SELECT * FROM chats WHERE task_id=?", (task_id,)):
            avoid.append(c["account_id"])
            if c["status"] in ACTIVE_CHAT:
                await self.send(c["host"], {"type": "cancel", "chat_id": c["id"]})
                self.db.update("chats", "id", c["id"], status="cancelled", finished=time.time())
        spec = json.loads(task["spec_json"])
        spec["_avoid_accounts"] = sorted(set(avoid))
        if note:
            spec["notes"] = (spec.get("notes", "") + "\n" + note).strip()
        self.db.update("tasks", "id", task_id, status="queued", spec_json=json.dumps(spec), updated=time.time(),
                       result_chat=None, result_version=0, attempts=0, error="")
        await self.schedule()
        return {"ok": True}

    # ------------------------------------------------------------------ scheduling
    def _free_agents(self) -> list[dict]:
        now = time.time()
        paused = set(self.db.kv_get("paused_accounts", []))
        out = []
        for a in self.db.q("SELECT * FROM accounts WHERE status='idle' AND host IS NOT NULL"):
            if a["id"] in paused or a["cooldown_until"] > now or not self.provider_enabled(a["provider"]):
                continue
            if a["host"] not in self.conns:
                continue
            out.append(a)
        return out

    async def schedule(self) -> None:
        if self.db.kv_get("paused_all", False):
            return
        free = self._free_agents()
        if not free:
            return
        free_ids = {a["id"] for a in free}
        # 1) pending follow-ups go to their own agent first
        for chat in self.db.q("SELECT * FROM chats WHERE pending_json!='[]' AND status NOT IN ('assigned','running','question')"):
            if chat["account_id"] in free_ids:
                acc = next(a for a in free if a["id"] == chat["account_id"])
                if await self._send_followup(chat, acc):
                    free = [a for a in free if a["id"] != acc["id"]]
                    free_ids.discard(acc["id"])
        if not free:
            return
        # 2) new work
        tasks = self.db.q("SELECT * FROM tasks WHERE status IN ('queued','waiting') ORDER BY priority ASC, created ASC")
        for task in tasks:
            if not free:
                break
            deps = json.loads(task["depends_json"] or "[]")
            if deps:
                st = {r["id"]: r["status"] for r in
                      self.db.q(f"SELECT id,status FROM tasks WHERE id IN ({','.join('?' * len(deps))})", deps)}
                if any(st.get(d) != "integrated" for d in deps):
                    if task["status"] != "waiting":
                        self.db.update("tasks", "id", task["id"], status="waiting")
                    continue
            spec = json.loads(task["spec_json"])
            n_active = self.db.one(f"SELECT COUNT(*) n FROM chats WHERE task_id=? AND status IN {ACTIVE_CHAT}",
                                   (task["id"],))["n"]
            need = task["redundancy"] - n_active
            used = {c["account_id"] for c in self.db.q("SELECT account_id FROM chats WHERE task_id=?", (task["id"],))}
            while need > 0 and free:
                acc = self._pick_agent(free, spec, task.get("pinned_account"), used)
                if not acc:
                    break
                free.remove(acc)
                used.add(acc["id"])
                await self._assign(task, spec, acc)
                need -= 1

    def _pick_agent(self, free: list[dict], spec: dict, pinned: str | None, used: set) -> dict | None:
        if pinned:
            return next((a for a in free if a["id"] == pinned), None)
        avoid = set(spec.get("_avoid_accounts", [])) | used
        pref = spec.get("provider_pref") or []
        cands = [a for a in free if a["id"] not in avoid] or [a for a in free if a["id"] not in used]
        if not cands:
            return None

        def score(a):
            p = pref.index(a["provider"]) if a["provider"] in pref else len(pref)
            quality = a["tasks_done"] - 2 * a["tasks_failed"]
            return (p, -quality, a["last_work_at"])
        return sorted(cands, key=score)[0]

    def _task_body(self, task: dict, spec: dict) -> str:
        lines = [f"GOAL:\n{spec.get('goal','')}"]
        if spec.get("files_to_create"):
            lines.append("FILES YOU MUST CREATE:\n" + "\n".join(f"- {f}" for f in spec["files_to_create"]))
        if spec.get("files_to_modify"):
            lines.append("EXISTING FILES YOU MAY MODIFY:\n" + "\n".join(f"- {f}" for f in spec["files_to_modify"]))
        if spec.get("interface"):
            lines.append(f"REQUIRED INTERFACE (match exactly):\n{spec['interface']}")
        if spec.get("context_files"):
            lines.append("READ THESE FIRST (with @@READ):\n" + "\n".join(f"- {f}" for f in spec["context_files"]))
        if spec.get("assets"):
            assets = spec["assets"] if isinstance(spec["assets"], list) else [spec["assets"]]
            lines.append("ASSETS AVAILABLE:\n" + "\n".join(f"- {a}" for a in assets))
        if spec.get("acceptance"):
            acc = spec["acceptance"] if isinstance(spec["acceptance"], list) else [spec["acceptance"]]
            lines.append("ACCEPTANCE CRITERIA:\n" + "\n".join(f"- {a}" for a in acc))
        if spec.get("notes"):
            lines.append(f"NOTES:\n{spec['notes']}")
        deps = json.loads(task.get("depends_json") or "[]")
        for d in deps:
            api = self.game.report_section(d, "Public API")
            if api:
                lines.append(f"API OF DEPENDENCY {d} (already in the project):\n{api}")
        return "\n\n".join(lines)

    async def _assign(self, task: dict, spec: dict, acc: dict) -> None:
        n = int(self.db.kv_get("chat_seq", 0)) + 1
        self.db.kv_set("chat_seq", n)
        chat_id = f"C{n:04d}-{task['id']}"
        bundle = self.game.bundle()
        contract = self.game.get_contract() or "(no contract yet - follow the task description)"
        body = self._task_body(task, spec)
        first = render(prompt("agent_system.md"), godot_version=self.cfg.get("godot_version", "4.x"),
                       report_template=prompt("report_template.md").strip(), contract=contract,
                       task_id=task["id"], title=task["title"], task_body=body,
                       file_list=bundle["file_list_text"])
        writable = list(spec.get("files_to_create") or []) + list(spec.get("files_to_modify") or [])
        now = time.time()
        self.db.x("INSERT INTO chats(id,task_id,account_id,host,status,started,updated) VALUES(?,?,?,?,?,?,?)",
                  (chat_id, task["id"], acc["id"], acc["host"], "assigned", now, now))
        self.db.update("tasks", "id", task["id"], status="running", updated=now)
        self.db.update("accounts", "id", acc["id"], status="working", current_chat=chat_id, status_since=now,
                       last_work_at=now)
        prov = self.providers.get(acc["provider"], {})
        msg = {"type": "assign", "kind": "new", "chat_id": chat_id, "task_id": task["id"], "account": acc["id"],
               "prompt": first, "bundle_hash": bundle["hash"], "writable": writable,
               "required": list(spec.get("files_to_create") or []),
               "contract": contract, "task_md": f"# {task['id']} {task['title']}\n\n{body}\n",
               "max_rounds": int(spec.get("max_rounds", self.cfg["max_rounds"])),
               "ctx_limit": int(prov.get("context_tokens", 64000)),
               "rollover_ratio": float(self.cfg["context_rollover_ratio"]),
               "question_timeout_s": int(self.cfg["question_timeout_s"]),
               "run_scenes": bool(spec.get("run_scenes", True))}
        if not await self.send(acc["host"], msg):
            self.db.update("chats", "id", chat_id, status="failed", error="host offline at assign")
            self.db.update("tasks", "id", task["id"], status="queued")
            return
        self.ev("info", "assign", f"{task['id']} -> {acc['id']} on {acc['host']} ({chat_id})",
                task=task["id"], chat=chat_id, account=acc["id"], host=acc["host"])

    async def _send_followup(self, chat: dict, acc: dict) -> bool:
        pend = json.loads(chat["pending_json"] or "[]")
        if not pend:
            return False
        message = "\n\n".join(pend)
        text = render(prompt("followup.md"), task_id=chat["task_id"], message=message)
        restore = None
        if chat["result_version"]:
            restore = {"chat_id": chat["id"], "version": chat["result_version"]}
        bundle = self.game.bundle()
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (chat["task_id"],))
        spec = json.loads(task["spec_json"]) if task else {}
        writable = list(spec.get("files_to_create") or []) + list(spec.get("files_to_modify") or [])
        now = time.time()
        msg = {"type": "assign", "kind": "followup", "chat_id": chat["id"], "task_id": chat["task_id"],
               "account": acc["id"], "prompt": text, "url": chat["url"], "bundle_hash": bundle["hash"],
               "restore": restore, "writable": writable, "contract": self.game.get_contract(),
               "required": list(spec.get("files_to_create") or []),
               "task_md": "", "max_rounds": int(self.cfg["max_rounds"]),
               "ctx_limit": int(self.providers.get(acc["provider"], {}).get("context_tokens", 64000)),
               "rollover_ratio": float(self.cfg["context_rollover_ratio"]),
               "question_timeout_s": int(self.cfg["question_timeout_s"]),
               "start_version": chat["result_version"], "ctx_tokens": chat["ctx_tokens"], "run_scenes": True}
        if not await self.send(acc["host"], msg):
            return False
        self.db.update("chats", "id", chat["id"], pending_json="[]", status="assigned", host=acc["host"], updated=now)
        self.db.update("accounts", "id", acc["id"], status="working", current_chat=chat["id"], status_since=now,
                       last_work_at=now)
        if task:
            self.db.update("tasks", "id", task["id"], status="running", updated=now)
        self.ev("info", "followup", f"Follow-up sent to {chat['id']} ({acc['id']})", chat=chat["id"],
                task=chat["task_id"], account=acc["id"])
        return True

    # ------------------------------------------------------------------ allocation of agents to hosts
    def desired_for(self, host: dict) -> int:
        d = host.get("desired_agents", -1)
        return int(self.cfg["default_agents_per_host"]) if d is None or d < 0 else int(d)

    async def allocate(self) -> None:
        now = time.time()
        paused = set(self.db.kv_get("paused_accounts", []))
        hosts = [h for h in self.db.q("SELECT * FROM hosts WHERE online=1") if h["name"] in self.conns]
        for h in hosts:
            mine = self.db.q("SELECT * FROM accounts WHERE host=? AND status NOT IN ('offline','stopped')", (h["name"],))
            # stop agents of disabled providers / disabled accounts when idle
            for a in mine:
                if (not self.provider_enabled(a["provider"]) or not a["enabled"]) and a["status"] == "idle":
                    await self.send(h["name"], {"type": "stop_agent", "account": a["id"]})
                    self.db.update("accounts", "id", a["id"], status="stopped", host=None)
            mine = [a for a in mine if self.provider_enabled(a["provider"]) and a["enabled"]]
            want = self.desired_for(h)
            if len(mine) > want:
                idle = sorted([a for a in mine if a["status"] in ("idle", "cooldown", "logged_out", "error", "captcha")],
                              key=lambda a: (a["status"] == "idle", a["last_work_at"]))
                for a in idle[: len(mine) - want]:
                    await self.send(h["name"], {"type": "stop_agent", "account": a["id"]})
                    self.db.update("accounts", "id", a["id"], status="stopped", host=None, current_chat=None)
            elif len(mine) < want:
                cands = self.db.q("SELECT * FROM accounts WHERE enabled=1 AND has_state=1 AND in_config=1 "
                                  "AND (host IS NULL OR status IN ('offline','stopped')) ORDER BY errors ASC, last_work_at ASC")
                cands = [a for a in cands if self.provider_enabled(a["provider"]) and a["id"] not in paused
                         and (not a["pin_host"] or a["pin_host"] == h["name"]) and a["cooldown_until"] < now]
                cands.sort(key=lambda a: (a["pin_host"] != h["name"],))
                for a in cands[: want - len(mine)]:
                    await self.start_agent(a["id"], h["name"])

    async def start_agent(self, account: str, host: str) -> bool:
        acc = self.db.one("SELECT * FROM accounts WHERE id=?", (account,))
        state_file = SECRETS_DIR / f"{account}.json"
        prov = self.providers.get(acc["provider"]) if acc else None
        if not acc or (not state_file.exists() and not (prov or {}).get("no_login")):
            return False
        if not prov:
            self.ev("error", "agent", f"Unknown provider '{acc['provider']}' for {account}", code="E_CONFIG",
                    account=account)
            self.db.update("accounts", "id", account, enabled=0)
            return False
        ok = await self.send(host, {"type": "start_agent", "account": account, "provider": prov,
                                    "storage_state": json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists()
                                    else {"cookies": [], "origins": []}})
        if ok:
            self.db.update("accounts", "id", account, host=host, status="starting", status_since=time.time())
        return ok

    async def check_health(self) -> None:
        now = time.time()
        timeout = float(self.cfg["heartbeat_timeout_s"])
        for h in self.db.q("SELECT * FROM hosts WHERE online=1"):
            if now - h["last_seen"] > timeout:
                self.db.update("hosts", "name", h["name"], online=0)
                self.ev("error", "host", f"Computer {h['name']} stopped responding", code="E_WORKER_OFFLINE",
                        host=h["name"])
        offline_hosts = {h["name"] for h in self.db.q("SELECT name FROM hosts WHERE online=0")}
        for h in offline_hosts:
            row = self.db.one("SELECT last_seen FROM hosts WHERE name=?", (h,))
            if row and now - row["last_seen"] > timeout:
                for c in self.db.q(f"SELECT * FROM chats WHERE host=? AND status IN {ACTIVE_CHAT}", (h,)):
                    await self._chat_failed(c["id"], f"computer {h} went offline", "E_WORKER_OFFLINE", h)
                self.db.x("UPDATE accounts SET status='offline', host=NULL, current_chat=NULL WHERE host=?", (h,))
        # stuck chats
        limit = float(self.cfg["task_timeout_min"]) * 60
        for c in self.db.q(f"SELECT * FROM chats WHERE status IN ('assigned','running')"):
            if now - (c["updated"] or c["started"]) > limit:
                await self.send(c["host"], {"type": "cancel", "chat_id": c["id"]})
                await self._chat_failed(c["id"], f"no progress for {int(limit/60)} min", "E_TIMEOUT", c["host"])
        # cooldowns that ended
        for a in self.db.q("SELECT * FROM accounts WHERE status='cooldown' AND cooldown_until<? AND host IS NOT NULL", (now,)):
            await self.send(a["host"], {"type": "cooldown_over", "account": a["id"]})

    async def loop(self) -> None:
        tick = 0
        while True:
            try:
                await self.check_health()
                await self.allocate()
                await self.schedule()
                if tick % 15 == 0:
                    await self.game.periodic()
            except Exception as e:  # never let the loop die
                import traceback
                traceback.print_exc()
                self.ev("error", "coordinator", f"loop error: {e}")
            tick += 1
            await asyncio.sleep(2)

    # ------------------------------------------------------------------ misc
    def save_storage_state(self, account: str, state: dict) -> None:
        (SECRETS_DIR / f"{account}.json").write_text(json.dumps(state), encoding="utf-8")
        self.db.update("accounts", "id", account, has_state=1, state_updated=time.time())

    def log_claude_tokens(self, tin: int, tout: int) -> None:
        self.db.x("INSERT INTO token_log VALUES(?,?,?,?,?,?)", (time.time(), "claude", "claude", "commander", tin, tout))
        self.db.kv_set("claude_last_seen", time.time())


def new_token() -> str:
    return pysecrets.token_urlsafe(9)
