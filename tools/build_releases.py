#!/usr/bin/env python3
"""Build deterministic, manifest-signed release tarballs.

The tool supports two modes:
  * --mode fixture: build a tiny C window-delivery smoke fixture (no SDL/X).
  * --mode sdl: build the real SDL application in this repository.

The produced payload always contains POSIX-relative paths. Build-time absolute
paths are explicitly hashed and scanned. The manifest classifies bundled and
system components so the server can choose a suitable artifact.
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
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EPOCH = 1700000000


def run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> str:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise RuntimeError("command failed (%s):\n%s" % (" ".join(cmd), p.stdout))
    return p.stdout


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_digest(root: Path) -> str:
    """Hash deterministic inputs used by this repository build."""
    h = hashlib.sha256()
    tracked: List[Path] = []
    if (root / ".git").exists():
        out = run(["git", "ls-files"], cwd=root)
        tracked = [root / line for line in out.splitlines() if line]
    else:
        for rel in ("src", "tests/fixtures", "assets", "Makefile"):
            p = root / rel
            if p.is_file():
                tracked.append(p)
            elif p.is_dir():
                tracked.extend(x for x in sorted(p.rglob("*")) if x.is_file())
    # Include untracked working-tree inputs as well; release fingerprints must
    # reflect what was built, not only files already recorded in a commit.
    for rel in ("src", "tests/fixtures", "assets", "Makefile"):
        p = root / rel
        if p.is_file():
            tracked.append(p)
        elif p.is_dir():
            tracked.extend(x for x in sorted(p.rglob("*")) if x.is_file())
    tracked = sorted(set(tracked))
    for p in tracked:
        if p.is_file():
            rel = p.relative_to(root).as_posix().encode()
            h.update(rel + b"\0" + sha256_file(p).encode() + b"\n")
    return h.hexdigest()


def add_file(tf: tarfile.TarInfo, path: Path, arcname: str, mtime: int, mode: Optional[int] = None) -> None:
    data = path.read_bytes()
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mtime = mtime
    ti.mode = mode if mode is not None else (0o755 if path.stat().st_mode & 0o111 else 0o644)
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    tf.addfile(ti, __import__("io").BytesIO(data))


def add_tree(tf: tarfile.TarInfo, src: Path, prefix: str, mtime: int) -> None:
    for p in sorted(x for x in src.rglob("*") if x.is_file()):
        rel = p.relative_to(src)
        if ".." in rel.parts or rel.is_absolute():
            raise ValueError("unsafe payload path %s" % rel)
        add_file(tf, p, prefix + "/" + rel.as_posix(), mtime)


def ldd_lines(binary: Path) -> List[str]:
    try:
        out = subprocess.run(["ldd", str(binary)], text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=10).stdout
    except Exception:
        return []
    return out.splitlines()


def parse_ldd(binary: Path) -> List[Dict[str, str]]:
    libs = []
    for line in ldd_lines(binary):
        line = line.strip()
        m = re.match(r"\s*(\S+)\s+=>\s+(\S+)\s+\(0x[0-9a-fA-F]+\)", line)
        if m:
            libs.append({"name": m.group(1), "path": m.group(2)})
            continue
        m = re.match(r"\s*(\S+)\s+\(0x[0-9a-fA-F]+\)", line)
        if m and "linux-vdso" not in m.group(1):
            libs.append({"name": m.group(1), "path": ""})
    return libs


def classify_libs(binary: Path, bundled_names: Iterable[str]) -> List[Dict[str, str]]:
    bundled = set(bundled_names)
    result = []
    for lib in parse_ldd(binary):
        name = lib["name"]
        if name in bundled:
            policy = "bundled"
        elif name.startswith("libvdv-"):
            policy = "required"
        elif name.startswith(("libX", "libwayland", "libGL", "libEGL", "libdrm",
                             "libxcb", "libfontconfig", "libfreetype")):
            policy = "system-required"
        elif name.startswith("libSDL"):
            policy = "system-required"
        else:
            policy = "system"
        item = dict(lib)
        item["policy"] = policy
        result.append(item)
    return result


def sanitize_build_paths(libs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out = []
    for lib in libs:
        item = dict(lib)
        path = item.get("path", "")
        if "/payload/" in path:
            item["path"] = "<payload>/" + path.split("/payload/", 1)[1]
        elif "/system-build-libs/" in path:
            item["path"] = "<build-only>/" + path.split("/system-build-libs/", 1)[1]
        out.append(item)
    return out


def elf_contains(path: Path, needle: str) -> bool:
    data = path.read_bytes()
    return needle.encode() in data


def build_fixture(payload: Path, stage_root: Path, policy: str, mtime: int) -> Tuple[Path, List[Dict[str, str]]]:
    src = ROOT / "tests/fixtures"
    bin_dir = payload / "bin"
    lib_dir = payload / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir()
    binary = bin_dir / "window-app"
    greeter_src = src / "libvdv-greeter.c"
    app_src = src / "window-fixture.c"

    cflags = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
              "-ffile-prefix-map=%s=." % ROOT, "-fdebug-prefix-map=%s=." % ROOT]
    if policy == "bundled":
        lib = lib_dir / "libvdv-greeter.so.1"
        run(["gcc", *cflags, "-shared", "-Wl,-soname,libvdv-greeter.so.1",
             str(greeter_src), "-o", str(lib)])
        run(["gcc", *cflags, str(app_src), str(lib),
             "-Wl,-rpath,$ORIGIN/../lib", "-Wl,--enable-new-dtags", "-o", str(binary)])
    elif policy == "system":
        # Keep the build-only copy outside the payload. This artifact links to
        # a component that must be present on the target machine.
        sysdir = stage_root / "system-build-libs"
        sysdir.mkdir()
        real_lib = sysdir / "libvdv-greeter.so.1"
        run(["gcc", *cflags, "-shared", "-Wl,-soname,libvdv-greeter.so.1",
             str(greeter_src), "-o", str(real_lib)])
        (sysdir / "libvdv-greeter.so").symlink_to("libvdv-greeter.so.1")
        run(["gcc", *cflags, str(app_src), "-L", str(sysdir), "-lvdv-greeter",
             "-Wl,-rpath,/opt/visual-window/lib", "-o", str(binary)])
    else:
        raise ValueError(policy)
    return binary, sanitize_build_paths(
        classify_libs(binary, ["libvdv-greeter.so.1"] if policy == "bundled" else [])
    )


def build_sdl(stage: Path, policy: str, mtime: int) -> Tuple[Path, List[Dict[str, str]]]:
    if shutil.which("sdl2-config") is None:
        raise RuntimeError("sdl2-config not found; use --mode fixture in environments without SDL2")
    binary = stage / "bin/visual-window-app"
    binary.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(mtime)
    run(["make", "clean"], cwd=ROOT, env=env)
    run(["make", "visual-window-app"], cwd=ROOT, env=env)
    shutil.copy2(ROOT / "visual-window-app", binary)
    shutil.copytree(ROOT / "assets", stage / "assets")
    libs = classify_libs(binary, [])
    if policy == "bundled":
        if shutil.which("patchelf") is None:
            raise RuntimeError("bundled SDL build requires patchelf")
        lib_dst = stage / "lib"
        lib_dst.mkdir(exist_ok=True)
        copied = []
        wanted = ("libSDL2-", "libSDL2_image", "libpng", "libjpeg", "libwebp",
                  "libtiff", "libz.", "libbz2", "liblzma", "libzstd")
        for lib in libs:
            src = Path(lib["path"])
            if not src.exists() or not src.name.startswith(wanted):
                continue
            # Keep soname; a production build should preserve symlink chains.
            dst = lib_dst / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
                copied.append(dst.name)
        run(["patchelf", "--set-rpath", "$ORIGIN/../lib", str(binary)])
        libs = classify_libs(binary, copied)
    elif policy != "system":
        raise ValueError(policy)
    return binary, sanitize_build_paths(libs)


def make_artifact(args: argparse.Namespace, outdir: Path, policy: str,
                  mtime: int, font_file: Optional[Path]) -> Path:
    with tempfile.TemporaryDirectory(prefix="vdv-build-") as td:
        stage_root = Path(td)
        payload = stage_root / "payload"
        payload.mkdir()
        if args.mode == "fixture":
            binary, libs = build_fixture(payload, stage_root, policy, mtime)
            app_name = "window-app"
        else:
            binary, libs = build_sdl(payload, policy, mtime)
            app_name = "visual-window-app"

        fonts_manifest = []
        if font_file:
            fd = payload / "fonts"
            fd.mkdir(exist_ok=True)
            shutil.copy2(font_file, fd / font_file.name)
            fonts_manifest.append({
                "family": args.font_family,
                "style": "Regular",
                "file": "fonts/" + font_file.name,
                "sha256": sha256_file(font_file),
            })

        files = []
        for p in sorted(payload.rglob("*")):
            if p.is_file() or p.is_symlink():
                rel = p.relative_to(payload).as_posix()
                if p.is_symlink():
                    continue
                files.append({"path": rel, "sha256": sha256_file(p),
                              "size": p.stat().st_size, "mode": oct(p.stat().st_mode & 0o777)})

        cwd = str(ROOT)
        leaked = []
        for p in sorted(payload.rglob("*")):
            if p.is_file() and (p.name == binary.name or p.suffix == ".so" or ".so." in p.name):
                if elf_contains(p, cwd):
                    leaked.append(p.relative_to(payload).as_posix())

        payload_size = sum(x["size"] for x in files)
        requirements = {
            "os": "linux",
            "arch": platform.machine(),
            "display": args.requires_display,
            "graphicsBackend": ["x11", "wayland"] if args.requires_display else ["none"],
            "requiredFonts": [{"family": args.font_family}] if args.font_family else [],
            "minDiskBytes": payload_size + 8 * 1024 * 1024,
            "systemLibraries": [],
        }
        if args.mode == "fixture":
            requirements["systemLibraries"] = ["libvdv-greeter.so.1"] if policy == "system" else []
            requirements["fixture"] = True
        else:
            if policy == "system":
                requirements["systemLibraries"] = sorted(
                    l["name"] for l in libs if l["policy"] == "system-required" or l["name"].startswith("libSDL"))
            else:
                requirements["systemLibraries"] = ["X11/compositor", "fontconfig/freetype"]

        smoke_env = {"VDV_SMOKE_FRAMES": "1"}
        if args.fixture_fail_first_frame:
            smoke_env["VDV_FIXTURE_FAIL_FIRST_FRAME"] = "1"
        source_digest_value = source_digest(ROOT)
        manifest = {
            "schemaVersion": 2,
            "manifestFormat": "vdv.release.v2",
            "appId": args.app_id,
            "version": args.version,
            "channel": args.channel,
            "dependencyPolicy": policy,
            "createdAt": mtime,
            "build": {
                "mode": args.mode,
                "sourceDateEpoch": mtime,
                "sourceCommit": run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip()
                    if (ROOT / ".git").exists() else "unknown",
                "worktreeDigest": source_digest_value,
                "compiler": run(["gcc", "--version"]).splitlines()[0],
                "buildHost": "",
                "cwdRedacted": "<source-root>->.",
                "absolutePathLeak": leaked,
            },
            "payload": {
                "root": ".",
                "entrypoint": "bin/" + app_name,
                "firstFrame": {
                    "timeoutSeconds": args.first_frame_timeout,
                    "smokeEnv": smoke_env,
                    "markerEnv": "VDV_FIRST_FRAME_FILE",
                    "expectsWindow": args.requires_display,
                },
                "files": files,
                "payloadSize": payload_size,
            },
            "requirements": requirements,
            "fonts": fonts_manifest,
            "libraries": libs,
            "compatibility": {
                "minClientVersion": 1,
                "maxSchemaVersion": 2,
            },
        }
        # Digest inputs (not the digest itself) are kept explicit and reproducible.
        digest_input = json.dumps(
            {k: v for k, v in manifest.items() if k not in ("digest",)},
            sort_keys=True, separators=(",", ":")
        ).encode()
        manifest["digest"] = hashlib.sha256(digest_input).hexdigest()

        add_dir = stage_root / "add"
        add_dir.mkdir()
        (add_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")

        artifact = outdir / ("%s_%s_%s_linux_%s.tar" %
                             (args.app_id, args.version, args.channel, policy))
        with tarfile.open(artifact, "w", format=tarfile.USTAR_FORMAT) as tf:
            ti = tarfile.TarInfo("manifest.json")
            data = (add_dir / "manifest.json").read_bytes()
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(data), mtime, 0o644, 0, 0, "", ""
            tf.addfile(ti, __import__("io").BytesIO(data))
            add_tree(tf, payload, "payload", mtime)
        digest = sha256_file(artifact)
        sidecar = artifact.with_suffix(artifact.suffix + ".sha256")
        sidecar.write_text("%s  %s\n" % (digest, artifact.name))
        meta = artifact.with_suffix(".json")
        meta.write_text(json.dumps({
            "artifact": artifact.name,
            "sha256": digest,
            "size": artifact.stat().st_size,
            "version": args.version,
            "channel": args.channel,
            "dependencyPolicy": policy,
            "payloadSize": payload_size,
            "requirements": requirements,
            "digest": manifest["digest"],
            "absolutePathLeak": leaked,
        }, sort_keys=True, indent=2) + "\n")
        return artifact


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["fixture", "sdl"], default="fixture")
    p.add_argument("--app-id", default="visual-window")
    p.add_argument("--version", default=time.strftime("%Y.%m.%d.%H%M", time.gmtime()))
    p.add_argument("--channel", default="fixture")
    p.add_argument("--out", default="dist/release")
    p.add_argument("--policies", default="system,bundled")
    p.add_argument("--font-file")
    p.add_argument("--font-family")
    p.add_argument("--first-frame-timeout", type=int, default=15)
    p.add_argument("--requires-display", action="store_true")
    p.add_argument("--fixture-fail-first-frame", action="store_true",
                   help="compile fixture that exits before writing first-frame marker")
    p.add_argument("--source-date-epoch", type=int, default=DEFAULT_EPOCH)
    args = p.parse_args()

    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)
    font = Path(args.font_file).expanduser().resolve() if args.font_file else None
    if font and not font.exists():
        p.error("--font-file does not exist: %s" % font)
    outputs = []
    for policy in args.policies.split(","):
        if policy:
            outputs.append(make_artifact(args, outdir, policy, args.source_date_epoch, font))
    for x in outputs:
        print(x.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
