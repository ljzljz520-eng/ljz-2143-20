"""SQLite 存储层（标准库 sqlite3，线程内加锁）。

记录三类事实：
  builds    构建指纹（build_id / 包 sha / 文件级清单）
  devices   设备登记与最近一次实测能力（probe_report 全量留档 JSON）
  channels  发行通道：指向某 build，含批次比例、暂停位、能力门禁
另保留 device_events 审计轨迹（每次上报的阶段变化）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    build_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    package       TEXT NOT NULL,
    package_size  INTEGER NOT NULL,
    package_sha256 TEXT NOT NULL,
    epoch         INTEGER NOT NULL,
    files_json    TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    client_version TEXT,
    probe_json   TEXT,
    phase        TEXT DEFAULT 'NEW',
    detail_json  TEXT,
    receipt_json TEXT,
    updated_at   REAL NOT NULL,
    registered_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    name        TEXT PRIMARY KEY,
    build_id    TEXT,
    batch       TEXT DEFAULT 'batch-0',
    rollout_pct INTEGER DEFAULT 100,
    paused      INTEGER DEFAULT 0,
    require     TEXT DEFAULT '{}',
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    at        REAL NOT NULL,
    phase     TEXT,
    message   TEXT
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- builds ----------
    def upsert_build(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO builds(build_id,name,version,package,
                   package_size,package_sha256,epoch,files_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(build_id) DO UPDATE SET
                     name=excluded.name, version=excluded.version,
                     package=excluded.package, package_size=excluded.package_size,
                     package_sha256=excluded.package_sha256,
                     files_json=excluded.files_json, epoch=excluded.epoch""",
                (rec["build_id"], rec["name"], rec["version"], rec["package"],
                 rec["package_size"], rec["package_sha256"],
                 rec.get("source_date_epoch", 0),
                 json.dumps(rec["files"], ensure_ascii=False), time.time()))
            self.db.commit()

    def get_build(self, build_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM builds WHERE build_id=?", (build_id,)).fetchone()
        return self._build_row(r) if r else None

    def latest_build(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM builds WHERE name=? ORDER BY created_at DESC, "
                "rowid DESC LIMIT 1", (name,)).fetchone()
        return self._build_row(r) if r else None

    def list_builds(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM builds ORDER BY created_at DESC").fetchall()
        return [self._build_row(r) for r in rows]

    @staticmethod
    def _build_row(r: sqlite3.Row) -> Dict[str, Any]:
        return {"build_id": r["build_id"], "name": r["name"],
                "version": r["version"], "package": r["package"],
                "package_size": r["package_size"],
                "package_sha256": r["package_sha256"],
                "source_date_epoch": r["epoch"],
                "files": json.loads(r["files_json"])}

    # ---------- devices ----------
    def register_device(self, device_id: str, name: str,
                        client_version: str) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self.db.execute(
                """INSERT INTO devices(id,name,client_version,updated_at,
                   registered_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     client_version=excluded.client_version, updated_at=excluded.updated_at""",
                (device_id, name, client_version, now, now))
            self.db.commit()
        return {"device_id": device_id}

    def save_probe(self, device_id: str, probe: Dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE devices SET probe_json=?, phase='PROBED', "
                "detail_json=?, updated_at=? WHERE id=?",
                (json.dumps(probe, ensure_ascii=False),
                 json.dumps({"probe_verdict": probe.get("verdict")},
                            ensure_ascii=False), time.time(), device_id))
            self.db.commit()

    def set_phase(self, device_id: str, phase: str,
                  detail: Optional[Dict[str, Any]] = None,
                  receipt: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE devices SET phase=?, detail_json=COALESCE(?,detail_json), "
                "receipt_json=COALESCE(?,receipt_json), updated_at=? WHERE id=?",
                (phase,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 json.dumps(receipt, ensure_ascii=False) if receipt else None,
                 time.time(), device_id))
            self.db.execute(
                "INSERT INTO device_events(device_id,at,phase,message) "
                "VALUES(?,?,?,?)",
                (device_id, time.time(), phase,
                 (detail or {}).get("message")))
            self.db.commit()

    def list_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM devices ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "name": r["name"],
                "client_version": r["client_version"], "phase": r["phase"],
                "probe": json.loads(r["probe_json"]) if r["probe_json"] else None,
                "detail": json.loads(r["detail_json"])
                if r["detail_json"] else None,
                "receipt": json.loads(r["receipt_json"])
                if r["receipt_json"] else None,
                "updated_at": r["updated_at"],
                "registered_at": r["registered_at"],
            })
        return out

    # ---------- channels ----------
    def upsert_channel(self, name: str, build_id: Optional[str],
                       batch: str, rollout_pct: int, paused: bool,
                       require: Dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO channels(name,build_id,batch,rollout_pct,paused,
                   require,updated_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET build_id=excluded.build_id,
                     batch=excluded.batch, rollout_pct=excluded.rollout_pct,
                     paused=excluded.paused, require=excluded.require,
                     updated_at=excluded.updated_at""",
                (name, build_id, batch, rollout_pct, 1 if paused else 0,
                 json.dumps(require, ensure_ascii=False), time.time()))
            self.db.commit()

    def get_channel(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self.db.execute("SELECT * FROM channels WHERE name=?",
                                (name,)).fetchone()
        if not r:
            return None
        return {"name": r["name"], "build_id": r["build_id"],
                "batch": r["batch"], "rollout_pct": r["rollout_pct"],
                "paused": bool(r["paused"]),
                "require": json.loads(r["require"])}

    def list_channels(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM channels ORDER BY name").fetchall()
        return [self.get_channel(r["name"]) for r in rows]  # type: ignore[list-item]

    def events(self, device_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM device_events WHERE device_id=? "
                "ORDER BY id DESC LIMIT ?", (device_id, limit)).fetchall()
        return [{"at": r["at"], "phase": r["phase"], "message": r["message"]}
                for r in rows]
