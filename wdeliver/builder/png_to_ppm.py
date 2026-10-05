"""极简 PNG 解码（标准库 zlib）+ 最近邻缩放 + P6 PPM 输出。

构建期工具，仅用于把随包背景从 1.7MB 的 RGBA PNG 转成小体积 PPM，
消除对目标机 libSDL2_image 的硬性依赖（依赖对比报告里给出数据）。
支持：8-bit，非隔行，color type 2(RGB)/6(RGBA)/0(灰度)。
"""
from __future__ import annotations

import struct
import zlib
from typing import List, Tuple


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(data: bytes) -> Tuple[int, int, List[Tuple[int, int, int]]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    idat = b""
    trns: Tuple[int, ...] = ()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = \
                struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"tRNS":
            trns = tuple(chunk)
        elif ctype == b"IEND":
            break
    if bit_depth != 8:
        raise ValueError(f"unsupported bit depth {bit_depth}")
    if interlace != 0:
        raise ValueError("interlaced PNG not supported")
    channels = {0: 1, 2: 3, 6: 4}.get(color_type)
    if not channels:
        raise ValueError(f"unsupported color type {color_type}")

    raw = zlib.decompress(idat)
    stride = width * channels
    pixels: List[Tuple[int, int, int]] = [(0, 0, 0)] * (width * height)
    prev = bytearray(stride)
    off = 0
    for y in range(height):
        ftype = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            x = line[i]
            if ftype == 1:
                x = (x + a) & 0xFF
            elif ftype == 2:
                x = (x + b) & 0xFF
            elif ftype == 3:
                x = (x + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                x = (x + _paeth(a, b, c)) & 0xFF
            line[i] = x
        prev = line
        for x in range(width):
            j = x * channels
            if channels == 1:
                v = line[j]
                alpha = 0 if trns and v == (trns[0] if trns else -1) else 255
                rgb = (v, v, v)
            elif channels == 3:
                rgb = (line[j], line[j + 1], line[j + 2])
                alpha = trns[line[j]] if trns and line[j] < len(trns) else 255
            else:
                rgb = (line[j], line[j + 1], line[j + 2])
                alpha = line[j + 3]
            if alpha != 255:  # 合成到黑色背景
                rgb = tuple(v * alpha // 255 for v in rgb)
            pixels[y * width + x] = rgb  # type: ignore[assignment]
    return width, height, pixels


def resize_nearest(src: List[Tuple[int, int, int]], sw: int, sh: int,
                   dw: int, dh: int) -> List[Tuple[int, int, int]]:
    out = [(0, 0, 0)] * (dw * dh)
    for y in range(dh):
        sy = min(sh - 1, y * sh // dh)
        for x in range(dw):
            sx = min(sw - 1, x * sw // dw)
            out[y * dw + x] = src[sy * sw + sx]
    return out


def encode_ppm(w: int, h: int, pixels: List[Tuple[int, int, int]]) -> bytes:
    out = bytearray(f"P6\n{w} {h}\n255\n".encode())
    for r, g, b in pixels:
        out += bytes((r, g, b))
    return bytes(out)


def convert(png_path: str, ppm_path: str, width: int, height: int) -> dict:
    with open(png_path, "rb") as fh:
        data = fh.read()
    sw, sh, px = decode_png(data)
    scaled = resize_nearest(px, sw, sh, width, height)
    encoded = encode_ppm(width, height, scaled)
    with open(ppm_path, "wb") as fh:
        fh.write(encoded)
    return {"src": png_path, "src_size": len(data), "src_dims": [sw, sh],
            "dst": ppm_path, "dst_size": len(encoded), "dst_dims": [width, height]}
