"""Everything that touches the game folder: bundles, integration, checks, backups,
hand-off docs and assets."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.config import STATE_DIR, TEMPLATES_DIR, resolve_path
from ..common.godot import Godot, ensure_check_script, find_godot

if TYPE_CHECKING:
    from .hub import Hub

EXCLUDE_DIRS = {".godot", ".git", "assets_inbox", "__pycache__", ".import_cache"}
EXCLUDE_HIVE = {"_hive/backups"}
MAX_BUNDLE_FILE = 8 * 1024 * 1024
TEXT_EXT = {".gd", ".tscn", ".tres", ".godot", ".md", ".txt", ".json", ".cfg", ".gdshader", ".csv", ".import", ".yaml"}


class GameOps:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.cfg = hub.cfg
        self.dir = resolve_path(self.cfg["game_dir"])
        self.backup_dir = resolve_path(self.cfg["backup_dir"])
        self.bundles_dir = STATE_DIR / "bundles"
        self.results_dir = STATE_DIR / "results"
        for d in (self.bundles_dir, self.results_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.godot = Godot(find_godot(self.cfg.get("godot_path", "")), max_parallel=2)
        self._bundle_cache: dict | None = None
        self._docs_dirty = True
        self._last_backup_fp = ""
        self._last_backup_t = 0.0
        self.init_game_dir()

    # ------------------------------------------------------------------ setup
    def init_game_dir(self) -> None:
        d = self.dir
        (d / "_hive" / "reports").mkdir(parents=True, exist_ok=True)
        (d / "assets_inbox").mkdir(parents=True, exist_ok=True)
        if not (d / "project.godot").exists():
            shutil.copyfile(TEMPLATES_DIR / "godot_sandbox" / "project.godot", d / "project.godot")
            txt = (d / "project.godot").read_text(encoding="utf-8").replace("MultiConnect Sandbox", "Hackathon Game")
            (d / "project.godot").write_text(txt, encoding="utf-8")
        ensure_check_script(d)
        gi = d / ".gitignore"
        if not gi.exists():
            gi.write_text(".godot/\nassets_inbox/\n*.tmp\n", encoding="utf-8")
        gdignore = d / "assets_inbox" / ".gdignore"
        if not gdignore.exists():
            gdignore.write_text("", encoding="utf-8")
        if not (d / "_hive" / "contract.md").exists():
            (d / "_hive" / "contract.md").write_text("", encoding="utf-8")
        # commander playbook for Claude Code (CLAUDE.md) and Codex (AGENTS.md); refreshed while unmodified
        pb = TEMPLATES_DIR / "commander_playbook.md"
        if pb.exists():
            src = pb.read_text(encoding="utf-8")
            for name in ("CLAUDE.md", "AGENTS.md"):
                dst = d / name
                if not dst.exists() or "<!-- multiconnect-playbook -->" in dst.read_text(encoding="utf-8", errors="replace"):
                    if not dst.exists() or dst.read_text(encoding="utf-8", errors="replace") != src:
                        dst.write_text(src, encoding="utf-8")
        if shutil.which("git") and not (d / ".git").exists():
            self._git("init", "-q")
            self._git_commit("MultiConnect: initial state")

    # ------------------------------------------------------------------ git
    def _git(self, *args: str) -> tuple[int, str]:
        if not shutil.which("git"):
            return -1, "git missing"
        p = subprocess.run(["git", "-c", "user.name=MultiConnect", "-c", "user.email=hive@local",
                            "-c", "core.autocrlf=false", "-C", str(self.dir), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return p.returncode, (p.stdout + p.stderr).strip()

    def _git_commit(self, message: str) -> str:
        if not (self.dir / ".git").exists():
            return ""
        self._git("add", "-A")
        code, out = self._git("commit", "-q", "-m", message)
        if code != 0:
            return ""
        _, h = self._git("rev-parse", "--short", "HEAD")
        return h

    # ------------------------------------------------------------------ files
    def iter_files(self):
        for root, dirs, files in os.walk(self.dir):
            rel_root = Path(root).relative_to(self.dir).as_posix()
            dirs[:] = [x for x in dirs if x not in EXCLUDE_DIRS
                       and (f"{rel_root}/{x}" if rel_root != "." else x) not in EXCLUDE_HIVE]
            for f in files:
                p = Path(root) / f
                rel = p.relative_to(self.dir).as_posix()
                yield rel, p

    def tree(self, max_items: int = 400) -> list[dict]:
        out = []
        for rel, p in sorted(self.iter_files()):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime,
                        "owner": (self.hub.db.one("SELECT task_id FROM file_owners WHERE path=?", (rel,)) or {}).get("task_id")})
            if len(out) >= max_items:
                break
        return out

    def safe_game_path(self, rel: str) -> Path | None:
        from ..common.protocol import clean_path
        c = clean_path(rel)
        if not c:
            return None
        p = (self.dir / c).resolve()
        if not str(p).startswith(str(self.dir.resolve())):
            return None
        return p

    def read_file(self, rel: str, max_bytes: int = 200_000) -> str | None:
        p = self.safe_game_path(rel)
        if not p or not p.is_file():
            return None
        return p.read_bytes()[:max_bytes].decode("utf-8", "replace")

    def write_file(self, rel: str, content: str) -> bool:
        p = self.safe_game_path(rel)
        if not p:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        self.invalidate()
        return True

    def invalidate(self) -> None:
        self._bundle_cache = None
        self._docs_dirty = True

    # ------------------------------------------------------------------ contract & hive docs
    def get_contract(self) -> str:
        p = self.dir / "_hive" / "contract.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def set_contract(self, text: str) -> int:
        p = self.dir / "_hive" / "contract.md"
        p.write_text(text, encoding="utf-8", newline="\n")
        v = int(self.hub.db.kv_get("contract_version", 0)) + 1
        self.hub.db.kv_set("contract_version", v)
        self.invalidate()
        self._git_commit(f"contract v{v}")
        return v

    def report_section(self, task_id: str, heading: str) -> str:
        p = self.dir / "_hive" / "reports" / f"{task_id}.md"
        if not p.exists():
            return ""
        txt = p.read_text(encoding="utf-8")
        m = re.search(rf"^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", txt, re.M | re.S | re.I)
        return m.group(1).strip() if m else ""

    def write_hive_docs_soon(self) -> None:
        self._docs_dirty = True

    def write_hive_docs(self) -> None:
        db = self.hub.db
        hive = self.dir / "_hive"
        tasks = db.q("SELECT * FROM tasks ORDER BY created")
        lines = ["# TASKS (generated)", "", "| id | status | title | deps | result |", "|---|---|---|---|---|"]
        for t in tasks:
            lines.append(f"| {t['id']} | {t['status']} | {t['title']} | {', '.join(json.loads(t['depends_json']))} | "
                         f"{t['result_chat'] or ''} v{t['result_version']} |")
        (hive / "TASKS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        assets = db.q("SELECT * FROM assets ORDER BY created")
        al = ["# ASSETS (generated)", "", "| id | status | path | spec | for |", "|---|---|---|---|---|"]
        for a in assets:
            al.append(f"| {a['id']} | {a['status']} | {a['path']} | {a['spec'][:80]} | {a['for_task'] or ''} |")
        (hive / "ASSETS.md").write_text("\n".join(al) + "\n", encoding="utf-8")

        decisions = db.kv_get("decisions", [])
        dl = ["# DECISIONS (generated, newest last)", ""] + [
            f"- {time.strftime('%H:%M', time.localtime(d['ts']))} {d['text']}" for d in decisions]
        (hive / "DECISIONS.md").write_text("\n".join(dl) + "\n", encoding="utf-8")

        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        active = db.q("SELECT c.*, t.title FROM chats c JOIN tasks t ON t.id=c.task_id "
                      "WHERE c.status IN ('assigned','running','question')")
        questions = db.q("SELECT * FROM questions WHERE answered=0")
        pending_assets = [a for a in assets if a["status"] in ("requested", "placeholder", "claimed")]
        last_check = db.kv_get("last_check", {})
        h = [
            "# HANDOFF - read this first (auto-generated " + time.strftime("%Y-%m-%d %H:%M") + ")",
            "",
            "You are the lead engineer (commander) of a MultiConnect hive building a Godot 2D game.",
            "Talk to the humans in Polish; write EVERYTHING else (tasks, contract, code, docs) in English.",
            "Start with the MCP tools: `hive_overview`, then `wait_for_events`.",
            "",
            "## Game brief",
            db.kv_get("game_brief", "(not set yet - ask the team and call set_notes(brief=...))"),
            "",
            "## Next steps (set by the previous commander)",
            db.kv_get("next_steps", "(none recorded)"),
            "",
            "## Status",
            "Tasks: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"),
            f"Contract version: {db.kv_get('contract_version', 0)} (file `_hive/contract.md`)",
            f"Last project check: {'OK' if last_check.get('ok') else 'FAILED' if last_check else 'never'}"
            + (f" - {len(last_check.get('errors', []))} errors" if last_check.get("errors") else ""),
            "",
            "## In progress",
        ]
        h += [f"- {c['task_id']} {c['title']} -> {c['account_id']} ({c['status']}, round {c['rounds']})" for c in active] or ["- nothing"]
        h += ["", "## Waiting for your review (status=done)"]
        h += [f"- {t['id']} {t['title']} (chat {t['result_chat']} v{t['result_version']})" for t in tasks if t["status"] == "done"] or ["- nothing"]
        h += ["", "## Failed tasks"]
        h += [f"- {t['id']} {t['title']}: {t['error'][:150]}" for t in tasks if t["status"] == "failed"] or ["- none"]
        h += ["", "## Open questions from agents"]
        h += [f"- Q{q['id']} ({q['task_id']}): {q['question'][:200]}" for q in questions] or ["- none"]
        h += ["", "## Assets still missing"]
        h += [f"- {a['id']} {a['path']} ({a['status']}): {a['spec'][:100]}" for a in pending_assets] or ["- none"]
        h += ["", "## Recent decisions"]
        h += [f"- {d['text']}" for d in decisions[-15:]] or ["- none"]
        h += ["", "## Files", "- Contract: `_hive/contract.md`", "- Agent reports: `_hive/reports/<task>.md`",
              "- Lists: `_hive/TASKS.md`, `_hive/ASSETS.md`, `_hive/DECISIONS.md`",
              f"- Backups: `{self.backup_dir}` (zip) + local git history in the game folder"]
        (hive / "HANDOFF.md").write_text("\n".join(h) + "\n", encoding="utf-8")
        self._docs_dirty = False

    # ------------------------------------------------------------------ bundles
    def bundle(self) -> dict:
        """Zip snapshot of the game for sandboxes, cached by content fingerprint."""
        fp_items = []
        files = []
        for rel, p in self.iter_files():
            try:
                st = p.stat()
            except OSError:
                continue
            if rel in ("CLAUDE.md", "AGENTS.md"):  # commander playbook, not for agents
                continue
            if st.st_size > MAX_BUNDLE_FILE or rel.startswith("_hive/") and not rel.startswith("_hive/tools/") \
                    and rel not in ("_hive/contract.md",):
                continue
            fp_items.append(f"{rel}|{st.st_size}|{st.st_mtime_ns}")
            files.append((rel, p))
        h = hashlib.sha1("\n".join(sorted(fp_items)).encode()).hexdigest()[:16]
        if self._bundle_cache and self._bundle_cache["hash"] == h:
            return self._bundle_cache
        zpath = self.bundles_dir / f"{h}.zip"
        if not zpath.exists():
            tmp = zpath.with_suffix(".tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
                for rel, p in files:
                    z.write(p, rel)
            tmp.replace(zpath)
            old = sorted(self.bundles_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime)
            for o in old[:-12]:
                o.unlink(missing_ok=True)
        rels = sorted(r for r, _ in files)
        important = [r for r in rels if r.endswith((".gd", ".tscn", ".tres", ".godot", ".gdshader", ".md"))]
        other = [r for r in rels if r not in important and not r.endswith(".import")]
        listing = important[:200] + ([f"... and {len(important) - 200} more"] if len(important) > 200 else [])
        if other:
            listing.append(f"(+ {len(other)} asset files, e.g. " + ", ".join(other[:15]) + ")")
        self._bundle_cache = {"hash": h, "path": str(zpath), "files": rels,
                              "file_list_text": "\n".join(listing) or "(empty project)"}
        return self._bundle_cache

    # ------------------------------------------------------------------ results / integration
    def result_zip(self, chat_id: str, version: int | None = None) -> tuple[Path, dict] | None:
        db = self.hub.db
        if version:
            r = db.one("SELECT * FROM results WHERE chat_id=? AND version=?", (chat_id, version))
        else:
            r = db.one("SELECT * FROM results WHERE chat_id=? ORDER BY version DESC LIMIT 1", (chat_id,))
        if not r:
            return None
        return Path(r["path"]), json.loads(r["manifest_json"])

    def read_result_file(self, chat_id: str, version: int, rel: str) -> str | None:
        rz = self.result_zip(chat_id, version)
        if not rz:
            return None
        with zipfile.ZipFile(rz[0]) as z:
            try:
                return z.read(rel).decode("utf-8", "replace")
            except KeyError:
                return None

    async def integrate(self, task_id: str, chat_id: str | None = None, version: int | None = None,
                        force: bool = False, include_tests: bool = False) -> dict:
        db = self.hub.db
        task = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return {"ok": False, "error": "unknown task"}
        chat_id = chat_id or task["result_chat"]
        if not chat_id:
            return {"ok": False, "error": "task has no result yet"}
        rz = self.result_zip(chat_id, version)
        if not rz:
            return {"ok": False, "error": f"no result for chat {chat_id}"}
        zpath, manifest = rz
        spec = json.loads(task["spec_json"])
        allowed_existing = set(spec.get("files_to_modify") or []) | set(spec.get("files_to_create") or [])
        files = [f for f in manifest.get("files", [])
                 if f not in ("REPORT.md", "_handover.md") and not f.startswith(("_meta/", "_context/"))
                 and (include_tests or not f.startswith("tests/"))]
        conflicts = []
        for f in files:
            owner = (db.one("SELECT task_id FROM file_owners WHERE path=?", (f,)) or {}).get("task_id")
            if owner and owner != task_id:
                conflicts.append(f"{f} is owned by {owner}")
            elif not owner and (self.dir / f).exists() and f not in allowed_existing:
                conflicts.append(f"{f} already exists (not created by a task)")
        if conflicts and not force:
            return {"ok": False, "error": "conflicts - call again with force=true to overwrite",
                    "conflicts": conflicts}
        self._git_commit(f"before integrating {task_id}")
        with zipfile.ZipFile(zpath) as z:
            names = set(z.namelist())
            for f in files:
                if f not in names:
                    continue
                dst = self.dir / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(z.read(f))
                db.x("INSERT INTO file_owners(path,task_id,updated) VALUES(?,?,?) "
                     "ON CONFLICT(path) DO UPDATE SET task_id=excluded.task_id, updated=excluded.updated",
                     (f, task_id, time.time()))
            if "REPORT.md" in names:
                (self.dir / "_hive" / "reports" / f"{task_id}.md").write_bytes(z.read("REPORT.md"))
        self.invalidate()
        needs_import = any(f.endswith(".gd") and "class_name" in (self.read_file(f) or "") for f in files)
        check = await self.godot.check(self.dir, files, reimport=needs_import)
        commit = self._git_commit(f"integrate {task_id} ({chat_id} v{manifest.get('version')})")
        db.update("tasks", "id", task_id, status="integrated", integrated_at=time.time(), updated=time.time())
        self.hub.ev("info", "integrate", f"Integrated {task_id}: {len(files)} files, check "
                    f"{'OK' if check['ok'] else 'FAILED'}", task=task_id, chat=chat_id)
        self.write_hive_docs()
        unblocked = self.hub.release_waiting()
        await self.hub.schedule()
        waiting_now = {t["id"] for t in db.q("SELECT id FROM tasks WHERE status='waiting'")}
        return {"ok": True, "files": files, "check": check, "commit": commit,
                "unblocked": unblocked, "still_waiting": sorted(waiting_now)}

    async def check_project(self, files: list[str] | None = None) -> dict:
        if files is None:
            files = [r for r, _ in self.iter_files() if r.endswith((".gd", ".tscn")) and not r.startswith("_hive/")]
        res = await self.godot.check(self.dir, files, reimport=True, timeout=240)
        self.hub.db.kv_set("last_check", {"ok": res["ok"], "errors": res["errors"][:30], "ts": time.time()})
        return res

    async def run_scene(self, scene: str, seconds: float = 5) -> dict:
        return await self.godot.run_scene(self.dir, scene, frames=int(seconds * 60), timeout=seconds + 60)

    # ------------------------------------------------------------------ backups
    def _fingerprint(self) -> str:
        items = []
        for rel, p in self.iter_files():
            try:
                st = p.stat()
                items.append(f"{rel}|{st.st_size}|{st.st_mtime_ns}")
            except OSError:
                pass
        return hashlib.sha1("\n".join(sorted(items)).encode()).hexdigest()

    def backup(self, label: str = "auto", force: bool = False) -> dict | None:
        fp = self._fingerprint()
        if not force and fp == self._last_backup_fp:
            return None
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = f"game-{ts}-{re.sub(r'[^a-zA-Z0-9_-]', '', label)[:20]}.zip"
        dst = self.backup_dir / name
        tmp = dst.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for rel, p in self.iter_files():
                try:
                    z.write(p, rel)
                except OSError:
                    pass
            z.writestr("_hive/handoff_db.json", json.dumps(self.export_state()))
        tmp.replace(dst)
        self._last_backup_fp = fp
        self._last_backup_t = time.time()
        self._rotate()
        self.hub.ev("info", "backup", f"Backup {name} ({dst.stat().st_size // 1024} KB)")
        return {"name": name, "size": dst.stat().st_size}

    def _rotate(self) -> None:
        keep = int(self.cfg["backup_keep"])
        files = sorted(self.backup_dir.glob("game-*.zip"), key=lambda p: p.name, reverse=True)
        hourly_seen = set()
        for i, f in enumerate(files):
            if i < keep or "manual" in f.name or "pre-restore" in f.name:
                continue
            hour = f.name[5:16]  # YYYYMMDD-HH
            if hour not in hourly_seen:
                hourly_seen.add(hour)
                continue
            f.unlink(missing_ok=True)

    def list_backups(self) -> list[dict]:
        out = []
        for f in sorted(self.backup_dir.glob("game-*.zip"), key=lambda p: p.name, reverse=True):
            out.append({"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
        return out

    def restore(self, name: str) -> dict:
        src = self.backup_dir / Path(name).name
        if not src.exists():
            return {"ok": False, "error": "backup not found"}
        self.backup("pre-restore", force=True)
        for rel, p in list(self.iter_files()):
            p.unlink(missing_ok=True)
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if n == "_hive/handoff_db.json":
                    continue
                z.extract(n, self.dir)
        self.invalidate()
        self._git_commit(f"restore {name}")
        self.hub.ev("warn", "backup", f"Game restored from {name}")
        return {"ok": True}

    def export_state(self) -> dict:
        db = self.hub.db
        return {"tasks": db.q("SELECT * FROM tasks"), "chats": db.q("SELECT * FROM chats"),
                "assets": db.q("SELECT * FROM assets"), "file_owners": db.q("SELECT * FROM file_owners"),
                "kv": {r["key"]: r["value"] for r in db.q("SELECT * FROM kv")}}

    # ------------------------------------------------------------------ hive (commander state) backup
    def hive_backup(self) -> dict:
        """Zip of everything needed to move the commander to another PC: the SQLite DB (consistent copy
        via the backup API), login cookies (state/secrets) and the commander/accounts config.
        Restore with `multiconnect restore-hive` (restore_hive.bat) on the new commander PC."""
        from ..common.config import CONFIG_DIR
        from .hub import SECRETS_DIR
        import sqlite3
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = f"hive-{ts}.zip"
        dst = self.backup_dir / name
        tmp = dst.with_suffix(".tmp")
        dbcopy = self.backup_dir / f".hive-{ts}.db"
        try:
            with self.hub.db.lock:
                out = sqlite3.connect(str(dbcopy))
                try:
                    self.hub.db.conn.backup(out)
                finally:
                    out.close()
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(dbcopy, "state/multiconnect.db")
                if SECRETS_DIR.exists():
                    for p in SECRETS_DIR.rglob("*"):
                        if p.is_file():
                            z.write(p, "state/secrets/" + p.relative_to(SECRETS_DIR).as_posix())
                acc = resolve_path(self.cfg.get("accounts_file", "config/accounts.yaml"))
                for p, arc in ((CONFIG_DIR / "commander.yaml", "config/commander.yaml"),
                               (acc, "config/accounts.yaml")):
                    if p.exists():
                        z.write(p, arc)
                z.writestr("HIVE_BACKUP.txt", f"MultiConnect hive backup {ts}\nfrom {os.environ.get('COMPUTERNAME', '')}\n"
                           f"game_dir={self.dir}\nContains LOGIN COOKIES - keep private, never upload.\n")
            tmp.replace(dst)
        finally:
            dbcopy.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
        for old in sorted(self.backup_dir.glob("hive-*.zip"), key=lambda p: p.name)[:-10]:
            old.unlink(missing_ok=True)
        self._last_hive_t = time.time()
        return {"name": name, "size": dst.stat().st_size}

    async def periodic(self) -> None:
        if self._docs_dirty:
            await asyncio.to_thread(self.write_hive_docs)
        await asyncio.to_thread(self.scan_inbox)
        every = float(self.cfg["backup_every_min"]) * 60
        if every > 0 and time.time() - self._last_backup_t > every:
            info = await asyncio.to_thread(self.backup, "auto")
            if info:
                await self.mirror(info["name"])
            else:
                self._last_backup_t = time.time()
        hive_every = float(self.cfg.get("hive_backup_every_min", 10)) * 60
        if hive_every > 0 and time.time() - getattr(self, "_last_hive_t", 0) > hive_every:
            try:
                info = await asyncio.to_thread(self.hive_backup)
                if self.cfg.get("mirror_hive", True):
                    await self.mirror(info["name"])
            except Exception as e:
                self._last_hive_t = time.time()
                self.hub.ev("warn", "backup", f"Hive backup failed: {e}")

    async def mirror(self, name: str) -> None:
        targets = self.cfg.get("mirror_backups_to") or "auto"
        hosts = list(self.hub.conns)
        if targets == "auto":
            me = os.environ.get("COMPUTERNAME", "")
            hosts = [h for h in hosts if h != me][:1]
        else:
            hosts = [h for h in hosts if h in targets]
        for h in hosts:
            await self.hub.send(h, {"type": "mirror_backup", "name": name})

    # ------------------------------------------------------------------ assets
    def request_assets(self, items: list[dict]) -> list[dict]:
        db = self.hub.db
        out = []
        for it in items:
            path = it.get("path") or ""
            from ..common.protocol import clean_path
            path = clean_path(path) or ""
            if not path:
                out.append({"error": "path required", "item": it})
                continue
            n = int(db.kv_get("asset_seq", 0)) + 1
            db.kv_set("asset_seq", n)
            aid = f"A{n:03d}"
            kind = it.get("kind") or ("sound" if path.endswith((".wav", ".ogg", ".mp3")) else
                                      "font" if path.endswith((".ttf", ".otf")) else "image")
            w, hgt, frames = int(it.get("width", 0) or 0), int(it.get("height", 0) or 0), int(it.get("frames", 1) or 1)
            db.x("INSERT INTO assets(id,name,path,kind,spec,priority,for_task,status,created,width,height,frames) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (aid, it.get("name") or Path(path).stem, path, kind, it.get("spec", ""), int(it.get("priority", 2)),
                  it.get("for_task"), "requested", time.time(), w, hgt, frames))
            status = "requested"
            if kind == "image" and w and hgt and not (self.dir / path).exists():
                self._placeholder(path, w, hgt, frames, it.get("name") or Path(path).stem)
                db.update("assets", "id", aid, status="placeholder")
                status = "placeholder"
            out.append({"id": aid, "path": path, "status": status})
            self.hub.ev("info", "asset", f"Asset requested {aid}: {path} - {it.get('spec','')[:120]}")
        self.invalidate()
        return out

    def _placeholder(self, path: str, w: int, h: int, frames: int, label: str) -> None:
        from PIL import Image, ImageDraw
        frames = max(1, frames)
        img = Image.new("RGBA", (w * frames, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color = tuple(int(hashlib.md5(label.encode()).hexdigest()[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        for f in range(frames):
            x0 = f * w
            d.rectangle([x0, 0, x0 + w - 1, h - 1], fill=color, outline=(255, 255, 255, 255))
            if w >= 8 and h >= 8:
                d.line([x0, 0, x0 + w - 1, h - 1], fill=(0, 0, 0, 160))
        dst = self.dir / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst)

    def deliver_asset(self, aid: str, filename: str, data: bytes, by: str = "") -> dict:
        db = self.hub.db
        a = db.one("SELECT * FROM assets WHERE id=?", (aid,))
        if not a:
            return {"ok": False, "error": "unknown asset"}
        warnings = []
        if a["kind"] == "image":
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                W, H = im.size
                if a["width"] and a["height"]:
                    exp_w = a["width"] * max(1, a["frames"])
                    if (W, H) != (exp_w, a["height"]):
                        warnings.append(f"size {W}x{H}, expected {exp_w}x{a['height']} "
                                        f"({a['frames']} frame(s) of {a['width']}x{a['height']})")
                if im.mode not in ("RGBA", "LA", "P"):
                    warnings.append(f"image mode {im.mode} has no transparency")
            except Exception as e:
                warnings.append(f"not a valid image: {e}")
        ext_req = Path(a["path"]).suffix.lower()
        ext_got = Path(filename).suffix.lower()
        if ext_got and ext_req and ext_got != ext_req:
            warnings.append(f"file type {ext_got} differs from requested {ext_req}")
        dst = self.dir / a["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        db.update("assets", "id", aid, status="delivered", delivered=time.time(), claimed_by=by or a["claimed_by"],
                  notes="; ".join(warnings))
        self.invalidate()
        self.hub.ev("info", "asset", f"Asset {aid} delivered: {a['path']}" + (f" (warnings: {'; '.join(warnings)})" if warnings else ""),
                    for_claude=True, data={"asset_id": aid, "path": a["path"], "warnings": warnings})
        self._git_commit(f"asset {aid} {a['path']}")
        return {"ok": True, "warnings": warnings, "path": a["path"]}

    def scan_inbox(self) -> None:
        inbox = self.dir / "assets_inbox"
        db = self.hub.db
        for f in inbox.iterdir() if inbox.exists() else []:
            if not f.is_file() or f.name.startswith(".") or f.suffix in (".tmp", ".part", ".crdownload"):
                continue
            try:
                if time.time() - f.stat().st_mtime < 2:
                    continue  # still being copied
            except OSError:
                continue
            match = None
            for a in db.q("SELECT * FROM assets WHERE status!='delivered'"):
                if Path(a["path"]).name.lower() == f.name.lower() or a["id"].lower() == f.stem.lower() \
                        or a["name"].lower() == f.stem.lower():
                    match = a
                    break
            data = f.read_bytes()
            if match:
                self.deliver_asset(match["id"], f.name, data, by="inbox")
            else:
                dst = self.dir / "assets" / "unsorted" / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
                self.invalidate()
                self.hub.ev("info", "asset", f"Unrequested asset arrived: assets/unsorted/{f.name}", for_claude=True,
                            data={"path": f"assets/unsorted/{f.name}"})
            f.unlink(missing_ok=True)
