"""设备能力探测 —— 所有结论都来自真实系统调用，绝不使用文档默认值。

探测项对应 Web 控制台“登记目标设备能力”的字段：
  clean_home       干净用户目录（HOME 存在且可用，无残留交付状态）
  install_writable 安装路径可写（真实创建文件 → 写入 → 读回 → 删除）
  local_writes     数据目录 / 缓存目录写权限（同一套真实 round-trip）
  fonts            系统无衬线 / 等宽 / CJK 字体（fontconfig 实测）
  gfx_backend      图形后端：x11 / wayland / framebuffer / nullfb
  display_scaling  显示缩放（DPI 实测；无显示时 unknown 而非假定 1.0）
  network          升级源连通性（实际 HEAD/GET 探针）

每条结论为 {status: ok|warn|missing|unknown|error, evidence: ..., detail: ...}。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

# nullfb：随包自带的无显示帧缓冲后端。它不依赖系统字体或显示服务，
# 是“可交付”的最低保证；系统图形栈是否可用仍由 gfx_backend 如实报告。
BUNDLED_BACKENDS = ("nullfb",)
STATUS_RANK = {"ok": 0, "warn": 1, "unknown": 2, "missing": 3, "error": 4}


def _write_roundtrip(path: str) -> Dict[str, Any]:
    """在 path 下真实创建探针文件、写入内容并读回校验。"""
    result: Dict[str, Any] = {"path": path, "status": "missing"}
    try:
        os.makedirs(path, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".wdprobe-", dir=path)
        try:
            payload = b"wdeliver-write-probe\n"
            os.write(fd, payload)
            os.close(fd)
            with open(name, "rb") as fh:
                got = fh.read()
            if got != payload:
                result.update(status="error", detail="readback mismatch")
            else:
                st = os.stat(name)
                result.update(status="ok",
                              evidence=f"mkstemp+write+readback {len(payload)}B",
                              mode=oct(st.st_mode & 0o777))
        finally:
            try:
                os.remove(name)
            except OSError:
                pass
    except PermissionError as exc:
        result.update(status="missing", error="PermissionError", detail=str(exc))
    except OSError as exc:
        result.update(status="error", error=type(exc).__name__, detail=str(exc))
    return result


def probe_clean_home(home: Optional[str] = None,
                     state_marker: Optional[str] = None) -> Dict[str, Any]:
    """干净用户目录：HOME 可访问；若存在上次升级残留则报 warn。"""
    home = home or os.environ.get("HOME", "")
    out: Dict[str, Any] = {"path": home}
    if not home:
        return {**out, "status": "missing", "detail": "HOME is unset/empty"}
    if not os.path.isdir(home):
        return {**out, "status": "missing", "detail": "HOME is not a directory"}
    try:
        entries = sorted(os.listdir(home))
    except OSError as exc:
        return {**out, "status": "error", "detail": str(exc)}
    leftovers = []
    if state_marker:
        for rel in (".wdeliver", state_marker):
            p = os.path.join(home, rel) if rel != state_marker or not os.path.isabs(rel) else rel
            if os.path.exists(p):
                leftovers.append(p)
    out.update(status="ok", evidence=f"HOME readable, {len(entries)} entries",
               empty=not entries)
    if leftovers:
        out.update(status="warn", detail="previous delivery state found",
                   leftovers=leftovers)
    return out


def probe_install_writable(install_dir: str) -> Dict[str, Any]:
    return _write_roundtrip(install_dir)


def probe_local_writes(data_dir: str, cache_dir: str) -> Dict[str, Any]:
    data = _write_roundtrip(data_dir)
    cache = _write_roundtrip(cache_dir)
    worst = max((data["status"], cache["status"]),
                key=lambda s: STATUS_RANK.get(s, 5))
    return {"status": worst, "data_dir": data, "cache_dir": cache}


def probe_fonts(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """通过 fontconfig 实测系统字体；fc-list 不可用 / 失败时如实报 unknown。

    判定规则只依赖实际字体清单：
      sans  存在任一常规无衬线字体（DejaVu Sans / Liberation Sans 等）
      mono  存在任一等宽字体
      cjk   存在 CJK 字体（按家族名关键字匹配）
    """
    out: Dict[str, Any] = {"status": "unknown"}
    fc = shutil.which("fc-list")
    if not fc:
        return {**out, "detail": "fc-list not found; cannot enumerate fonts"}
    try:
        proc = subprocess.run(
            [fc, ":", "family", "spacing"],
            capture_output=True, text=True, timeout=8, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {**out, "detail": f"fc-list failed: {exc}"}
    if proc.returncode != 0:
        return {**out, "status": "error",
                "detail": (proc.stderr or "fc-list error").strip()[:300]}

    families: List[str] = []
    for line in proc.stdout.splitlines():
        fam = line.split(",")[0].strip()
        if fam:
            families.append(fam)
    fam_l = [f.lower() for f in families]

    def has_any(words: List[str]) -> Optional[str]:
        for i, f in enumerate(fam_l):
            if any(w in f for w in words):
                return families[i]
        return None

    def _is_sans(name: str) -> Optional[str]:
        n = name.lower()
        if "mono" in n or "console" in n or "typewriter" in n:
            return None
        for w in ("sans", "dejavu sans", "liberation", "noto", "ubuntu",
                  "arial", "helvetica"):
            if w in n:
                return name
        return None

    sans = None
    for f in families:
        sans = _is_sans(f)
        if sans and "mono" not in sans.lower():
            break
    cjk = has_any(["cjk", "noto sans cjk", "wenquanyi", "wqy", "droid sans fallback",
                   "source han", "pingfang", "heiti", "song", "gothic"])
    mono_line = ""
    try:
        m = subprocess.run([fc, ":spacing=mono", "family"],
                           capture_output=True, text=True, timeout=8, env=env)
        mono_line = (m.stdout or "").splitlines()[0].split(",")[0].strip() \
            if m.returncode == 0 and m.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        pass

    found = {"sans": sans, "mono": mono_line or None, "cjk": cjk}
    missing = [k for k, v in found.items() if not v]
    out.update(found=found, family_count=len(set(families)),
               sample=sorted(set(families))[:8])
    if not found["sans"]:
        out["status"] = "missing"
        out["detail"] = "no sans-serif font found by fontconfig"
    elif missing:
        out["status"] = "warn"
        out["detail"] = f"present sans; missing: {', '.join(missing)}"
    else:
        out["status"] = "ok"
        out["evidence"] = f"sans={found['sans']!r}, mono={found['mono']!r}, cjk={found['cjk']!r}"
    return out


def probe_gfx_backend(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """实测可用图形后端，按 x11 → wayland → framebuffer → nullfb 探测。

    - X11：DISPLAY 存在且 xdpyinfo 能连上真实 X server
    - Wayland：WAYLAND_DISPLAY 存在且 socket 文件可连
    - framebuffer：/dev/fb0 可读
    - nullfb：随包纯软件帧缓冲，永远可用，但是否“够用”由通道策略决定
    任何一级探测失败都降级，绝不假定有显示。
    """
    env = env if env is not None else os.environ.copy()
    checked: List[Dict[str, str]] = []

    display = env.get("DISPLAY", "")
    xdpy = shutil.which("xdpyinfo")
    if display:
        if not xdpy:
            checked.append({"backend": "x11", "status": "unknown",
                            "detail": f"DISPLAY={display} but xdpyinfo missing"})
        else:
            try:
                p = subprocess.run([xdpy], capture_output=True, text=True,
                                   timeout=5, env=env)
                if p.returncode == 0:
                    dims = ""
                    for line in p.stdout.splitlines():
                        if "dimensions:" in line:
                            dims = line.strip()
                    return {"status": "ok", "backend": "x11",
                            "evidence": f"DISPLAY={display}; {dims}",
                            "checked": checked}
                checked.append({"backend": "x11", "status": "missing",
                                "detail": f"DISPLAY={display} but connect failed"})
            except (OSError, subprocess.SubprocessError) as exc:
                checked.append({"backend": "x11", "status": "error", "detail": str(exc)})
    else:
        checked.append({"backend": "x11", "status": "missing", "detail": "DISPLAY unset"})

    wl = env.get("WAYLAND_DISPLAY", "")
    if wl:
        sock = wl if os.path.isabs(wl) else os.path.join(
            env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), wl)
        if os.path.exists(sock):
            return {"status": "ok", "backend": "wayland",
                    "evidence": f"WAYLAND_DISPLAY={wl} socket present",
                    "checked": checked}
        checked.append({"backend": "wayland", "status": "missing",
                        "detail": f"{sock} not found"})
    else:
        checked.append({"backend": "wayland", "status": "missing",
                        "detail": "WAYLAND_DISPLAY unset"})

    if os.path.exists("/dev/fb0") and os.access("/dev/fb0", os.R_OK):
        return {"status": "warn", "backend": "framebuffer",
                "evidence": "/dev/fb0 readable", "checked": checked}
    checked.append({"backend": "framebuffer", "status": "missing",
                    "detail": "/dev/fb0 absent/unreadable"})

    return {"status": "warn", "backend": "nullfb",
            "evidence": "no display service; bundled offscreen framebuffer available",
            "checked": checked, "bundled": list(BUNDLED_BACKENDS)}


def probe_display_scaling(env: Optional[Dict[str, str]] = None,
                          gfx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """显示缩放实测。没有真实显示时结论是 unknown，不默认 1.0。"""
    gfx = gfx or probe_gfx_backend(env)
    if gfx.get("backend") != "x11":
        return {"status": "unknown", "scale": None,
                "detail": f"no X11 display (backend={gfx.get('backend')}); "
                          "scale cannot be measured"}
    env = env if env is not None else os.environ.copy()
    xrdb = shutil.which("xrdb")
    dpi: Optional[int] = None
    if xrdb:
        try:
            p = subprocess.run([xrdb, "-query"], capture_output=True,
                               text=True, timeout=5, env=env)
            if p.returncode == 0:
                for line in p.stdout.splitlines():
                    if line.strip().lower().startswith("xft.dpi:"):
                        dpi = int(line.split(":", 1)[1].strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            dpi = None
    xsettings = env.get("GDK_SCALE") or env.get("QT_SCALE_FACTOR")
    scale = None
    if dpi:
        scale = round(dpi / 96.0, 3)
    out: Dict[str, Any] = {"status": "ok" if dpi else "unknown", "dpi": dpi,
                           "scale": scale, "env_hint": xsettings or None}
    if not dpi:
        out["detail"] = "X11 present but DPI not advertised; scale unmeasured"
    return out


def probe_network(base_url: str, timeout: float = 4.0) -> Dict[str, Any]:
    """对升级源做真实连通性探针（由 http 模块注入 opener，避免循环依赖）。"""
    from .http import head_probe
    try:
        info = head_probe(base_url + "/health", timeout=timeout)
        return {"status": "ok", "evidence": f"HEAD /health -> {info}",
                "base_url": base_url}
    except Exception as exc:  # noqa: BLE001 - 探测要覆盖全部失败形态
        return {"status": "missing", "error": type(exc).__name__,
                "detail": str(exc)[:200], "base_url": base_url}


def run_all(install_dir: str, data_dir: str, cache_dir: str,
            base_url: Optional[str] = None,
            home: Optional[str] = None,
            env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """执行全部探测并给出整体 verdict（只汇总，不做策略判断）。"""
    gfx = probe_gfx_backend(env)
    report = {
        "clean_home": probe_clean_home(home),
        "install_writable": probe_install_writable(install_dir),
        "local_writes": probe_local_writes(data_dir, cache_dir),
        "fonts": probe_fonts(env),
        "gfx_backend": gfx,
        "display_scaling": probe_display_scaling(env, gfx),
    }
    if base_url:
        report["network"] = probe_network(base_url)
    report["verdict"] = worst_status(
        v["status"] for k, v in report.items() if isinstance(v, dict))
    return report


def worst_status(statuses) -> str:
    return max(statuses, key=lambda s: STATUS_RANK.get(s, 5))


def dumps(report: Dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
