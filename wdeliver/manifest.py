"""发行清单协议与兼容窗口。

为什么需要“兼容窗口”而不是单一 JSON：
升级期间新服务器与旧客户端会同时在线。服务器必须能按客户端声明的协议版本
给出 *降级视图*；而新客户端接到旧服务器（或未来的新字段）时必须忽略未知键，
不能因为多一个字段就拒绝安装。真正不兼容时（``min_client_version`` 高于
客户端）由服务器返回 409 CLIENT_TOO_OLD，而不是让客户端猜。

已登记协议版本：
  1.0  初始字段：包地址 / 大小 / 摘要 / 文件清单
  1.1  新增 release_channel / rollout_batch / release_notes（灰度通道与批次）
  1.2  新增 requirements（实测门禁）/ fallback_on_fail / min_client_version
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, Optional

SUPPORTED_VERSIONS = ("1.0", "1.1", "1.2")
LATEST_VERSION = SUPPORTED_VERSIONS[-1]

# 每个版本相对上一版本新增的顶层字段。
# 注意：files 内部自 1.0 起就允许携带未知键，解析器一律忽略（前向兼容契约）。
_ADDED: Dict[str, frozenset] = {
    "1.0": frozenset({
        "manifest_version", "build_id", "version", "package_url",
        "package_size", "package_sha256", "files",
    }),
    "1.1": frozenset({"release_channel", "rollout_batch", "release_notes"}),
    "1.2": frozenset({"requirements", "fallback_on_fail", "min_client_version"}),
}

REQUIRED_MASTER_KEYS = frozenset({
    "build_id", "version", "package_url", "package_size",
    "package_sha256", "files",
})


def fields_for(version: str) -> frozenset:
    """返回某协议版本可见的全部顶层字段。"""
    if version not in _ADDED:
        raise ValueError(f"unknown manifest protocol version: {version!r}")
    keys = set()
    for ver in SUPPORTED_VERSIONS:
        keys |= _ADDED[ver]
        if ver == version:
            break
    return frozenset(keys)


def negotiate(client_versions: Iterable[str],
              server_versions: Iterable[str] = SUPPORTED_VERSIONS,
              ) -> Optional[str]:
    """挑选双方都支持的最高版本；无交集返回 None（调用方回 409/400）。"""
    server = set(server_versions)
    common = [v for v in SUPPORTED_VERSIONS
              if v in server and v in set(client_versions)]
    return common[-1] if common else None


def make_view(master: Dict[str, Any], version: str) -> Dict[str, Any]:
    """把完整清单裁剪成某协议版本的视图（新字段对旧客户端不可见）。"""
    missing = REQUIRED_MASTER_KEYS - set(master)
    if missing:
        raise ValueError(f"master manifest missing keys: {sorted(missing)}")
    if version not in _ADDED:
        raise ValueError(f"unknown manifest protocol version: {version!r}")
    view = {k: copy.deepcopy(master[k]) for k in fields_for(version) if k in master}
    view["manifest_version"] = version
    return view


class ManifestError(ValueError):
    """清单语义不合法（结构上合法但无法安全安装）。"""


def parse(data: Dict[str, Any], client_version: str = "1.0.0") -> Dict[str, Any]:
    """解析任意版本的清单。

    前向兼容契约：未知顶层键 / 未知文件键一律忽略并记录到 ``ignored_keys``，
    这样“旧客户端接到新字段”不会崩溃，只会按 1.0 的语义安装。
    结构硬错误（缺地址、摘要长度不对、files 不是列表）才抛 ManifestError。
    """
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")

    version = str(data.get("manifest_version", "1.0"))
    if version not in SUPPORTED_VERSIONS:
        # 未来版本：按“所见即已知”的宽松模式继续，未知键全部忽略。
        version = LATEST_VERSION

    known = fields_for(version)
    ignored = sorted(k for k in data if k not in known and k != "ignored_keys")

    out: Dict[str, Any] = {"manifest_version": version, "ignored_keys": ignored}

    def need_str(key: str) -> str:
        if key not in data or not isinstance(data[key], str) or not data[key]:
            raise ManifestError(f"field {key!r} must be a non-empty string")
        return data[key]

    out["build_id"] = need_str("build_id")
    out["version"] = need_str("version")
    out["package_url"] = need_str("package_url")
    out["package_sha256"] = need_str("package_sha256")

    if len(out["package_sha256"]) != 64:
        raise ManifestError("package_sha256 must be 64 hex chars (sha256)")
    if not isinstance(data.get("package_size"), int) or data["package_size"] < 0:
        raise ManifestError("package_size must be a non-negative integer")
    out["package_size"] = data["package_size"]

    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestError("files must be a non-empty list")
    clean_files = []
    for i, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            raise ManifestError(f"files[{i}] must be an object with a string path")
        cf = {"path": f["path"]}
        if isinstance(f.get("size"), int):
            cf["size"] = f["size"]
        if isinstance(f.get("sha256"), str) and len(f["sha256"]) == 64:
            cf["sha256"] = f["sha256"]
        cf["ignored_keys"] = sorted(
            k for k in f if k not in ("path", "size", "sha256"))
        clean_files.append(cf)
    out["files"] = clean_files

    if version >= "1.1":
        for k in ("release_channel", "rollout_batch", "release_notes"):
            if k in data:
                out[k] = data[k]

    if version >= "1.2":
        if "requirements" in data:
            req = data["requirements"]
            if not isinstance(req, dict):
                raise ManifestError("requirements must be an object")
            out["requirements"] = req
        out["fallback_on_fail"] = bool(data.get("fallback_on_fail", True))
        mcv = data.get("min_client_version")
        if mcv is not None:
            if not isinstance(mcv, str):
                raise ManifestError("min_client_version must be a string")
            out["min_client_version"] = mcv

    # 硬门禁：服务器声明本构建需要更新的客户端时，旧客户端必须停下，
    # 而不是带着残缺语义强行安装。
    if "min_client_version" in out and _vcmp(client_version, out["min_client_version"]) < 0:
        raise ManifestError(
            f"CLIENT_TOO_OLD: build requires client>={out['min_client_version']}, "
            f"this client is {client_version}")
    return out


def _vcmp(a: str, b: str) -> int:
    """按数字段比较版本号（1.10 > 1.9），非数字段按 0 处理。"""
    pa = [int(x) if x.isdigit() else 0 for x in a.split(".")]
    pb = [int(x) if x.isdigit() else 0 for x in b.split(".")]
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)
