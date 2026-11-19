"""Godot helpers shared by the worker (sandbox checks) and the coordinator (game checks)."""
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import sys
from pathlib import Path

from .config import ROOT, TEMPLATES_DIR

CHECK_SCRIPT_REL = "_hive/tools/check.gd"
ERR_LINE_RE = re.compile(r"(SCRIPT ERROR|Parse Error|ERROR:|Compile Error|Failed to load|Identifier .* not declared|HIVE_FAIL|HIVE_MISSING|WARNING: .*res://)", re.I)
NOISE_RE = re.compile(r"(Godot Engine v|OpenGL|Vulkan|Using Device|^\s*$|HIVE_OK|HIVE_DONE|at: (?:_init|load) \(core/|editor_settings|EditorSettings)", re.I)


def find_godot(configured: str = "") -> str | None:
    cands: list[str] = []
    if configured:
        cands.append(configured)
    if os.environ.get("GODOT"):
        cands.append(os.environ["GODOT"])
    for pattern in [str(ROOT / ".godot_bin" / "**" / "*console*.exe"),
                    str(ROOT / ".godot_bin" / "**" / "Godot*.exe"),
                    str(ROOT / ".godot_bin" / "**" / "godot*")]:
        cands.extend(sorted(glob.glob(pattern, recursive=True), reverse=True))
    for name in ("godot", "godot4", "Godot", "godot.exe", "godot4.exe"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    if sys.platform == "win32":
        for base in [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""),
                     os.path.expanduser("~/Desktop"), os.path.expanduser("~/Downloads"), "C:/Godot", "D:/Godot"]:
            if base:
                cands.extend(sorted(glob.glob(os.path.join(base, "**", "Godot*console*.exe"), recursive=False), reverse=True))
                cands.extend(sorted(glob.glob(os.path.join(base, "Godot*", "Godot*console*.exe")), reverse=True))
    for c in cands:
        if c and Path(c).is_file():
            return str(Path(c))
    return None


def ensure_check_script(project_dir: Path) -> None:
    dst = project_dir / CHECK_SCRIPT_REL
    src = TEMPLATES_DIR / "godot_sandbox" / CHECK_SCRIPT_REL
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


async def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, **kwargs)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -999, "TIMEOUT"
    return proc.returncode or 0, out.decode("utf-8", "replace")


def filter_errors(output: str, limit: int = 40) -> list[str]:
    lines = output.splitlines()
    errs: list[str] = []
    for i, l in enumerate(lines):
        if ERR_LINE_RE.search(l) and not NOISE_RE.search(l):
            item = l.strip()
            if i + 1 < len(lines) and lines[i + 1].strip().startswith("at:"):
                item += "  " + lines[i + 1].strip()
            if item not in errs:
                errs.append(item)
        if len(errs) >= limit:
            break
    return errs


class Godot:
    def __init__(self, path: str | None, max_parallel: int = 2):
        self.path = path
        self.sem = asyncio.Semaphore(max(1, max_parallel))

    @property
    def available(self) -> bool:
        return bool(self.path)

    async def import_project(self, project: Path, timeout: float = 120) -> tuple[bool, str]:
        if not self.path:
            return False, "godot not available"
        async with self.sem:
            code, out = await _run([self.path, "--headless", "--path", str(project), "--import"], project, timeout)
            # --import exists since Godot 4.3; older 4.x builds need the editor round-trip instead
            if code not in (0, -999):
                code, out = await _run([self.path, "--headless", "--path", str(project), "--editor", "--quit"], project, timeout)
        return code in (0,), out

    async def check(self, project: Path, files: list[str], timeout: float = 90, reimport: bool = False) -> dict:
        """Return {"ok": bool, "errors": [...], "checked": [...], "available": bool}."""
        files = [f for f in files if f.endswith((".gd", ".tscn", ".tres", ".scn", ".res"))]
        if not self.path:
            return {"ok": True, "available": False, "errors": [], "checked": files,
                    "note": "Godot not found on this computer; only basic syntax checks were done."}
        if not files:
            return {"ok": True, "available": True, "errors": [], "checked": []}
        ensure_check_script(project)
        if reimport or not (project / ".godot" / "global_script_class_cache.cfg").exists():
            await self.import_project(project)
        async with self.sem:
            code, out = await _run([self.path, "--headless", "--path", str(project), "-s",
                                    "res://" + CHECK_SCRIPT_REL, "--", *files], project, timeout)
        errors = filter_errors(out)
        if code == -999:
            errors.insert(0, "Godot check timed out")
        ok = code == 0 and not any(("ERROR" in e.upper() or "HIVE_FAIL" in e) for e in errors)
        if "HIVE_DONE" not in out and code != -999:
            ok = False
            if not errors:
                errors = out.strip().splitlines()[-15:]
        return {"ok": ok, "available": True, "errors": errors, "checked": files}

    async def run_scene(self, project: Path, scene: str, frames: int = 300, timeout: float = 60) -> dict:
        if not self.path:
            return {"ok": True, "available": False, "errors": [], "output": "Godot not available"}
        scene = scene if scene.startswith("res://") else "res://" + scene
        async with self.sem:
            code, out = await _run([self.path, "--headless", "--path", str(project), "--quit-after", str(frames), scene],
                                   project, timeout)
        errors = filter_errors(out)
        if code == -999:
            errors.insert(0, f"Scene did not exit within {timeout}s")
        tail = "\n".join([l for l in out.splitlines() if not NOISE_RE.search(l)][-30:])
        return {"ok": code == 0 and not errors, "available": True, "errors": errors, "output": tail}


def basic_syntax_check(path: str, text: str) -> list[str]:
    """Fallback checks when Godot is missing."""
    errs = []
    if path.endswith(".gd"):
        for n, line in enumerate(text.split("\n"), 1):
            lead = line[: len(line) - len(line.lstrip(" \t"))]
            if " " in lead and "\t" in lead:
                errs.append(f"{path}:{n}: mixed tabs and spaces in indentation")
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack = []
        in_str = None
        for ch in re.sub(r"#.*", "", text):
            if in_str:
                if ch == in_str:
                    in_str = None
                continue
            if ch in "\"'":
                in_str = ch
            elif ch in pairs:
                stack.append(pairs[ch])
            elif ch in ")]}":
                if not stack or stack.pop() != ch:
                    errs.append(f"{path}: unbalanced bracket '{ch}'")
                    break
        if stack and not errs:
            errs.append(f"{path}: unclosed bracket(s)")
    return errs
