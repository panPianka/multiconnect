"""Agent text protocol: parse chat replies into actions, sanitize paths & code.

A reply arrives as a list of segments extracted from the chat DOM:
    [{"type": "text", "text": "..."}, {"type": "code", "text": "..."}, ...]
Code segments keep exact whitespace, which is why every protocol command should
live inside a code block. Text segments are scanned as a fallback.

Commands:
    @@WRITE <path>      (first line of a code block; rest = full file content)
    @@REPLACE <path>    (first line; then SEARCH/REPLACE blocks)
    @@READ <path>  @@LIST [dir]  @@CHECK  @@RUN <scene>  @@ASK <question>  @@DONE
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

CMD_RE = re.compile(r"^\s*[`*_>]*\s*@@\s*(WRITE|REPLACE|READ|LIST|CHECK|RUN|ASK|DONE)\b[`*_]*\s*(.*?)\s*[`*_]*\s*$", re.I)
SR_RE = re.compile(r"<{5,9} ?SEARCH\s*\n(.*?)\n?={5,9}\s*\n(.*?)\n?>{5,9} ?REPLACE", re.S)
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
MAX_FILE_BYTES = 400_000


@dataclass
class Action:
    kind: str                     # write|replace|read|list|check|run|ask|done
    arg: str = ""
    body: str = ""
    replacements: list[tuple[str, str]] = field(default_factory=list)


def clean_path(raw: str) -> str | None:
    """Return a safe relative posix path, or None if it escapes the sandbox."""
    p = raw.strip().strip("`'\"*").strip()
    if p.lower().startswith("res://"):
        p = p[6:]
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or re.match(r"^[a-zA-Z]:", p) or "\x00" in p:
        return None
    parts = PurePosixPath(p).parts
    if any(part in ("..", "") for part in parts):
        return None
    return str(PurePosixPath(*parts))


def normalize_code(path: str, text: str) -> str:
    text = text.translate(ZERO_WIDTH).replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    if path.endswith(".gd"):
        text = _gdscript_tabs(text)
    if not text.endswith("\n"):
        text += "\n"
    return text


def _gdscript_tabs(text: str) -> str:
    lines = text.split("\n")
    space_indents = [len(l) - len(l.lstrip(" ")) for l in lines if l.startswith(" ") and l.strip()]
    if not space_indents:
        return text
    unit = min(i for i in space_indents if i > 0)
    unit = 4 if unit >= 4 else unit
    out = []
    for l in lines:
        lead = l[: len(l) - len(l.lstrip(" \t"))]
        rest = l[len(lead):]
        tabs = lead.count("\t")
        spaces = lead.count(" ")
        tabs += spaces // unit
        out.append("\t" * tabs + rest if rest else "")
    return "\n".join(out)


def _parse_line_cmd(line: str) -> tuple[str, str] | None:
    m = CMD_RE.match(line)
    if not m:
        return None
    return m.group(1).upper(), m.group(2).strip()


def parse_reply(segments: list[dict]) -> tuple[list[Action], list[str]]:
    """Return (actions, warnings)."""
    actions: list[Action] = []
    warnings: list[str] = []
    pending_write: str | None = None  # @@WRITE found in prose, waiting for next code block

    for seg in segments:
        text = seg.get("text", "")
        if seg.get("type") == "code":
            body_lines = text.replace("\r\n", "\n").split("\n")
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            first = _parse_line_cmd(body_lines[0]) if body_lines else None
            if first and first[0] in ("WRITE", "REPLACE"):
                kind, arg = first
                rest = "\n".join(body_lines[1:])
                actions.append(_make_file_action(kind, arg, rest, warnings))
                pending_write = None
                continue
            if pending_write is not None and not first:
                actions.append(_make_file_action("WRITE", pending_write, text, warnings))
                pending_write = None
                continue
            # command block: every @@ line is a command; ASK may span following lines
            i = 0
            found = False
            while i < len(body_lines):
                c = _parse_line_cmd(body_lines[i])
                if c:
                    found = True
                    kind, arg = c
                    if kind == "ASK":
                        extra = []
                        j = i + 1
                        while j < len(body_lines) and not _parse_line_cmd(body_lines[j]):
                            extra.append(body_lines[j])
                            j += 1
                        actions.append(Action("ask", (arg + "\n" + "\n".join(extra)).strip()))
                        i = j
                        continue
                    if kind in ("WRITE", "REPLACE"):
                        warnings.append(f"@@{kind} must be the FIRST line of its own code block")
                    else:
                        actions.append(Action(kind.lower(), arg))
                i += 1
            if not found and pending_write is None:
                pass  # plain code without command: ignored
        else:
            for line in text.split("\n"):
                c = _parse_line_cmd(line)
                if not c:
                    continue
                kind, arg = c
                if kind in ("WRITE", "REPLACE"):
                    pending_write = arg if kind == "WRITE" else None
                    if kind == "REPLACE":
                        warnings.append("@@REPLACE must be the first line inside a code block")
                elif kind == "ASK":
                    actions.append(Action("ask", arg))
                else:
                    actions.append(Action(kind.lower(), arg))
    if pending_write is not None:
        warnings.append(f"@@WRITE {pending_write} had no code block after it")
    # de-duplicate repeated CHECK/DONE
    seen = set()
    dedup = []
    for a in actions:
        key = (a.kind, a.arg) if a.kind in ("check", "done", "list") else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        dedup.append(a)
    return dedup, warnings


def _make_file_action(kind: str, arg: str, body: str, warnings: list[str]) -> Action:
    path = clean_path(arg)
    if kind == "WRITE":
        if path is None:
            warnings.append(f"Rejected unsafe path: {arg!r}")
            return Action("invalid", arg)
        return Action("write", path, body)
    reps = SR_RE.findall(body.replace("\r\n", "\n"))
    if path is None:
        warnings.append(f"Rejected unsafe path: {arg!r}")
        return Action("invalid", arg)
    if not reps:
        warnings.append(f"@@REPLACE {arg}: no valid SEARCH/REPLACE blocks found")
    return Action("replace", path, body, reps)


def segments_to_text(segments: list[dict]) -> str:
    out = []
    for s in segments:
        if s.get("type") == "code":
            out.append("```\n" + s.get("text", "") + "\n```")
        else:
            out.append(s.get("text", ""))
    return "\n".join(out)
