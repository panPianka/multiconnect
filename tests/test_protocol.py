from multiconnect.common.protocol import clean_path, normalize_code, parse_reply
from multiconnect.common.tokens import estimate_tokens


def code(t):
    return {"type": "code", "text": t}


def text(t):
    return {"type": "text", "text": t}


def test_clean_path():
    assert clean_path("scripts/player.gd") == "scripts/player.gd"
    assert clean_path("./scripts/player.gd") == "scripts/player.gd"
    assert clean_path("res://scenes/main.tscn") == "scenes/main.tscn"
    assert clean_path("`scripts\\a.gd`") == "scripts/a.gd"
    assert clean_path(".gitignore") == ".gitignore"
    assert clean_path("../evil.gd") is None
    assert clean_path("a/../../evil.gd") is None
    assert clean_path("/etc/passwd") is None
    assert clean_path("C:/Windows/x") is None
    assert clean_path("") is None


def test_normalize_gdscript_spaces_to_tabs():
    src = "extends Node\r\nfunc a():\r\n    if x:\r\n        pass\u200b\n"
    out = normalize_code("a.gd", src)
    assert out == "extends Node\nfunc a():\n\tif x:\n\t\tpass\n"
    assert normalize_code("a.txt", "x\u00a0y") == "x y\n"


def test_write_block():
    acts, warns = parse_reply([text("Here:"), code("@@WRITE scripts/p.gd\nextends Node\n"), code("@@CHECK")])
    assert [a.kind for a in acts] == ["write", "check"]
    assert acts[0].arg == "scripts/p.gd" and acts[0].body == "extends Node\n"
    assert not warns


def test_write_in_prose_takes_next_code_block():
    acts, warns = parse_reply([text("@@WRITE scripts/p.gd"), code("extends Node")])
    assert acts[0].kind == "write" and acts[0].body == "extends Node"


def test_markdown_decorated_commands():
    acts, _ = parse_reply([text("**@@DONE**"), text("`@@CHECK`")])
    assert {a.kind for a in acts} == {"done", "check"}


def test_replace_blocks():
    body = "@@REPLACE a.gd\n<<<<<<< SEARCH\nvar x = 1\n=======\nvar x = 2\n>>>>>>> REPLACE\n"
    acts, warns = parse_reply([code(body)])
    assert acts[0].kind == "replace" and acts[0].replacements == [("var x = 1", "var x = 2")]
    assert not warns


def test_ask_multiline_and_dedup():
    acts, _ = parse_reply([code("@@ASK Which signal?\nShould it be typed?\n@@CHECK\n@@CHECK")])
    assert acts[0].kind == "ask" and "typed" in acts[0].arg
    assert [a.kind for a in acts].count("check") == 1


def test_unsafe_write_rejected():
    acts, warns = parse_reply([code("@@WRITE ../../x.gd\nboom")])
    assert acts[0].kind == "invalid" and warns


def test_write_not_first_line_warns():
    acts, warns = parse_reply([code("@@CHECK\n@@WRITE a.gd\n")])
    assert any("FIRST line" in w for w in warns)


def test_tokens():
    assert estimate_tokens("") == 0
    assert 20 <= estimate_tokens("hello world " * 10) <= 40
