#!/usr/bin/env python3
"""Cross-machine upgrade agent for the C window application.

The agent never assumes display/font/write capabilities. Every decision is
based on a local probe. Downloads are not treated as completion: completion is
reported only after install and first-frame verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

CLIENT_VERSION = 2
APP_ID = "visual-window"
COMMON_FONTS = ["DejaVu Sans", "Liberation Sans", "Noto Sans CJK SC", "WenQuanYi Zen Hei", "Arial"]
COMMON_LIBS = ["libc.so.6", "libSDL2-2.0.so.0", "libSDL2_image-2.0.so.0",
               "libX11.so.6", "libfontconfig.so.1", "libfreetype.so.6",
               "libvdv-greeter.so.1"]


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run_tool(args: List[str], timeout: int = 6, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def check_writable(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": str(path), "exists": path.exists(), "writable": False}
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".vdv-write-", dir=str(path))
        os.write(fd, b"vdv write probe\n")
        os.close(fd)
        probe = Path(tmp)
        probe.unlink()
        info["writable"] = os.access(path, os.W_OK)
    except Exception as exc:
        info["writable"] = False
        info["error"] = "%s: %s" % (exc.__class__.__name__, exc)
    try:
        usage = shutil.disk_usage(path)
        info.update(totalBytes=usage.total, usedBytes=usage.used, freeBytes=usage.free)
    except Exception:
        pass
    return info


def probe_display(home: Path) -> Dict[str, Any]:
    display = os.environ.get("DISPLAY", "")
    x: Dict[str, Any] = {"available": False, "display": display, "reason": "", "commands": {}}
    backends: Dict[str, Any] = {}
    scale: Dict[str, Any] = {"scale": None, "dpi": None, "method": "unavailable"}

    x11_available = False
    if display:
        if display.startswith(":"):
            sock = Path("/tmp/.X11-unix/X" + display[1:].split(".")[0])
            x11_available = sock.exists()
        env = clean_env(home)
        rc, out, err = run_tool(["xdpyinfo"], env=env)
        x["commands"]["xdpyinfo"] = {"available": which("xdpyinfo") is not None, "returncode": rc}
        if rc == 0:
            x11_available = True
            dim = re.search(r"dimensions:\s+(\d+)x(\d+) pixels", out)
            res = re.search(r"resolution:\s+(\d+)x(\d+)", out)
            if dim:
                x["width"] = int(dim.group(1)); x["height"] = int(dim.group(2))
            if res:
                dpi = int(round((int(res.group(1)) + int(res.group(2))) / 2))
                scale["dpi"] = dpi
        else:
            x["reason"] = (err or out or "xdpyinfo failed").strip().splitlines()[0][:200]
    else:
        x["reason"] = "DISPLAY is not set"
    backends["x11"] = {
        "available": x11_available,
        "display": display,
        "socketProbed": True,
        "probe": "xdpyinfo/socket; never assumed from documentation",
    }
    x["available"] = x11_available

    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%s" % os.getuid())
    candidates = [Path(runtime) / wayland] if wayland else list(Path(runtime).glob("wayland-*"))
    wl = [str(p) for p in candidates if p.exists()]
    backends["wayland"] = {"available": bool(wl), "socket": wl[0] if wl else "",
                           "waylandDisplay": wayland, "probe": "XDG_RUNTIME_DIR socket"}

    # SDL preference is environment; actual usable display still comes from X/WL probes.
    sdl_driver = os.environ.get("SDL_VIDEODRIVER", "")
    if sdl_driver:
        backends["sdl_env"] = {"available": True, "driver": sdl_driver}

    if x11_available:
        env = clean_env(home)
        rc, out, err = run_tool(["xrandr"], env=env)
        x["commands"]["xrandr"] = {"available": which("xrandr") is not None, "returncode": rc}
        if rc == 0:
            m = re.search(r"connected(?: primary)? (\d+)x(\d+)\+\d+\+\d+.*?(\d+)mm x (\d+)mm", out)
            if m and int(m.group(3)) > 0:
                wpx, hpx, wmm, hmm = map(int, m.groups())
                dpi_x = wpx / (wmm / 25.4)
                dpi_y = hpx / (hmm / 25.4)
                dpi = int(round((dpi_x + dpi_y) / 2))
                scale["dpi"] = dpi
                scale["scale"] = round(max(dpi_x, dpi_y) / 96.0, 3)
                scale["method"] = "xrandr physical size"
                x["width"], x["height"] = wpx, hpx
            else:
                scale["method"] = "xrandr connected, physical size unavailable"
        if scale.get("dpi") is None:
            gdk = os.environ.get("GDK_SCALE") or os.environ.get("QT_SCALE_FACTOR")
            if gdk:
                try:
                    scale["scale"] = float(gdk); scale["method"] = "toolkit env"
                except ValueError:
                    pass
    return {**x, "backends": backends, "scale": scale}


def font_probe(required: List[str], bundled_files: List[Path]) -> Dict[str, Any]:
    fc_match = which("fc-match")
    fc_scan = which("fc-scan")
    available = set()
    evidence = {}
    for family in sorted(set(COMMON_FONTS + required)):
        if fc_match:
            rc, out, err = run_tool(["fc-match", "-f", "%{family[0]}", family], timeout=5)
            matched = out.strip()
            # fc-match always falls back, so require a useful prefix/family match.
            ok = rc == 0 and bool(matched) and matched.lower() != "sans" and (
                family.lower().startswith(matched.lower()) or matched.lower().startswith(family.lower().split()[0]))
            if ok:
                available.add(family)
            evidence[family] = {"matched": matched, "ok": ok, "error": err.strip()}
        else:
            evidence[family] = {"ok": False, "error": "fc-match not installed"}
    bundled = []
    for f in bundled_files:
        item = {"file": str(f), "exists": f.exists(), "family": None, "usable": False}
        if f.exists():
            item["size"] = f.stat().st_size
            if fc_scan:
                rc, out, err = run_tool(["fc-scan", "--format", "%{family[0]}", str(f)], timeout=5)
                item["family"] = out.strip() or None
                item["usable"] = rc == 0 and bool(out.strip())
                item["error"] = err.strip()
            else:
                item["error"] = "fc-scan unavailable; file present but font usability not proven"
        bundled.append(item)
        if item.get("usable") and item.get("family"):
            available.add(item["family"])
    missing = [x for x in required if x not in available]
    return {
        "availableFamilies": sorted(available), "requiredFamilies": required,
        "missing": missing, "bundled": bundled,
        "probe": "fc-match/fc-scan and file existence",
        "conclusion": "all required fonts observed" if not missing else "missing: " + ", ".join(missing),
    }


def library_probe(required: List[str]) -> Dict[str, Any]:
    ldconfig = which("ldconfig") or ("/sbin/ldconfig" if Path("/sbin/ldconfig").exists() else None)
    if ldconfig:
        rc, out, err = run_tool([ldconfig, "-p"], timeout=8)
    else:
        rc, out, err = 127, "", "ldconfig not installed or outside PATH"
    table: Dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            m = re.match(r"\s*(\S+)\s+\((?:[^)]*)\)\s*=>\s*(.+)$", line)
            if m:
                table[m.group(1)] = m.group(2).strip()
    names = sorted(set(COMMON_LIBS + required))
    available = [n for n in names if n in table]
    missing = [n for n in required if n not in table]
    return {"available": available, "missing": missing, "ldconfigAvailable": rc == 0,
            "error": err.strip() if rc else "", "required": required,
            "conclusion": "required system components observed" if not missing else "missing: " + ", ".join(missing)}


def clean_env(home: Path) -> Dict[str, str]:
    # Construct instead of inheriting a developer/user environment.
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "LOGNAME": os.environ.get("LOGNAME", "vdv"),
        "USER": os.environ.get("USER", "vdv"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/run/user/%s" % os.getuid()),
    }
    for k in ("DISPLAY", "WAYLAND_DISPLAY", "SDL_VIDEODRIVER", "GDK_SCALE", "QT_SCALE_FACTOR",
              "LD_LIBRARY_PATH", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def probe(home: Path, install_root: Path, requirements: Optional[Dict[str, Any]] = None,
          bundled_fonts: Optional[List[Path]] = None) -> Dict[str, Any]:
    requirements = requirements or {}
    req_fonts = []
    for f in requirements.get("requiredFonts", []):
        req_fonts.append(f.get("family") if isinstance(f, dict) else str(f))
    req_libs = list(requirements.get("systemLibraries", []))
    home.mkdir(parents=True, exist_ok=True)
    cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    info = platform.platform()
    uname = platform.uname()
    return {
        "schemaVersion": 2,
        "probeTime": int(time.time()),
        "os": {"kernel": uname.release, "system": uname.system, "arch": uname.machine,
               "platform": info, "glibc": ".".join(map(str, platform.libc_ver()[1:])) if platform.libc_ver()[0] else None},
        "network": {"hostname": socket.gethostname(), "serverReachability": "checked during HTTP calls"},
        "display": probe_display(home),
        "fonts": font_probe(req_fonts, bundled_fonts or []),
        "libraries": library_probe(req_libs),
        "paths": {
            "home": check_writable(home),
            "installRoot": check_writable(install_root),
            "cache": check_writable(cache),
            "state": check_writable(state),
        },
        "requirementsProbed": requirements,
    }


class Reporter:
    def __init__(self, server: str, device_id: str, client_version: int, state_dir: Path) -> None:
        self.server = server.rstrip("/")
        self.device_id = device_id
        self.client_version = client_version
        self.state_dir = state_dir
        self.events: List[Dict[str, Any]] = []

    def report(self, phase: str, status: str, detail: Dict[str, Any]) -> Dict[str, Any]:
        event = {"deviceId": self.device_id, "phase": phase, "status": status,
                 "detail": detail, "clientVersion": self.client_version, "time": int(time.time())}
        self.events.append(event)
        atomic_write(self.state_dir / "events.json", jdump(self.events))
        try:
            req = urllib.request.Request(self.server + "/api/agent/reports",
                                         data=json.dumps(event).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            event["deliveryError"] = str(exc)
            atomic_write(self.state_dir / "events.json", jdump(self.events))
            return {"accepted": False, "error": str(exc)}


def http_json(url: str, protocol: int) -> Dict[str, Any]:
    sep = "&" if "?" in url else "?"
    full = url + "%sprotocol=%d" % (sep, protocol)
    with urllib.request.urlopen(full, timeout=15) as resp:
        return json.loads(resp.read().decode())


def download_resumable(url: str, dest: Path, expected_size: Optional[int],
                       reporter: Reporter, fail_first_after: int = 0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        attempt += 1
        have = dest.stat().st_size if dest.exists() else 0
        if expected_size and have >= expected_size:
            return
        headers = {}
        full_url = url
        if attempt == 1 and fail_first_after:
            full_url += ("&" if "?" in full_url else "?") + "testAbortAfter=" + str(fail_first_after)
        if have:
            headers["Range"] = "bytes=%d-" % have
        reporter.report("downloading", "info",
                        {"attempt": attempt, "bytesDownloaded": have,
                         "expectedBytes": expected_size, "resumed": bool(have)})
        try:
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                total_header = resp.headers.get("content-range", "")
                with dest.open(mode) as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if have % (1024 * 1024) < 64 * 1024:
                            reporter.report("downloading", "progress",
                                            {"bytesDownloaded": have, "expectedBytes": expected_size,
                                             "attempt": attempt})
                # A deliberate first-attempt socket close is expected and resumable.
                if attempt == 1 and fail_first_after and not (expected_size and have >= expected_size):
                    raise ConnectionError("simulated network interruption after %d bytes" % have)
                if expected_size and dest.stat().st_size != expected_size:
                    if attempt < 5:
                        continue
                    raise IOError("short download: %d != %s" % (dest.stat().st_size, expected_size))
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            reporter.report("download_interrupted", "warning",
                            {"attempt": attempt, "bytesDownloaded": dest.stat().st_size if dest.exists() else 0,
                             "error": str(exc)})
            if attempt >= 5:
                raise
            time.sleep(min(attempt, 3))


def safe_extract(tar_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf.getmembers():
            p = (target / m.name).resolve()
            if p != target_resolved and target_resolved not in p.parents:
                raise RuntimeError("unsafe archive member: %s" % m.name)
            if m.issym() or m.islnk():
                raise RuntimeError("archive link members are rejected: %s" % m.name)
        tf.extractall(target)


def verify_payload(root: Path, manifest: Dict[str, Any]) -> List[str]:
    errors = []
    listed = {f["path"]: f for f in manifest.get("payload", {}).get("files", [])}
    seen = set()
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        seen.add(rel)
        item = listed.get(rel)
        if not item:
            errors.append("unlisted payload file: " + rel)
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        if h != item.get("sha256"):
            errors.append("sha256 mismatch: " + rel)
        mode = f.stat().st_mode & 0o777
        if item.get("mode") and oct(mode) != item["mode"]:
            errors.append("mode mismatch: %s %s!=%s" % (rel, oct(mode), item["mode"]))
    missing = sorted(set(listed) - seen)
    errors.extend("missing payload file: " + x for x in missing)
    return errors


def first_frame(root: Path, manifest: Dict[str, Any], home: Path, timeout: Optional[int] = None) -> Dict[str, Any]:
    entry = root / manifest["payload"]["entrypoint"]
    ff = manifest.get("payload", {}).get("firstFrame", {})
    timeout = int(timeout or ff.get("timeoutSeconds", 15))
    marker = root / "first-frame-result.json"
    if marker.exists():
        marker.unlink()
    env = clean_env(home)
    for k, v in (ff.get("smokeEnv") or {}).items():
        env[k] = str(v)
    env[ff.get("markerEnv", "VDV_FIRST_FRAME_FILE")] = str(marker)
    if (root / "lib").exists():
        env["LD_LIBRARY_PATH"] = str(root / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    start = time.time()
    proc = subprocess.Popen([str(entry)], cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while time.time() - start < timeout:
        if marker.exists():
            try:
                result = json.loads(marker.read_text())
                result["elapsedSeconds"] = round(time.time() - start, 3)
                result["expectsWindow"] = ff.get("expectsWindow", False)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                return result
            except json.JSONDecodeError:
                pass
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    out, err = proc.communicate(timeout=2)
    if proc.poll() is None:
        proc.kill(); out, err = proc.communicate(timeout=2)
    return {"ok": False, "fixture": bool(manifest.get("requirements", {}).get("fixture")),
            "elapsedSeconds": round(time.time() - start, 3), "exitCode": proc.returncode,
            "stdout": out[-1000:], "stderr": err[-1000:],
            "error": "timed out waiting for first-frame marker" if proc.returncode is None else "process exited before first frame"}


def preflight(probe_result: Dict[str, Any]) -> List[str]:
    errors = []
    for name in ("home", "installRoot", "cache", "state"):
        p = probe_result["paths"].get(name, {})
        if not p.get("writable"):
            errors.append("%s is not writable: %s" % (name, p.get("error", "unknown")))
    if probe_result.get("requirementsProbed", {}).get("display") and not probe_result["display"].get("available"):
        errors.append("display required but unavailable: " + probe_result["display"].get("reason", "unknown"))
    if probe_result["fonts"].get("missing"):
        errors.append("font preflight failed: " + ", ".join(probe_result["fonts"]["missing"]))
    if probe_result["libraries"].get("missing"):
        errors.append("system library preflight failed: " + ", ".join(probe_result["libraries"]["missing"]))
    return errors


def local_preflight_with_payload(probe_result: Dict[str, Any], manifest: Dict[str, Any], root: Path) -> List[str]:
    errors = preflight(probe_result)
    bundled_fonts = [root / f.get("file", "") for f in manifest.get("fonts", []) if f.get("file")]
    fp = font_probe([], bundled_fonts)
    for item in fp["bundled"]:
        if item.get("exists") and not item.get("usable"):
            # A bundled font file must be validated; its mere presence is not enough.
            errors.append("bundled font not usable: %s (%s)" % (item.get("file"), item.get("error", "unknown")))
    return errors


def cmd_probe(args: argparse.Namespace) -> int:
    req = {}
    if args.requirements:
        req = json.loads(Path(args.requirements).read_text())
    fonts = [Path(x) for x in (args.bundled_font or [])]
    result = probe(Path(args.home), Path(args.install_root), req, fonts)
    out = Path(args.output) if args.output else None
    text = jdump(result) + "\n"
    if out:
        atomic_write(out, text)
    print(text)
    return 1 if preflight(result) else 0


def replace_symlink(link: Path, target: Path) -> None:
    tmp = link.with_name(link.name + ".next")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target.resolve())
    tmp.replace(link)


def rollback_old(install_root: Path, home: Path, reporter: Reporter) -> bool:
    current = install_root / "current"
    if not current.is_symlink():
        reporter.report("rollback_complete", "success", {"state": "clean/no previous release"})
        return True
    old = current.resolve()
    manifest_path = old.parent / "manifest.json"
    if not manifest_path.exists():
        reporter.report("rollback_failed", "failure", {"error": "previous manifest missing", "path": str(old)})
        return False
    manifest = json.loads(manifest_path.read_text())
    res = first_frame(old, manifest, home)
    if res.get("ok"):
        reporter.report("rollback_complete", "success", {"path": str(old), "firstFrame": res})
        return True
    reporter.report("rollback_failed", "failure", {"path": str(old), "firstFrame": res})
    return False


def cmd_upgrade(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    install_root = Path(args.install_root).resolve()
    state_dir = home / ".vdv"
    state_dir.mkdir(parents=True, exist_ok=True)
    reporter = Reporter(args.server, args.device_id, args.protocol, state_dir)
    try:
        assignment = http_json("%s/api/agent/devices/%s/assignment?app=%s" %
                               (args.server.rstrip("/"), urllib.parse.quote(args.device_id), APP_ID),
                               args.protocol)
    except Exception as exc:
        reporter.report("assignment_failed", "failure", {"error": str(exc)})
        return 3
    if assignment.get("action") != "upgrade":
        reporter.report("blocked", "info", assignment)
        print(jdump(assignment))
        return 0
    # Both v1 base fields and v2 extension fields can be present. Old v1 clients
    # ignore extensions instead of rejecting the response.
    ext = assignment.get("extensions") or {}
    manifest = assignment.get("manifest") or {}
    requirements = ext.get("requirements") or manifest.get("requirements") or {}
    probe_result = probe(home, install_root, requirements)
    reporter.report("probe", "success", probe_result)
    errors = preflight(probe_result)
    if errors:
        reporter.report("preflight_failed", "failure", {"errors": errors, "probe": probe_result})
        print(jdump({"preflight": "failed", "errors": errors}))
        return 2
    reporter.report("preflight_passed", "success", {"summary": {
        "display": probe_result["display"]["available"],
        "fontCount": len(probe_result["fonts"]["availableFamilies"]),
        "installWritable": probe_result["paths"]["installRoot"]["writable"]}})

    cache = home / ".cache/vdv"
    tar = cache / ("release-%s.tar" % assignment["releaseId"])
    try:
        download_resumable(args.server.rstrip("/") + assignment["downloadUrl"], tar,
                           int(assignment["size"]), reporter, args.fail_first_after)
        artifact_hash = hashlib.sha256(tar.read_bytes()).hexdigest()
        if assignment.get("artifactSha256") and artifact_hash != assignment["artifactSha256"]:
            raise RuntimeError("artifact sha256 mismatch: %s != %s" %
                               (artifact_hash, assignment["artifactSha256"]))
        reporter.report("download_verified", "success", {
            "bytesDownloaded": tar.stat().st_size, "size": assignment["size"],
            "sha256": artifact_hash,
            "note": "package complete only; not an upgrade completion signal"})
        if getattr(args, "stop_after_download", False):
            print(jdump({"download": "verified", "installed": False, "complete": False}))
            return 0
    except Exception as exc:
        reporter.report("download_failed", "failure", {"error": str(exc)})
        return 4

    staging = install_root / "staging"
    release_dir = install_root / ("releases/%s-%s" % (assignment["version"], assignment["releaseId"]))
    try:
        if staging.exists(): shutil.rmtree(staging)
        if release_dir.exists(): shutil.rmtree(release_dir)
        reporter.report("installing", "info", {"from": str(tar), "to": str(release_dir)})
        safe_extract(tar, staging)
        extracted_manifest = json.loads((staging / "manifest.json").read_text())
        payload_errors = verify_payload(staging / "payload", extracted_manifest)
        if payload_errors:
            raise RuntimeError("; ".join(payload_errors[:20]))
        payload_errors = local_preflight_with_payload(probe_result, extracted_manifest, staging / "payload")
        if payload_errors:
            raise RuntimeError("; ".join(payload_errors))
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(release_dir)
        reporter.report("installed", "success", {"releaseId": assignment["releaseId"], "path": str(release_dir)})
    except Exception as exc:
        reporter.report("install_failed", "failure", {"error": str(exc)})
        rollback_old(install_root, home, reporter)
        if staging.exists(): shutil.rmtree(staging, ignore_errors=True)
        return 5

    result = first_frame(release_dir / "payload", manifest or extracted_manifest, home,
                         args.first_frame_timeout)
    if not result.get("ok"):
        reporter.report("first_frame_failed", "failure", result)
        reporter.report("rollback_started", "info", {"keptFailedRelease": str(release_dir)})
        ok = rollback_old(install_root, home, reporter)
        return 6 if ok else 7

    current = install_root / "current"
    previous = current.resolve() if current.is_symlink() else None
    replace_symlink(current, release_dir / "payload")
    reporter.report("first_frame_verified", "success", result)
    atomic_write(state_dir / "current.json", jdump({
        "releaseId": assignment["releaseId"], "version": assignment["version"],
        "policy": assignment["dependencyPolicy"], "path": str(current.resolve()),
        "previous": str(previous) if previous else None}))
    print(jdump({"upgrade": "complete", "firstFrame": result, "current": str(current.resolve())}))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve(); root = Path(args.install_root).resolve()
    reporter = Reporter(args.server, args.device_id, args.protocol, home / ".vdv")
    return 0 if rollback_old(root, home, reporter) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--home", required=True); p.add_argument("--install-root", required=True)
    p.add_argument("--requirements"); p.add_argument("--bundled-font", action="append")
    p.add_argument("--output"); p.set_defaults(func=cmd_probe)
    u = sub.add_parser("upgrade")
    u.add_argument("--server", default="http://127.0.0.1:18080")
    u.add_argument("--device-id", required=True); u.add_argument("--home", required=True)
    u.add_argument("--install-root", required=True); u.add_argument("--protocol", type=int, choices=[1, 2], default=2)
    u.add_argument("--fail-first-after", type=int, default=0)
    u.add_argument("--stop-after-download", action="store_true",
                   help="verify package bytes, then stop before install")
    u.add_argument("--first-frame-timeout", type=int)
    u.set_defaults(func=cmd_upgrade)
    r = sub.add_parser("rollback")
    r.add_argument("--server", default="http://127.0.0.1:18080")
    r.add_argument("--device-id", required=True); r.add_argument("--home", required=True)
    r.add_argument("--install-root", required=True); r.add_argument("--protocol", type=int, default=2)
    r.set_defaults(func=cmd_rollback)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
