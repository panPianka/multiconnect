"""Configuration loading. All config files are YAML with sane defaults so the tool
runs even when a file is missing. Paths are resolved relative to the tool root."""
from __future__ import annotations

import copy
import os
import socket
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("MC_ROOT", Path(__file__).resolve().parents[2]))
CONFIG_DIR = ROOT / "config"
# MC_STATE_DIR / MC_AGENTS_DIR let `multiconnect selftest` run a throw-away hive next to the real one
STATE_DIR = Path(os.environ.get("MC_STATE_DIR") or ROOT / "state")
AGENTS_DIR = Path(os.environ.get("MC_AGENTS_DIR") or ROOT / "agents")
PROMPTS_DIR = ROOT / "prompts"
TEMPLATES_DIR = ROOT / "templates"
PROVIDERS_DIR = CONFIG_DIR / "providers"

DEFAULT_COMMANDER: dict[str, Any] = {
    "team_token": "change-me",
    "host": "0.0.0.0",
    "port": 8765,
    "discovery_port": 8766,
    "game_dir": "F:/hackathon-game",
    "backup_dir": "F:/hackathon-backups",
    "backup_every_min": 10,
    "backup_keep": 30,
    "mirror_backups_to": [],          # worker host names that receive backup copies
    "hive_backup_every_min": 10,      # hive-*.zip (DB + secrets + config) for restore-hive; 0 = off
    "mirror_hive": True,
    "godot_path": "",                 # empty = auto-detect
    "default_agents_per_host": 6,
    "max_rounds": 10,
    "question_timeout_s": 600,
    "heartbeat_timeout_s": 20,
    "task_timeout_min": 25,
    "context_rollover_ratio": 0.7,
    "disabled_providers": [],
    "accounts_file": "config/accounts.yaml",
}

DEFAULT_WORKER: dict[str, Any] = {
    "name": "",                        # empty = computer name
    "coordinator_url": "",             # empty = auto-discover on LAN (UDP beacon, then subnet scan)
    "coordinator_port": 8765,          # port the subnet scan looks for
    "team_token": "change-me",
    "discovery_port": 8766,
    "godot_path": "",
    "max_parallel_godot": 0,           # 0 = cores // 2
    "browser_mode": "mini",            # mini | hidden | headless
    "browser_channel": "chromium",     # chromium | chrome | msedge
    "window_size": [480, 640],
    "idle_close_page_s": 180,
    "poll_interval_s": 1.0,
    "reply_timeout_s": 420,
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def load_commander_config(path: str | None = None) -> dict:
    p = Path(path) if path else CONFIG_DIR / "commander.yaml"
    cfg = _deep_merge(DEFAULT_COMMANDER, load_yaml(p) or {})
    if os.environ.get("MC_TOKEN"):
        cfg["team_token"] = os.environ["MC_TOKEN"]
    return cfg


def load_worker_config(path: str | None = None) -> dict:
    p = Path(path) if path else CONFIG_DIR / "worker.yaml"
    cfg = _deep_merge(DEFAULT_WORKER, load_yaml(p) or {})
    if not cfg["name"]:
        cfg["name"] = os.environ.get("COMPUTERNAME") or socket.gethostname()
    if os.environ.get("MC_TOKEN"):
        cfg["team_token"] = os.environ["MC_TOKEN"]
    if os.environ.get("MC_COORDINATOR"):
        cfg["coordinator_url"] = os.environ["MC_COORDINATOR"]
    return cfg


def load_accounts(path: Path) -> list[dict]:
    data = load_yaml(path) or {}
    accs = data.get("accounts", []) if isinstance(data, dict) else []
    out = []
    for a in accs:
        if not a or "id" not in a or "provider" not in a:
            continue
        out.append({
            "id": str(a["id"]),
            "provider": str(a["provider"]),
            "owner": str(a.get("owner", "")),
            "label": str(a.get("label", "")),
            "pin_host": a.get("pin_host") or None,
            "enabled": bool(a.get("enabled", True)),
        })
    return out


PROVIDER_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "context_tokens": 64000,
    "new_chat_url": "",
    "login_url": "",
    "home_url": "",
    "selectors": {
        "input": "textarea",
        "send_button": "",
        "stop_button": "",
        "assistant_message": "",
        "code_block": "pre",
        "logged_in": "",
        "login_required": "",
    },
    "send_with": "enter",         # enter | button | ctrl_enter
    "input_mode": "auto",         # auto | paste | fill | type
    "url_login_patterns": ["login", "signin", "sign-in", "auth"],
    "rate_limit_patterns": [
        "rate limit", "too many requests", "usage limit", "you've reached",
        "you have reached", "limit reached", "try again later", "server is busy",
        "come back later",
    ],
    "captcha_patterns": ["captcha", "challenges.cloudflare.com", "turnstile", "verify you are human", "just a moment"],
    "error_patterns": ["something went wrong", "network error", "an error occurred"],
    "block_url_patterns": ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico",
                           "*.woff", "*.woff2", "*.ttf", "*.otf", "*.mp4", "*.webm", "*.mp3",
                           "*google-analytics*", "*googletagmanager*", "*doubleclick*",
                           "*sentry.io*", "*segment.io*", "*hotjar*", "*intercom*"],
    "settle_ms": 2500,
    "cooldown_min": 30,
    "notes": "",
}


def load_providers(overrides: dict[str, dict] | None = None) -> dict[str, dict]:
    provs: dict[str, dict] = {}
    if PROVIDERS_DIR.exists():
        for f in sorted(PROVIDERS_DIR.glob("*.yaml")):
            data = load_yaml(f) or {}
            pid = data.get("id") or f.stem
            provs[pid] = _deep_merge(PROVIDER_DEFAULTS, data)
            provs[pid]["id"] = pid
    for pid, ov in (overrides or {}).items():
        if pid in provs:
            provs[pid] = _deep_merge(provs[pid], ov)
    return provs


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (ROOT / path)
