#!/usr/bin/env python3
"""WDeliver 服务端。

职责（对应用户需求）：
- Web 控制台登记目标设备能力、配置发行通道与升级批次
- 提供资源（.wdz 包）与安装清单（按客户端协议版本协商视图）
- 数据库记录构建指纹及每台设备的探测结果
- 控制台“升级完成”只在收到首帧 receipt 后点亮；下载中只显示进度

测试能力（仅 WDELIVER_TEST=1 开启，生产模式关闭）：
- /packages/x.wdz?cut_after=N   发送 N 字节后掐断，验证续传
- 清单请求头 X-Test-Inject-Future 注入未知字段，验证旧客户端忽略
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from wdeliver.manifest import (LATEST_VERSION, SUPPORTED_VERSIONS,  # noqa: E402
                               ManifestError, make_view, negotiate, parse)
from wdeliver.server.db import Store  # noqa: E402

CONSOLE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "console.html")

PHASE_ORDER = ["NEW", "PROBED", "PREFLIGHT", "DOWNLOADING", "VERIFIED",
               "INSTALLING", "FIRSTFRAME", "ACTIVE", "ROLLEDBACK", "FAILED"]


class ServerState:
    def __init__(self, store: Store, package_dir: str, test_mode: bool):
        self.store = store
        self.package_dir = os.path.abspath(package_dir)
        self.test_mode = test_mode

    def build_master_manifest(self, device: Dict[str, Any], channel_name: str
                              ) -> Optional[Dict[str, Any]]:
        ch = self.store.get_channel(channel_name)
        if not ch or not ch.get("build_id"):
            return None
        build = self.store.get_build(ch["build_id"])
        if not build:
            return None
        # 升级批次：用 device_id+batch 做确定性哈希，保证同一台设备结论稳定。
        import hashlib as _h
        h = int(_h.sha256(f"{device['id']}|{ch['batch']}".encode()).hexdigest(),
                16) % 100
        if h >= ch["rollout_pct"]:
            return {"_no_update": True,
                    "reason": f"not-in-batch ({h}>={ch['rollout_pct']})",
                    "channel": channel_name, "batch": ch["batch"]}
        files = [{"path": f["path"], "size": f["size"], "sha256": f["sha256"]}
                 for f in build["files"]]
        return {
            "build_id": build["build_id"],
            "version": build["version"],
            "package_url": f"/packages/{build['package']}",
            "package_size": build["package_size"],
            "package_sha256": build["package_sha256"],
            "files": files,
            "release_channel": channel_name,
            "rollout_batch": ch["batch"],
            "release_notes": f"build {build['version']} on {channel_name}",
            "requirements": ch.get("require") or {},
            "fallback_on_fail": True,
            "min_client_version": (ch.get("require") or {}).get(
                "min_client_version", "1.0.0"),
        }


def make_handler(state: ServerState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WDeliver/1.0"

        def log_message(self, fmt, *args):  # 安静日志，测试输出更易读
            sys.stderr.write("[server] " + fmt % args + "\n")

        # ---------- helpers ----------
        def _json(self, code: int, obj: Any, extra: Optional[dict] = None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        def _json_body(self) -> Any:
            raw = self._body()
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _device_or_404(self, dev_id: str) -> Optional[Dict[str, Any]]:
            for d in state.store.list_devices():
                if d["id"] == dev_id:
                    return d
            self._json(404, {"error": "device not registered"})
            return None

        # ---------- routing ----------
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                if p == "/health":
                    return self._json(200, {"ok": True})
                if p in ("/", "/console", "/index.html"):
                    return self._console()
                if p == "/api/console-state":
                    return self._console_state()
                if p.startswith("/api/manifest/device/"):
                    return self._manifest(p.rsplit("/", 1)[-1], q)
                if p.startswith("/packages/"):
                    return self._package(os.path.basename(p), q)
                if p.startswith("/api/channels"):
                    parts = [x for x in p.split("/") if x]
                    if len(parts) == 3:
                        ch = state.store.get_channel(parts[2])
                        return self._json(200, ch or {"error": "not found"})
                    return self._json(200, state.store.list_channels())
                if p.startswith("/api/devices/") and p.endswith("/events"):
                    dev_id = "/".join(p.split("/")[3:-1])
                    return self._json(200, state.store.events(dev_id))
                if p.startswith("/api/devices/"):
                    dev_id = p[len("/api/devices/"):]
                    d = next((x for x in state.store.list_devices()
                              if x["id"] == dev_id), None)
                    return self._json(200, d or {"error": "not found"})
                if p == "/api/builds":
                    return self._json(200, state.store.list_builds())
                self._json(404, {"error": "unknown path", "path": p})
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": type(exc).__name__,
                                 "detail": str(exc)})

        def do_POST(self):
            p = urlparse(self.path).path
            try:
                if p == "/api/devices/register":
                    b = self._json_body()
                    state.store.register_device(b["device_id"],
                                                b.get("name", b["device_id"]),
                                                b.get("client_version", "?"))
                    return self._json(200, {"ok": True, "device_id": b["device_id"]})
                if p.startswith("/api/devices/") and p.endswith("/probe"):
                    dev_id = p[len("/api/devices/"):-len("/probe")]
                    if not self._device_or_404(dev_id):
                        return
                    state.store.save_probe(dev_id, self._json_body())
                    return self._json(200, {"ok": True})
                if p.startswith("/api/devices/") and p.endswith("/report"):
                    dev_id = p[len("/api/devices/"):-len("/report")]
                    if not self._device_or_404(dev_id):
                        return
                    b = self._json_body()
                    state.store.set_phase(
                        dev_id, b.get("phase", "UNKNOWN"),
                        detail=b.get("detail"), receipt=b.get("receipt"))
                    return self._json(200, {"ok": True})
                if p == "/api/channels":
                    b = self._json_body()
                    state.store.upsert_channel(
                        b["name"], b.get("build_id"),
                        b.get("batch", "batch-0"),
                        int(b.get("rollout_pct", 100)),
                        bool(b.get("paused", False)),
                        b.get("require", {}))
                    return self._json(200, {"ok": True})
                if p == "/api/builds":
                    rec = self._json_body()
                    required = {"build_id", "name", "version", "package",
                                "package_size", "package_sha256", "files"}
                    if not required <= set(rec):
                        return self._json(400, {"error": "incomplete build record"})
                    pkg_path = os.path.join(state.package_dir, rec["package"])
                    if not os.path.isfile(pkg_path):
                        return self._json(400, {"error": "package file missing",
                                                "expected": pkg_path})
                    state.store.upsert_build(rec)
                    return self._json(200, {"ok": True, "build_id": rec["build_id"]})
                if p == "/api/test/fault":
                    if not state.test_mode:
                        return self._json(403, {"error": "test mode disabled"})
                    # cut_after 通过下载查询参数生效，此端点用于自检/清空。
                    return self._json(200, {"ok": True, "test_mode": True})
                self._json(404, {"error": "unknown path", "path": p})
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"bad JSON: {exc}"})
            except KeyError as exc:
                self._json(400, {"error": f"missing key: {exc}"})

        def do_PUT(self):
            p = urlparse(self.path).path
            try:
                if p.startswith("/api/packages/"):
                    name = os.path.basename(p)
                    if "/" in name or name.startswith("."):
                        return self._json(400, {"error": "bad name"})
                    data = self._body()
                    dest = os.path.join(state.package_dir, name)
                    os.makedirs(state.package_dir, exist_ok=True)
                    with open(dest, "wb") as fh:
                        fh.write(data)
                    return self._json(200, {"ok": True, "name": name,
                                            "size": len(data),
                                            "sha256": hashlib.sha256(data).hexdigest()})
                self._json(404, {"error": "unknown path"})
            except OSError as exc:
                self._json(500, {"error": str(exc)})

        # ---------- endpoints ----------
        def _console(self):
            with open(CONSOLE_HTML, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _console_state(self):
            devices = state.store.list_devices()
            for d in devices:
                d["phase_complete"] = bool(
                    d["phase"] == "ACTIVE" and d.get("receipt")
                    and d["receipt"].get("frame_sha256"))
            return self._json(200, {
                "devices": devices, "channels": state.store.list_channels(),
                "builds": [{"build_id": b["build_id"], "name": b["name"],
                            "version": b["version"],
                            "package_size": b["package_size"],
                            "package_sha256": b["package_sha256"]}
                           for b in state.store.list_builds()],
                "phases": PHASE_ORDER,
            })

        def _manifest(self, dev_id: str, q: Dict[str, list]):
            device = next((x for x in state.store.list_devices()
                           if x["id"] == dev_id), None)
            if not device:
                return self._json(404, {"error": "device not registered"})
            channel = (q.get("channel") or ["stable"])[0]
            master = state.build_master_manifest(device, channel)
            if master is None:
                return self._json(200, {"update": False,
                                        "reason": "no-build-on-channel"})
            if master.get("_no_update"):
                m = {"update": False, **{k: v for k, v in master.items()
                                         if k != "_no_update"}}
                return self._json(200, m)

            accept = self.headers.get("X-Manifest-Accept", LATEST_VERSION)
            client_ver = self.headers.get("X-Client-Version", "1.0.0")
            offered = [v.strip() for v in accept.split(",") if v.strip()]
            chosen = negotiate(offered, SUPPORTED_VERSIONS)
            if chosen is None:
                return self._json(409, {
                    "error": "CLIENT_TOO_OLD",
                    "detail": f"no common manifest version; client={offered}, "
                              f"server={list(SUPPORTED_VERSIONS)}"})
            view = make_view(master, chosen)
            # 测试注入：旧客户端必须忽略未知字段，不能崩溃也不能改变安装语义。
            if state.test_mode and \
                    self.headers.get("X-Test-Inject-Future") == "1":
                view["future_schedule_window"] = {"at": "2030-01-01T00:00:00Z"}
                view["future_policy"] = {"quantum_rollback": True}
                if view["files"]:
                    view["files"][0]["future_compression"] = "zstd-99"
            # 服务端语义校验：保证下发清单自身合法。
            try:
                parse(view, client_ver)
            except ManifestError as exc:
                return self._json(409, {"error": "CLIENT_TOO_OLD",
                                        "detail": str(exc)})
            view["update"] = True
            self._json(200, view,
                       {"X-Manifest-Served-Version": chosen})

        def _package(self, name: str, q: Dict[str, list]):
            if "/" in name or name.startswith("."):
                return self._json(400, {"error": "bad name"})
            path = os.path.join(state.package_dir, name)
            if not os.path.isfile(path):
                return self._json(404, {"error": "package not found",
                                        "path": path})
            total = os.path.getsize(path)
            start, end = 0, total - 1
            rng = self.headers.get("Range")
            status = 200
            if rng and rng.startswith("bytes="):
                spec = rng.split("=", 1)[1].split("-")
                start = int(spec[0]) if spec[0] else 0
                if len(spec) > 1 and spec[1]:
                    end = min(int(spec[1]), total - 1)
                status = 206
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{total}")
            else:
                self.send_response(200)
            length = end - start + 1
            cut = None
            if state.test_mode and "cut_after" in q:
                cut = int(q["cut_after"][0])
                length = min(length, cut)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                    if cut is not None:
                        self.wfile.flush()
                        self.close_connection = True  # 模拟中途断网
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    return Handler


def serve(host: str, port: int, db_path: str, package_dir: str,
          test_mode: bool) -> ThreadingHTTPServer:
    store = Store(db_path)
    state = ServerState(store, package_dir, test_mode)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.state = state  # type: ignore[attr-defined]
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8448)
    ap.add_argument("--db", default="run/server.db")
    ap.add_argument("--package-dir", default="packages")
    ap.add_argument("--test", action="store_true",
                    help="开启故障注入接口（仅供验收测试）")
    args = ap.parse_args()
    httpd = serve(args.host, args.port, args.db, args.package_dir,
                  args.test or os.environ.get("WDELIVER_TEST") == "1")
    print(f"WDeliver server on http://{args.host}:{args.port} "
          f"(test_mode={httpd.state.test_mode})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
