"""巷口 结构化状态的本地 SQLite 存储。"""

from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class StateDatabase:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 5:
                raise RuntimeError(f"状态数据库版本 {version} 高于当前程序支持的版本 5")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, owner_user_id INTEGER, plan_json TEXT,
                    shop_ids_json TEXT NOT NULL, shop_prices_json TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_user_id, updated_at);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, owner_user_id INTEGER,
                    record_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_user_id, created_at);
                CREATE TABLE IF NOT EXISTS user_memory (
                    owner_user_id INTEGER PRIMARY KEY, preferences_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voucher_actions (
                    id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL,
                    record_json TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_voucher_actions_owner
                    ON voucher_actions(owner_user_id, updated_at);
                CREATE TABLE IF NOT EXISTS voucher_action_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)
            # v2 is additive and upgrades existing v0/v1 databases without replacing state.
            conn.execute("CREATE TABLE IF NOT EXISTS task_checkpoints ("
                         "run_id TEXT PRIMARY KEY, owner_user_id INTEGER, session_id TEXT NOT NULL, "
                         "plan_json TEXT NOT NULL, revision INTEGER NOT NULL, phase TEXT NOT NULL, "
                         "updated_at REAL NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS business_tasks (owner_user_id INTEGER NOT NULL, "
                         "session_id TEXT NOT NULL, record_json TEXT NOT NULL, updated_at REAL NOT NULL, "
                         "PRIMARY KEY(owner_user_id, session_id))")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
                    user_text TEXT NOT NULL, assistant_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, run_id TEXT, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id, created_at);
                CREATE TRIGGER IF NOT EXISTS delete_session_turns AFTER DELETE ON sessions
                BEGIN DELETE FROM conversation_turns WHERE session_id=OLD.id; END;
            """)
            # v5：会话摘要（长对话压缩后长期保留）与恢复事件（用于统计恢复成功率）。
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL,
                    summary TEXT NOT NULL, covered_turns INTEGER NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resume_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL, ok INTEGER NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_resume_events_run ON resume_events(run_id);
                CREATE TRIGGER IF NOT EXISTS delete_session_summary AFTER DELETE ON sessions
                BEGIN DELETE FROM session_summaries WHERE session_id=OLD.id; END;
            """)
            conn.execute("PRAGMA user_version=5")
        os.chmod(self.path, 0o600)

    def check(self) -> bool:
        with self.connect() as conn:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def backup(self, destination: str) -> str:
        target = Path(destination).expanduser().resolve()
        if str(target) == self.path:
            raise ValueError("备份目标不能与源数据库相同")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path) as source, sqlite3.connect(str(target)) as backup:
            source.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性校验失败")
        os.chmod(target, 0o600)
        return str(target)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()
