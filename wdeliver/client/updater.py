#!/usr/bin/env python3
"""客户端升级器（交付验证状态机）。

阶段：REGISTER → PROBED → PREFLIGHT → DOWNLOADING → VERIFIED →
      INSTALLING（暂存首帧通过后原子切换 current）→ FIRSTFRAME（激活后首帧）
      → ACTIVE；激活后首帧失败 → ROLLEDBACK（切回旧版并复验）；
      激活前任何失败 → FAILED（current 从未改变，旧版继续可用）。

铁律：
- 控制台只有在 ACTIVE + receipt.frame_sha256 时才显示完成；
  DOWNLOADING / VERIFIED / INSTALLING 都不是完成。
- 激活 = 一次 symlink 的 rename，天然可回退；旧版本目录保留到确认新版可用。
- 下载可跨进程续传，最终必须 sha256 + 文件级 hash 双校验。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from wdeliver import CLIENT_VERSION  # noqa: E402
from wdeliver.client import http as httpc  # noqa: E402
from wdeliver.client.probe import run_all  # noqa: E402
from wdeliver.manifest import LATEST_VERSION, ManifestError, parse  # noqa: E402

PHASES = ["REGISTER", "PROBED", "PREFLIGHT", "DOWNLOADING", "VERIFIED",
          "INSTALLING", "FIRSTFRAME", "ACTIVE", "ROLLEDBACK", "FAILED"]


class Updater:
    def __init__(self, args: argparse.Namespace):
        self.a = args
        self.server = args.server.rstrip("/")
        self.install = os.path.abspath(args.install_dir)
        self.data = os.path.abspath(args.data_dir)
        self.cache = os.path.abspath(args.cache_dir)
        self.state_dir = os.path.abspath(args.state_dir)
        # 注意：安装目录此刻不能创建——它是否可写必须由探测实测决定，
        # 预检不过就绝不落盘任何安装文件。versions 延迟到 INSTALLING 再建。
        self.versions = os.path.join(self.install, "versions")
        self.current = os.path.join(self.install, "current")
        self.dl_dir = os.path.join(self.data, "downloads")
        for d in (self.dl_dir, self.cache, self.state_dir):
            os.makedirs(d, exist_ok=True)
        self.journal_path = os.path.join(self.state_dir, "journal.jsonl")
        self.state_path = os.path.join(self.state_dir, "state.json")
        # 兼容窗口：可用 --emulate-client 1.0 模拟旧客户端协商行为。
        self.client_version = args.emulate_client or CLIENT_VERSION
        # 各代客户端能理解的最高清单协议（兼容窗口：1.0 客户端只能收 1.0 视图）。
        self.max_manifest = {
            "1.0.0": "1.0",
            "1.1.0": "1.1",
        }.get(self.client_version, LATEST_VERSION)
        self._last_report = 0.0
        # 探测隔离环境：所有子进程（fontconfig 等）的 HOME/缓存都指向
        # 显式指定的工作 HOME，绝不污染登录用户的真实 HOME。
        self.probe_home = os.path.abspath(args.home or os.path.join(self.state_dir, "home"))
        os.makedirs(self.probe_home, exist_ok=True)
        self.probe_env = os.environ.copy()
        self.probe_env["HOME"] = self.probe_home
        self.probe_env["XDG_CACHE_HOME"] = os.path.join(self.data, "xdg-cache")
        self.probe_env["XDG_DATA_HOME"] = os.path.join(self.data, "xdg-data")

    # ---------- 本地留痕 ----------
    def journal(self, event: str, **kw) -> None:
        rec = {"at": time.time(), "event": event, **kw}
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    def save_state(self, **kw) -> None:
        data: Dict[str, Any] = {}
        if os.path.isfile(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        data.update(kw)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)

    # ---------- 服务端上报 ----------
    def report(self, phase: str, detail: Optional[Dict[str, Any]] = None,
               receipt: Optional[Dict[str, Any]] = None,
               throttle: float = 0.0) -> None:
        now = time.time()
        if throttle and now - self._last_report < throttle:
            return
        self._last_report = now
        self.journal("phase", phase=phase, detail=detail)
        self.save_state(phase=phase)
        try:
            httpc.request(
                "POST", f"{self.server}/api/devices/{self.a.device}/report",
                data={"phase": phase, "detail": detail, "receipt": receipt},
                timeout=5)
        except Exception as exc:  # noqa: BLE001 - 上报失败不阻断本地升级
            self.journal("report-failed", error=str(exc))

    # ---------- 注册 + 探测 ----------
    def register(self) -> None:
        httpc.request("POST", f"{self.server}/api/devices/register", data={
            "device_id": self.a.device, "name": self.a.name,
            "client_version": self.client_version})

    def probe(self, post: bool = True) -> Dict[str, Any]:
        # network 探测失败不影响其余本地能力探测，worst_status 已含其结论。
        report = run_all(self.install, self.data, self.cache,
                         base_url=self.server,
                         home=self.probe_home, env=self.probe_env)
        with open(os.path.join(self.state_dir, "probe.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
        if post:
            try:
                httpc.request(
                    "POST", f"{self.server}/api/devices/{self.a.device}/probe",
                    data=report)
                self.report("PROBED", {"probe_verdict": report["verdict"]})
            except Exception as exc:  # noqa: BLE001 - 离线仍可本地探测
                report["server_post"] = {"status": "failed",
                                         "error": type(exc).__name__}
                self.journal("probe-post-failed", error=str(exc))
        return report

    # ---------- 清单协商 ----------
    def fetch_manifest(self) -> Dict[str, Any]:
        url = (f"{self.server}/api/manifest/device/{self.a.device}"
               f"?channel={self.a.channel}")
        try:
            raw = httpc.request(
                "GET", url, headers={
                    "X-Manifest-Accept": self.max_manifest,
                    "X-Client-Version": self.client_version,
                    **({"X-Test-Inject-Future": "1"}
                       if self.a.inject_future else {})},
                timeout=8)
        except httpc.HTTPError as exc:
            if exc.status == 409:
                raise RuntimeError(
                    f"服务器拒绝升级（兼容窗口外）: {exc.body}") from None
            raise
        if isinstance(raw, dict) and not raw.get("update", False):
            raise NoUpdate(raw.get("reason", "no update"))
        view = parse(raw, self.client_version)  # 本地再校验，纵深防御
        with open(os.path.join(self.state_dir,
                               f"manifest-{view['version']}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2, sort_keys=True)
        self.journal("manifest", version=view["manifest_version"],
                     build=view["build_id"], ignored=view["ignored_keys"])
        return view

    # ---------- 预检（对照实测，不对照文档） ----------
    def preflight(self, probe: Dict[str, Any],
                  manifest: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        req = manifest.get("requirements") or {}
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, evidence: str, required: bool = True):
            checks.append({"check": name, "pass": bool(ok), "evidence": evidence,
                           "blocks": required and not ok})

        add("install_dir_writable",
            probe["install_writable"]["status"] == "ok",
            probe["install_writable"].get("evidence",
                probe["install_writable"].get("detail", "?")))
        lw = probe["local_writes"]
        add("local_dirs_writable", lw["status"] == "ok",
            f"data={lw['data_dir']['status']} cache={lw['cache_dir']['status']}")
        gfx = probe["gfx_backend"]
        need_be = req.get("gfx_backend")
        if need_be == "x11":
            add("gfx_backend_x11", gfx["backend"] == "x11",
                f"detected={gfx['backend']}: {gfx.get('detail') or gfx.get('evidence')}")
        elif need_be == "sdl":
            add("gfx_backend_sdl_capable",
                gfx["backend"] in ("x11", "wayland", "framebuffer"),
                f"detected={gfx['backend']}")
        elif need_be and need_be != "nullfb":
            add(f"gfx_backend_{need_be}", False,
                f"unknown required backend {need_be!r}")
        else:
            add("gfx_backend_any", True,
                f"detected={gfx['backend']} (nullfb fallback allowed)",
                required=False)
        if req.get("font_sans"):
            found = probe["fonts"].get("found") or {}
            add("system_sans_font", bool(found.get("sans")),
                f"sans={found.get('sans') or '未实测到'}")
        if req.get("cjk_font"):
            found = probe["fonts"].get("found") or {}
            add("system_cjk_font", bool(found.get("cjk")),
                f"cjk={found.get('cjk') or '未实测到'}")
        # 网络在 DOWNLOADING 阶段会被真实检验；预检时仅记录。
        blocked = [c for c in checks if c["blocks"]]
        verdict = "pass" if not blocked else "blocked"
        return verdict, {"checks": checks, "blocked":
                         [c["check"] for c in blocked]}

    def choose_backend(self, probe: Dict[str, Any],
                       manifest: Dict[str, Any]) -> str:
        req = manifest.get("requirements") or {}
        have = probe["gfx_backend"]["backend"]
        need = req.get("gfx_backend")
        if need == "nullfb":
            return "nullfb"
        if have in ("x11", "wayland", "framebuffer"):
            return "sdl"
        return "nullfb"  # 未强制真实显示时：如实降级，receipt 记录 nullfb

    # ---------- 下载（断点续传） ----------
    def download(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        dest = os.path.join(self.dl_dir, f"{manifest['build_id']}.wdz.part")
        url = httpc.join(self.server, manifest["package_url"])
        params: Dict[str, Any] = {}
        if os.environ.get("WD_DOWNLOAD_CUT"):
            # 验收：服务器（测试模式）在发送 N 字节后掐断连接。
            params["cut_after"] = os.environ["WD_DOWNLOAD_CUT"]
        self.report("DOWNLOADING", {"bytes": os.path.getsize(dest)
                    if os.path.exists(dest) else 0,
                    "total": manifest["package_size"], "phase": "start"})
        def on_prog(n: int, total: int):
            self.report("DOWNLOADING",
                        {"bytes": n, "total": total, "phase": "downloading"},
                        throttle=0.25)
        info = httpc.download(
            url, dest, manifest["package_size"], manifest["package_sha256"],
            max_resumes=int(os.environ.get("WD_MAX_RESUMES", "8")),
            extra_params=params, on_progress=on_prog)
        self.journal("downloaded", **info)
        return {"path": dest, **info}

    # ---------- 解包 + 文件级校验 ----------
    @staticmethod
    def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
        dest_abs = os.path.abspath(dest)
        for m in tar.getmembers():
            if not (m.isreg() or m.isdir()):
                raise IOError(f"unsafe member type: {m.name}")
            target = os.path.abspath(os.path.join(dest_abs, m.name))
            if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
                raise IOError(f"path traversal in package: {m.name}")
        tar.extractall(dest)

    def install_version(self, manifest: Dict[str, Any],
                        pkg_path: str) -> str:
        version = manifest["version"]
        os.makedirs(self.versions, exist_ok=True)
        staging = os.path.join(self.versions, f".staging-{version}")
        if os.path.exists(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)
        with tarfile.open(pkg_path, "r:gz") as tar:
            self._safe_extract(tar, staging)
        # 文件级 sha256 复核（清单列出的每个文件都必须在盘且一致）。
        bad: List[str] = []
        present = set()
        for f in manifest["files"]:
            p = os.path.join(staging, f["path"])
            present.add(f["path"])
            if not os.path.isfile(p):
                bad.append(f"{f['path']}: missing")
                continue
            if f.get("sha256"):
                import hashlib
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for blk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(blk)
                if h.hexdigest() != f["sha256"]:
                    bad.append(f"{f['path']}: hash mismatch")
        if bad:
            shutil.rmtree(staging)
            raise RuntimeError("file verification failed: " + "; ".join(bad[:5]))
        with open(os.path.join(staging, ".manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
        final = os.path.join(self.versions, version)
        if os.path.exists(final):
            shutil.rmtree(final)
        os.replace(staging, final)
        return final

    # ---------- 首帧验证钩子 ----------
    def run_firstframe(self, root: str, stage: str, backend: str,
                       manifest: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        out_dir = os.path.join(self.state_dir,
                               f"firstframe-{stage}-{manifest['version']}")
        os.makedirs(out_dir, exist_ok=True)
        hook = os.path.join(root, "hooks", "firstframe.py")
        env = os.environ.copy()
        env["WDELIVER_STAGE"] = stage
        proc = subprocess.run(
            [sys.executable, hook, "--backend", backend, "--root", root,
             "--out-dir", out_dir, "--version", manifest["version"],
             "--build-id", manifest["build_id"],
             "--manifest", os.path.join(root, ".manifest.json"),
             "--stage", stage],
            capture_output=True, text=True, env=env, timeout=45)
        receipt: Dict[str, Any] = {}
        rp = os.path.join(out_dir, "receipt.json")
        if os.path.isfile(rp):
            with open(rp, encoding="utf-8") as fh:
                receipt = json.load(fh)
        if proc.returncode != 0:
            receipt["error"] = proc.stderr.strip()[-400:]
            receipt["exit_code"] = proc.returncode
        return proc.returncode, receipt

    def _current_target(self) -> Optional[str]:
        if os.path.islink(self.current):
            return os.readlink(self.current)
        return None

    def _activate(self, version: str) -> None:
        tmp = os.path.join(self.install, "current.tmp")
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.join("versions", version), tmp)
        os.replace(tmp, self.current)  # 原子切换

    # ---------- 主流程 ----------
    def run(self) -> int:
        try:
            self.register()
        except Exception as exc:  # noqa: BLE001
            print(f"register failed: {exc}", file=sys.stderr)
            return 10
        probe = self.probe()
        print("[probe] verdict=" + probe["verdict"])
        for key in ("fonts", "gfx_backend", "display_scaling",
                    "install_writable"):
            item = probe[key]
            print(f"  {key}: {item['status']} "
                  f"{item.get('backend', item.get('found', ''))} "
                  f"{item.get('detail', '')}")

        try:
            manifest = self.fetch_manifest()
        except NoUpdate as info:
            print(f"[manifest] {info}")
            return 0
        except Exception as exc:  # noqa: BLE001
            self.report("FAILED", {"message": f"manifest: {exc}"})
            print(f"manifest failed: {exc}", file=sys.stderr)
            return 11

        self.report("PREFLIGHT", {"phase": "checking"})
        verdict, pf = self.preflight(probe, manifest)
        self.journal("preflight", **pf)
        if verdict != "pass":
            msg = "预检未通过（基于实测，不做默认假设）: " + \
                  ", ".join(pf["blocked"])
            self.report("FAILED", {"message": msg, "checks": pf["checks"]})
            print(msg, file=sys.stderr)
            return 12
        print("[preflight] pass; backend="
              + self.choose_backend(probe, manifest))

        backend = self.choose_backend(probe, manifest)
        previous = self._current_target()

        try:
            dl = self.download(manifest)
        except Exception as exc:  # noqa: BLE001
            self.report("FAILED", {"message": f"download: {exc}"})
            print(f"download failed: {exc}", file=sys.stderr)
            return 13
        self.report("VERIFIED", {"bytes": dl["bytes"], "resumes": dl["resumes"],
                                 "package_sha256": dl["sha256"]})

        try:
            self.report("INSTALLING", {"phase": "extract"})
            new_root = self.install_version(manifest, dl["path"])
        except Exception as exc:  # noqa: BLE001
            self.report("FAILED", {"message": f"install: {exc}"})
            return 14

        # 1) 暂存首帧（尚未影响 current）
        rc, staged_receipt = self.run_firstframe(
            new_root, "staged", backend, manifest)
        if rc != 0:
            shutil.rmtree(new_root, ignore_errors=True)
            self.report("FAILED",
                        {"message": "staged firstframe failed; current "
                                    f"unchanged ({previous}): "
                                    f"{staged_receipt.get('error', rc)}"})
            print("staged firstframe FAILED; current untouched", file=sys.stderr)
            return 15

        # 2) 原子激活
        self._activate(manifest["version"])
        self.save_state(current=manifest["version"],
                        previous=previous, backend=backend)
        self.report("FIRSTFRAME", {"phase": "active verification"})

        # 3) 激活后首帧 —— 失败必须回退
        active_root = os.path.realpath(self.current)
        rc2, active_receipt = self.run_firstframe(
            active_root, "active", backend, manifest)
        if rc2 != 0:
            if previous:
                # previous 是旧 symlink 的*相对目标*（如 versions/1.0.0），
                # 不能拿它当绝对路径重建；取版本名后重新指向。
                old_target = os.path.join("versions",
                                          os.path.basename(previous.rstrip("/")))
                tmp = os.path.join(self.install, "current.tmp")
                if os.path.lexists(tmp):
                    os.remove(tmp)
                os.symlink(old_target, tmp)
                os.replace(tmp, self.current)
                # 复验旧版仍可首帧，回退才算成功
                old_root = os.path.realpath(self.current)
                rc3, old_receipt = self.run_firstframe(
                    old_root, "active", backend,
                    {**manifest, "version": os.path.basename(old_root),
                     "build_id": "previous"})
                self.report("ROLLEDBACK", {
                    "message": f"active firstframe failed; restored {previous}",
                    "new_version_error": active_receipt.get("error"),
                    "rollback_verify_exit": rc3},
                    receipt=old_receipt if rc3 == 0 else None)
                print("ROLLED BACK to", previous, file=sys.stderr)
                return 16
            self.report("FAILED",
                        {"message": "active firstframe failed and no previous "
                                    "version exists to roll back"})
            return 17

        self.report("ACTIVE",
                    {"message": "delivered and first-frame verified",
                     "version": manifest["version"], "backend": backend,
                     "bytes": dl["bytes"], "resumes": dl["resumes"]},
                    receipt=active_receipt)
        print(f"[done] ACTIVE v{manifest['version']} backend={backend} "
              f"frame={active_receipt['frame_sha256'][:12]}")
        return 0


class NoUpdate(Exception):
    pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="WDeliver client updater")
    ap.add_argument("--server", default="http://127.0.0.1:8448")
    ap.add_argument("--device", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--install-dir", dest="install_dir", required=True)
    ap.add_argument("--data-dir", dest="data_dir", required=True)
    ap.add_argument("--cache-dir", dest="cache_dir", required=True)
    ap.add_argument("--state-dir", dest="state_dir", required=True)
    ap.add_argument("--home", default=None,
                    help="探测专用 HOME（缺省落到 state-dir/home，不污染真实 HOME）")
    ap.add_argument("--emulate-client", default=None,
                    help="模拟旧客户端版本（如 1.0.0），用于兼容窗口验收")
    ap.add_argument("--inject-future", action="store_true",
                    help="请求服务器注入未来字段（验收旧客户端忽略新字段）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("probe")
    sub.add_parser("update")
    args = ap.parse_args(argv)
    args.name = args.name or args.device
    u = Updater(args)
    if args.cmd == "probe":
        # 纯本地探测：服务端不可达也必须出能力结论（控制台补登记即可）。
        try:
            u.register()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[probe] register skipped: {exc}\n")
        rep = u.probe(post=False)
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return u.run()


if __name__ == "__main__":
    raise SystemExit(main())
