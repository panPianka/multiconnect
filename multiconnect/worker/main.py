"""Worker daemon: runs on every computer with agents. Connects OUT to the
coordinator (so only the commander needs an open port), runs agents, uploads results."""
from __future__ import annotations

import asyncio
import uuid
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx
import psutil
import websockets

from ..common.config import AGENTS_DIR, ROOT, load_worker_config
from ..common.godot import Godot, find_godot
from .agent import AgentRuntime
from .browser import BrowserManager, interactive_login

VERSION = "0.1.0"


async def discover(port: int, timeout: float = 15) -> str | None:
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            try:
                m = json.loads(data)
                if m.get("mc") == 1 and not fut.done():
                    fut.set_result(f"http://{addr[0]}:{m['port']}")
            except Exception:
                pass
    try:
        transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("0.0.0.0", port), allow_broadcast=True,
                                                           reuse_port=None)
    except OSError:
        return None
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        transport.close()


class Worker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.token = cfg["team_token"]
        self.base = cfg.get("coordinator_url", "").rstrip("/")
        self.browser = BrowserManager(cfg)
        cores = psutil.cpu_count(logical=False) or 2
        self.godot = Godot(find_godot(cfg.get("godot_path", "")), int(cfg.get("max_parallel_godot") or max(1, cores // 2)))
        self.agents: dict[str, AgentRuntime] = {}
        self.ws = None
        self.send_lock = asyncio.Lock()
        self._bundle_locks: dict[str, asyncio.Lock] = {}
        self.outbox_lock = asyncio.Lock()
        self.providers: dict = {}
        self.cache = AGENTS_DIR / "_cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.outbox_file = AGENTS_DIR / "_outbox.json"
        self.http: httpx.AsyncClient | None = None
        self.logins: set[str] = set()
        psutil.cpu_percent(None)

    # ------------------------------------------------------------------ transport
    async def send(self, msg: dict) -> None:
        ws = self.ws
        if not ws:
            return
        try:
            async with self.send_lock:
                await ws.send(json.dumps(msg))
        except Exception:
            pass

    async def event(self, level: str, message: str, code: str = "", account: str = "", chat_id: str = "",
                    kind: str = "agent", **data) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {level.upper()} {message}")
        await self.send({"type": "event", "level": level, "message": message, "code": code, "account": account,
                         "chat_id": chat_id, "kind": kind, "data": data or None})

    def headers(self) -> dict:
        return {"x-team-token": self.token}

    async def get_bundle(self, h: str) -> Path:
        p = self.cache / f"bundle-{h}.zip"
        lock = self._bundle_locks.setdefault(h, asyncio.Lock())
        async with lock:  # several agents start on the same snapshot at once
            if not p.exists():
                r = await self.http.get(f"{self.base}/api/w/bundle/{h}", headers=self.headers(), timeout=120)
                r.raise_for_status()
                tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
                tmp.write_bytes(r.content)
                try:
                    tmp.replace(p)
                except OSError:
                    tmp.unlink(missing_ok=True)
                old = sorted(self.cache.glob("bundle-*.zip"), key=lambda x: x.stat().st_mtime)
                for o in old[:-8]:
                    try:
                        o.unlink(missing_ok=True)
                    except OSError:
                        pass  # still open in a sandbox copy
        return p

    async def get_result(self, chat_id: str, version: int) -> bytes | None:
        local = AGENTS_DIR.glob(f"*/chats/{chat_id}/result_v{version}.zip")
        for p in local:
            return p.read_bytes()
        r = await self.http.get(f"{self.base}/api/w/result/{chat_id}/{version}", headers=self.headers(), timeout=60)
        return r.content if r.status_code == 200 else None

    async def upload_state(self, account: str, state: dict) -> None:
        try:
            await self.http.post(f"{self.base}/api/w/state/{account}", json=state, headers=self.headers(), timeout=30)
        except Exception:
            pass

    async def upload_screenshot(self, account: str, data: bytes) -> None:
        try:
            await self.http.post(f"{self.base}/api/w/screenshot/{account}", content=data, headers=self.headers(),
                                 timeout=30)
        except Exception:
            pass

    # ------------------------------------------------------------------ outbox (results survive network drops)
    def _outbox(self) -> list:
        try:
            return json.loads(self.outbox_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    async def outbox_add(self, chat_id: str, version: int, path: Path) -> None:
        async with self.outbox_lock:
            box = self._outbox()
            box.append({"chat_id": chat_id, "version": version, "path": str(path)})
            self.outbox_file.write_text(json.dumps(box), encoding="utf-8")
        await self.flush_outbox()

    async def flush_outbox(self) -> None:
        async with self.outbox_lock:  # one flusher at a time: no double uploads, no lost entries
            await self._flush_outbox_locked()

    async def _flush_outbox_locked(self) -> None:
        box = self._outbox()
        if not box or not self.http or not self.base:
            return
        left = []
        for item in box:
            try:
                p = Path(item["path"])
                files = {"file": (p.name, p.read_bytes(), "application/zip")}
                r = await self.http.post(f"{self.base}/api/w/result", headers=self.headers(), timeout=60,
                                         data={"meta": json.dumps({"chat_id": item["chat_id"], "version": item["version"]})},
                                         files=files)
                if r.status_code != 200:
                    raise RuntimeError(r.text[:200])
            except FileNotFoundError:
                continue
            except Exception as e:
                left.append(item)
                print(f"upload of {item['chat_id']} v{item['version']} failed, will retry: {e}")
        self.outbox_file.write_text(json.dumps(left), encoding="utf-8")

    # ------------------------------------------------------------------ system info
    def sysinfo(self) -> dict:
        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage(str(ROOT))
            free_gb = round(disk.free / 1e9, 1)
        except Exception:
            free_gb = None
        return {"ram_total_gb": round(vm.total / 1e9, 1), "ram_used_pct": vm.percent,
                "ram_free_gb": round(vm.available / 1e9, 1), "cpu_pct": psutil.cpu_percent(None),
                "cores": psutil.cpu_count(), "disk_free_gb": free_gb, "agents": len(self.agents),
                "pages_open": sum(1 for a in self.agents.values() if a.page and not a.page.is_closed()),
                "mode": self.browser.mode, "godot": bool(self.godot.path), "outbox": len(self._outbox()),
                "py": sys.version.split()[0]}

    # ------------------------------------------------------------------ finding the commander
    def _remember_commander(self, url: str) -> None:
        try:
            (AGENTS_DIR / "_last_commander.txt").write_text(url, encoding="utf-8")
        except OSError:
            pass

    async def find_commander(self) -> str:
        """configured URL -> last working URL -> UDP beacon -> outbound subnet scan (works behind firewalls)."""
        from ..common.net import scan_for_commander
        port = int(self.cfg.get("coordinator_port", 8765))
        cands = []
        if self.cfg.get("coordinator_url"):
            cands.append(self.cfg["coordinator_url"].rstrip("/"))
        try:
            cands.append((AGENTS_DIR / "_last_commander.txt").read_text(encoding="utf-8").strip())
        except OSError:
            pass
        for u in dict.fromkeys(c for c in cands if c):
            try:
                r = await self.http.get(u + "/api/w/accounts", headers=self.headers(), timeout=4)
                if r.status_code == 200:
                    return u
                if r.status_code == 401:
                    print(f"Commander at {u} rejects our team_token - copy team_token from the commander's config.")
            except Exception:
                pass
        print("Looking for the commander on the LAN (UDP beacon, 5 s)...")
        u = await discover(int(self.cfg.get("discovery_port", 8766)), timeout=5)
        if u:
            return u
        print(f"No beacon (often blocked by the firewall). Scanning the local network for port {port}...")
        u = await scan_for_commander(port, self.token)
        return u or ""

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        self.http = httpx.AsyncClient()
        try:  # local sites: test chat (provider mock) + manual relay inbox (provider manual), 127.0.0.1 only
            from ..mockchat.server import start_in_thread
            if start_in_thread(8790):
                print("Manual relay inbox (plan B): http://127.0.0.1:8790/manual/inbox")
        except Exception as e:
            print(f"local sites not started: {e}")
        asyncio.create_task(self.background())
        delay = 2
        while True:
            if not self.base:
                self.base = await self.find_commander()
                if not self.base:
                    print("Commander not found. Is commander.bat running? Same network? Same team_token?\n"
                          "  Fix: put coordinator_url: http://<commander-ip>:8765 into config/worker.yaml, or run\n"
                          "  netcheck.bat <commander-ip> on this PC to see what blocks the connection. Retrying...")
                    await asyncio.sleep(5)
                    continue
                print(f"Found commander at {self.base}")
            ws_url = self.base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws/worker?token={self.token}"
            try:
                async with websockets.connect(ws_url, max_size=50 * 1024 * 1024, ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws
                    delay = 2
                    self.fails = 0
                    self._remember_commander(self.base)
                    await ws.send(json.dumps({"type": "hello", "host": self.name, "version": VERSION,
                                              "godot": self.godot.path or "", "sys": self.sysinfo(),
                                              "mode": self.browser.mode,
                                              "agents": [{"account": a.account, "status": a.status,
                                                          "chat_id": a.chat_id} for a in self.agents.values()]}))
                    print(f"Connected to commander as '{self.name}'")
                    await self.flush_outbox()
                    async for raw in ws:
                        try:
                            await self.handle(json.loads(raw))
                        except Exception as e:
                            import traceback
                            traceback.print_exc()
                            await self.event("error", f"worker error handling message: {e}", code="E_WORKER")
            except Exception as e:
                print(f"Connection to commander lost/failed ({e}); retrying in {delay}s")
            self.ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)
            self.fails = getattr(self, "fails", 0) + 1
            if delay >= 20 and (not self.cfg.get("coordinator_url") or self.fails % 6 == 0):
                self.base = ""  # re-discover: the commander may have moved to another PC / IP

    async def background(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(5)
            tick += 1
            try:
                await self.send({"type": "heartbeat", "sys": {**self.sysinfo(), "browser_ram_mb": await self.browser.chromium_ram_mb()},
                                 "agents": [a.info() for a in self.agents.values()]})
                for a in list(self.agents.values()):
                    await a.close_page_if_idle(float(self.cfg.get("idle_close_page_s", 180)))
                    if a.status == "cooldown" and a.cooldown_until and time.time() > a.cooldown_until:
                        await a.set_status("idle", "cooldown over")
                if tick % 6 == 0:
                    await self.flush_outbox()
                if self.browser.browser and not self.browser.browser.is_connected():
                    await self.event("error", "Browser crashed - restarting it", code="E_BROWSER_CRASH")
                    await self.restart_browser()
            except Exception as e:
                print("background error:", e)

    async def restart_browser(self) -> None:
        busy = [a for a in self.agents.values() if a.task and not a.task.done()]
        for a in busy:
            a.task.cancel()
        await self.browser.stop()
        await self.browser.start()
        for a in self.agents.values():
            try:
                await a.recreate_context()
                await a.set_status("idle", "browser restarted")
            except Exception as e:
                await a.set_status("error", f"context failed: {e}")

    # ------------------------------------------------------------------ message handlers
    async def handle(self, m: dict) -> None:
        t = m.get("type")
        acc = self.agents.get(m.get("account", ""))
        if t == "welcome":
            self.providers = m.get("providers", {})
            if m.get("mode") and m["mode"] != self.browser.mode:
                await self.set_mode(m["mode"])
        elif t == "providers":
            self.providers = m.get("providers", {})
            for a in self.agents.values():
                newp = self.providers.get(a.provider.get("id"))
                if newp:
                    a.provider = newp
                    if a.driver:
                        a.driver.prov = newp
                        a.driver.sel = newp.get("selectors", {})
        elif t == "start_agent":
            if m["account"] in self.agents:
                await self.agents[m["account"]].set_status(self.agents[m["account"]].status)
                return
            rt = AgentRuntime(self, m["account"], m["provider"], m.get("storage_state"))
            self.agents[m["account"]] = rt
            await rt.start()
        elif t == "stop_agent":
            if acc:
                self.agents.pop(acc.account, None)
                await acc.stop()
        elif t == "assign":
            if not acc:
                await self.send({"type": "chat_failed", "chat_id": m["chat_id"], "reason": "agent not running here",
                                 "code": "E_NO_AGENT"})
                return
            acc.assign(m)
        elif t == "answer":
            for a in self.agents.values():
                a.answer(m["chat_id"], m["text"])
        elif t == "cancel":
            for a in self.agents.values():
                a.cancel(m["chat_id"])
        elif t == "cooldown_over":
            if acc and acc.status == "cooldown":
                acc.cooldown_until = 0
                await acc.set_status("idle", "cooldown cleared")
        elif t == "screenshot":
            await self.reply(m, await self.screenshot(acc))
        elif t in ("show_agent", "hide_agent"):
            if acc:
                await acc.ensure_page()
                await self.browser.show(acc.page, acc.account, show=(t == "show_agent"))
        elif t == "reload_agent":
            if acc and acc.page and not (acc.task and not acc.task.done()):
                await acc.page.reload()
        elif t == "restart_agent":
            if acc:
                prov, state = acc.provider, acc.storage_state
                await acc.stop()
                rt = AgentRuntime(self, acc.account, prov, state)
                self.agents[acc.account] = rt
                await rt.start()
        elif t == "set_mode":
            await self.set_mode(m["mode"])
        elif t == "login":
            asyncio.create_task(self.do_login(m["account"], m.get("provider") or {}))
        elif t == "test_selector":
            await self.reply(m, await self.test_selector(acc, m.get("selector", "")))
        elif t == "get_transcript":
            p = next(iter(AGENTS_DIR.glob(f"*/chats/{m['chat_id']}/transcript.md")), None)
            await self.reply(m, {"ok": bool(p), "text": p.read_text(encoding="utf-8")[-200_000:] if p else ""})
        elif t == "mirror_backup":
            asyncio.create_task(self.mirror_backup(m["name"]))
        elif t == "stop_all":
            for a in list(self.agents.values()):
                await a.stop()
            self.agents.clear()
        elif t == "restart":
            await self.event("warn", f"{self.name}: restarting worker on request", kind="host")
            for a in list(self.agents.values()):
                await a.stop()
            await self.browser.stop()
            os._exit(3)  # worker.bat restarts on exit code 3

    async def reply(self, m: dict, data: dict) -> None:
        await self.send({"type": "reply", "req_id": m.get("req_id"), "data": data})

    async def set_mode(self, mode: str) -> None:
        if mode not in ("mini", "hidden", "headless") or mode == self.browser.mode:
            return
        old = self.browser.mode
        self.browser.mode = mode
        if self.browser.browser:
            if (old == "headless") != (mode == "headless"):
                await self.restart_browser()
            else:
                for a in self.agents.values():
                    if a.page and not a.page.is_closed():
                        try:
                            await self.browser.show(a.page, a.account, show=False)
                        except Exception:
                            pass
        await self.event("info", f"{self.name}: browser mode -> {mode}", kind="host")

    async def screenshot(self, acc: AgentRuntime | None) -> dict:
        if not acc:
            return {"ok": False, "error": "agent not running"}
        try:
            if not acc.page or acc.page.is_closed():
                if acc.task and not acc.task.done():
                    return {"ok": False, "error": "no page"}
                await acc.ensure_page()
                await acc.page.goto(acc.provider.get("new_chat_url") or acc.provider.get("home_url"),
                                    wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2)
            data = await acc.page.screenshot(type="jpeg", quality=45)
            await self.upload_screenshot(acc.account, data)
            return {"ok": True, "url": acc.page.url, "ts": time.time()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def test_selector(self, acc: AgentRuntime | None, selector: str) -> dict:
        if not acc:
            return {"ok": False, "error": "agent not running here"}
        if acc.task and not acc.task.done():
            pass  # highlighting is harmless while working
        try:
            await acc.ensure_page()
            if acc.page.url in ("about:blank", ""):
                await acc.page.goto(acc.provider.get("new_chat_url") or acc.provider.get("home_url"),
                                    wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2)
            from .providers.generic import ChatDriver
            d = acc.driver or ChatDriver(acc.page, acc.provider)
            res = await d.highlight(selector) if selector else {}
            state = await d.state()
            data = await acc.page.screenshot(type="jpeg", quality=55)
            await self.upload_screenshot(acc.account, data)
            return {"ok": True, "result": res, "page_state": {k: state.get(k) for k in
                    ("count", "generating", "input", "login", "logged_in", "url", "title", "captcha")}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def do_login(self, account: str, prov: dict) -> None:
        if account in self.logins:
            return
        self.logins.add(account)
        try:
            await self.event("info", f"Login window opened for {account} on {self.name} - log in, then close the window",
                             account=account, kind="login")
            await self.send({"type": "agent_status", "account": account, "status": "login",
                             "detail": f"login window open on {self.name}"})
            state = None
            try:
                r = await self.http.get(f"{self.base}/api/w/state/{account}", headers=self.headers(), timeout=10)
                if r.status_code == 200:
                    state = r.json()
            except Exception:
                pass
            res = await interactive_login(prov, state)
            ok = bool(res)
            if ok:
                await self.upload_state(account, res["state"])
            await self.send({"type": "login_result", "account": account, "ok": ok,
                             "verified": bool(res and res.get("verified"))})
            await self.send({"type": "agent_status", "account": account,
                             "status": "offline" if account not in self.agents else "idle", "detail": ""})
            if ok and account in self.agents:
                self.agents[account].storage_state = res["state"]
                await self.agents[account].recreate_context()
        except Exception as e:
            await self.event("error", f"login failed for {account}: {e}", account=account, code="E_LOGIN")
        finally:
            self.logins.discard(account)

    async def mirror_backup(self, name: str) -> None:
        d = ROOT / "mirror_backups"
        d.mkdir(exist_ok=True)
        try:
            r = await self.http.get(f"{self.base}/api/w/backup/{name}", headers=self.headers(), timeout=300)
            r.raise_for_status()
            (d / name).write_bytes(r.content)
            for pattern in ("game-*.zip", "hive-*.zip"):
                old = sorted(d.glob(pattern), key=lambda p: p.name)
                for o in old[:-6]:
                    o.unlink(missing_ok=True)
        except Exception as e:
            print("mirror backup failed:", e)


def run_worker(config_path: str | None = None) -> None:
    cfg = load_worker_config(config_path)
    w = Worker(cfg)
    print(f"MultiConnect worker '{cfg['name']}' | Godot: {w.godot.path or 'NOT FOUND'} | mode: {cfg['browser_mode']}")
    try:
        asyncio.run(w.run())
    except KeyboardInterrupt:
        pass
