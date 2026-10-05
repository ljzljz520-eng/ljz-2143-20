#!/usr/bin/env python3
"""把本地构建产物发布到服务器：上传 .wdz 并登记构建指纹。"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from wdeliver.client import http as httpc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8448")
    ap.add_argument("--build-record", required=True,
                    help="builder 产出的 <name>_<version>.build.json")
    args = ap.parse_args()
    with open(args.build_record, encoding="utf-8") as fh:
        rec = json.load(fh)
    pkg_path = os.path.join(os.path.dirname(args.build_record) or ".",
                            rec["package"])
    with open(pkg_path, "rb") as fh:
        up = httpc.request("PUT",
                           f"{args.server}/api/packages/{rec['package']}",
                           data=fh.read(),
                           headers={"Content-Type": "application/gzip"})
    print("uploaded:", up)
    resp = httpc.request("POST", f"{args.server}/api/builds", data=rec)
    print("registered:", resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
