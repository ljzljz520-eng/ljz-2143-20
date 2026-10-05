"""最小 HTTP 客户端：普通 JSON 请求 + 支持 Range 续传的分块下载。

“网络断在升级中途”不是模拟整个连接消失，而是：服务器在发送若干字节后
提前关闭连接（``?cut_after=``）。下载器发现 short read 后用
``Range: bytes=N-`` 从断点继续，直到实际字节数与清单 package_size 一致，
并以 sha256 最终背书。重试次数可配，超出即报错由上层回退/保持旧版。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urljoin


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.body = body


def request(method: str, url: str, *, data: Any = None,
            headers: Optional[Dict[str, str]] = None,
            timeout: float = 10.0) -> Any:
    body = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if isinstance(data, (bytes, bytearray)):
        body = bytes(data)  # 裸字节体（Content-Type 由调用方决定）
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise HTTPError(exc.code, exc.read().decode("utf-8", "replace"), url) from None
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype or raw[:1] in (b"{", b"["):
        return json.loads(raw.decode("utf-8"))
    return raw


def head_probe(url: str, timeout: float = 4.0) -> str:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return f"{resp.status} {resp.headers.get('Content-Length', '?')}B"


def append_params(url: str, **params) -> str:
    q = {k: v for k, v in params.items() if v is not None}
    if not q:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode(q)


def download(url: str, dest: str, expected_size: int,
             expected_sha256: str, *, max_resumes: int = 8,
             chunk_size: int = 65536, read_timeout: float = 15.0,
             extra_params: Optional[Dict[str, Any]] = None,
             on_progress=None) -> Dict[str, Any]:
    """可续传下载。返回 {resumes, short_reads, sha256, bytes}。

    - 已存在的部分文件会被复用（跨进程续传，模拟“重试升级”按钮）。
    - 每次 short read 退避后重连并带 Range；服务器若不支持 Range 会立刻暴露。
    - 最终必须同时满足字节数和 sha256，二者缺一即失败（上层绝不激活）。
    """
    if extra_params:
        url = append_params(url, **extra_params)
    resumes = 0
    short_reads = 0
    attempt = 0
    while True:
        attempt += 1
        have = _size(dest)
        headers = {}
        mode = "ab"
        if have:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=read_timeout) as resp:
                if have and resp.status != 206:
                    # 服务器无视 Range：重新完整下载，避免数据拼接损坏。
                    have, mode = 0, "wb"
                with open(dest, mode) as out:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        out.write(chunk)
                        have += len(chunk)
                        if on_progress:
                            on_progress(have, expected_size)
                        if expected_size and have > expected_size:
                            raise IOError(
                                f"download exceeded expected size {expected_size}")
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            short_reads += 1
            if resumes >= max_resumes:
                raise IOError(
                    f"download interrupted {resumes} times, giving up: {exc}") from None
            resumes += 1
            time.sleep(min(0.2 * resumes, 1.0))
            continue
        have = _size(dest)
        if have < expected_size:
            short_reads += 1
            if resumes >= max_resumes:
                raise IOError(
                    f"server ended transfer at {have}/{expected_size} bytes; "
                    f"resumed {resumes} times")
            resumes += 1
            time.sleep(min(0.2 * resumes, 1.0))
            continue
        if have > expected_size:
            raise IOError(f"size mismatch: {have} > {expected_size}")
        break

    sha = hashlib.sha256()
    with open(dest, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(blk)
    if sha.hexdigest() != expected_sha256:
        raise IOError(f"sha256 mismatch: {sha.hexdigest()} != {expected_sha256}")
    return {"resumes": resumes, "short_reads": short_reads,
            "sha256": sha.hexdigest(), "bytes": have, "attempts": attempt}


def _size(path: str) -> int:
    import os
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def join(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
