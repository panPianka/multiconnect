"""SQLite storage (a single file, part of Python - no server, no install)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts(
  name TEXT PRIMARY KEY, ip TEXT, online INTEGER DEFAULT 0, last_seen REAL DEFAULT 0,
  desired_agents INTEGER DEFAULT -1, mode TEXT DEFAULT '', sys_json TEXT DEFAULT '{}',
  version TEXT DEFAULT '', godot TEXT DEFAULT '', connected_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, provider TEXT, owner TEXT, label TEXT, pin_host TEXT, enabled INTEGER DEFAULT 1,
  has_state INTEGER DEFAULT 0, state_updated REAL DEFAULT 0, host TEXT, status TEXT DEFAULT 'offline',
  status_detail TEXT DEFAULT '', status_since REAL DEFAULT 0, last_work_at REAL DEFAULT 0,
  cooldown_until REAL DEFAULT 0, tasks_done INTEGER DEFAULT 0, tasks_failed INTEGER DEFAULT 0,
  rounds_total INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, current_chat TEXT, in_config INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, title TEXT, spec_json TEXT, status TEXT, priority INTEGER DEFAULT 3,
  depends_json TEXT DEFAULT '[]', created REAL, updated REAL, result_chat TEXT, result_version INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', integrated_at REAL DEFAULT 0, redundancy INTEGER DEFAULT 1,
  pinned_account TEXT);
CREATE TABLE IF NOT EXISTS chats(
  id TEXT PRIMARY KEY, task_id TEXT, account_id TEXT, host TEXT, url TEXT DEFAULT '', status TEXT,
  rounds INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, ctx_tokens INTEGER DEFAULT 0,
  started REAL, updated REAL, finished REAL DEFAULT 0, result_version INTEGER DEFAULT 0, error TEXT DEFAULT '',
  pending_json TEXT DEFAULT '[]', parent_chat TEXT);
CREATE TABLE IF NOT EXISTS results(
  chat_id TEXT, version INTEGER, task_id TEXT, path TEXT, manifest_json TEXT, created REAL,
  PRIMARY KEY(chat_id, version));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, kind TEXT, code TEXT, host TEXT, account TEXT,
  chat TEXT, task TEXT, message TEXT, data_json TEXT DEFAULT '{}', for_claude INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ev_claude ON events(for_claude, id);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, task_id TEXT, account_id TEXT, question TEXT,
  answer TEXT, created REAL, answered REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS assets(
  id TEXT PRIMARY KEY, name TEXT, path TEXT, kind TEXT, spec TEXT, priority INTEGER DEFAULT 2, for_task TEXT,
  status TEXT, claimed_by TEXT DEFAULT '', created REAL, delivered REAL DEFAULT 0, notes TEXT DEFAULT '',
  width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, frames INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS file_owners(path TEXT PRIMARY KEY, task_id TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS token_log(ts REAL, account TEXT, provider TEXT, host TEXT, tin INTEGER, tout INTEGER);
CREATE INDEX IF NOT EXISTS tok_ts ON token_log(ts);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    def q(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def x(self, sql: str, args: tuple | list = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def update(self, table: str, key: str, keyval: Any, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE {table} SET {cols} WHERE {key}=?", [*fields.values(), keyval])

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def event(self, level: str, kind: str, message: str, *, code: str = "", host: str = "", account: str = "",
              chat: str = "", task: str = "", data: dict | None = None, for_claude: bool = False) -> int:
        return self.x("INSERT INTO events(ts,level,kind,code,host,account,chat,task,message,data_json,for_claude) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), level, kind, code, host or "", account or "", chat or "", task or "",
                       message, json.dumps(data or {}), 1 if for_claude else 0))
