"""MultiConnect command line.

  multiconnect commander            start the coordinator + web panel (commander PC)
  multiconnect worker               start a worker (every agent PC, also the commander PC)
  multiconnect login <account>      log one account in by hand (small browser window)
  multiconnect mcp                  MCP stdio server for Claude Code / Codex
  multiconnect setup                interactive wizard: writes config/*.yaml
  multiconnect doctor               check Python, Chromium, Godot, network, configs
  multiconnect mockchat             fake chat site for testing (http://127.0.0.1:8790)
  multiconnect selftest             full pipeline test on this PC (fake chat, ~2-3 min, touches nothing real)
  multiconnect solo                 commander + worker on this PC only (no network needed)
  multiconnect netcheck [ip]        LAN / firewall diagnosis (run on commander, then on a worker with its IP)
  multiconnect restore-hive [zip]   move the commander to this PC from the newest hive-*.zip (--game: also the game)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import shutil
import socket
import sys
from pathlib import Path

from .common.config import (CONFIG_DIR, ROOT, STATE_DIR, load_commander_config, load_providers,
                            load_worker_config, load_yaml, resolve_path, save_yaml)


# ---------------------------------------------------------------------------- commander
def cmd_commander(a) -> None:
    import logging
    import uvicorn
    from .coordinator.app import create_app

    cfg = load_commander_config(a.config)
    if cfg["team_token"] == "change-me":
        print("WARNING: team_token is still 'change-me'. Run setup.bat (multiconnect setup) first.")
    logs = STATE_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(logs / "commander.log", encoding="utf-8"),
                                                      logging.StreamHandler()])
    ips = _local_ips()
    print("=" * 64)
    print(" MultiConnect commander")
    print(f" Panel:      http://127.0.0.1:{cfg['port']}/   (LAN: " +
          ", ".join(f"http://{ip}:{cfg['port']}/" for ip in ips) + ")")
    print(f" Game dir:   {cfg['game_dir']}")
    print(f" Backups:    {cfg['backup_dir']}")
    print("=" * 64)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.get("host", "0.0.0.0"), port=int(cfg["port"]), log_level="warning",
                ws_max_size=64 * 1024 * 1024)


# ---------------------------------------------------------------------------- worker
def cmd_worker(a) -> None:
    from .worker.main import run_worker
    run_worker(a.config)


# ---------------------------------------------------------------------------- login
def cmd_login(a) -> None:
    """Standalone login: works on any PC that can reach the coordinator, even without a running worker."""
    import httpx
    from .worker.browser import interactive_login
    from .worker.main import discover

    cfg = load_worker_config(a.config)

    async def run():
        base = cfg["coordinator_url"] or await discover(int(cfg["discovery_port"]))
        if not base:
            print("Coordinator not found. Set coordinator_url in config/worker.yaml.")
            return 1
        base = base.rstrip("/")
        h = {"x-team-token": cfg["team_token"]}
        async with httpx.AsyncClient(timeout=30) as c:
            accs = (await c.get(f"{base}/api/w/accounts", headers=h)).json()
            if isinstance(accs, dict) and accs.get("error"):
                print("Error:", accs["error"])
                return 1
            acc = next((x for x in accs if x["id"] == a.account), None)
            if not acc:
                print(f"Unknown account '{a.account}'. Known: " + ", ".join(x["id"] for x in accs))
                return 1
            provs = (await c.get(f"{base}/api/w/providers", headers=h)).json()
            prov = provs.get(acc["provider"])
            if not prov:
                print(f"Provider '{acc['provider']}' has no config.")
                return 1
            old = None
            r = await c.get(f"{base}/api/w/state/{a.account}", headers=h)
            if r.status_code == 200:
                old = r.json()
        print(f"Opening a browser for {a.account} ({acc['provider']}). Log in, then close the window "
              f"(or wait - it is detected automatically).")
        res = await interactive_login(prov, old, on_progress=lambda s: print("  ", s, end="\r"))
        if not res or not res.get("state"):
            print("\nNo login state captured.")
            return 1
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/api/w/state/{a.account}", json=res["state"], headers=h)
        print(f"\nSaved login for {a.account} (verified: {bool(res.get('verified'))}, upload HTTP {r.status_code}).")
        return 0

    sys.exit(asyncio.run(run()) or 0)


# ---------------------------------------------------------------------------- mcp
def cmd_mcp(a) -> None:
    from .mcp_server.server import run_mcp
    run_mcp()


# ---------------------------------------------------------------------------- mockchat
def cmd_mockchat(a) -> None:
    from .mockchat.server import run_mock
    print(f"MockChat on http://127.0.0.1:{a.port}/  (provider id: mock)")
    run_mock(a.port)


# ---------------------------------------------------------------------------- setup wizard
def _ask(q: str, default: str = "") -> str:
    if not sys.stdin.isatty():
        return default
    v = input(f"{q} [{default}]: ").strip()
    return v or default


def _local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips) or ["127.0.0.1"]


def cmd_setup(a) -> None:
    print("MultiConnect setup wizard (Enter = default)\n")
    role = a.role or _ask("Is this the COMMANDER PC (runs Claude + panel) or a WORKER PC? (commander/worker)",
                          "commander")
    CONFIG_DIR.mkdir(exist_ok=True)
    if role.startswith("c"):
        p = CONFIG_DIR / "commander.yaml"
        cur = load_yaml(p) or load_yaml(CONFIG_DIR / "commander.example.yaml") or {}
        tok = cur.get("team_token")
        if not tok or tok == "change-me":
            tok = secrets.token_hex(4) + "-" + secrets.token_hex(4)
        cur["team_token"] = _ask("Team token (all PCs must use the same)", tok)
        cur["game_dir"] = _ask("Game folder (OUTSIDE this repo)", cur.get("game_dir", "F:/hackathon-game"))
        cur["backup_dir"] = _ask("Backup folder", cur.get("backup_dir", "F:/hackathon-backups"))
        cur["default_agents_per_host"] = int(_ask("Default agents per computer",
                                                  str(cur.get("default_agents_per_host", 6))))
        save_yaml(p, cur)
        print(f"\nWrote {p}")
        if not (CONFIG_DIR / "accounts.yaml").exists() and (CONFIG_DIR / "accounts.example.yaml").exists():
            shutil.copy(CONFIG_DIR / "accounts.example.yaml", CONFIG_DIR / "accounts.yaml")
            print(f"Wrote {CONFIG_DIR / 'accounts.yaml'} - EDIT IT: list every chat account.")
        token = cur["team_token"]
    else:
        token = None
    p = CONFIG_DIR / "worker.yaml"
    w = load_yaml(p) or load_yaml(CONFIG_DIR / "worker.example.yaml") or {}
    w["team_token"] = token or _ask("Team token (shown on the commander's setup / panel)", w.get("team_token", ""))
    w["coordinator_url"] = _ask("Coordinator URL (empty = auto-discover on LAN)",
                                w.get("coordinator_url", "") if not token else "http://127.0.0.1:8765")
    w["name"] = _ask("Computer name shown in the panel", w.get("name") or os.environ.get("COMPUTERNAME", ""))
    w["browser_mode"] = _ask("Browser windows: mini | hidden | headless", w.get("browser_mode", "mini"))
    save_yaml(p, w)
    print(f"Wrote {p}\n")
    if token:
        print(f"Team token: {token}\nOn every worker PC run setup.bat, choose 'worker' and enter this token.")


# ---------------------------------------------------------------------------- doctor
def cmd_doctor(a) -> None:
    from .common.godot import find_godot

    ok = True

    def line(good: bool, what: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f" [{'OK' if good else '!!'}] {what}" + (f" - {detail}" if detail else ""))

    print("MultiConnect doctor\n")
    line(sys.version_info >= (3, 11), "Python", sys.version.split()[0])
    line(True, "Platform", f"{platform.system()} {platform.release()} | tool root {ROOT}")
    try:
        import psutil
        vm = psutil.virtual_memory()
        line(vm.total > 7.5e9, "RAM", f"{vm.total / 2**30:.1f} GB total, {vm.available / 2**30:.1f} GB free "
                                       f"(~{max(1, int(vm.available / 2**30 / 0.45))} agents fit)")
    except Exception as e:
        line(False, "psutil", str(e))
    bdir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ROOT / ".browsers"))
    chromes = list(bdir.glob("chromium-*")) if bdir.exists() else []
    line(bool(chromes), "Chromium (Playwright)", str(chromes[0]) if chromes else
         f"missing in {bdir} - run setup.bat (or set browser_channel: msedge in worker.yaml)")
    wcfg = load_worker_config()
    gd = find_godot(wcfg.get("godot_path") or None)
    line(bool(gd), "Godot", gd or "not found - put Godot console exe in .godot_bin\\ or set godot_path")
    for name in ("commander.yaml", "worker.yaml", "accounts.yaml"):
        p = CONFIG_DIR / name
        line(p.exists(), f"config/{name}", "present" if p.exists() else "missing (run setup.bat)")
    provs = load_providers()
    line(bool(provs), "Providers", ", ".join(f"{k}{'' if v.get('enabled') else '(off)'}" for k, v in provs.items()))
    ccfg = load_commander_config()
    if (CONFIG_DIR / "commander.yaml").exists():
        line(ccfg["team_token"] != "change-me", "Team token set")
        gdir = resolve_path(ccfg["game_dir"])
        line(not str(gdir.resolve()).startswith(str(ROOT.resolve())), "Game dir outside the tool repo", str(gdir))
    line(shutil.which("git") is not None, "git (for game history)", shutil.which("git") or
         "not found - integration still works, without git commits")
    url = wcfg.get("coordinator_url") or f"http://127.0.0.1:{ccfg['port']}"
    try:
        import httpx
        r = httpx.get(url.rstrip("/") + "/api/ping", timeout=3)
        line(r.status_code == 200, "Coordinator reachable", url)
        r = httpx.get(url.rstrip("/") + "/api/w/accounts", headers={"x-team-token": wcfg["team_token"]}, timeout=3)
        line(r.status_code == 200, "Worker team token accepted", f"HTTP {r.status_code}")
    except Exception as e:
        line(False, "Coordinator reachable", f"{url}: {type(e).__name__} (fine if the commander is not started yet)")
    print("\nAll good." if ok else "\nSome checks failed - see above.")


# ---------------------------------------------------------------------------- main
# ---------------------------------------------------------------------------- restore-hive
def _newest(patterns_dirs: list[tuple[Path, str]]) -> Path | None:
    found = []
    for d, pat in patterns_dirs:
        if d.exists():
            found += list(d.glob(pat))
    return max(found, key=lambda p: p.name[5:], default=None) if found else None  # name: hive-/game-<ts>


def cmd_restore_hive(a) -> None:
    """Commander PC died or must move: restore DB + logins + config from the newest hive-*.zip
    (workers keep mirror copies in mirror_backups/), optionally also the newest game zip."""
    import time
    import zipfile
    import httpx
    cfg = load_commander_config(None)
    dirs = [ROOT / "mirror_backups", resolve_path(cfg["backup_dir"])]
    src = Path(a.zip) if a.zip else _newest([(d, "hive-*.zip") for d in dirs])
    if not src or not src.exists():
        print("No hive-*.zip found in: " + ", ".join(str(d) for d in dirs))
        print("Copy one from the old commander's backup folder or another PC's mirror_backups\\ and pass its path.")
        sys.exit(1)
    try:
        if httpx.get(f"http://127.0.0.1:{cfg['port']}/api/ping", timeout=2).status_code == 200:
            print("A commander is running on this PC - close commander.bat first, then run this again.")
            sys.exit(1)
    except httpx.HTTPError:
        pass
    ts = time.strftime("%Y%m%d-%H%M%S")
    print(f"Restoring hive from {src}")
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if "state/multiconnect.db" not in names:
            print("Not a hive backup (no state/multiconnect.db inside).")
            sys.exit(1)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        db = STATE_DIR / "multiconnect.db"
        if db.exists():
            db.replace(STATE_DIR / f"multiconnect.db.before-restore-{ts}")
            print(f"  old DB kept as state/multiconnect.db.before-restore-{ts}")
        for ext in ("-wal", "-shm"):
            (STATE_DIR / f"multiconnect.db{ext}").unlink(missing_ok=True)
        db.write_bytes(z.read("state/multiconnect.db"))
        n = 0
        for nm in names:
            if nm.startswith("state/secrets/") and not nm.endswith("/"):
                rel = Path(nm[len("state/secrets/"):])
                if ".." in rel.parts:
                    continue
                dst = STATE_DIR / "secrets" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(z.read(nm))
                n += 1
        print(f"  DB restored, {n} login file(s) restored")
        for nm in ("config/commander.yaml", "config/accounts.yaml"):
            if nm not in names:
                continue
            dst = ROOT / nm
            if dst.exists() and not a.overwrite_config:
                alt = dst.with_name(dst.name + ".from-hive")
                alt.write_bytes(z.read(nm))
                print(f"  {nm} exists here - kept it; the backup copy is in {alt.name} (or use --overwrite-config)")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(z.read(nm))
                print(f"  {nm} restored")
    cfg = load_commander_config(None)
    game_dir = resolve_path(cfg["game_dir"])
    if a.game:
        gz = _newest([(ROOT / "mirror_backups", "game-*.zip"), (resolve_path(cfg["backup_dir"]), "game-*.zip")])
        if not gz:
            print("  no game-*.zip found - game folder not touched")
        else:
            if game_dir.exists() and any(game_dir.iterdir()):
                old = game_dir.with_name(game_dir.name + f".old-{ts}")
                game_dir.rename(old)
                print(f"  existing game folder moved to {old}")
            game_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(gz) as z:
                for nm in z.namelist():
                    if nm == "_hive/handoff_db.json" or ".." in Path(nm).parts:
                        continue
                    z.extract(nm, game_dir)
            print(f"  game restored from {gz.name} into {game_dir}")
    elif not game_dir.exists():
        print(f"  NOTE: game_dir {game_dir} does not exist on this PC. Run again with --game to unpack the newest "
              "game backup, or edit game_dir in config/commander.yaml.")
    print("Done. Start commander.bat. Workers find the new commander by themselves (LAN scan) -")
    print("if one does not, set coordinator_url in its config/worker.yaml or restart worker.bat.")
    print("Tasks that were running are re-queued automatically when their agents reconnect or time out.")


# ---------------------------------------------------------------------------- solo
def cmd_solo(a) -> None:
    """Plan D: commander + worker on this PC only (127.0.0.1) - works with no LAN at all."""
    import subprocess
    import time
    import httpx
    if not (CONFIG_DIR / "commander.yaml").exists():
        print("No config/commander.yaml on this PC yet - running the commander setup first.")
        cmd_setup(argparse.Namespace(role="commander"))
    cfg = load_commander_config(None)
    url = f"http://127.0.0.1:{cfg['port']}"
    os.environ["MC_COORDINATOR"] = url
    os.environ["MC_TOKEN"] = cfg["team_token"]
    flags = 0x00000010 if sys.platform == "win32" else 0  # CREATE_NEW_CONSOLE
    print(f"Starting the commander in a new window ({url}) ...")
    proc = subprocess.Popen([sys.executable, "-m", "multiconnect.cli", "commander"], cwd=str(ROOT),
                            creationflags=flags)
    for _ in range(60):
        try:
            if httpx.get(url + "/api/ping", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(1)
    print(f"Panel: {url}/   team token: {cfg['team_token']}")
    print("Claude Code on this PC: connect_claude.bat once, then open Claude Code in the game folder.")
    try:
        from .worker.main import run_worker
        run_worker(None)
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------- netcheck
def cmd_netcheck(a) -> None:
    """Network diagnosis. Typical use:
       commander PC:  netcheck --listen        (or just run commander.bat)
       worker PC:     netcheck 192.168.1.10    -> tells exactly which layer fails"""
    import time
    import httpx
    from .common import net

    port = a.port
    print("=" * 64)
    print(" This computer: " + socket.gethostname())
    for ip, plen in net.local_ipv4s():
        print(f"   IP {ip}/{plen}")
    prof = net.network_profiles()
    if prof:
        print(" Network profile(s):\n   " + prof.replace("\n", "\n   "))
        if "Public" in prof:
            print("   ! 'Public' network: Windows blocks INBOUND connections for new programs by default.")
            print("     Workers can still connect OUT. The COMMANDER PC must accept inbound -> test with --listen.")
    exes = {sys.executable, getattr(sys, "_base_executable", sys.executable)}
    for exe in exes:
        rules = net.firewall_rules_for(exe)
        print(f" Firewall rules for {exe}:")
        for r in rules or ["   (none - Windows will ask on first listen; without admin: click Cancel, then test)"]:
            print("   " + r)
            if "Block" in r and "Inbound" in r:
                print("   ! An inbound BLOCK rule exists for Python here: this PC cannot be the commander")
                print("     unless an admin removes it. Use another PC as commander, or tunnel.bat (plan C).")
    print("=" * 64)
    if a.listen:
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"ok": True, "name": "multiconnect-netcheck", "host": socket.gethostname()}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                print(f"   <- probe from {self.client_address[0]}  (INBOUND WORKS)")

            def log_message(self, *args):
                pass
        try:
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
        except OSError as e:
            print(f"Cannot listen on port {port}: {e}. Is the commander already running? Stop it or use --port.")
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Listening on port {port} for {a.seconds} s. On ANOTHER PC run:  netcheck.bat <one of the IPs above>")
        print("If a Windows Firewall window pops up: click Allow (or Cancel if you have no admin) and keep testing.")
        try:
            time.sleep(a.seconds)
        except KeyboardInterrupt:
            pass
        srv.shutdown()
        return
    if not a.ip:
        print("Usage: netcheck --listen   (on the future commander)   |   netcheck <commander-ip>   (on a worker)")
        return
    target = a.ip if "://" not in a.ip else a.ip.split("://", 1)[1].split("/")[0].split(":")[0]
    print(f"Testing the path to {target}:{port} ...")
    ping = subprocess_run(["ping", "-n", "2", "-w", "1000", target])
    print(f" 1. ping (ICMP):        {'OK' if ping == 0 else 'no reply (ICMP is often blocked - not fatal)'}")
    s2 = socket.socket()
    s2.settimeout(3)
    t0 = time.time()
    rc = s2.connect_ex((target, port))
    s2.close()
    if rc != 0:
        print(f" 2. TCP port {port}:      FAILED (code {rc}, {time.time() - t0:.1f}s)")
        if time.time() - t0 > 2.8:
            print("    -> timed out = packets dropped: FIREWALL on the commander PC (or wrong IP / other subnet).")
        else:
            print("    -> refused = PC reachable but nothing listens: commander.bat (or netcheck --listen) not running,")
            print("       or a different port in config.")
        print("    Plan B: pick another PC as commander (run netcheck --listen there and test again).")
        print("    Plan C: tunnel.bat on the commander (internet tunnel, needs no inbound) -> use the https URL.")
        print("    Plan D: solo.bat on each PC (commander + worker on one PC, no network needed).")
        return
    print(f" 2. TCP port {port}:      OK")
    try:
        r = httpx.get(f"http://{target}:{port}/api/ping", timeout=5).json()
        print(f" 3. HTTP:               OK ({r.get('name')})")
        if r.get("name") != "multiconnect":
            print("    (this is the netcheck listener, not the commander - network path is fine)")
            return
    except Exception as e:
        print(f" 3. HTTP:               FAILED ({e}) - something else listens on this port?")
        return
    tok = load_worker_config(None).get("team_token", "")
    r = httpx.get(f"http://{target}:{port}/api/w/accounts", headers={"x-team-token": tok}, timeout=5)
    print(f" 4. team_token:         {'OK' if r.status_code == 200 else 'REJECTED - copy team_token from the commander config into config/worker.yaml'}")
    if r.status_code == 200:
        print("All good. Set coordinator_url: http://%s:%d in config/worker.yaml (or leave empty for auto)." % (target, port))


def subprocess_run(cmd: list[str]) -> int:
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode
    except Exception:
        return -1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="multiconnect", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("commander"); s.add_argument("--config"); s.set_defaults(fn=cmd_commander)
    s = sub.add_parser("worker"); s.add_argument("--config"); s.set_defaults(fn=cmd_worker)
    s = sub.add_parser("login"); s.add_argument("account"); s.add_argument("--config"); s.set_defaults(fn=cmd_login)
    s = sub.add_parser("mcp"); s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("setup"); s.add_argument("--role", choices=["commander", "worker"]); s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("doctor"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("solo"); s.set_defaults(fn=cmd_solo)
    s = sub.add_parser("restore-hive"); s.add_argument("zip", nargs="?"); s.add_argument("--game", action="store_true")
    s.add_argument("--overwrite-config", action="store_true"); s.set_defaults(fn=cmd_restore_hive)
    s = sub.add_parser("netcheck"); s.add_argument("ip", nargs="?"); s.add_argument("--listen", action="store_true")
    s.add_argument("--port", type=int, default=8765); s.add_argument("--seconds", type=int, default=300)
    s.set_defaults(fn=cmd_netcheck)
    s = sub.add_parser("selftest"); s.add_argument("--mode", default="headless", choices=["headless", "mini", "hidden"])
    s.add_argument("--agents", type=int, default=3); s.add_argument("--keep", action="store_true")
    s.add_argument("--manual", action="store_true", help="test plan B: the manual copy-paste relay")
    s.set_defaults(fn=lambda a: sys.exit(__import__("multiconnect.selftest", fromlist=["x"]).run_selftest(
        a.mode, a.agents, a.keep, manual=a.manual)))
    s = sub.add_parser("mockchat"); s.add_argument("--port", type=int, default=8790); s.set_defaults(fn=cmd_mockchat)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
