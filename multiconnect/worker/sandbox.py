"""A sandbox = clean copy of the game for ONE chat. Agents can only touch files here."""
from __future__ import annotations

import io
import json
import shutil
import time
import zipfile
from fnmatch import fnmatch
from pathlib import Path

from ..common.protocol import MAX_FILE_BYTES, Action, normalize_code

ALWAYS_WRITABLE = ["REPORT.md", "_handover.md", "tests/*", "tests/**"]


class Sandbox:
    def __init__(self, root: Path, writable: list[str]):
        self.root = root
        self.writable = list(writable)
        self.base_files: set[str] = set()
        self.written: dict[str, float] = {}   # path -> time written
        self.meta_file = root.parent / "sandbox_meta.json"
        if self.meta_file.exists():
            m = json.loads(self.meta_file.read_text(encoding="utf-8"))
            self.base_files = set(m.get("base_files", []))
            self.written = m.get("written", {})

    def save_meta(self) -> None:
        self.meta_file.write_text(json.dumps({"base_files": sorted(self.base_files), "written": self.written,
                                              "writable": self.writable}), encoding="utf-8")

    @property
    def exists(self) -> bool:
        return self.root.exists() and self.meta_file.exists()

    def prepare(self, bundle_zip: Path, restore_zip: bytes | None, contract: str, task_md: str) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        with zipfile.ZipFile(bundle_zip) as z:
            z.extractall(self.root)
            self.base_files = {n for n in z.namelist() if not n.endswith("/")}
        self.written = {}
        if restore_zip:
            with zipfile.ZipFile(io.BytesIO(restore_zip)) as z:
                man = json.loads(z.read("_meta/manifest.json"))
                for f in man.get("files", []):
                    try:
                        data = z.read(f)
                    except KeyError:
                        continue
                    dst = self.root / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                    self.written[f] = time.time()
        self.write_context(contract, task_md)
        self.save_meta()

    def write_context(self, contract: str, task_md: str) -> None:
        ctx = self.root / "_context"
        ctx.mkdir(exist_ok=True)
        (ctx / ".gdignore").write_text("", encoding="utf-8")
        if contract is not None:
            (ctx / "contract.md").write_text(contract or "(no contract)", encoding="utf-8")
        if task_md:
            (ctx / "task.md").write_text(task_md, encoding="utf-8")

    # ------------------------------------------------------------------ permissions
    def can_write(self, rel: str) -> tuple[bool, str]:
        if rel.startswith("_context/") or rel.startswith("_hive/") or rel == "project.godot":
            return False, "read-only (context / project config)"
        if rel.startswith(".godot/"):
            return False, "engine cache folder"
        if any(fnmatch(rel, p) for p in self.writable + ALWAYS_WRITABLE):
            return True, ""
        if rel in self.base_files:
            return False, "existing project file not listed in your task (ask with @@ASK if you must change it)"
        return True, "new file not listed in the task - allowed, mention it in REPORT.md"

    # ------------------------------------------------------------------ actions
    def write(self, a: Action) -> str:
        ok, note = self.can_write(a.arg)
        if not ok:
            return f"DENIED @@WRITE {a.arg}: {note}"
        data = normalize_code(a.arg, a.body)
        if len(data.encode()) > MAX_FILE_BYTES:
            return f"DENIED @@WRITE {a.arg}: file too large"
        p = self.root / a.arg
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8", newline="\n")
        self.written[a.arg] = time.time()
        self.save_meta()
        n = data.count("\n")
        return f"WROTE {a.arg} ({n} lines)" + (f" - note: {note}" if note else "")

    def replace(self, a: Action) -> str:
        ok, note = self.can_write(a.arg)
        if not ok:
            return f"DENIED @@REPLACE {a.arg}: {note}"
        p = self.root / a.arg
        if not p.exists():
            return f"FAILED @@REPLACE {a.arg}: file does not exist (use @@WRITE)"
        if not a.replacements:
            return f"FAILED @@REPLACE {a.arg}: no SEARCH/REPLACE blocks found"
        text = p.read_text(encoding="utf-8")
        applied, missing = 0, []
        for search, repl in a.replacements:
            s_norm = normalize_code(a.arg, search).rstrip("\n")
            r_norm = normalize_code(a.arg, repl).rstrip("\n") if repl.strip() else ""
            if s_norm and s_norm in text:
                text = text.replace(s_norm, r_norm, 1)
                applied += 1
            elif search in text:
                text = text.replace(search, repl, 1)
                applied += 1
            else:
                missing.append(search.strip().split("\n")[0][:80])
        p.write_text(text, encoding="utf-8", newline="\n")
        self.written[a.arg] = time.time()
        self.save_meta()
        msg = f"REPLACED in {a.arg}: {applied} block(s) applied"
        if missing:
            msg += "; NOT FOUND (send the exact current text or use @@WRITE): " + " | ".join(missing)
        return msg

    def read(self, rel: str, limit: int = 14000) -> str:
        p = self.root / rel
        if not p.is_file():
            near = [f for f in self.list_files() if Path(f).name == Path(rel).name][:5]
            return f"FAILED @@READ {rel}: not found" + (f" (did you mean: {', '.join(near)})" if near else "")
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"@@READ {rel}: binary file, {len(data)} bytes"
        if p.suffix == ".png":
            return f"@@READ {rel}: image"
        cut = ""
        if len(text) > limit:
            text, cut = text[:limit], f"\n... (truncated, {len(data)} bytes total)"
        return f"CONTENT OF {rel}:\n```\n{text}\n```{cut}"

    def list_files(self, sub: str = "") -> list[str]:
        base = (self.root / sub) if sub else self.root
        out = []
        if not base.exists():
            return out
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                if rel.startswith(".godot/") or rel.endswith(".import") or rel.endswith(".uid"):
                    continue
                out.append(rel)
        return out

    def listing(self, sub: str = "") -> str:
        files = self.list_files(sub)
        if not files:
            return f"@@LIST {sub or '.'}: empty"
        shown = files[:250]
        extra = f"\n... and {len(files) - 250} more" if len(files) > 250 else ""
        return f"FILES in {sub or '.'}:\n" + "\n".join(shown) + extra

    def written_files(self) -> list[str]:
        return [f for f in sorted(self.written) if (self.root / f).exists()]

    # ------------------------------------------------------------------ result
    def build_result(self, manifest: dict, transcript: str) -> bytes:
        buf = io.BytesIO()
        files = self.written_files()
        if "REPORT.md" not in files and (self.root / "REPORT.md").exists():
            files.append("REPORT.md")
        manifest = dict(manifest, files=files)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(self.root / f, f)
            z.writestr("_meta/manifest.json", json.dumps(manifest, indent=1))
            z.writestr("_meta/transcript.md", transcript)
        return buf.getvalue()
