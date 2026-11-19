"""Generic chat-website driver. Every provider is described by a YAML file
(config/providers/*.yaml) - URLs, CSS selectors and text patterns - so a new chat
site or a changed layout is fixed by editing YAML (or the panel's selector tester)."""
from __future__ import annotations

import asyncio
import re
import time

JS_STATE = r"""
(s) => {
  const all = (sel) => { if (!sel) return []; try { return document.querySelectorAll(sel); } catch (e) { return []; } };
  const one = (sel) => { if (!sel) return null; try { return document.querySelector(sel); } catch (e) { return null; } };
  const visible = (e) => !!(e && (e.offsetParent !== null || e.getClientRects().length));
  const msgs = all(s.assistant_message);
  const last = msgs.length ? msgs[msgs.length - 1] : null;
  const lastText = last ? (last.innerText || '') : '';
  const stop = one(s.stop_button);
  const input = one(s.input);
  let alerts = '';
  for (const e of all('[role=alert], [role=alertdialog], [class*=toast], [class*=Toast], [class*=error-message], [class*=rate-limit], [class*=limit-reached]')) {
    if (visible(e)) alerts += ' ' + (e.innerText || '');
    if (alerts.length > 1500) break;
  }
  let captcha = false;
  for (const f of all('iframe')) {
    const src = (f.src || '') + ' ' + (f.title || '');
    if (/captcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha/i.test(src)) { captcha = true; break; }
  }
  if (/just a moment|attention required|verify you are human/i.test(document.title)) captcha = true;
  return {count: msgs.length, len: lastText.length, tail: lastText.slice(-500),
          generating: visible(stop), input: visible(input), login: visible(one(s.login_required)),
          logged_in: s.logged_in ? visible(one(s.logged_in)) : visible(input),
          url: location.href, title: document.title, alerts: alerts.slice(0, 1500), captcha};
}
"""

JS_EXTRACT = r"""
(a) => {
  let msgs = [];
  try { msgs = document.querySelectorAll(a.msg); } catch (e) { return null; }
  const root = msgs[msgs.length - 1];
  if (!root) return null;
  let codes = new Set();
  try { codes = new Set(root.querySelectorAll(a.code || 'pre')); } catch (e) {}
  const segs = []; let buf = '';
  const BLOCK = /^(P|DIV|LI|UL|OL|H1|H2|H3|H4|H5|H6|TABLE|TR|BLOCKQUOTE|SECTION|ARTICLE|HR|DETAILS|SUMMARY)$/;
  const SKIP = /^(BUTTON|SVG|STYLE|SCRIPT|NOSCRIPT|TEMPLATE|NAV)$/i;
  const flush = () => { if (buf.trim()) segs.push({type: 'text', text: buf}); buf = ''; };
  const walk = (n) => {
    if (n.nodeType === 3) { buf += n.nodeValue; return; }
    if (n.nodeType !== 1 || SKIP.test(n.tagName)) return;
    if (n.getAttribute && n.getAttribute('aria-hidden') === 'true' && !codes.has(n)) return;
    if (codes.has(n)) {
      flush();
      const c = n.tagName === 'CODE' ? n : (n.querySelector('code') || n);
      segs.push({type: 'code', text: c.textContent});
      return;
    }
    if (n.tagName === 'BR') { buf += '\n'; return; }
    if (n.tagName === 'CODE') { buf += n.textContent; return; }
    const block = BLOCK.test(n.tagName);
    if (block) buf += '\n';
    for (const ch of n.childNodes) walk(ch);
    if (block) buf += '\n';
  };
  walk(root);
  flush();
  return segs;
}
"""

JS_HIGHLIGHT = r"""
(sel) => {
  let els = [];
  try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { return {error: 'invalid selector: ' + e.message}; }
  document.querySelectorAll('[data-mc-hl]').forEach(e => { e.style.outline = e.dataset.mcHl; delete e.dataset.mcHl; });
  els.slice(0, 50).forEach(e => { e.dataset.mcHl = e.style.outline || ''; e.style.outline = '3px solid #ff2d95'; });
  const visible = els.filter(e => e.offsetParent !== null || e.getClientRects().length).length;
  return {count: els.length, visible,
          samples: els.slice(-3).map(e => ({tag: e.tagName.toLowerCase(), text: (e.innerText || e.value || '').slice(0, 160)}))};
}
"""


class ProviderError(Exception):
    def __init__(self, code: str, message: str, cooldown_s: float = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.cooldown_s = cooldown_s


class ChatDriver:
    def __init__(self, page, prov: dict, on_status=None):
        self.page = page
        self.prov = prov
        self.sel = prov.get("selectors", {})
        self.on_status = on_status or (lambda *a, **k: None)

    # ------------------------------------------------------------------ state
    async def state(self) -> dict:
        try:
            return await self.page.evaluate(JS_STATE, self.sel)
        except Exception as e:
            return {"error": str(e), "count": 0, "len": 0, "tail": "", "generating": False, "input": False,
                    "login": False, "logged_in": False, "url": self.page.url, "title": "", "alerts": "", "captcha": False}

    def _looks_login_url(self, url: str) -> bool:
        home = self.prov.get("new_chat_url") or self.prov.get("home_url") or ""
        u = url.lower()
        if home and u.startswith(home.lower().rstrip("/")) and not any(p in u for p in ("/login", "/auth", "signin")):
            return False
        return any(p in u for p in self.prov.get("url_login_patterns", []))

    def _match(self, text: str, key: str) -> str | None:
        t = (text or "").lower()
        for p in self.prov.get(key, []):
            if p.lower() in t:
                return p
        return None

    async def ensure_ready(self, timeout: float = 60, captcha_wait: float = 600) -> None:
        """Wait until the chat input is usable. Raises ProviderError for login/captcha."""
        deadline = time.time() + timeout
        captcha_since = None
        while True:
            st = await self.state()
            if st.get("captcha"):
                captcha_since = captcha_since or time.time()
                self.on_status("captcha", "Human verification on the page - open the window and solve it")
                if time.time() - captcha_since > captcha_wait:
                    raise ProviderError("E_CAPTCHA", "captcha was not solved in time")
                await asyncio.sleep(3)
                continue
            if captcha_since:
                self.on_status("working", "captcha solved")
                captcha_since = None
                deadline = time.time() + timeout
            if st.get("input"):
                return
            if st.get("login") or self._looks_login_url(st.get("url", "")):
                raise ProviderError("E_LOGGED_OUT", f"login required ({st.get('url')})")
            if time.time() > deadline:
                raise ProviderError("E_SELECTOR", f"chat input not found with selector "
                                    f"{self.sel.get('input')!r} (url {st.get('url')}, title {st.get('title')!r})")
            await asyncio.sleep(1)

    # ------------------------------------------------------------------ navigation
    async def new_chat(self) -> None:
        url = self.prov.get("new_chat_url") or self.prov.get("home_url")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self.ensure_ready()

    async def open(self, url: str) -> None:
        if not url:
            return await self.new_chat()
        if self.page.url.rstrip("/") != url.rstrip("/"):
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self.ensure_ready()
        await asyncio.sleep(1.5)  # let history render

    # ------------------------------------------------------------------ send / receive
    async def _fill(self, text: str) -> None:
        loc = self.page.locator(self.sel.get("input") or "textarea").first
        await loc.wait_for(state="visible", timeout=20000)
        await loc.click()
        mode = self.prov.get("input_mode", "auto")
        info = await loc.evaluate("e => ({tag: e.tagName, ce: e.isContentEditable})")
        if mode in ("auto", "fill"):
            try:
                await loc.fill(text, timeout=20000)
            except Exception:
                mode = "paste"
        if mode == "paste" or (mode == "auto" and info["ce"]):
            got = await self._input_len(loc)
            if got < len(text) * 0.8:
                await loc.evaluate("""(e, t) => { e.focus();
                    if (e.isContentEditable) { e.innerHTML = ''; }
                    const dt = new DataTransfer(); dt.setData('text/plain', t);
                    e.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""", text)
                await asyncio.sleep(0.4)
        if mode == "type":
            await loc.press_sequentially(text, delay=2)
        got = await self._input_len(loc)
        if got < min(len(text), 50) * 0.5:
            await self.page.keyboard.insert_text(text)
        await asyncio.sleep(0.3)

    async def _input_len(self, loc) -> int:
        try:
            return await loc.evaluate("e => (e.value !== undefined && e.tagName !== 'DIV' ? e.value : e.innerText || '').length")
        except Exception:
            return 0

    async def _submit(self) -> None:
        how = self.prov.get("send_with", "enter")
        btn = self.sel.get("send_button")
        if how == "button" and btn:
            b = self.page.locator(btn).last
            for _ in range(40):
                try:
                    if await b.is_enabled():
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            await b.click()
        elif how == "ctrl_enter":
            await self.page.keyboard.press("Control+Enter")
        else:
            await self.page.keyboard.press("Enter")

    async def send_and_wait(self, text: str, timeout: float = 420) -> list[dict]:
        await self.ensure_ready()
        before = await self.state()
        before_count = before.get("count", 0)
        await self._fill(text)
        await self._submit()
        started = False
        t0 = time.time()
        resent = False
        last_len, stable_since = -1, time.time()
        settle = float(self.prov.get("settle_ms", 2500)) / 1000
        while True:
            await asyncio.sleep(0.8)
            st = await self.state()
            if st.get("captcha"):
                await self.ensure_ready()
                continue
            if not started:
                if st["count"] > before_count or st["generating"]:
                    started = True
                    stable_since = time.time()
                elif time.time() - t0 > 15 and not resent:
                    resent = True  # Enter may have been swallowed: try the button once
                    btn = self.sel.get("send_button")
                    if btn:
                        try:
                            await self.page.locator(btn).last.click(timeout=3000)
                        except Exception:
                            pass
                elif time.time() - t0 > 45:
                    self._check_limits(st, early=True)
                    raise ProviderError("E_SEND", "message was not accepted by the chat (no reply started)")
                continue
            if st["len"] != last_len:
                last_len = st["len"]
                stable_since = time.time()
            if not st["generating"] and time.time() - stable_since >= settle and st["count"] > before_count:
                break
            if time.time() - t0 > timeout:
                raise ProviderError("E_TIMEOUT", f"no complete reply after {int(timeout)}s")
        self._check_limits(st)
        segs = await self.page.evaluate(JS_EXTRACT, {"msg": self.sel.get("assistant_message"),
                                                     "code": self.sel.get("code_block") or "pre"})
        if not segs:
            raise ProviderError("E_SELECTOR", f"could not read the reply with selector {self.sel.get('assistant_message')!r}")
        return segs

    def _check_limits(self, st: dict, early: bool = False) -> None:
        alerts = st.get("alerts", "")
        tail = st.get("tail", "") if (st.get("len", 0) < 700 or early) else ""
        hit = self._match(alerts, "rate_limit_patterns") or self._match(tail, "rate_limit_patterns")
        if hit:
            cool = float(self.prov.get("cooldown_min", 30)) * 60
            raise ProviderError("E_RATE_LIMIT", f"rate limit detected ('{hit}')", cooldown_s=cool)
        err = self._match(alerts, "error_patterns")
        if err and st.get("len", 0) < 50:
            raise ProviderError("E_PROVIDER", f"chat site error: {alerts.strip()[:200]}")

    async def highlight(self, selector: str) -> dict:
        return await self.page.evaluate(JS_HIGHLIGHT, selector)
