import io
import json
import zipfile

from multiconnect.common.protocol import Action
from multiconnect.worker.sandbox import Sandbox


def make_bundle(tmp_path):
    z = tmp_path / "bundle.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("project.godot", "config_version=5\n")
        f.writestr("scripts/existing.gd", "extends Node\nvar x = 1\n")
    return z


def test_permissions_and_actions(tmp_path):
    sb = Sandbox(tmp_path / "chat1" / "sandbox", ["scripts/player.gd", "scripts/existing.gd"])
    sb.prepare(make_bundle(tmp_path), None, "contract", "task")
    assert (sb.root / "_context" / "contract.md").read_text() == "contract"

    assert sb.can_write("project.godot")[0] is False
    assert sb.can_write("_context/task.md")[0] is False
    assert sb.can_write("REPORT.md")[0] is True
    assert sb.can_write("scripts/player.gd") == (True, "")
    ok, note = sb.can_write("scripts/extra.gd")
    assert ok and "not listed" in note

    r = sb.write(Action("write", "scripts/player.gd", "extends Node\nfunc a():\n    pass"))
    assert r.startswith("WROTE")
    assert (sb.root / "scripts/player.gd").read_text() == "extends Node\nfunc a():\n\tpass\n"

    r = sb.replace(Action("replace", "scripts/existing.gd", "", [("var x = 1", "var x = 2")]))
    assert "1 block" in r
    assert "var x = 2" in (sb.root / "scripts/existing.gd").read_text()

    assert "DENIED" in sb.write(Action("write", "project.godot", "x"))
    assert "CONTENT OF" in sb.read("scripts/player.gd")
    assert "did you mean" in sb.read("player.gd")

    sb.write(Action("write", "REPORT.md", "# report"))
    data = sb.build_result({"task": "T1"}, "transcript")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        man = json.loads(z.read("_meta/manifest.json"))
        assert set(man["files"]) == {"scripts/player.gd", "scripts/existing.gd", "REPORT.md"}

    # restore into a fresh sandbox (follow-up on another computer)
    sb2 = Sandbox(tmp_path / "chat2" / "sandbox", ["scripts/player.gd"])
    sb2.prepare(make_bundle(tmp_path), data, "contract", "task")
    assert (sb2.root / "scripts/player.gd").exists()
    assert "scripts/player.gd" in sb2.written_files()
