"""omnimix 曲库头的透明混淆层

某些 omnimix 包会把 music_omni.bin 的**前 64 字节**用固定串 `subnimix` 异或，
所以文件头不是 `IIDX` —— 直接按明文解析会判成坏库。
这里统一提供 read/write：读到混淆的自动解开，写回时保持原样。
"""
from __future__ import annotations

import pathlib

OMNI_KEY = b"subnimix"
OMNI_MASK = 64


def _xor(raw: bytes) -> bytes:
    out = bytearray(raw)
    for i in range(min(OMNI_MASK, len(out))):
        out[i] ^= OMNI_KEY[i % len(OMNI_KEY)]
    return bytes(out)


def unmask(raw: bytes) -> bytes:
    return _xor(raw)


def mask(raw: bytes) -> bytes:
    return _xor(raw)


def is_masked(raw: bytes) -> bool:
    """头部不是 IIDX、但异或前 64 字节后是 IIDX → 混淆格式"""
    return (len(raw) >= OMNI_MASK and raw[:4] != b"IIDX"
            and unmask(raw)[:4] == b"IIDX")


def read_db(path) -> tuple[bytes, bool]:
    """读曲库 →（明文, 是否混淆）。不认识的格式抛 ValueError。"""
    p = pathlib.Path(path)
    raw = p.read_bytes()
    if raw[:4] == b"IIDX":
        return raw, False
    if is_masked(raw):
        return unmask(raw), True
    raise ValueError(f"不是 IIDX 曲库（头部是 {raw[:4]!r}）：{p}")


def write_db(path, plain: bytes, masked: bool) -> None:
    pathlib.Path(path).write_bytes(mask(plain) if masked else plain)
