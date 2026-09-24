"""芯片批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 photocurrent REAL, photocurrent_unit TEXT,
 optical_power REAL, optical_power_unit TEXT,
 conversion_version TEXT,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS analysis_runs(
 run_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, conversion_version TEXT NOT NULL,
 input_fingerprint TEXT NOT NULL UNIQUE, result_json TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL);
"""

# 旧版 measurements 表没有单位列；逐列补齐，使既有数据库文件可继续打开。
_MIGRATION_COLUMNS = (
    ("photocurrent", "REAL"),
    ("photocurrent_unit", "TEXT"),
    ("optical_power", "REAL"),
    ("optical_power_unit", "TEXT"),
    ("conversion_version", "TEXT"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用同一个服务实例，
    # 因此允许连接跨线程使用；写操作均由 BEGIN IMMEDIATE 事务串行化。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    existing = {row[1] for row in db.execute("PRAGMA table_info(measurements)")}
    for name, affinity in _MIGRATION_COLUMNS:
        if name not in existing:
            # 历史测量行的单位列保持 NULL：它们是“未声明单位”的旧记录，
            # 重新分析时必须显式标识，禁止静默按毫瓦解读。
            db.execute(f"ALTER TABLE measurements ADD COLUMN {name} {affinity}")
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))
