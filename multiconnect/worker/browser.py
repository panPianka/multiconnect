"""One shared, slimmed-down Chromium per computer. Each agent gets an isolated
BrowserContext (own cookies = own account) instead of a full browser process."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..common.config import ROOT

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))

SLIM_ARGS = [
    "--disable-extensions", "--disable-component-extensions-with-background-pages",
    "--disable-background-networking", "--disable-sync", "--disable-default-apps",
    "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication,CalculateNativeWinOcclusion,InterestFeedContentSuggestions",
    "--disable-client-side-phishing-detection", "--disable-breakpad", "--no-first-run",
    "--no-default-browser-check", "--mute-audio", "--disable-notifications",
    "--metrics-recording-only", "--password-store=basic",
    # keep minimized / hidden windows working at full speed
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows", "--disable-ipc-flooding-protection",
    "--disable-hang-monitor",
    "--js-flags=--max-old-space-size=512",
]
VIEWPORT = {"width": 1100, "height": 820}


class BrowserManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = cfg.get("browser_mode", "mini")
        self.pw = None
        self.browser: Browser | None = None
        self.slots: dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.lock:
            if self.browser and self.browser.is_connected():
                return
            if not self.pw:
                self.pw = await async_playwright().start()
            args = list(SLIM_ARGS)
            if self.mode != "headless":
                args.append("--disable-gpu")
            channel = self.cfg.get("browser_channel", "chromium")
            kwargs = {"headless": self.mode == "headless", "args": args}
            if channel and channel != "chromium":
                kwargs["channel"] = channel
            self.browser = await self.pw.chromium.launch(**kwargs)

    async def stop(self) -> None:
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        self.browser = None

    async def new_context(self, storage_state: dict | None) -> BrowserContext:
        await self.start()
        return await self.browser.new_context(storage_state=storage_state, viewport=VIEWPORT,
                                              locale="en-US", timezone_id=None)

    def _slot(self, account: str) -> int:
        if account not in self.slots:
            used = set(self.slots.values())
            i = 0
            while i in used:
                i += 1
            self.slots[account] = i
        return self.slots[account]

    def release(self, account: str) -> None:
        self.slots.pop(account, None)

    async def new_page(self, ctx: BrowserContext, account: str, block: list[str]) -> Page:
        page = await ctx.new_page()
        try:
            cdp = await ctx.new_cdp_session(page)
            if block:
                await cdp.send("Network.enable")
                await cdp.send("Network.setBlockedURLs", {"urls": block})
            await self.place_window(cdp, account)
        except Exception:
            pass
        return page

    async def place_window(self, cdp, account: str, show: bool = False) -> None:
        if self.mode == "headless":
            return
        win = await cdp.send("Browser.getWindowForTarget")
        wid = win["windowId"]
        if self.mode == "hidden" and not show:
            await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
            return
        w, h = self.cfg.get("window_size", [480, 640])
        if show:
            w, h = 1150, 900
        slot = self._slot(account)
        cols = max(1, 1920 // (w + 4))
        x, y = (slot % cols) * (w + 4), (slot // cols % 2) * (h // 2 + 30)
        await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        await cdp.send("Browser.setWindowBounds", {"windowId": wid,
                                                   "bounds": {"left": x, "top": y, "width": w, "height": h}})

    async def show(self, page: Page, account: str, show: bool = True) -> None:
        if self.mode == "headless":
            return
        cdp = await page.context.new_cdp_session(page)
        await self.place_window(cdp, account, show=show)
        if show:
            await page.bring_to_front()
        await cdp.detach()

    async def chromium_ram_mb(self) -> float:
        try:
            import psutil
            total = 0
            me = psutil.Process()
            for c in me.children(recursive=True):
                n = (c.name() or "").lower()
                if "chrom" in n or "headless_shell" in n or "msedge" in n or "chrome" in n:
                    total += c.memory_info().rss
            return round(total / 1048576, 1)
        except Exception:
            return 0.0


async def interactive_login(prov: dict, storage_state: dict | None = None, timeout_s: int = 900,
                            on_progress=None) -> dict | None:
    """Open a normal small window so a human can log in; return the storage state."""
    from .providers.generic import JS_STATE
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, args=["--no-first-run", "--no-default-browser-check",
                                                             "--disable-extensions", "--window-size=1000,820"])
    ctx = await browser.new_context(storage_state=storage_state, viewport=None, locale="en-US")
    page = await ctx.new_page()
    url = prov.get("login_url") or prov.get("home_url") or prov.get("new_chat_url")
    state = None
    ok_streak = 0
    closed = asyncio.Event()
    page.on("close", lambda *_: closed.set())
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while loop.time() < deadline and not closed.is_set():
            try:
                state = await ctx.storage_state()
                st = await page.evaluate(JS_STATE, prov.get("selectors", {}))
                logged = st.get("logged_in") and st.get("input") and not st.get("login")
                ok_streak = ok_streak + 1 if logged else 0
                if on_progress:
                    on_progress("logged_in" if logged else "waiting")
                if ok_streak >= 3:
                    state = await ctx.storage_state()
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass
            try:
                await asyncio.wait_for(closed.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await pw.stop()
    if state and (ok_streak >= 3 or state.get("cookies")):
        return {"state": state, "verified": ok_streak >= 3}
    return None
