"""A fake chat website for testing the whole pipeline without real accounts.
It behaves like a (dumb but protocol-following) AI chat. Scenario knobs via env:
MOCK_BAD_FORMAT=1   first reply ignores the protocol
MOCK_RATE_LIMIT=N   every N-th conversation answers with a usage-limit message
MOCK_DELAY=1.5      seconds of "thinking"
MOCK_BROKEN=1       first code written has a syntax error (tests the fix loop)"""
from __future__ import annotations

import asyncio
import html
import os
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()
CONVS: dict[str, dict] = {}
COUNTER = {"n": 0}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>MockChat</title>
<style>body{font-family:sans-serif;margin:0;background:#111;color:#ddd}#messages{padding:12px;padding-bottom:140px}
.msg{margin:8px 0;padding:8px;border-radius:6px}.user{background:#223}.assistant{background:#232}
pre{background:#000;padding:6px;overflow:auto}#bar{position:fixed;bottom:0;left:0;right:0;background:#1a1a1a;padding:8px;display:flex;gap:6px}
textarea{flex:1;height:70px}#stop{display:none}</style></head><body>
<div id="messages">__MESSAGES__</div>
<div id="bar"><textarea id="prompt" placeholder="Message MockChat"></textarea>
<button id="send">Send</button><button id="stop">Stop</button></div>
<script>
let conv = "__CONV__";
const ta = document.getElementById('prompt');
async function send(){
  const text = ta.value; if(!text.trim()) return; ta.value='';
  const m = document.getElementById('messages');
  const u = document.createElement('div'); u.className='msg user'; u.textContent=text; m.appendChild(u);
  document.getElementById('stop').style.display='inline';
  let id = conv;
  const r = await fetch('/api/chat', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({conv:id, text})});
  const j = await r.json();
  if (!conv) { conv = j.conv; history.replaceState(null, '', '/c/' + j.conv); }
  const a = document.createElement('div'); a.className='msg assistant'; m.appendChild(a);
  // simulate streaming
  const full = j.html; let i = 0;
  while (i < full.length) { i = Math.min(full.length, i + 400); a.innerHTML = full.slice(0, i); await new Promise(r=>setTimeout(r, 60)); }
  a.innerHTML = full;
  document.getElementById('stop').style.display='none';
  window.scrollTo(0, document.body.scrollHeight);
}
document.getElementById('send').onclick = send;
ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
</script></body></html>"""

LOGIN = """<!doctype html><html><head><title>Log in - MockChat</title></head><body style="background:#111;color:#ddd">
<form id="login-form" method="post" action="/login"><h2>MockChat login</h2>
<input name="user" placeholder="email" value="test@example.com"><button type="submit">Log in</button></form></body></html>"""


def md_to_html(text: str) -> str:
    out, pos = [], 0
    for m in re.finditer(r"```[ \t]*([\w+#.-]*)[ \t]*\r?\n(.*?)```", text, re.S):
        out.append("<p>" + html.escape(text[pos:m.start()]).replace("\n", "<br>") + "</p>")
        lang = m.group(1)
        out.append(f'<div class="code-header">{lang or "text"} <button>Copy</button></div>'
                   f'<pre><code class="language-{lang}">' + html.escape(m.group(2).rstrip("\n")) + "</code></pre>")
        pos = m.end()
    out.append("<p>" + html.escape(text[pos:]).replace("\n", "<br>") + "</p>")
    return "".join(out)


def gd_for(path: str, broken: bool) -> str:
    cls = "".join(p.capitalize() for p in re.sub(r"\W", "_", path.rsplit("/", 1)[-1].split(".")[0]).split("_"))
    if path.endswith(".tscn"):
        return '[gd_scene format=3]\n\n[node name="Root" type="Node2D"]\n'
    if path.endswith(".gd"):
        body = (f"extends Node\nclass_name Mock{cls}\n\nsignal finished\n\n@export var speed: float = 100.0\n\n"
                f"func _ready() -> void:\n    print(\"{cls} ready\")\n\nfunc do_work(amount: int) -> int:\n"
                f"    var total := amount * 2\n    finished.emit()\n    return total\n")
        if broken:
            body = body.replace("var total := amount * 2", "var total := (amount * 2")
        return body
    return f"# {path}\n"


def brain(conv: dict, text: str) -> str:
    conv["turn"] += 1
    if os.environ.get("MOCK_BAD_FORMAT") and conv["turn"] == 1:
        return "Sure! I will create the player script. Here is how I would approach it..."
    if "conversation is getting too long" in text:
        return "```\n@@WRITE _handover.md\nAll files written. Only REPORT.md and DONE left.\n```"
    if "FILES YOU MUST CREATE" in text or "MESSAGE FROM THE LEAD ENGINEER" in text or "HANDOVER FROM THE PREVIOUS" in text:
        files = re.findall(r"^- (\S+\.(?:gd|tscn|tres|md|txt|json))\s*$", text.split("ASSETS AVAILABLE")[0], re.M)
        files = [f for f in files if not f.startswith("_context")] or conv.get("files") or ["scripts/mock.gd"]
        conv["files"] = files
        broken = bool(os.environ.get("MOCK_BROKEN")) and not conv.get("fixed")
        parts = ["I'll write the files now."]
        for f in files:
            lang = "gdscript" if f.endswith(".gd") else ""
            parts.append(f"```{lang}\n@@WRITE {f}\n{gd_for(f, broken)}```")
        parts.append("```\n@@CHECK\n```")
        return "\n\n".join(parts)
    if "CHECK FAILED" in text or "DONE REFUSED" in text and "errors" in text:
        conv["fixed"] = True
        parts = ["Fixing the errors."]
        for f in conv.get("files", []):
            lang = "gdscript" if f.endswith(".gd") else ""
            parts.append(f"```{lang}\n@@WRITE {f}\n{gd_for(f, False)}```")
        parts.append("```\n@@CHECK\n```")
        return "\n\n".join(parts)
    if "CHECK OK" in text or "DONE REFUSED" in text or "CHECK: no script" in text or "Godot not installed" in text:
        files = conv.get("files", [])
        report = ("# REPORT mock\n## Summary\nMock implementation of the requested files.\n## Files\n"
                  + "\n".join(f"- `{f}` - mock" for f in files) +
                  "\n## Public API\n- `func do_work(amount: int) -> int` - doubles amount\n- `signal finished`\n"
                  "## Dependencies\nnone\n## Integration\nAttach the script to a Node.\n## Assets needed\nnone\n"
                  "## Decisions & assumptions\nnone\n## Known limitations / TODO\nmock only\n## How to test\nrun it\n")
        return f"```markdown\n@@WRITE REPORT.md\n{report}```\n\n```\n@@DONE\n```"
    if "FORMAT ERROR" in text:
        return "```\n@@LIST .\n```"
    if "ANSWER FROM LEAD ENGINEER" in text:
        return "```\n@@CHECK\n```"
    return "```\n@@CHECK\n```"


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN


@app.post("/login")
async def do_login():
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("mock_session", uuid.uuid4().hex, max_age=86400 * 30)
    return r


@app.get("/", response_class=HTMLResponse)
@app.get("/new", response_class=HTMLResponse)
async def home(request: Request):
    if not request.cookies.get("mock_session"):
        return RedirectResponse("/login")
    return PAGE.replace("__MESSAGES__", "").replace("__CONV__", "")


@app.get("/c/{cid}", response_class=HTMLResponse)
async def conv_page(cid: str, request: Request):
    if not request.cookies.get("mock_session"):
        return RedirectResponse("/login")
    conv = CONVS.get(cid)
    msgs = ""
    for role, text in (conv or {}).get("log", []):
        inner = html.escape(text) if role == "user" else md_to_html(text)
        msgs += f'<div class="msg {role}">{inner}</div>'
    return PAGE.replace("__MESSAGES__", msgs).replace("__CONV__", cid)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    cid = body.get("conv") or uuid.uuid4().hex[:12]
    if cid not in CONVS:
        COUNTER["n"] += 1
        CONVS[cid] = {"turn": 0, "log": [], "n": COUNTER["n"]}
    conv = CONVS[cid]
    text = body.get("text", "")
    await asyncio.sleep(float(os.environ.get("MOCK_DELAY", "1.0")))
    rl = int(os.environ.get("MOCK_RATE_LIMIT", "0") or 0)
    if rl and conv["n"] % rl == 0 and conv["turn"] == 0:
        reply = "You've reached your usage limit. Please try again later."
    else:
        reply = brain(conv, text)
    conv["log"] += [("user", text), ("assistant", reply)]
    return JSONResponse({"conv": cid, "html": md_to_html(reply)})


# ============================================================================ MANUAL relay (plan B)
# Provider "manual": the agent's browser page waits here; a HUMAN copies each message into ANY chat
# (ChatGPT, Gemini, ... in their normal browser) and pastes the answer back on /manual/inbox.
MANUAL: dict[str, dict] = {}

MANUAL_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Manual relay agent</title>
<style>body{font-family:sans-serif;margin:0;background:#101418;color:#ddd}#messages{padding:12px;padding-bottom:300px}
.msg{margin:8px 0;padding:8px;border-radius:6px;white-space:pre-wrap}.user{background:#223;max-height:160px;overflow:auto}
.assistant{background:#232;white-space:normal}pre{background:#000;padding:6px;overflow:auto}
#bar{position:fixed;bottom:0;left:0;right:0;background:#1a1a1a;padding:8px}#prompt{width:100%;height:40px}
#stop{display:none;background:#a60;color:#fff;border:0;padding:6px 10px}#human{width:100%;height:120px}
.hint{color:#fc6;font-size:13px}</style></head><body>
<div id="messages">__MESSAGES__</div>
<div id="bar"><textarea id="prompt" placeholder="(the bridge types here)"></textarea>
<button id="send">Send</button><button id="stop">WAITING FOR A HUMAN...</button>
<div class="hint">Waiting messages: open <a href="/manual/inbox" target="_blank" style="color:#6cf">http://127.0.0.1:__PORT__/manual/inbox</a>
in any browser on this PC, or paste the AI answer below.</div>
<textarea id="human" placeholder="Paste the AI's full answer here (use the chat's Copy button so code blocks keep their ``` fences)"></textarea>
<button id="human-send">Submit answer</button></div>
<script>
let conv = "__CONV__";
const ta = document.getElementById('prompt'), m = document.getElementById('messages'), stop = document.getElementById('stop');
async function send(){
  const text = ta.value; if(!text.trim()) return; ta.value='';
  const u = document.createElement('div'); u.className='msg user'; u.textContent=text; m.appendChild(u);
  stop.style.display='inline';
  const r = await fetch('/api/manual/send', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({conv, text})});
  const j = await r.json();
  if (!conv) { conv = j.conv; history.replaceState(null, '', '/manual/c/' + j.conv); }
  poll(j.n);
}
async function poll(n){
  while (true) {
    await new Promise(r=>setTimeout(r, 1500));
    try {
      const j = await (await fetch('/api/manual/state?conv=' + conv + '&n=' + n)).json();
      if (j.html) {
        const a = document.createElement('div'); a.className='msg assistant'; a.innerHTML = j.html; m.appendChild(a);
        stop.style.display='none'; window.scrollTo(0, document.body.scrollHeight); return;
      }
    } catch (e) {}
  }
}
document.getElementById('send').onclick = send;
ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
document.getElementById('human-send').onclick = async () => {
  const h = document.getElementById('human'); if (!h.value.trim() || !conv) return;
  await fetch('/api/manual/reply', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({conv, text: h.value})});
  h.value = '';
};
</script></body></html>"""

INBOX_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>MultiConnect manual inbox</title>
<style>body{font-family:sans-serif;background:#101418;color:#ddd;margin:16px;max-width:1100px}
.item{border:1px solid #345;border-radius:8px;padding:10px;margin:12px 0;background:#161c22}
pre{background:#000;padding:8px;max-height:220px;overflow:auto;white-space:pre-wrap}textarea{width:100%;height:160px}
button{padding:6px 12px;margin:4px 4px 4px 0}.ok{color:#6c6}h1{font-size:20px}.muted{color:#889}</style></head><body>
<h1>Manual relay inbox <span class="muted">(plan B: you are the browser)</span></h1>
<ol class="muted"><li>Click <b>Copy message</b> and paste it into any AI chat (ChatGPT, Gemini, DeepSeek...).
Use ONE AI chat per agent chat id (keep the same conversation for all its messages).</li>
<li>When the AI finishes, press the chat's own <b>Copy</b> button under the answer (keeps the ``` code fences).</li>
<li>Paste it into the box and click <b>Submit answer</b>.</li></ol>
<div id="list"><p class="muted">No messages waiting.</p></div>
<script>
const shown = {};
async function refresh(){
  let j; try { j = await (await fetch('/api/manual/pending')).json(); } catch (e) { return; }
  const list = document.getElementById('list');
  const keys = new Set(j.items.map(x => x.conv + ':' + x.n));
  for (const k of Object.keys(shown)) if (!keys.has(k)) { shown[k].remove(); delete shown[k]; }
  for (const x of j.items) {
    const k = x.conv + ':' + x.n; if (shown[k]) continue;
    const d = document.createElement('div'); d.className = 'item';
    d.innerHTML = '<b>Agent chat ' + x.conv + '</b> <span class="muted">message #' + x.n + ', ' + x.text.length + ' chars' +
      (x.n === 1 ? ' - START A NEW AI CHAT for this one' : ' - continue the SAME AI chat') + '</span>' +
      '<pre></pre><button class="cp">Copy message</button><span class="ok"></span>' +
      '<textarea placeholder="Paste the AI answer here"></textarea><button class="sb">Submit answer</button>';
    d.querySelector('pre').textContent = x.text;
    d.querySelector('.cp').onclick = async () => {
      try { await navigator.clipboard.writeText(x.text); } catch (e) {
        const t = document.createElement('textarea'); t.value = x.text; document.body.appendChild(t); t.select();
        document.execCommand('copy'); t.remove(); }
      d.querySelector('.ok').textContent = ' copied!';
    };
    d.querySelector('.sb').onclick = async () => {
      const v = d.querySelector('textarea').value; if (!v.trim()) return;
      await fetch('/api/manual/reply', {method:'POST', headers:{'content-type':'application/json'},
                                         body: JSON.stringify({conv: x.conv, text: v})});
      d.remove(); delete shown[k];
    };
    if (list.querySelector('p')) list.innerHTML = '';
    list.appendChild(d); shown[k] = d;
  }
  if (!list.children.length) list.innerHTML = '<p class="muted">No messages waiting.</p>';
  document.title = (j.items.length ? '(' + j.items.length + ') ' : '') + 'MultiConnect manual inbox';
}
refresh(); setInterval(refresh, 2500);
</script></body></html>"""


def fence_bare_commands(text: str) -> str:
    """A human may copy an answer WITHOUT its ``` fences (selected text instead of the Copy button).
    Re-wrap bare @@ commands so the bridge still sees them as code blocks."""
    if "```" in text or "@@" not in text:
        return text
    out: list[str] = []
    block: list[str] | None = None

    def close() -> None:
        nonlocal block
        if block is not None:
            out.append("```\n" + "\n".join(block).rstrip("\n") + "\n```")
            block = None
    for line in text.split("\n"):
        if re.match(r"^\s*@@(WRITE|REPLACE)\b", line):
            close()
            block = [line.strip()]
        elif re.match(r"^\s*@@(READ|LIST|CHECK|RUN|ASK|DONE)\b", line):
            close()
            out.append("```\n" + line.strip() + "\n```")
        elif block is not None:
            block.append(line)
        else:
            out.append(line)
    close()
    return "\n".join(out)


def _manual_page(cid: str, request: Request) -> str:
    conv = MANUAL.get(cid) or {"log": []}
    msgs = ""
    for role, text in conv["log"]:
        inner = html.escape(text) if role == "user" else md_to_html(text)
        msgs += f'<div class="msg {role}">{inner}</div>'
    port = request.url.port or 8790
    return MANUAL_PAGE.replace("__MESSAGES__", msgs).replace("__CONV__", cid).replace("__PORT__", str(port))


@app.get("/manual/", response_class=HTMLResponse)
@app.get("/manual/new", response_class=HTMLResponse)
async def manual_new(request: Request):
    return _manual_page("", request)


@app.get("/manual/c/{cid}", response_class=HTMLResponse)
async def manual_conv(cid: str, request: Request):
    return _manual_page(cid, request)


@app.get("/manual/inbox", response_class=HTMLResponse)
async def manual_inbox():
    return INBOX_PAGE


@app.post("/api/manual/send")
async def manual_send(request: Request):
    body = await request.json()
    cid = body.get("conv") or uuid.uuid4().hex[:8]
    conv = MANUAL.setdefault(cid, {"log": [], "pending": None, "n": 0})
    conv["n"] += 1
    conv["pending"] = body.get("text", "")
    conv["log"].append(("user", conv["pending"]))
    return {"conv": cid, "n": conv["n"]}


@app.get("/api/manual/state")
async def manual_state(conv: str, n: int):
    c = MANUAL.get(conv)
    if not c or c["n"] != n or c["pending"] is not None:
        return {"html": None}
    return {"html": md_to_html(c["log"][-1][1])}


@app.post("/api/manual/reply")
async def manual_reply(request: Request):
    body = await request.json()
    c = MANUAL.get(body.get("conv", ""))
    if not c or c["pending"] is None:
        return JSONResponse({"ok": False, "error": "nothing is waiting in this chat"}, status_code=409)
    c["log"].append(("assistant", fence_bare_commands(body.get("text", "").replace("\r\n", "\n"))))
    c["pending"] = None
    return {"ok": True}


@app.get("/api/manual/pending")
async def manual_pending():
    return {"items": [{"conv": k, "n": v["n"], "text": v["pending"]} for k, v in MANUAL.items()
                      if v["pending"] is not None]}


def run_mock(port: int = 8790) -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def start_in_thread(port: int = 8790) -> bool:
    """Run the local test/manual site inside the worker (127.0.0.1 only - no firewall prompt)."""
    import socket
    import threading
    s = socket.socket()
    try:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False  # already running (mockchat.bat or another worker)
    finally:
        s.close()
    threading.Thread(target=run_mock, args=(port,), daemon=True, name="local-sites").start()
    return True
