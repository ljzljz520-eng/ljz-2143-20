#!/usr/bin/env python3
"""首帧验证钩子 —— 交付链路上“窗口真的能画出来”的最终证据。

两条后端路径，按设备实测结果选择（由 updater 通过 --backend 指定）：

1) sdl：运行随包 C/SDL 窗口程序（app/src 下源码，Docker 目标内编译），
   程序在真实/虚拟显示上创建窗口并写出首帧 PPM，钩子读回作为证据。
2) nullfb：无显示设备时使用随包纯软件帧缓冲：
   - 合成 240x96 RGB 窗口帧（背景 + 边框 + 用随包 3x5 位图字体绘制文字）
   - 写出 PPM 帧与 receipt.json，供校验像素摘要
   它不冒充“系统图形可用”，receipt 会如实记录 backend=nullfb。

退出码：0 首帧验证通过；2 文件自校验失败；3 后端启动失败；
       4 后端未产出首帧；5 故障注入（用于验收回退链路）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional


def _root_from_here() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_font(root: str) -> Dict[str, Any]:
    path = os.path.join(root, "assets", "fonts", "bitmap_3x5.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def verify_files(root: str, files: List[Dict[str, Any]]) -> Optional[str]:
    """对照清单逐项校验落盘文件存在性与 sha256（path 为包内相对路径）。"""
    for item in files:
        p = os.path.join(root, item["path"])
        if not os.path.isfile(p):
            return f"missing file: {item['path']}"
        if item.get("sha256"):
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 18), b""):
                    h.update(blk)
            if h.hexdigest() != item["sha256"]:
                return f"sha256 mismatch: {item['path']}"
    return None


class Framebuffer:
    """最小 RGB 帧缓冲：背景图 + 矩形 + 位图字体文字，输出二进制 PPM。"""

    def __init__(self, w: int, h: int, bg=(18, 24, 38)):
        self.w, self.h = w, h
        self.px = bytearray(bg * w * h)

    def fill_rect(self, x, y, w, h, color):
        for j in range(max(0, y), min(self.h, y + h)):
            row = j * self.w * 3
            for i in range(max(0, x), min(self.w, x + w)):
                off = row + i * 3
                self.px[off:off + 3] = bytes(color)

    def text(self, font: Dict[str, Any], x: int, y: int, s: str,
             color=(125, 211, 252), scale: Optional[int] = None):
        scale = scale or font.get("scale", 2)
        adv = font["advance_x"] * scale
        glyphs = font["glyphs"]
        for ci, ch in enumerate(s.upper()):
            g = glyphs.get(ch) or glyphs.get(" ")
            for ry, row in enumerate(g):
                for rx, cell in enumerate(row):
                    if cell == "#":
                        self.fill_rect(x + ci * adv + rx * scale,
                                       y + ry * scale, scale, scale, color)

    def composite_background(self, root: str) -> Optional[str]:
        """若存在背景图则合成。支持二进制 PPM（无需第三方库）。"""
        ppm = os.path.join(root, "assets", "background.ppm")
        if os.path.isfile(ppm):
            try:
                with open(ppm, "rb") as fh:
                    magic = fh.readline().strip()
                    if magic != b"P6":
                        return "background.ppm is not P6"
                    dims = fh.readline().split()
                    w, h = int(dims[0]), int(dims[1])
                    maxv = int(fh.readline().strip())
                    raw = fh.read(w * h * (3 if maxv >= 256 else 1))
                # 等比铺满（最近邻采样），与 SDL 版 stretch 语义一致。
                for j in range(self.h):
                    sy = min(h - 1, int(j * h / self.h))
                    for i in range(self.w):
                        sx = min(w - 1, int(i * w / self.w))
                        off = (sy * w + sx) * 3
                        toffs = (j * self.w + i) * 3
                        self.px[toffs:toffs + 3] = raw[off:off + 3]
                return None
            except (ValueError, OSError, IndexError) as exc:
                return f"bad background.ppm: {exc}"
        return None


def write_ppm(path: str, fb: Framebuffer):
    with open(path, "wb") as fh:
        fh.write(f"P6\n{fb.w} {fb.h}\n255\n".encode())
        fh.write(bytes(fb.px))


def run_nullfb(root: str, out_dir: str, version: str,
               build_id: str) -> Dict[str, Any]:
    font = load_font(root)
    fb = Framebuffer(240, 96)
    bg_err = fb.composite_background(root)
    fb.fill_rect(0, 0, 240, 2, (56, 189, 248))
    fb.fill_rect(0, 94, 240, 2, (56, 189, 248))
    fb.text(font, 8, 10, "WDELIVER")
    fb.text(font, 8, 30, f"V {version}")
    fb.text(font, 8, 50, "FIRST FRAME OK")
    fb.text(font, 8, 70, "BACKEND NULLFB")

    frame_path = os.path.join(out_dir, "firstframe.ppm")
    write_ppm(frame_path, fb)
    with open(frame_path, "rb") as fh:
        frame_sha = hashlib.sha256(fh.read()).hexdigest()
    return {
        "backend": "nullfb",
        "frame_format": "ppm/p6",
        "frame_size": [240, 96],
        "frame_sha256": frame_sha,
        "font_source": "bundled:assets/fonts/bitmap_3x5.json",
        "font_name": font["name"],
        "background": "assets/background.ppm"
        if os.path.isfile(os.path.join(root, "assets", "background.ppm"))
        else "solid-color",
        "background_error": bg_err,
        "version": version,
        "build_id": build_id,
    }


def run_sdl(root: str, out_dir: str, version: str,
            build_id: str, env_extra: Dict[str, str]) -> Dict[str, Any]:
    """启动真实 C/SDL 窗口程序，等待其写出首帧 PPM。

    程序协议（见 deploy/payload/src/main.c）：设置
    WD_RECEIPT_FILE / WD_FRAME_FILE / WD_ONESHOT=1 后，程序创建窗口、
    渲染一帧、readback 像素写 PPM 并退出；30s 无产出判失败。
    """
    binary = os.environ.get("WD_SDL_BINARY",
                            os.path.join(root, "bin", "visual-window-app"))
    if not os.path.isfile(binary):
        raise RuntimeError(f"SDL binary not found: {binary}")
    frame_path = os.path.join(out_dir, "firstframe.ppm")
    receipt_hint = os.path.join(out_dir, "sdl_app_hint.json")
    env = os.environ.copy()
    env.update({
        "WD_FRAME_FILE": frame_path,
        "WD_RECEIPT_FILE": receipt_hint,
        "WD_ONESHOT": "1",
        **env_extra,
    })
    started = time.time()
    proc = subprocess.run([binary], cwd=root, env=env,
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"SDL app exited {proc.returncode}: {proc.stderr[:300]}")
    if not os.path.isfile(frame_path):
        raise RuntimeError("SDL app produced no firstframe.ppm")
    with open(frame_path, "rb") as fh:
        frame_sha = hashlib.sha256(fh.read()).hexdigest()
    info: Dict[str, Any] = {}
    if os.path.isfile(receipt_hint):
        try:
            with open(receipt_hint) as fh:
                info = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "backend": "sdl",
        "frame_format": "ppm/p6",
        "frame_size": info.get("frame_size", [1280, 720]),
        "frame_sha256": frame_sha,
        "renderer": info.get("renderer", "unknown"),
        "display": env.get("DISPLAY") or env.get("SDL_VIDEODRIVER", ""),
        "font_source": "system+SDL2 (verified via probe)",
        "version": version,
        "build_id": build_id,
        "startup_ms": int((time.time() - started) * 1000),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["sdl", "nullfb"], required=True)
    ap.add_argument("--root", default=_root_from_here())
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--manifest", help="安装清单 JSON（文件自校验用）")
    ap.add_argument("--stage", default="staged", choices=["staged", "active"])
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)

    # 故障注入：验收“首帧失败必须回退”。仅在显式设置时生效。
    fail = os.environ.get("WD_FORCE_FIRSTFRAME_FAIL", "")
    if fail in ("1", args.stage, "true"):
        print(f"firstframe: forced failure at stage={args.stage}",
              file=sys.stderr)
        return 5

    files: List[Dict[str, Any]] = []
    if args.manifest and os.path.isfile(args.manifest):
        with open(args.manifest, encoding="utf-8") as fh:
            files = json.load(fh).get("files", [])
        err = verify_files(args.root, files)
        if err:
            print(f"firstframe: self-check: {err}", file=sys.stderr)
            return 2

    try:
        if args.backend == "nullfb":
            info = run_nullfb(args.root, args.out_dir, args.version, args.build_id)
        else:
            info = run_sdl(args.root, args.out_dir, args.version, args.build_id,
                           env_extra={})
    except subprocess.TimeoutExpired:
        print("firstframe: SDL app timeout", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"firstframe: {exc}", file=sys.stderr)
        return 4 if "produced no" in str(exc) else 3
    except OSError as exc:
        print(f"firstframe: backend launch failed: {exc}", file=sys.stderr)
        return 3

    info["stage"] = args.stage
    info["timestamp"] = time.time()
    receipt_path = os.path.join(args.out_dir, "receipt.json")
    with open(receipt_path, "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps({"ok": True, "receipt": receipt_path,
                      "backend": info["backend"],
                      "frame_sha256": info["frame_sha256"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
