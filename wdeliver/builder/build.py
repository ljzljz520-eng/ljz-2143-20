#!/usr/bin/env python3
"""可重复构建入口。

产物：
  packages/<name>_<version>.wdz        确定性 tar.gz（固定 mtime/uid/gid/顺序）
  packages/<name>_<version>.build.json 构建记录（文件级 sha256、双指纹）

指纹：
  build_id       内容树哈希：只依赖声明文件的 (path, content)，
                 与打包时间无关 —— 两次构建 build_id 必须一致（可重复验证）。
  package_sha256 包文件哈希（归档层指纹，供清单 package_sha256 校验）。

绝对路径红线：打包前扫描所有文本文件，出现构建机家目录、/home/<user>、
/Users/<user>、构建机绝对 CWD 即拒绝；env shebang 白名单放行可移植路径。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tomllib  # Python 3.11+；3.8-3.10 见 README 备注
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from wdeliver.builder.png_to_ppm import convert  # noqa: E402

DEFAULT_EPOCH = 1_700_000_000  # 固定 SOURCE_DATE_EPOCH，保证可重复
TEXT_SUFFIXES = {".py", ".c", ".h", ".json", ".toml", ".md", ".txt", ".sh"}
SHEBANG_WHITELIST = ("/usr/bin/env", "/bin/sh", "/bin/bash", "/usr/bin/python3")
ABS_PATH_PATTERNS = [
    re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+"),
    re.compile(rb"/Users/[A-Za-z0-9._-]+"),
]


def scan_no_absolute_paths(root: str, rel_files: List[str]) -> List[str]:
    problems: List[str] = []
    home = os.path.expanduser("~").encode()
    cwd = os.path.realpath(os.getcwd()).encode()
    for rel in rel_files:
        full = os.path.join(root, rel)
        if os.path.splitext(rel)[1] not in TEXT_SUFFIXES:
            continue
        with open(full, "rb") as fh:
            data = fh.read()
        for line_no, line in enumerate(data.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(b"#!"):
                if not stripped.split()[0:2] == [b""] and not any(
                        stripped.startswith(("#!" + w).encode())
                        for w in SHEBANG_WHITELIST):
                    # 非白名单 shebang 且含真实绝对路径
                    if re.search(rb"^#!(/usr|/bin)/", stripped):
                        continue
            if home not in (b"/root", b"/") and home and home in line:
                problems.append(f"{rel}:{line_no}: contains build HOME {home!r}")
            if cwd and cwd in line:
                problems.append(f"{rel}:{line_no}: contains build CWD {cwd!r}")
            for pat in ABS_PATH_PATTERNS:
                if pat.search(line):
                    problems.append(
                        f"{rel}:{line_no}: developer absolute path matched "
                        f"{pat.pattern!r}: {line.strip()[:120]!r}")
    return sorted(set(problems))


def tree_hash(root: str, rel_files: List[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(rel_files):
        with open(os.path.join(root, rel), "rb") as fh:
            content = fh.read()
        h.update(rel.encode())
        h.update(b"\0")
        h.update(str(len(content)).encode())
        h.update(b"\0")
        h.update(hashlib.sha256(content).digest())
    return h.hexdigest()


def file_records(root: str, rel_files: List[str]) -> List[Dict[str, Any]]:
    recs = []
    for rel in sorted(rel_files):
        full = os.path.join(root, rel)
        h = hashlib.sha256()
        with open(full, "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        recs.append({"path": rel, "size": os.path.getsize(full),
                     "sha256": h.hexdigest()})
    return recs


def deterministic_tar_gz(root: str, rel_files: List[str], epoch: int) -> bytes:
    buf = io.BytesIO()
    # gzip 头中的 mtime 也清零，避免归档字节随构建时间变化。
    gz = gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0,
                       compresslevel=9)
    with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for rel in sorted(rel_files):
            full = os.path.join(root, rel)
            ti = tar.gettarinfo(full, arcname=rel)
            ti.mtime = epoch
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = 0o755 if rel.endswith(".py") and rel.startswith("hooks/") else 0o644
            with open(full, "rb") as fh:
                tar.addfile(ti, fh)
    gz.close()
    return buf.getvalue()


def records_from_archive(blob: bytes) -> List[Dict[str, Any]]:
    """从最终 .wdz 归档计算文件级记录（path/size/sha256），
    保证清单描述的就是实际交付字节，而不是 staging 源文件。"""
    import io
    import tarfile
    out: List[Dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isreg():
                continue
            f = tar.extractfile(m)
            content = f.read() if f else b""
            out.append({"path": m.name, "size": m.size,
                        "sha256": hashlib.sha256(content).hexdigest()})
    return sorted(out, key=lambda r: r["path"])


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="reproducible wdeliver build")
    ap.add_argument("--payload", default="deploy/payload")
    ap.add_argument("--source-png", default="assets/background.png")
    ap.add_argument("--out-dir", default="packages")
    ap.add_argument("--version", default="1.0.0")
    ap.add_argument("--epoch", type=int, default=DEFAULT_EPOCH)
    ap.add_argument("--bg-width", type=int, default=320)
    ap.add_argument("--bg-height", type=int, default=180)
    args = ap.parse_args()

    with open(os.path.join(args.payload, "payload.toml"), "rb") as fh:
        meta = tomllib.load(fh)
    name = meta["name"]
    rel_files = list(meta["files"])

    # 所有构建产物（PNG 转换、BUILD.json、归档）都在临时 staging 副本里完成，
    # 绝不改写源载荷树，保证重复构建互不污染、工作区保持干净。
    import tempfile
    staging = tempfile.mkdtemp(prefix="wdeliver-stage-")
    try:
        for rel in set(rel_files + ["payload.toml"]):
            src = os.path.join(args.payload, rel)
            dst = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        work_root = staging
        work_files = [f for f in rel_files]

        # 构建期资产转换：PNG -> PPM（缩小到声明尺寸）。
        conv = None
        if os.path.isfile(args.source_png):
            ppm_rel = "assets/background.ppm"
            ppm_full = os.path.join(work_root, ppm_rel)
            os.makedirs(os.path.dirname(ppm_full), exist_ok=True)
            conv = convert(args.source_png, ppm_full, args.bg_width,
                           args.bg_height)
            if ppm_rel not in work_files:
                work_files.append(ppm_rel)

        problems = scan_no_absolute_paths(work_root, work_files)
        if problems:
            print("BUILD REJECTED: developer absolute paths found:",
                  file=sys.stderr)
            for p in problems:
                print("  " + p, file=sys.stderr)
            return 2

        # BUILD.json 必须先落盘再算树哈希：版本号与 epoch 是构建身份的一部分，
        # 否则两个不同版本会得到相同 build_id（曾经踩过的坑）。
        with open(os.path.join(work_root, "BUILD.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"name": name, "version": args.version,
                       "source_date_epoch": args.epoch,
                       "built_by": "wdeliver-build"}, fh,
                      ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        work_files = sorted(set(work_files + ["BUILD.json"]))
        build_id = tree_hash(work_root, work_files)

        blob = deterministic_tar_gz(work_root, work_files, args.epoch)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    os.makedirs(args.out_dir, exist_ok=True)
    pkg_name = f"{name}_{args.version}.wdz"
    pkg_path = os.path.join(args.out_dir, pkg_name)
    with open(pkg_path, "wb") as fh:
        fh.write(blob)

    rec = {
        "name": name,
        "version": args.version,
        "build_id": build_id,
        "package": pkg_name,
        "package_size": len(blob),
        "package_sha256": hashlib.sha256(blob).hexdigest(),
        "source_date_epoch": args.epoch,
        "files": records_from_archive(blob),
        "asset_conversion": conv,
    }
    rec_path = os.path.join(args.out_dir,
                            f"{name}_{args.version}.build.json")
    with open(rec_path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps({"ok": True, "package": pkg_path,
                      "build_id": build_id[:12],
                      "package_sha256": rec["package_sha256"][:12],
                      "size": len(blob), "files": len(rec["files"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
