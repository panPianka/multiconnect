"""Hive backup on the old commander -> restore-hive on a 'new' PC (both in throw-away MC_ROOT folders)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

MAKE_BACKUP = r"""
import json, sys
from multiconnect.common.config import load_commander_config
from multiconnect.coordinator.hub import Hub, SECRETS_DIR
cfg = load_commander_config(None)
hub = Hub(cfg)
hub.db.kv_set("marker", "old-commander")
(SECRETS_DIR / "acc1.json").write_text('{"cookies": [1]}', encoding="utf-8")
info = hub.game.hive_backup()
print(json.dumps(info))
"""

CHECK = r"""
from multiconnect.common.config import load_commander_config
from multiconnect.coordinator.db import DB
from multiconnect.common.config import STATE_DIR
db = DB(STATE_DIR / "multiconnect.db")
print(db.kv_get("marker"))
"""


def _root(tmp: Path, name: str) -> Path:
    r = tmp / name
    (r / "config").mkdir(parents=True)
    shutil.copytree(REPO / "config" / "providers", r / "config" / "providers")
    shutil.copytree(REPO / "templates", r / "templates")
    shutil.copytree(REPO / "prompts", r / "prompts")
    return r


def _run(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MC_")}
    env["MC_ROOT"] = str(root)
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run([sys.executable, *args], cwd=str(REPO), env=env, capture_output=True, text=True,
                          timeout=120)


def test_hive_backup_and_restore(tmp_path):
    old = _root(tmp_path, "old")
    cfg = {"team_token": "t", "port": 59991, "game_dir": str(old / "game"), "backup_dir": str(old / "backups"),
           "accounts_file": "config/accounts.yaml"}
    (old / "config" / "commander.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (old / "config" / "accounts.yaml").write_text(yaml.safe_dump({"accounts": [{"id": "acc1", "provider": "mock"}]}),
                                                  encoding="utf-8")
    r = _run(old, ["-c", MAKE_BACKUP])
    assert r.returncode == 0, r.stderr
    zips = list((old / "backups").glob("hive-*.zip"))
    assert len(zips) == 1

    new = _root(tmp_path, "new")
    (new / "mirror_backups").mkdir()
    shutil.copy(zips[0], new / "mirror_backups" / zips[0].name)
    r = _run(new, ["-m", "multiconnect.cli", "restore-hive"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (new / "state" / "secrets" / "acc1.json").read_text(encoding="utf-8") == '{"cookies": [1]}'
    assert (new / "config" / "commander.yaml").exists() and (new / "config" / "accounts.yaml").exists()
    r = _run(new, ["-c", CHECK])
    assert r.stdout.strip() == "old-commander", r.stderr
