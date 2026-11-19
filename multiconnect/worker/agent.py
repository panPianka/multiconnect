"""AgentRuntime: one logged-in chat account + the bridge loop that turns the chat
into a coding agent working inside a per-chat sandbox."""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.config import AGENTS_DIR, PROMPTS_DIR
from ..common.godot import basic_syntax_check
from ..common.protocol import parse_reply, segments_to_text
from ..common.tokens import estimate_tokens
from .providers.generic import ChatDriver, ProviderError
from .sandbox import Sandbox

if TYPE_CHECKING:
    from .main import Worker


def tmpl(name: str, **vals) -> str:
    t = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    for k, v in vals.items():
        t = t.replace("{{" + k + "}}", str(v))
    return t


class AgentRuntime:
    def __init__(self, worker: "Worker", account: str, provider: dict, storage_state: dict):
        self.w = worker
        self.account = account
        self.provider = provider
        self.storage_state = storage_state
        self.ctx = None
        self.page = None
        self.driver: ChatDriver | None = None
        self.status = "starting"
        self.detail = ""
        self.chat_id: str | None = None
        self.task: asyncio.Task | None = None
        self.answers: dict[str, asyncio.Future] = {}
        self.last_active = time.time()
        self.cooldown_until = 0.0
        self.dir = AGENTS_DIR / account
        self.dir.mkdir(parents=True, exist_ok=True)
        self.page_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            self.ctx = await self.w.browser.new_context(self.storage_state)
            await self.set_status("idle", "ready")
            asyncio.create_task(self.validate_login())
        except Exception as e:
            await self.set_status("error", f"browser context failed: {e}")
            await self.w.event("error", f"{self.account}: cannot start browser context: {e}", code="E_BROWSER",
                               account=self.account)

    async def validate_login(self) -> None:
        """Quick check right after start so the panel shows logged-out accounts early."""
        try:
            async with self.page_lock:
                await self.ensure_page()
                await self.driver.new_chat()
        except ProviderError as e:
            if self.status == "idle":
                await self.handle_provider_error(e, None)
        except Exception:
            pass

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        await self.save_state(upload=True)
        try:
            if self.ctx:
                await self.ctx.close()
        except Exception:
            pass
        self.ctx = self.page = self.driver = None
        self.w.browser.release(self.account)
        await self.set_status("stopped", "")

    async def recreate_context(self) -> None:
        """Used after a browser restart (e.g. mode change)."""
        state = self.storage_state
        self.ctx = await self.w.browser.new_context(state)
        self.page = self.driver = None

    async def ensure_page(self):
        if self.page is None or self.page.is_closed():
            if self.ctx is None:
                self.ctx = await self.w.browser.new_context(self.storage_state)
            self.page = await self.w.browser.new_page(self.ctx, self.account, self.provider.get("block_url_patterns", []))
            self.driver = ChatDriver(self.page, self.provider, on_status=self._driver_status)
        self.last_active = time.time()
        return self.page

    async def close_page_if_idle(self, idle_s: float) -> None:
        if self.status == "idle" and self.page and not self.page.is_closed() and time.time() - self.last_active > idle_s:
            async with self.page_lock:
                await self.save_state(upload=False)
                try:
                    await self.page.close()
                except Exception:
                    pass
                self.page = self.driver = None

    async def save_state(self, upload: bool = True) -> None:
        if not self.ctx:
            return
        try:
            self.storage_state = await self.ctx.storage_state()
            if upload:
                await self.w.upload_state(self.account, self.storage_state)
        except Exception:
            pass

    def _driver_status(self, status: str, detail: str) -> None:
        asyncio.create_task(self.set_status(status, detail))

    async def set_status(self, status: str, detail: str = "", **extra) -> None:
        self.status = status
        self.detail = detail
        self.last_active = time.time()
        await self.w.send({"type": "agent_status", "account": self.account, "status": status, "detail": detail[:400],
                           "chat_id": self.chat_id or "", **extra})

    def info(self) -> dict:
        return {"account": self.account, "status": self.status, "detail": self.detail[:200], "chat_id": self.chat_id,
                "page_open": bool(self.page and not self.page.is_closed())}

    # ------------------------------------------------------------------ commands from coordinator
    def assign(self, msg: dict) -> None:
        if self.task and not self.task.done():
            asyncio.create_task(self.w.send({"type": "chat_failed", "chat_id": msg["chat_id"],
                                             "reason": "agent busy", "code": "E_BUSY"}))
            return
        self.chat_id = msg["chat_id"]
        self.task = asyncio.create_task(self._run_guarded(msg))

    def cancel(self, chat_id: str) -> None:
        if self.chat_id == chat_id and self.task and not self.task.done():
            self.task.cancel()

    def answer(self, chat_id: str, text: str) -> None:
        fut = self.answers.get(chat_id)
        if fut and not fut.done():
            fut.set_result(text)

    async def _run_guarded(self, msg: dict) -> None:
        chat_id = msg["chat_id"]
        try:
            await self.set_status("working", f"{msg['task_id']} starting")
            async with self.page_lock:
                await self.run_chat(msg)
            await self.save_state(upload=True)
            if self.status == "working":
                await self.set_status("idle", "")
        except asyncio.CancelledError:
            await self.w.event("info", f"{chat_id} cancelled", account=self.account, chat_id=chat_id)
            await self.set_status("idle", "cancelled")
        except ProviderError as e:
            await self.handle_provider_error(e, chat_id)
        except Exception as e:
            tb = traceback.format_exc()
            (self.dir / "last_error.txt").write_text(tb, encoding="utf-8")
            await self.screenshot_on_error(chat_id)
            await self.w.send({"type": "chat_failed", "chat_id": chat_id, "reason": f"bridge error: {e}",
                               "code": "E_BRIDGE"})
            await self.set_status("idle", f"last error: {e}")
        finally:
            self.chat_id = None

    async def handle_provider_error(self, e: ProviderError, chat_id: str | None) -> None:
        if chat_id:
            await self.screenshot_on_error(chat_id)
            await self.w.send({"type": "chat_failed", "chat_id": chat_id, "reason": e.message, "code": e.code})
        if e.code == "E_RATE_LIMIT":
            self.cooldown_until = time.time() + (e.cooldown_s or 1800)
            await self.set_status("cooldown", e.message, cooldown_until=self.cooldown_until)
        elif e.code == "E_LOGGED_OUT":
            await self.set_status("logged_out", e.message)
        elif e.code == "E_CAPTCHA":
            await self.set_status("captcha", e.message)
        else:
            await self.w.event("warn", f"{self.account}: {e.code} {e.message}", code=e.code, account=self.account,
                               chat_id=chat_id or "")
            await self.set_status("idle", f"last error: {e.code}")

    async def screenshot_on_error(self, chat_id: str) -> None:
        try:
            if self.page and not self.page.is_closed():
                d = self.dir / "chats" / chat_id / "screenshots"
                d.mkdir(parents=True, exist_ok=True)
                data = await self.page.screenshot(type="jpeg", quality=50)
                (d / f"error-{int(time.time())}.jpg").write_bytes(data)
                await self.w.upload_screenshot(self.account, data)
        except Exception:
            pass

    # ------------------------------------------------------------------ the bridge loop
    async def run_chat(self, m: dict) -> None:
        chat_id, task_id = m["chat_id"], m["task_id"]
        cdir = self.dir / "chats" / chat_id
        cdir.mkdir(parents=True, exist_ok=True)
        transcript = cdir / "transcript.md"
        chat_meta_f = cdir / "chat.json"
        meta = json.loads(chat_meta_f.read_text(encoding="utf-8")) if chat_meta_f.exists() else {}
        meta.update({"chat_id": chat_id, "task_id": task_id, "account": self.account,
                     "provider": self.provider.get("id")})
        if m["kind"] == "new":
            (cdir / "first_prompt.md").write_text(m["prompt"], encoding="utf-8")

        def log(who: str, text: str) -> None:
            with open(transcript, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n### {who} [{time.strftime('%H:%M:%S')}]\n\n{text}\n")

        def save_meta():
            chat_meta_f.write_text(json.dumps(meta, indent=1), encoding="utf-8")

        # sandbox
        sb = Sandbox(cdir / "sandbox", m.get("writable", []))
        if m["kind"] == "new" or not sb.exists:
            bundle = await self.w.get_bundle(m["bundle_hash"])
            restore = None
            if m.get("restore"):
                restore = await self.w.get_result(m["restore"]["chat_id"], m["restore"]["version"])
            await asyncio.to_thread(sb.prepare, bundle, restore, m.get("contract", ""), m.get("task_md", ""))
        else:
            sb.writable = m.get("writable", sb.writable)
            sb.write_context(m.get("contract") or None, "")
        if m.get("contract") is not None:
            sb.write_context(m.get("contract"), m.get("task_md", ""))

        # chat page
        await self.ensure_page()
        url = m.get("url") or meta.get("url")
        message = m["prompt"]
        if m["kind"] == "new":
            await self.driver.new_chat()
        elif url:
            await self.driver.open(url)
        else:
            first = (cdir / "first_prompt.md")
            await self.driver.new_chat()
            if first.exists():
                message = first.read_text(encoding="utf-8") + "\n\n" + message
        await self.w.send({"type": "chat_update", "chat_id": chat_id, "status": "running"})

        max_rounds = int(m.get("max_rounds", 10))
        ctx_limit = int(m.get("ctx_limit", 64000))
        ratio = float(m.get("rollover_ratio", 0.7))
        version = int(m.get("start_version", meta.get("version", 0)))
        ctx_tokens = int(m.get("ctx_tokens", meta.get("ctx_tokens", 0)))
        bad_format = 0
        checked_since_write = False
        last_check: dict = {"ok": None, "errors": []}
        warnings_all: list[str] = []
        round_no = 0

        while True:
            round_no += 1
            if round_no > max_rounds:
                await self.w.send({"type": "chat_failed", "chat_id": chat_id, "code": "E_MAX_ROUNDS",
                                   "reason": f"not finished after {max_rounds} rounds; last errors: "
                                             f"{'; '.join(last_check.get('errors', [])[:3])}"})
                return
            await self.set_status("working", f"{task_id} round {round_no}: waiting for reply")
            log(f"BRIDGE -> {self.account}", message)
            segs = await self.driver.send_and_wait(message, timeout=float(self.provider.get("reply_timeout_s") or self.w.cfg.get("reply_timeout_s", 420)))
            reply_text = segments_to_text(segs)
            log(f"{self.account} -> BRIDGE", reply_text)
            tin, tout = estimate_tokens(message), estimate_tokens(reply_text)
            ctx_tokens += tin + tout
            meta["url"] = self.page.url
            meta["ctx_tokens"] = ctx_tokens
            save_meta()
            await self.w.send({"type": "chat_update", "chat_id": chat_id, "rounds": round_no, "url": self.page.url,
                               "ctx_tokens": ctx_tokens, "tin": tin, "tout": tout, "round_inc": True})

            actions, warns = parse_reply(segs)
            warnings_all.extend(warns)
            if not [a for a in actions if a.kind != "invalid"]:
                bad_format += 1
                if bad_format >= 3:
                    await self.w.send({"type": "chat_failed", "chat_id": chat_id, "code": "E_PROTOCOL",
                                       "reason": "agent does not follow the command format"})
                    return
                message = tmpl("reminder.md", warnings="\n".join(warns))
                round_no -= 1 if bad_format == 1 else 0
                continue
            bad_format = 0

            await self.set_status("working", f"{task_id} round {round_no}: executing {len(actions)} command(s)")
            results: list[str] = list(f"WARNING: {w}" for w in warns)
            done = False
            for a in actions:
                if a.kind == "write":
                    results.append(sb.write(a))
                    checked_since_write = False
                elif a.kind == "replace":
                    results.append(sb.replace(a))
                    checked_since_write = False
                elif a.kind == "read":
                    from ..common.protocol import clean_path
                    p = clean_path(a.arg)
                    results.append(sb.read(p) if p else f"FAILED @@READ {a.arg}: invalid path")
                elif a.kind == "list":
                    from ..common.protocol import clean_path
                    results.append(sb.listing(clean_path(a.arg) or "" if a.arg not in ("", ".", "/") else ""))
                elif a.kind == "check":
                    await self.set_status("working", f"{task_id}: Godot check")
                    last_check = await self.run_check(sb)
                    checked_since_write = True
                    results.append(self.format_check(last_check))
                elif a.kind == "run":
                    from ..common.protocol import clean_path
                    scene = clean_path(a.arg)
                    if not scene:
                        results.append(f"FAILED @@RUN {a.arg}: invalid path")
                    elif not m.get("run_scenes", True):
                        results.append("@@RUN is disabled for this task")
                    else:
                        await self.set_status("working", f"{task_id}: running {scene}")
                        r = await self.w.godot.run_scene(sb.root, scene, frames=300, timeout=60)
                        if not r.get("available"):
                            results.append("@@RUN skipped: Godot is not installed on this computer")
                        else:
                            results.append(f"RUN {scene}: {'OK, no errors' if r['ok'] else 'ERRORS'}\n" +
                                           "\n".join(r["errors"][:20]) +
                                           (f"\nOutput tail:\n{r['output'][-1500:]}" if r.get("output") else ""))
                elif a.kind == "ask":
                    answer = await self.ask(chat_id, a.arg, float(m.get("question_timeout_s", 600)))
                    results.append(f"ANSWER FROM LEAD ENGINEER: {answer}")
                elif a.kind == "done":
                    done = True
                elif a.kind == "invalid":
                    results.append(f"INVALID COMMAND: {a.arg}")

            if done:
                problems = []
                if not checked_since_write and sb.written_files():
                    await self.set_status("working", f"{task_id}: final Godot check")
                    last_check = await self.run_check(sb)
                    checked_since_write = True
                    if not last_check["ok"]:
                        results.append(self.format_check(last_check))
                if last_check.get("ok") is False:
                    problems.append("Godot check still reports errors (see above). Fix them.")
                required = [f for f in m.get("required", []) if not (sb.root / f).exists()]
                if required:
                    problems.append("Missing required files: " + ", ".join(required))
                if not (sb.root / "REPORT.md").exists():
                    problems.append("REPORT.md is missing. Write it with @@WRITE REPORT.md using the template.")
                if problems:
                    results.append("DONE REFUSED:\n- " + "\n- ".join(problems))
                else:
                    version += 1
                    meta["version"] = version
                    save_meta()
                    await self.finish(chat_id, task_id, sb, version, round_no, last_check, warnings_all,
                                      transcript.read_text(encoding="utf-8"))
                    log("BRIDGE", f"RESULT v{version} delivered")
                    return

            if ctx_tokens > ctx_limit * ratio:
                await self.set_status("working", f"{task_id}: conversation too long, moving to a fresh chat")
                message = await self.rollover(sb, log, m, results)
                ctx_tokens = estimate_tokens(message)
                round_no = max(0, round_no - 2)
                continue

            footer = "Continue. Remember: commands only inside code blocks; @@CHECK after changes; REPORT.md then @@DONE."
            message = tmpl("results.md", round=round_no, max_rounds=max_rounds,
                           results="\n\n".join(results) or "(nothing to report)", footer=footer)

    async def rollover(self, sb: Sandbox, log, m: dict, pending_results: list[str]) -> str:
        segs = await self.driver.send_and_wait(tmpl("rollover.md"))
        actions, _ = parse_reply(segs)
        for a in actions:
            if a.kind == "write" and a.arg == "_handover.md":
                sb.write(a)
        handover = (sb.root / "_handover.md").read_text(encoding="utf-8") if (sb.root / "_handover.md").exists() \
            else segments_to_text(segs)
        log("BRIDGE", "ROLLOVER to a new chat")
        await self.driver.new_chat()
        first = m["prompt"] if m["kind"] == "new" else (self.dir / "chats" / m["chat_id"] / "first_prompt.md").read_text(encoding="utf-8")
        return (first + "\n\n# HANDOVER FROM THE PREVIOUS AGENT (continue from here; files already exist in the sandbox)\n"
                + handover + "\n\n# LATEST RESULTS\n" + "\n".join(pending_results)[-4000:]
                + "\n\nFiles already written: " + ", ".join(sb.written_files()))

    async def run_check(self, sb: Sandbox) -> dict:
        files = [f for f in sb.written_files() if f.endswith((".gd", ".tscn", ".tres"))]
        basic = []
        for f in files:
            try:
                basic += basic_syntax_check(f, (sb.root / f).read_text(encoding="utf-8"))
            except Exception:
                pass
        reimport = any("class_name" in (sb.root / f).read_text(encoding="utf-8", errors="replace")
                       for f in files if f.endswith(".gd"))
        res = await self.w.godot.check(sb.root, files, reimport=reimport)
        if not res.get("available"):
            res["ok"] = not basic
            res["errors"] = basic
        return res

    @staticmethod
    def format_check(r: dict) -> str:
        if not r.get("checked"):
            return "CHECK: no script/scene files written yet."
        head = "CHECK OK: no errors" if r["ok"] else "CHECK FAILED"
        if not r.get("available"):
            head += " (Godot not installed here - basic syntax check only)"
        body = "\n".join(r.get("errors", [])[:25])
        return f"{head} ({len(r['checked'])} file(s): {', '.join(r['checked'][:10])})" + (f"\n{body}" if body else "")

    async def ask(self, chat_id: str, question: str, timeout: float) -> str:
        fut = asyncio.get_running_loop().create_future()
        self.answers[chat_id] = fut
        await self.set_status("working", f"question: {question[:120]}")
        await self.w.send({"type": "question", "chat_id": chat_id, "text": question})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            await self.w.send({"type": "chat_question_expired", "chat_id": chat_id})
            return ("No answer in time. Make the most reasonable decision yourself, follow the contract, "
                    "and document the decision in REPORT.md under 'Decisions & assumptions'.")
        finally:
            self.answers.pop(chat_id, None)
            await self.w.send({"type": "chat_update", "chat_id": chat_id, "status": "running"})

    async def finish(self, chat_id: str, task_id: str, sb: Sandbox, version: int, rounds: int, check: dict,
                     warnings: list[str], transcript: str) -> None:
        report = (sb.root / "REPORT.md").read_text(encoding="utf-8") if (sb.root / "REPORT.md").exists() else ""
        import re
        msum = re.search(r"^##\s*Summary\s*$(.*?)(?=^##\s|\Z)", report, re.M | re.S | re.I)
        manifest = {"chat_id": chat_id, "task_id": task_id, "version": version, "account": self.account,
                    "provider": self.provider.get("id"), "rounds": rounds, "check_ok": check.get("ok"),
                    "check_errors": check.get("errors", [])[:20], "godot": bool(check.get("available")),
                    "warnings": warnings[-20:], "summary": (msum.group(1).strip() if msum else report[:400]),
                    "finished": time.time()}
        data = await asyncio.to_thread(sb.build_result, manifest, transcript)
        out = self.dir / "chats" / chat_id / f"result_v{version}.zip"
        out.write_bytes(data)
        await self.w.outbox_add(chat_id, version, out)
        await self.set_status("idle", f"delivered {task_id} v{version}")
