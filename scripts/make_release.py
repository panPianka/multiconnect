"""Build release zips into dist/ (and optionally publish them as a GitHub release).

  multiconnect-vX.Y.Z-source.zip   = exactly the committed files (git archive) - small, never has secrets
  multiconnect-vX.Y.Z-offline.zip  = source + portable toolchain (.tools python/uv-cache, .browsers,
                                      .godot_bin, offline/) -> unzip on a PC WITHOUT internet, run setup.bat

Usage (from make_release.bat):  python scripts/make_release.py [--no-offline] [--publish] [--version X.Y.Z]
Run make_offline_bundle.bat first if you want the offline zip.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
# portable toolchain folders that go into the offline zip (NOT .venv: it has absolute paths and is
# rebuilt offline by _ensure_py.bat from .tools\uv-cache on the target PC)
TOOL_DIRS = [".tools/python", ".tools/uv-cache", ".tools/uv", ".browsers", ".godot_bin", "offline"]
# anything matching these must never be inside a release
FORBIDDEN = re.compile(r"(^|/)(state|agents|mirror_backups|dist|\.venv|\.git)/|(^|/)config/(commander|worker|accounts)\.yaml$"
                       r"|codex_mcp_snippet\.toml$|hive-[^/]*\.zip$|(^|/)secrets/")


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, check=check)


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def check_clean() -> None:
    st = sh("git", "status", "--porcelain").stdout.strip()
    if st:
        print("!! Uncommitted changes - commit them first (the release is built from the last commit):")
        print(st)
        sys.exit(1)
    if not sh("git", "rev-parse", "--verify", "HEAD", check=False).stdout.strip():
        print("!! No commits yet. See docs/01_GITHUB.md")
        sys.exit(1)


def source_zip(ver: str, worktree: bool = False) -> Path:
    out = DIST / f"multiconnect-v{ver}-source.zip"
    if not worktree:
        sh("git", "archive", "--format=zip", f"--prefix=multiconnect/", "-o", str(out), "HEAD")
        return out
    # --pendrive: the current files (committed or not), still filtered by .gitignore
    files = sh("git", "ls-files", "-co", "--exclude-standard", "-z").stdout.split("\0")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(f for f in files if f):
            p = ROOT / rel
            if p.is_file():
                z.write(p, "multiconnect/" + rel)
    return out


def offline_zip(ver: str, src: Path) -> Path | None:
    missing = [d for d in (".tools/python", ".tools/uv-cache", ".browsers", "offline") if not (ROOT / d).exists()]
    if missing:
        print(f"   skipped offline zip: missing {missing} - run make_offline_bundle.bat first")
        return None
    out = DIST / f"multiconnect-v{ver}-offline.zip"
    tmp = out.with_suffix(".tmp")
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        with zipfile.ZipFile(src) as s:  # the committed files first
            for info in s.infolist():
                z.writestr(info, s.read(info))
        for d in TOOL_DIRS:
            base = ROOT / d
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(ROOT).as_posix()
                    # uv cache: skip lock/tmp files that are useless and sometimes locked
                    if rel.endswith((".lock", ".tmp")) and "uv-cache" in rel:
                        continue
                    # .pyc caches are rebuilt on first run; they only add size and long paths (MAX_PATH 260)
                    if "/__pycache__/" in rel:
                        continue
                    z.write(p, "multiconnect/" + rel)
                    n += 1
                    if n % 5000 == 0:
                        print(f"   ... {n} files")
    tmp.replace(out)
    return out


def verify(z: Path) -> None:
    with zipfile.ZipFile(z) as f:
        bad = [n for n in f.namelist() if FORBIDDEN.search(n[len("multiconnect/"):] if n.startswith("multiconnect/") else n)]
    if bad:
        z.unlink()
        print(f"!! {z.name} contained private files and was DELETED: {bad[:10]}")
        sys.exit(1)


def find_gh() -> str | None:
    for c in (shutil.which("gh"), ROOT / ".tools" / "gh" / "bin" / "gh.exe"):
        if c and Path(c).exists():
            return str(c)
    return None


def publish(ver: str, files: list[Path]) -> None:
    gh = find_gh()
    tag = f"v{ver}"
    if not gh:
        print("\n gh (GitHub CLI) not found - upload by hand: GitHub > Releases > Draft a new release,")
        print(f" tag {tag}, drag in: " + ", ".join(f.name for f in files) + "   (details: docs/02_RELEASE.md)")
        return
    if sh(gh, "auth", "status", check=False).returncode != 0:
        print(f"\n Not logged in to GitHub. Run:  \"{gh}\" auth login   then make_release.bat --publish again.")
        return
    if sh("git", "remote", check=False).stdout.strip() == "":
        print("\n No git remote. Create the repo first: github_setup.bat (docs/01_GITHUB.md)")
        return
    sh("git", "push", "origin", "HEAD", check=False)
    if sh("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode != 0:
        sh("git", "tag", "-a", tag, "-m", f"MultiConnect {tag}")
    r = sh("git", "push", "origin", tag, check=False)
    if r.returncode != 0:
        print(r.stderr)
    notes = DIST / "release_notes.md"
    notes.write_text(
        f"# MultiConnect {tag}\n\n"
        "- **source.zip** - for PCs with internet: unzip, run `setup.bat`.\n"
        "- **offline.zip** - everything included (Python, Chromium, Godot, cloudflared): unzip on a PC without "
        "internet, run `setup.bat` (no admin needed).\n\n"
        "Start here: `README.md`, then `RUNBOOK.md` (Polish: checklists and fallback plans).\n", encoding="utf-8")
    exists = sh(gh, "release", "view", tag, check=False).returncode == 0
    if exists:
        r = sh(gh, "release", "upload", tag, *[str(f) for f in files], "--clobber", check=False)
    else:
        r = sh(gh, "release", "create", tag, *[str(f) for f in files], "--title", f"MultiConnect {tag}",
               "--notes-file", str(notes), check=False)
    print(r.stdout or "", r.stderr or "")
    print(" Release published." if r.returncode == 0 else " !! gh release failed (see above).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version")
    ap.add_argument("--no-offline", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--pendrive", action="store_true",
                    help="build from the current files (no commit needed), offline zip only, never publishes")
    a = ap.parse_args()
    ver = a.version or version()
    if a.pendrive:
        a.publish = False
    else:
        check_clean()
    DIST.mkdir(exist_ok=True)
    print(f"Building MultiConnect v{ver} release into {DIST}")
    src = source_zip(ver, worktree=a.pendrive)
    verify(src)
    files = [src]
    print(f" + {src.name}  ({src.stat().st_size / 1e6:.1f} MB)")
    if not a.no_offline:
        print(" building offline zip (big, a few minutes) ...")
        off = offline_zip(ver, src)
        if off:
            verify(off)
            size = off.stat().st_size
            print(f" + {off.name}  ({size / 1e6:.0f} MB)")
            if size > 2_000_000_000:
                print(" !! over GitHub's 2 GB per-file limit - share it by USB/network instead of the release")
            else:
                files.append(off)
    if a.publish:
        publish(ver, files)
    else:
        print("\nDone. To publish on GitHub: make_release.bat --publish  (or upload dist\\*.zip by hand)")


if __name__ == "__main__":
    main()
