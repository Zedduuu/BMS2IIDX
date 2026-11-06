"""S3P0 容器（IIDX 27+ 的原生音频格式）

实测结构（来自 30000/31001/32009 等现代曲目）：
  文件   = b"S3P0" + u32 条目数 + 条目数×(u32 off, u32 len) + 各条目 + 4 字节尾
  条目   = 32 字节 S3V0 头 + WMA(ASF) 载荷
  S3V0 头 = b"S3V0" + u32 filestart(=0x20) + u32 载荷长度 + u32 (未知，非 CRC) + 16 字节
  载荷   = 标准 WMAv2 / 44100Hz / 立体声（ffmpeg -c:a wmav2 可直接产出）
  条目 1 = 整首歌；条目 2..N = 0.2~0.4 秒的短音效
  采样号 N ↔ 第 N 个条目（1 基，与谱面 0x07/0x02 的 value 一致）

【参考实现 · 未接入流程】
本模块只用于记录 S3P0/S3V0 的结构与生成算法，**转换流程不会调用它**：
  · 目前所有歌曲的键音统一写 <id>.2dx（ADPCM，已真机验证）
  · 这里产出的 <id>.s3p 是另一种容器（WMAv2 载荷），游戏里 .s3p 优先于 .2dx，
    理论上音质更好，但尚未做过真机验证，所以没有接进 convert
如果以后要做"音质优先"的选项，从这里接：build_s3p() 产出容器字节，
写到 <sound>/<id>/<id>.s3p 即可；采样号与谱面 0x02/0x07 的 value 一一对应。
"""
from __future__ import annotations

from . import cli_log

import shutil
import struct
import tempfile
import wave
from pathlib import Path

RATE = 44100
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
S3V0_MAGIC = b"S3V0"
S3P0_MAGIC = b"S3P0"
HEADER_SIZE = 0x20


def encode_wma(raw_mono_s16: bytes, channels: int = 2, bitrate: str = "192k") -> bytes:
    """44.1k 单声道 s16 裸 PCM → WMA(ASF) 字节。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        pcm = td / "in.wav"
        n = len(raw_mono_s16) // 2
        s = struct.unpack("<%dh" % n, raw_mono_s16[: n * 2])
        with wave.open(str(pcm), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(RATE)
            if channels == 2:
                out = bytearray()
                for v in s:
                    out += struct.pack("<hh", v, v)
                w.writeframes(bytes(out))
            else:
                w.writeframes(raw_mono_s16)
        wma = td / "out.wma"
        cli_log.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(pcm),
             "-ar", str(RATE), "-ac", str(channels), "-c:a", "wmav2", "-b:a", bitrate,
             str(wma)],
            check=True, capture_output=True,
        )
        return wma.read_bytes()


def wma_of_file(src: Path, channels: int = 2, bitrate: str = "192k") -> bytes:
    """任意音频文件 → WMA 字节（一次 ffmpeg 调用，不做中转）。"""
    with tempfile.TemporaryDirectory() as td:
        wma = Path(td) / "out.wma"
        proc = cli_log.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-ar", str(RATE), "-ac", str(channels), "-c:a", "wmav2", "-b:a", bitrate,
             str(wma)],
            capture_output=True,
        )
        if proc.returncode != 0 or not wma.exists():
            raise RuntimeError(f"WMA 编码失败 {src.name}: {proc.stderr.decode('utf-8','replace')[:160]}")
        return wma.read_bytes()


def s3v0(payload: bytes, ident: int = 0) -> bytes:
    head = (S3V0_MAGIC
            + struct.pack("<I", HEADER_SIZE)
            + struct.pack("<I", len(payload))
            + struct.pack("<I", ident)
            + b"\x00" * 16)
    assert len(head) == HEADER_SIZE
    return head + payload


def build_s3p(payloads: list[bytes], idents: list[int] | None = None) -> bytes:
    """payloads[i] 是采样号 i+1 的 WMA 字节。"""
    count = len(payloads)
    blobs = [s3v0(p, (idents[i] if idents else 0)) for i, p in enumerate(payloads)]
    table_off = 8
    first = table_off + 8 * count
    offs, cur = [], first
    for blob in blobs:
        offs.append(cur)
        cur += len(blob)
    out = bytearray()
    out += S3P0_MAGIC
    out += struct.pack("<I", count)
    for off, blob in zip(offs, blobs):
        out += struct.pack("<II", off, len(blob))
    for blob in blobs:
        out += blob
    out += b"\x00" * 4          # 真实文件末尾也有 4 字节
    return bytes(out)


def inspect(path: Path) -> dict:
    """读回一个 s3p，用于自检。"""
    b = Path(path).read_bytes()
    assert b[:4] == S3P0_MAGIC, b[:8]
    count = struct.unpack_from("<I", b, 4)[0]
    table = [struct.unpack_from("<II", b, 8 + i * 8) for i in range(count)]
    info = {"count": count, "size": len(b), "entries": []}
    for i, (off, ln) in enumerate(table[:5], start=1):
        blob = b[off:off + ln]
        assert blob[:4] == S3V0_MAGIC, blob[:4]
        filestart, size, ident = struct.unpack_from("<III", blob, 4)
        info["entries"].append({"n": i, "off": off, "len": ln, "payload": size, "ident": ident})
    info["table_end"] = 8 + count * 8
    info["first_off"] = table[0][0] if table else None
    info["last_end"] = table[-1][0] + table[-1][1] if table else None
    return info
