#!/usr/bin/env python3
"""随包携带依赖 vs 使用系统组件 —— 体积/兼容性对比与选择依据。

方法：所有“系统组件是否存在”的结论都来自真实探测（ldconfig / which /
fc-list / 探测数据），不写死“齐全”。输出 JSON + Markdown 表，
docs/deployment.md 的“依赖选择依据”一节直接引用该表。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

# 组件的“随包”成本来自真实包内文件统计，不拍脑袋。
BUNDLED_ITEMS = [
    ("nullfb 纯软件帧缓冲", "hooks/firstframe.py"),
    ("位图字体 3x5（44 字形）", "assets/fonts/bitmap_3x5.json"),
    ("PPM 背景（PNG 转换后）", "assets/background.ppm"),
]

# 候选系统组件：探测命令 + 若缺失的影响。
SYSTEM_CHECKS = [
    ("libSDL2", ["ldconfig", "-p"], "libsdl2-2.0"),
    ("libSDL2_image", ["ldconfig", "-p"], "libsdl2_image"),
    ("fontconfig (fc-list)", None, "fc-list"),
    ("X server 客户端工具 (xdpyinfo)", None, "xdpyinfo"),
    ("Xvfb 虚拟显示", None, "Xvfb"),
]


def file_size(root: str, rel: str) -> Optional[int]:
    p = os.path.join(root, rel)
    return os.path.getsize(p) if os.path.isfile(p) else None


def packaged_size(package_path: str, rel: str) -> Optional[int]:
    """该文件在压缩包内实际占用的字节数（tar member 头部+压缩流近似：
    用 gzip 压缩单文件内容估算，避免给出大于包体的虚假小计）。"""
    import gzip
    if not os.path.isfile(package_path):
        return None
    import tarfile
    with tarfile.open(package_path, "r:gz") as tar:
        for m in tar.getmembers():
            if m.name == rel:
                f = tar.extractfile(m)
                raw = f.read() if f else b""
                return len(gzip.compress(raw, compresslevel=9))
    return None


def check_system() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ldconfig_cache = ""
    if shutil.which("ldconfig"):
        try:
            ldconfig_cache = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True,
                timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            ldconfig_cache = ""
    for label, cmd, needle in SYSTEM_CHECKS:
        present = False
        evidence = ""
        if cmd == ["ldconfig", "-p"]:
            present = needle in ldconfig_cache
            evidence = "ldconfig -p" if present else "not in ldconfig cache"
        else:
            path = shutil.which(needle)
            present = path is not None
            evidence = path or f"{needle} not found in PATH"
        out.append({"component": label, "present": present, "evidence": evidence})
    # 字体实测（不能只看 fontconfig 在不在）。
    try:
        from wdeliver.client.probe import probe_fonts
        fonts = probe_fonts()
    except Exception:  # noqa: BLE001
        fonts = {"status": "unknown"}
    found = fonts.get("found") or {}
    out.append({"component": "系统 sans-serif 字体",
                "present": bool(found.get("sans")),
                "evidence": json.dumps(found or fonts,
                                       ensure_ascii=False)[:200]})
    return out


def analyze(extracted_payload: str, package_path: str,
                package_size: int) -> Dict[str, Any]:
    bundled: List[Dict[str, Any]] = []
    total_bundled = 0
    for label, rel in BUNDLED_ITEMS:
        size = packaged_size(package_path, rel)
        disk = file_size(extracted_payload, rel)
        if size is not None:
            total_bundled += size
        bundled.append({"item": label, "path": rel,
                        "packaged_size": size, "on_disk_size": disk,
                        "present": disk is not None})
    system = check_system()
    missing = [c["component"] for c in system if not c["present"]]
    return {
        "package_size": package_size,
        "bundled": bundled,
        "bundled_total": total_bundled,
        "system": system,
        "missing_on_this_host": missing,
        "recommendation": [
            "随包携带（包内实测体积见上）：nullfb 后端 + 位图字体 + PPM 背景，"
            "保证无显示/无字体/无 SDL2_image 的机器仍能完成首帧验证；",
            "系统组件（不占包体）：libSDL2 / X server / 系统字体 —— 真实窗口"
            "渲染依赖它们，安装前由 probe 实测；缺失则通道策略要求 sdl 时预检失败，"
            "允许 nullfb 时降级并在 receipt 如实标注；",
            "背景选用 PPM 而非 PNG：构建期转换，免除对 libSDL2_image 的硬依赖，"
            "包体由原图大小决定（见 asset_conversion 字段），代价是无透明/高压缩；",
            "不随包 libSDL2：单个 .so 约 MB 级且与 glibc/ABI 强耦合，随包体积大且"
            "跨发行版兼容性差；改为声明系统组件 + 实测门禁 + 回退后端。",
        ],
    }


def to_markdown(report: Dict[str, Any]) -> str:
    lines = ["# 依赖对比（随包 vs 系统）", "",
             f"安装包总体积：**{report['package_size']} 字节**", "",
             "## 随包携带项", "",
             "| 项目 | 包内路径 | 体积(字节) |", "|---|---|---:|"]
    for b in report["bundled"]:
        lines.append(f"| {b['item']} | `{b['path']}` | "
                     f"{b['packaged_size'] if b['packaged_size'] is not None else '缺失!'} |")
    lines += [f"| **随包小计** | | **{report['bundled_total']}** |", "",
              "## 系统组件（本机实测）", "",
              "| 组件 | 结论 | 证据 |", "|---|---|---|"]
    for c in report["system"]:
        lines.append(f"| {c['component']} | "
                     f"{'✅ 存在' if c['present'] else '❌ 缺失'} | "
                     f"{c['evidence']} |")
    lines += ["", "## 选择依据", ""]
    for i, r in enumerate(report["recommendation"], 1):
        lines.append(f"{i}. {r}")
    lines.append("")
    if report["missing_on_this_host"]:
        lines += ["> ⚠️ 本机缺失：" + "、".join(report["missing_on_this_host"])
                  + "。这些项由探测在目标机上重新实测，不在文档中默认齐全。", ""]
    return "\n".join(lines)


def _latest_package() -> str:
    if not os.path.isdir("packages"):
        return ""
    for name in sorted(os.listdir("packages"), reverse=True):
        if name.endswith(".wdz"):
            return os.path.join("packages", name)
    return ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", default="/tmp/extract",
                    help="解包后的载荷目录（构建验收时由测试脚本提供）")
    ap.add_argument("--package", default="")
    ap.add_argument("--json-out", default="packages/deps-report.json")
    ap.add_argument("--md-out", default="docs/deps-comparison.md")
    args = ap.parse_args(argv)
    package_size = os.path.getsize(args.package) if args.package and \
        os.path.isfile(args.package) else 0
    if not package_size:
        for name in sorted(os.listdir("packages")) if os.path.isdir("packages") else []:
            if name.endswith(".wdz"):
                package_size = os.path.getsize(os.path.join("packages", name))
                break
    report = analyze(args.payload, args.package or _latest_package(), package_size)
    os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
    with open(args.md_out, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(report))
    print(json.dumps({"ok": True, "package_size": package_size,
                      "bundled_total": report["bundled_total"],
                      "missing": report["missing_on_this_host"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    raise SystemExit(main())
