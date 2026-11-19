"""Plan-B pieces: manual relay fence repair, task ids, dependency release, LAN scan targets."""
import json
import os
import subprocess
import sys
from pathlib import Path

from multiconnect.common.protocol import parse_reply
from multiconnect.mockchat.server import fence_bare_commands, md_to_html

REPO = Path(__file__).resolve().parents[1]


def test_fence_bare_commands_rewraps_pasted_answer():
    pasted = ("Sure, here is the file.\n@@WRITE scripts/a.gd\nextends Node\n\nfunc _ready():\n\tpass\n"
              "@@CHECK\n@@DONE\nThanks!")
    fixed = fence_bare_commands(pasted)
    assert fixed.count("```") == 6
    assert "```\n@@WRITE scripts/a.gd\nextends Node\n\nfunc _ready():\n\tpass\n```" in fixed
    assert fixed.startswith("Sure, here is the file.")


def test_fence_bare_commands_leaves_fenced_and_plain_text():
    fenced = "```\n@@WRITE a.gd\nx\n```"
    assert fence_bare_commands(fenced) == fenced
    assert fence_bare_commands("just talking") == "just talking"


def test_md_to_html_accepts_language_tags_with_symbols():
    html = md_to_html("```c#\nint x;\n```\n``` gdscript \nvar y\n```")
    assert html.count("<pre") == 2


def test_parse_reply_after_fence_repair():
    fixed = fence_bare_commands("@@WRITE scripts/b.gd\nextends Node\n@@DONE")
    blocks = [{"type": "code", "text": b} for b in fixed.split("```")[1::2]]
    acts, _ = parse_reply(blocks)
    assert [a.kind for a in acts] == ["write", "done"]
    assert acts[0].arg == "scripts/b.gd"


HUB_SCRIPT = r"""
import json
from multiconnect.coordinator.hub import Hub
from multiconnect.common.config import load_commander_config
hub = Hub(load_commander_config(None))
r1 = hub.dispatch([{"id": "T001", "title": "a", "goal": "g", "files_to_create": ["a.gd"]},
                   {"id": "T002", "title": "b", "goal": "g", "files_to_create": ["b.gd"]}])
r2 = hub.dispatch([{"title": "c", "goal": "g", "files_to_create": ["c.gd"], "depends_on": ["T001"]}])
new_id = r2["created"][0]["id"]
hub.db.update("tasks", "id", new_id, status="waiting")
before = hub.release_waiting()
hub.db.update("tasks", "id", "T001", status="integrated")
after = hub.release_waiting()
status = hub.db.one("SELECT status FROM tasks WHERE id=?", (new_id,))["status"]
print(json.dumps({"r1": r1, "new_id": new_id, "before": before, "after": after, "status": status}))
"""


def test_task_ids_and_release_waiting(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    import shutil
    shutil.copytree(REPO / "config" / "providers", root / "config" / "providers")
    shutil.copytree(REPO / "templates", root / "templates")
    shutil.copytree(REPO / "prompts", root / "prompts")
    import yaml
    (root / "config" / "commander.yaml").write_text(yaml.safe_dump(
        {"team_token": "t", "game_dir": str(root / "game"), "backup_dir": str(root / "backups")}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MC_")}
    env.update(MC_ROOT=str(root), PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, "-c", HUB_SCRIPT], cwd=str(REPO), env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert not d["r1"]["errors"]
    assert d["new_id"] == "T003"  # auto id skips T001/T002 chosen by hand
    assert d["before"] == [] and d["after"] == ["T003"] and d["status"] == "queued"


def test_scan_targets_are_private_and_bounded():
    from multiconnect.common.net import local_ipv4s, scan_targets
    t = scan_targets(64)
    assert len(t) <= 64
    mine = {ip for ip, _ in local_ipv4s()}
    assert not (set(t) & mine)
