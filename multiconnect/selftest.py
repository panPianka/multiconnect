"""`multiconnect selftest` - proves the whole pipeline works on THIS computer in ~2-3 minutes.

Starts, in a throw-away folder (never touches the real state/, agents/ or game):
  fake chat site (mock provider)  ->  commander on 127.0.0.1  ->  worker with 3 browser agents
then acts like Claude: contract, 3 tasks (one depends on another), the fix loop after a broken file,
integration into a git game folder, Godot check, backup. Prints PASS/FAIL per step.
Only 127.0.0.1 is used, so the Windows firewall is never involved."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from .common.config import ROOT


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _port_busy(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


class Result:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def __call__(self, ok: bool, what: str, detail: str = "") -> bool:
        self.rows.append((ok, what, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {what}" + (f"  - {detail}" if detail else ""), flush=True)
        return ok


def _fake_human(stop) -> None:
    """Plays the human of the manual relay: answers /manual/inbox items with the mock brain.
    Every 2nd answer is pasted WITHOUT code fences (like a careless copy) to test the repair."""
    from .mockchat.server import brain
    convs: dict[str, dict] = {}
    n = 0
    while not stop.is_set():
        try:
            items = httpx.get("http://127.0.0.1:8790/api/manual/pending", timeout=5).json()["items"]
        except Exception:
            items = []
        for it in items:
            conv = convs.setdefault(it["conv"], {"turn": 0, "log": []})
            reply = brain(conv, it["text"])
            n += 1
            if n % 2 == 0:
                reply = "\n".join(l for l in reply.split("\n") if not l.startswith("```"))
            httpx.post("http://127.0.0.1:8790/api/manual/reply", json={"conv": it["conv"], "text": reply}, timeout=5)
        stop.wait(1)


def run_selftest(mode: str = "headless", agents: int = 3, keep: bool = False, broken: bool = True,
                 timeout_s: int = 420, manual: bool = False) -> int:
    import threading
    base_dir = ROOT / "state" / "selftest" / time.strftime("%Y%m%d-%H%M%S")
    base_dir.mkdir(parents=True, exist_ok=True)
    port, disc = _free_port(), _free_port()
    token = "selftest-" + os.urandom(4).hex()
    base = f"http://127.0.0.1:{port}"
    H = {"x-team-token": token}
    acc_ids = [f"selftest{i}" for i in range(1, agents + 1)]
    provider = "manual" if manual else "mock"
    (base_dir / "accounts.yaml").write_text(yaml.safe_dump({"accounts": [
        {"id": a, "provider": provider, "owner": "selftest"} for a in acc_ids]}), encoding="utf-8")
    human_stop = threading.Event()
    (base_dir / "commander.yaml").write_text(yaml.safe_dump({
        "team_token": token, "host": "127.0.0.1", "port": port, "discovery_port": disc,
        "game_dir": (base_dir / "game").as_posix(), "backup_dir": (base_dir / "backups").as_posix(),
        "accounts_file": (base_dir / "accounts.yaml").as_posix(), "default_agents_per_host": agents,
        "max_rounds": 8, "backup_every_min": 0}), encoding="utf-8")
    (base_dir / "worker.yaml").write_text(yaml.safe_dump({
        "name": "selftest-pc", "coordinator_url": base, "team_token": token, "discovery_port": disc,
        "browser_mode": mode}), encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1", MC_STATE_DIR=str(base_dir / "state"),
               MC_AGENTS_DIR=str(base_dir / "agents"), MOCK_DELAY="0.5", MOCK_BROKEN="1" if broken else "")
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
    procs: list[subprocess.Popen] = []
    logs = {}

    def start(name: str, *args: str) -> None:
        logs[name] = open(base_dir / f"{name}.log", "w", encoding="utf-8")
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        procs.append(subprocess.Popen([sys.executable, "-m", "multiconnect.cli", *args], cwd=str(ROOT), env=env,
                                      stdout=logs[name], stderr=subprocess.STDOUT, creationflags=flags))

    def c(method: str, path: str, body=None, timeout: float = 60, params=None):
        r = httpx.request(method, base + path, json=body, params=params, headers=H, timeout=timeout)
        return r.json()

    def wait(pred, secs: float):
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                v = pred()
                if v:
                    return v
            except Exception:
                pass
            time.sleep(1)
        return None

    R = Result()
    print(f"MultiConnect selftest  (folder {base_dir})", flush=True)
    t_start = time.time()
    try:
        # 1. fake chat site
        if _port_busy(8790):
            ok = wait(lambda: "MockChat" in httpx.get("http://127.0.0.1:8790/login", timeout=3).text, 5)
            if not R(bool(ok), "fake chat site", "port 8790 is used by something else - close it" if not ok
                     else "already running (reused; fix-loop test may be skipped)"):
                return 1
        else:
            start("mockchat", "mockchat", "--port", "8790")
            if not R(bool(wait(lambda: httpx.get("http://127.0.0.1:8790/login", timeout=3).status_code == 200, 30)),
                     "fake chat site starts"):
                return 1
        # 2. commander
        start("commander", "commander", "--config", str(base_dir / "commander.yaml"))
        if not R(bool(wait(lambda: httpx.get(base + "/api/ping", timeout=3).status_code == 200, 40)),
                 "commander starts", base):
            return 1
        R(httpx.get(base + "/api/p/state", timeout=5).status_code == 401, "panel rejects a missing team token")
        cookie = {"cookies": [{"name": "mock_session", "value": "selftest", "domain": "127.0.0.1", "path": "/",
                               "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax"}], "origins": []}
        if manual:
            httpx.post(f"{base}/api/p/provider/manual", json={"enabled": True}, headers=H, timeout=10).raise_for_status()
            threading.Thread(target=_fake_human, args=(human_stop,), daemon=True).start()
            print("  (manual relay mode: a simulated human answers on /manual/inbox)", flush=True)
        else:
            for a in acc_ids:
                httpx.post(f"{base}/api/w/state/{a}", json=cookie, headers=H, timeout=10).raise_for_status()
        # 3. worker + browser agents
        start("worker", "worker", "--config", str(base_dir / "worker.yaml"))
        idle = wait(lambda: sum(1 for a in c("GET", "/api/p/state")["agents"] if a["status"] == "idle") >= agents, 120)
        st = c("GET", "/api/p/state")
        if not R(bool(idle), f"worker connects and {agents} browser agents become idle",
                 ", ".join(f"{a['id']}={a['status']}" for a in st["agents"])):
            return 1
        host = (st.get("hosts") or [{}])[0].get("sys", {})
        if host.get("godot"):
            R(True, "Godot found on this computer")
        else:
            print("  [WARN] Godot NOT found on this computer - agents still work, but their code is only "
                  "syntax-checked here. Run setup.bat or put Godot_*_console.exe into .godot_bin\\", flush=True)
        # 4. work
        c("POST", "/api/c/contract", {"text": "# Contract\nAutoload Events (scripts/events.gd).\n"})
        res = c("POST", "/api/c/dispatch", {"tasks": [
            {"id": "T001", "title": "Health", "goal": "Health node.", "files_to_create": ["scripts/health.gd"]},
            {"id": "T002", "title": "Score UI", "goal": "Score label.", "files_to_create": ["scripts/score_ui.gd"]},
            {"id": "T003", "title": "Enemy", "goal": "Uses health.", "files_to_create": ["scripts/enemy.gd"],
             "depends_on": ["T001"]}]})
        R(not res.get("errors"), "dispatch 3 tasks", json.dumps(res.get("errors")) if res.get("errors") else "")
        done, integrated, t0 = set(), [], time.time()
        while len(done) < 3 and time.time() - t0 < timeout_s:
            evs = c("GET", "/api/c/events", timeout=90, params={"timeout": 30})
            for e in evs.get("events", []):
                if e.get("kind") == "task_done" and e["task"] not in done:
                    done.add(e["task"])
                    ir = c("POST", "/api/c/integrate", {"task_id": e["task"]}, timeout=300)
                    R(bool(ir.get("ok")) and ir.get("check", {}).get("ok", False),
                      f"{e['task']} finished by an agent and integrated", (e.get("message") or "")[:90])
                    integrated.append(e["task"])
                elif e.get("kind") == "task" and "FAILED" in (e.get("message") or ""):
                    done.add(e["task"])
                    R(False, f"{e['task']} finished", e.get("message", "")[:200])
        R(len(integrated) == 3, "all 3 tasks done (T003 waited for T001)",
          f"{len(integrated)}/3 in {int(time.time() - t0)} s")
        if integrated:
            order_ok = "T003" not in integrated or integrated.index("T003") > integrated.index("T001")
            R(order_ok, "dependency order respected")
        chk = c("POST", "/api/c/check", {}, timeout=300)
        R(bool(chk.get("ok")), "whole-game Godot check", "; ".join(chk.get("errors", [])[:3]))
        snap = c("POST", "/api/c/snapshot", {"label": "selftest"}, timeout=300)
        R(bool(snap.get("ok")) and bool(snap.get("backup")), "backup zip + git commit + HANDOFF.md",
          (snap.get("backup") or {}).get("name", ""))
        tok = c("GET", "/api/p/state")["tokens"]
        R(tok.get("total", 0) > 0, "token counting", f"{tok.get('total')} tokens")
        R(subprocess.run(["git", "-C", str(base_dir / "game"), "log", "--oneline"], capture_output=True,
                         text=True).stdout.count("\n") >= 3, "game folder has git history")
    except Exception as e:
        R(False, "selftest crashed", repr(e)[:300])
    finally:
        human_stop.set()
        for p in procs:
            p.terminate()
        time.sleep(1.5)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        for f in logs.values():
            f.close()
    bad = [r for r in R.rows if not r[0]]
    print("-" * 64)
    print(f"SELFTEST {'PASSED' if not bad else 'FAILED'}  ({len(R.rows) - len(bad)}/{len(R.rows)} ok, "
          f"{int(time.time() - t_start)} s)")
    if bad:
        print(f"Logs kept in {base_dir}  (commander.log, worker.log, mockchat.log)")
    elif not keep:
        def _force(func, path, _exc):  # git object files are read-only on Windows
            try:
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass
        for _ in range(3):  # Godot/git may still hold a handle for a moment
            shutil.rmtree(base_dir, onerror=_force)
            if not base_dir.exists():
                break
            time.sleep(1)
    return 0 if not bad else 1
