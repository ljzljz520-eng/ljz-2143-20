#!/usr/bin/env python3
"""Window delivery control-plane server.

Only Python standard library is required. It stores release fingerprints and
per-device probes in SQLite, serves resumable artifacts, and projects release
configuration into client protocol v1/v2 compatibility windows.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tarfile
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BASE_V1_FIELDS = {
    "action", "appId", "releaseId", "version", "channel", "dependencyPolicy",
    "downloadUrl", "sha256", "artifactSha256", "size", "manifest", "reasonCode", "reason",
}


def now() -> int:
    return int(time.time())


def version_key(version: str) -> Tuple[int, ...]:
    parts = re.findall(r"\d+", str(version))
    return tuple(int(x) for x in parts[:4]) if parts else (0,)


def db_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices(
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          arch TEXT NOT NULL,
          registered_at INTEGER NOT NULL,
          last_seen INTEGER,
          capabilities TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS reports(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          phase TEXT NOT NULL,
          status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '{}',
          client_version INTEGER NOT NULL DEFAULT 2,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reports_device_time ON reports(device_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          app_id TEXT NOT NULL,
          version TEXT NOT NULL,
          channel TEXT NOT NULL,
          dependency_policy TEXT NOT NULL,
          artifact TEXT NOT NULL UNIQUE,
          size INTEGER NOT NULL,
          artifact_sha256 TEXT NOT NULL DEFAULT '',
          sha256 TEXT NOT NULL,
          manifest TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(app_id, version, channel, dependency_policy)
        );
        CREATE TABLE IF NOT EXISTS channels(
          app_id TEXT NOT NULL,
          name TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          rollout_percent INTEGER NOT NULL DEFAULT 100,
          min_client_version INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(app_id, name)
        );
        CREATE TABLE IF NOT EXISTS batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          app_id TEXT NOT NULL,
          channel TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_devices(
          batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
          device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          PRIMARY KEY(batch_id, device_id)
        );
        CREATE TABLE IF NOT EXISTS assignments_obs(
          device_id TEXT PRIMARY KEY,
          release_id INTEGER,
          policy TEXT,
          reason TEXT,
          updated_at INTEGER NOT NULL
        );
        """
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(releases)")}
    if "artifact_sha256" not in cols:
        con.execute("ALTER TABLE releases ADD COLUMN artifact_sha256 TEXT NOT NULL DEFAULT ''")
    con.commit()


def json_dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def read_manifest_from_tar(path: Path) -> Dict[str, Any]:
    with tarfile.open(path, "r:*") as tf:
        member = tf.extractfile("manifest.json")
        if member is None:
            raise ValueError("manifest.json missing")
        return json.loads(member.read().decode("utf-8"))


class Store:
    def __init__(self, db: Path, artifact_dir: Path) -> None:
        self.con = db_connect(db)
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        init_db(self.con)
        self.lock = threading.RLock()

    def all(self, sql: str, args: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return list(self.con.execute(sql, args).fetchall())

    def one(self, sql: str, args: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args: Tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur

    def register_device(self, data: Dict[str, Any]) -> Dict[str, Any]:
        device_id = str(data.get("id") or data.get("deviceId") or "").strip()
        if not device_id:
            raise ValueError("device id is required")
        name = str(data.get("name") or device_id)
        arch = str(data.get("arch") or data.get("architecture") or "unknown")
        caps = data.get("capabilities") or {}
        ts = now()
        with self.lock:
            exists = self.con.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
            if exists:
                self.con.execute(
                    "UPDATE devices SET name=?, arch=?, capabilities=? WHERE id=?",
                    (name, arch, json_dumps(caps).decode(), device_id),
                )
            else:
                self.con.execute(
                    "INSERT INTO devices(id,name,arch,registered_at,capabilities) VALUES(?,?,?,?,?)",
                    (device_id, name, arch, ts, json_dumps(caps).decode()),
                )
            self.con.commit()
        return {"id": device_id, "name": name, "arch": arch, "capabilities": caps}

    def publish_release(self, src: Path) -> Dict[str, Any]:
        if not src.is_file():
            raise FileNotFoundError(str(src))
        manifest = read_manifest_from_tar(src)
        payload = manifest.get("payload", {})
        release_id = None
        dst = self.artifact_dir / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        h = hashlib.sha256()
        with dst.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        artifact_sha = h.hexdigest()
        app_id = manifest.get("appId", "visual-window")
        version = manifest["version"]
        channel = manifest["channel"]
        policy = manifest["dependencyPolicy"]
        fingerprint = {
            "manifestDigest": manifest.get("digest"),
            "sourceCommit": manifest.get("build", {}).get("sourceCommit"),
            "sourceDateEpoch": manifest.get("build", {}).get("sourceDateEpoch"),
            "compiler": manifest.get("build", {}).get("compiler"),
            "worktreeDigest": manifest.get("build", {}).get("worktreeDigest"),
            "absolutePathLeak": manifest.get("build", {}).get("absolutePathLeak", []),
            "payloadFileCount": len(payload.get("files", [])),
            "payloadSize": payload.get("payloadSize"),
        }
        with self.lock:
            self.con.execute(
                """
                INSERT INTO releases(app_id,version,channel,dependency_policy,artifact,size,
                                     artifact_sha256,sha256,manifest,fingerprint,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(app_id,version,channel,dependency_policy) DO UPDATE SET
                  artifact=excluded.artifact,size=excluded.size,
                  artifact_sha256=excluded.artifact_sha256,
                  sha256=excluded.sha256,
                  manifest=excluded.manifest,fingerprint=excluded.fingerprint,created_at=excluded.created_at
                """,
                (app_id, version, channel, policy, dst.name, src.stat().st_size,
                 artifact_sha, manifest.get("digest", ""), json_dumps(manifest).decode(),
                 json_dumps(fingerprint).decode(), now()),
            )
            row = self.con.execute(
                "SELECT id FROM releases WHERE artifact=?", (dst.name,)
            ).fetchone()
            release_id = int(row[0])
            self.con.commit()
        return {"id": release_id, "artifact": dst.name, "version": version,
                "channel": channel, "policy": policy, "fingerprint": fingerprint}

    def latest_report(self, device_id: str) -> Optional[Dict[str, Any]]:
        row = self.one(
            "SELECT detail,phase,status,created_at FROM reports WHERE device_id=? "
            "ORDER BY created_at DESC,id DESC LIMIT 1", (device_id,))
        if not row:
            return None
        d = json.loads(row["detail"])
        d.update({"_phase": row["phase"], "_status": row["status"], "_createdAt": row["created_at"]})
        return d

    def add_report(self, data: Dict[str, Any]) -> Dict[str, Any]:
        device_id = str(data.get("deviceId") or "")
        device = self.one("SELECT id FROM devices WHERE id=?", (device_id,))
        if not device:
            # Autoregistration keeps field tests possible, while console remains
            # the intended place to register declared capabilities.
            self.register_device({"id": device_id, "name": data.get("deviceName", device_id),
                                  "arch": (data.get("detail") or {}).get("os", {}).get("arch", "unknown")})
        phase = str(data.get("phase") or "unknown")
        status = str(data.get("status") or "info")
        detail = data.get("detail") or {}
        client_version = int(data.get("clientVersion") or 2)
        ts = now()
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO reports(device_id,phase,status,detail,client_version,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (device_id, phase, status, json_dumps(detail).decode(), client_version, ts),
            )
            self.con.execute("UPDATE devices SET last_seen=?, arch=COALESCE(NULLIF(?, ''),arch) WHERE id=?",
                             (ts, str(detail.get("os", {}).get("arch") or ""), device_id))
            self.con.commit()
            rid = cur.lastrowid
        return {"id": rid, "accepted": True, "serverTime": ts,
                # Unknown fields from newer clients must not break ingestion.
                "ignoredUnknownFields": []}

    def assignment(self, device_id: str, protocol: int, app_id: str) -> Tuple[int, Dict[str, Any]]:
        device = self.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if device is None:
            return 404, {"error": "device not registered", "deviceId": device_id}
        report = self.latest_report(device_id) or {}
        batch = self.one(
            """
            SELECT b.* FROM batches b JOIN batch_devices bd ON bd.batch_id=b.id
            WHERE bd.device_id=? AND b.app_id=? AND b.active=1
            ORDER BY b.created_at DESC,b.id DESC LIMIT 1
            """,
            (device_id, app_id),
        )
        base = {"action": "idle", "appId": app_id}
        if batch is None:
            return 200, self.project(base, protocol)
        channel = self.one("SELECT * FROM channels WHERE app_id=? AND name=?",
                           (app_id, batch["channel"]))
        if channel is None or not channel["active"]:
            return 200, self.project({"action": "blocked", "appId": app_id,
                                      "reasonCode": "channel_inactive",
                                      "reason": "release channel is inactive"}, protocol)
        if protocol < int(channel["min_client_version"]):
            return 200, self.project({"action": "blocked", "appId": app_id,
                                      "reasonCode": "client_too_old",
                                      "reason": "channel requires a newer client compatibility window"},
                                     protocol)
        releases = self.all(
            "SELECT * FROM releases WHERE app_id=? AND channel=? ORDER BY id ASC",
            (app_id, batch["channel"]),
        )
        if not releases:
            return 200, self.project({"action": "blocked", "appId": app_id,
                                      "reasonCode": "no_release",
                                      "reason": "channel has no published release"}, protocol)
        candidates = []
        rejection = None
        for row in releases:
            manifest = json.loads(row["manifest"])
            ok, reason = self.artifact_compatible(manifest, device, report)
            if ok:
                candidates.append((row, manifest))
            else:
                rejection = reason
        if not candidates:
            self.remember_assignment(device_id, None, None, rejection or "no compatible artifact")
            return 200, self.project({"action": "blocked", "appId": app_id,
                                      "reasonCode": "requirements_failed",
                                      "reason": rejection or "requirements failed"}, protocol)
        latest_version = max(version_key(r["version"]) for r, _ in candidates)
        candidates = [c for c in candidates if version_key(c[0]["version"]) == latest_version]
        candidates.sort(key=lambda item: (int(item[0]["size"]), int(item[0]["id"])))
        row, manifest = candidates[0]
        policy = row["dependency_policy"]
        response = {
            "action": "upgrade",
            "appId": app_id,
            "releaseId": row["id"],
            "version": row["version"],
            "channel": row["channel"],
            "dependencyPolicy": policy,
            "downloadUrl": "/artifacts/%d/%s" % (row["id"], urllib.parse.quote(row["artifact"])),
            "sha256": manifest.get("digest"),
            "artifactSha256": row["artifact_sha256"],
            "size": row["size"],
            "manifest": self.manifest_for_client(manifest, protocol),
        }
        if protocol >= 2:
            response["extensions"] = {
                "schemaVersion": 2,
                "requirements": manifest.get("requirements", {}),
                "firstFrame": manifest.get("payload", {}).get("firstFrame", {}),
                "selection": {
                    "eligiblePolicies": [r["dependency_policy"] for r, _ in candidates],
                    "chosenBy": "newest-version;smallest-compatible-artifact",
                    "probeAt": report.get("_createdAt"),
                },
                "buildFingerprint": json.loads(row["fingerprint"]),
            }
        self.remember_assignment(device_id, int(row["id"]), policy, "selected")
        return 200, self.project(response, protocol)

    def artifact_compatible(self, manifest: Dict[str, Any], device: sqlite3.Row,
                            report: Dict[str, Any]) -> Tuple[bool, str]:
        req = manifest.get("requirements", {})
        arch = req.get("arch")
        probe_arch = (report.get("os") or {}).get("arch") or device["arch"]
        if arch and probe_arch and arch != probe_arch:
            return False, "architecture mismatch: release=%s device=%s" % (arch, probe_arch)
        display = report.get("display") or {}
        if req.get("display") and display.get("available") is not True:
            return False, "no usable display detected"
        wanted_backends = set(req.get("graphicsBackend") or [])
        actual_backends = set((display.get("backends") or {}).keys())
        if wanted_backends and actual_backends and not (wanted_backends & actual_backends):
            return False, "graphics backend mismatch: required=%s detected=%s" % (
                ",".join(sorted(wanted_backends)), ",".join(sorted(actual_backends)))
        fonts_available = set((report.get("fonts") or {}).get("availableFamilies") or [])
        bundled_fonts = {f.get("family") for f in manifest.get("fonts", []) if f.get("family")}
        missing_fonts = []
        for font in req.get("requiredFonts") or []:
            family = font.get("family") if isinstance(font, dict) else str(font)
            if family not in fonts_available and family not in bundled_fonts:
                missing_fonts.append(family)
        if missing_fonts:
            return False, "missing required font(s): %s" % ", ".join(missing_fonts)
        libs_available = set((report.get("libraries") or {}).get("available") or [])
        missing_libs = [x for x in req.get("systemLibraries", []) if x not in libs_available]
        if manifest.get("dependencyPolicy") == "system" and missing_libs:
            return False, "missing system component(s): %s" % ", ".join(missing_libs)
        paths = report.get("paths") or {}
        install = paths.get("installRoot") or {}
        free = int(install.get("freeBytes") or 0)
        need = int(req.get("minDiskBytes") or 0)
        if free and need and free < need:
            return False, "insufficient disk: required=%d free=%d" % (need, free)
        return True, ""

    def manifest_for_client(self, manifest: Dict[str, Any], protocol: int) -> Dict[str, Any]:
        if protocol >= 2:
            return copy.deepcopy(manifest)
        # v1 clients receive only fields their compatibility window knows.
        keep = {"schemaVersion", "appId", "version", "channel", "dependencyPolicy",
                "payload", "requirements", "digest"}
        return {k: v for k, v in manifest.items() if k in keep}

    def project(self, response: Dict[str, Any], protocol: int) -> Dict[str, Any]:
        if protocol >= 2:
            out = copy.deepcopy(response)
            out.setdefault("extensions", {"schemaVersion": 2})
            return out
        return {k: v for k, v in response.items() if k in BASE_V1_FIELDS}

    def remember_assignment(self, device_id: str, release_id: Optional[int],
                            policy: Optional[str], reason: str) -> None:
        self.execute(
            "INSERT INTO assignments_obs(device_id,release_id,policy,reason,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET release_id=excluded.release_id,"
            "policy=excluded.policy,reason=excluded.reason,updated_at=excluded.updated_at",
            (device_id, release_id, policy, reason, now()),
        )

    def state(self) -> Dict[str, Any]:
        devices = []
        for d in self.all("SELECT * FROM devices ORDER BY id"):
            reports = self.all(
                "SELECT id,phase,status,detail,client_version,created_at FROM reports "
                "WHERE device_id=? ORDER BY created_at DESC,id DESC LIMIT 8", (d["id"],))
            obs = self.one("SELECT * FROM assignments_obs WHERE device_id=?", (d["id"],))
            devices.append({
                "id": d["id"], "name": d["name"], "arch": d["arch"],
                "registeredAt": d["registered_at"], "lastSeen": d["last_seen"],
                "declaredCapabilities": json.loads(d["capabilities"]),
                "latestProbe": self.latest_report(d["id"]),
                "reports": [dict(r, detail=json.loads(r["detail"])) for r in reports],
                "assignment": dict(obs) if obs else None,
            })
        releases = []
        for r in self.all("SELECT id,app_id,version,channel,dependency_policy,artifact,size,"
                          "artifact_sha256,sha256,fingerprint,created_at FROM releases ORDER BY id DESC"):
            item = dict(r)
            item["fingerprint"] = json.loads(r["fingerprint"])
            releases.append(item)
        channels = [dict(x) for x in self.all("SELECT * FROM channels ORDER BY app_id,name")]
        batches = []
        for b in self.all("SELECT * FROM batches ORDER BY id DESC"):
            devs = [x[0] for x in self.all(
                "SELECT device_id FROM batch_devices WHERE batch_id=? ORDER BY device_id", (b["id"],))]
            batches.append({**dict(b), "deviceIds": devs})
        return {"devices": devices, "releases": releases, "channels": channels, "batches": batches,
                "serverTime": now()}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "VDVControl/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def send_json(self, status: int, obj: Any) -> None:
        body = json_dumps(obj)
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> Dict[str, Any]:
        n = int(self.headers.get("content-length", "0") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                self.static_file("index.html")
            elif parsed.path == "/api/health":
                self.send_json(200, {"ok": True, "time": now()})
            elif parsed.path == "/api/admin/state":
                self.send_json(200, self.store.state())
            elif parsed.path.startswith("/api/agent/devices/"):
                rest = parsed.path.split("/")
                # /api/agent/devices/<id>/assignment
                if len(rest) == 6 and rest[5] == "assignment":
                    device_id = urllib.parse.unquote(rest[4])
                    protocol = int(qs.get("protocol", ["2"])[0])
                    app_id = qs.get("app", ["visual-window"])[0]
                    status, obj = self.store.assignment(device_id, max(1, min(2, protocol)), app_id)
                    self.send_json(status, obj)
                else:
                    self.send_json(404, {"error": "unknown agent API"})
            elif parsed.path.startswith("/artifacts/"):
                self.serve_artifact(parsed.path, qs)
            else:
                self.send_json(404, {"error": "not found"})
        except Exception as exc:  # keep API errors machine-readable
            self.send_json(500, {"error": str(exc), "type": exc.__class__.__name__})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/admin/devices":
                self.send_json(200, self.store.register_device(data))
            elif parsed.path == "/api/admin/channels":
                self.store.execute(
                    "INSERT INTO channels(app_id,name,active,rollout_percent,min_client_version) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(app_id,name) DO UPDATE SET active=excluded.active,"
                    "rollout_percent=excluded.rollout_percent,min_client_version=excluded.min_client_version",
                    (data.get("appId", "visual-window"), data["name"],
                     1 if data.get("active", True) else 0, int(data.get("rolloutPercent", 100)),
                     int(data.get("minClientVersion", 1))),
                )
                self.send_json(200, {"accepted": True})
            elif parsed.path == "/api/admin/batches":
                cur = self.store.execute(
                    "INSERT INTO batches(name,app_id,channel,active,created_at) VALUES(?,?,?,?,?)",
                    (data["name"], data.get("appId", "visual-window"), data["channel"],
                     1 if data.get("active", True) else 0, now()),
                )
                bid = int(cur.lastrowid)
                for did in data.get("deviceIds", []):
                    self.store.execute(
                        "INSERT OR IGNORE INTO batch_devices(batch_id,device_id) VALUES(?,?)",
                        (bid, str(did)),
                    )
                self.send_json(200, {"id": bid, "accepted": True})
            elif parsed.path == "/api/admin/releases/publish":
                src = Path(data["path"]).expanduser()
                if not src.is_absolute():
                    src = ROOT / src
                self.send_json(200, self.store.publish_release(src))
            elif parsed.path == "/api/agent/reports":
                self.send_json(200, self.store.add_report(data))
            else:
                self.send_json(404, {"error": "not found"})
        except (ValueError, KeyError, json.JSONDecodeError, FileNotFoundError) as exc:
            self.send_json(400, {"error": str(exc), "type": exc.__class__.__name__})
        except Exception as exc:
            self.send_json(500, {"error": str(exc), "type": exc.__class__.__name__})

    def static_file(self, name: str) -> None:
        path = ROOT / "deploy" / "console" / name
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_artifact(self, path: str, qs: Dict[str, List[str]]) -> None:
        m = re.match(r"^/artifacts/(\d+)/(.+)$", path)
        if not m:
            self.send_json(404, {"error": "artifact not found"})
            return
        row = self.store.one("SELECT * FROM releases WHERE id=?", (int(m.group(1)),))
        if row is None:
            self.send_json(404, {"error": "artifact not found"})
            return
        name = urllib.parse.unquote(m.group(2))
        if name != row["artifact"]:
            self.send_json(404, {"error": "artifact name mismatch"})
            return
        fpath = self.store.artifact_dir / row["artifact"]
        total = fpath.stat().st_size
        abort_after = int(qs.get("testAbortAfter", ["0"])[0] or 0)
        start, end = 0, total - 1
        rng = self.headers.get("range")
        if rng:
            mm = re.match(r"bytes=(\d*)-(\d*)", rng)
            if mm:
                if mm.group(1):
                    start = int(mm.group(1))
                if mm.group(2):
                    end = min(int(mm.group(2)), total - 1)
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("content-range", "bytes */%d" % total)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
        if abort_after:
            end = min(end, start + abort_after - 1)
        self.send_response(206 if rng or abort_after else 200)
        self.send_header("content-type", "application/x-tar")
        self.send_header("content-length", str(end - start + 1))
        self.send_header("accept-ranges", "bytes")
        self.send_header("content-range", "bytes %d-%d/%d" % (start, end, total))
        self.end_headers()
        with fpath.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 64, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: Any, store: Store, verbose: bool) -> None:
        super().__init__(addr, Handler)
        self.store = store
        self.verbose = verbose


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--db", default="var/vdv.db")
    ap.add_argument("--artifact-dir", default="var/artifacts")
    ap.add_argument("--port-file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    store = Store(ROOT / args.db, ROOT / args.artifact_dir)
    srv = ThreadingHTTPServer((args.host, args.port), store, args.verbose)
    if args.port_file:
        Path(args.port_file).write_text(str(srv.server_address[1]) + "\n", encoding="utf-8")
    print("VDV control plane on http://%s:%d" % srv.server_address, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
