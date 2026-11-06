"""音频打包：BMS 的采样文件 → IIDX 的 2DX9 容器

已由真实数据验证的事实：
  · 容器 = 0x48 字节头 + 偏移表(4×N) + N 个条目，条目头 24 字节 + 标准 RIFF/WAVE
  · 一个条目 = 一个采样，采样号 N 对应第 N 个条目（1 基），条目数必须覆盖最大采样号
  · 编码 = MS-ADPCM，44100Hz；单声道 block align 128/256/1024 在真实文件里都出现过
  · 预览音是独立的 `_pre.2dx`，10 秒整
"""
from __future__ import annotations

import pathlib

from . import cli_log

import hashlib
import shutil
import struct
import tempfile
import wave
from pathlib import Path

RATE = 44100
PREVIEW_SECONDS = 10.0
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
CACHE_DIR = Path(__file__).resolve().parent.parent / "work" / "audio_cache"
# 缓存版本号：编码参数变化时必须改它，否则会读到旧参数编出来的产物
CACHE_VER = "v2_"


class AudioError(RuntimeError):
    pass


def decode_to_pcm(src: Path, seconds: float | None = None) -> list[float]:
    """把任意采样文件解码成 44.1kHz 单声道 float 列表。"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if seconds:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += ["-i", str(src), "-ar", str(RATE), "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = cli_log.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioError(f"解码失败 {src.name}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    raw = proc.stdout
    n = len(raw) // 2
    return [v / 32768.0 for v in struct.unpack("<%dh" % n, raw[:n * 2])]


def _write_wav(path: Path, raw_mono: bytes, channels: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        if channels == 2:
            n = len(raw_mono) // 2
            s = struct.unpack("<%dh" % n, raw_mono[: n * 2])
            out = bytearray()
            for v in s:
                out += struct.pack("<hh", v, v)
            w.writeframes(bytes(out))
        else:
            w.writeframes(raw_mono)


def s16_to_adpcm_riff(raw: bytes, channels: int = 1, block_size: int | None = None) -> bytes:
    """44.1k 单声道 s16 裸 PCM → MS-ADPCM RIFF；channels=2 时复制成双声道。

    真实曲目几乎都是 44.1k / 2ch / block 256（mon/2dxTools 的 2dxWavConvert 里
    写明 channels "have to be 2 for later hacks to work"）。
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        pcm = td / "in.wav"
        _write_wav(pcm, raw, channels)
        out = td / "out.wav"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(pcm),
               "-ar", str(RATE), "-ac", str(channels), "-c:a", "adpcm_ms"]
        if block_size:
            cmd += ["-block_size", str(block_size)]
        cmd.append(str(out))
        cli_log.run(cmd, check=True, capture_output=True)
        return _clean_riff(out.read_bytes())


def pcm_to_adpcm_riff(samples: list[float], channels: int = 1,
                      block_size: int | None = None) -> bytes:
    """float 采样 → MS-ADPCM 的 RIFF/WAVE 字节（去掉 ffmpeg 附带的元数据块）。"""
    raw = b"".join(
        struct.pack("<h", int(max(-1.0, min(0.9999, s)) * 32767)) for s in samples)
    return s16_to_adpcm_riff(raw, channels=channels, block_size=block_size)


def _clean_riff(b: bytes) -> bytes:
    assert b[:4] == b"RIFF" and b[8:12] == b"WAVE", "不是 RIFF/WAVE"
    keep = []
    i = 12
    while i + 8 <= len(b):
        cid = b[i:i + 4]
        sz = struct.unpack_from("<I", b, i + 4)[0]
        body = b[i + 8:i + 8 + sz]
        if cid in (b"fmt ", b"fact", b"data"):
            keep.append((cid, body))
        i += 8 + sz + (sz & 1)
    ids = [c for c, _ in keep]
    if b"fmt " not in ids or b"data" not in ids:
        raise AudioError(f"编码结果缺少必要块: {ids}")
    body = b"".join(c + struct.pack("<I", len(v)) + v + (b"\0" if len(v) & 1 else b"")
                    for c, v in keep)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


# 真实 omni v30/31/32 曲目（30223/31252/31254…）的条目字段块：
# unk1=0x3231  trackId  unk2=64  attenuation=1  loop=0
#
# trackId 是关键（实测 31252：条目 1 = 0，其余 883 条 = 0xFFFF）：
#   trackId = 0..7  → 占用第 N 号音轨（同一音轨是单声道，新声音会掐断旧的）
#   trackId = 0xFFFF(-1) → 一次性音效，不占音轨，可以叠加，也不会掐断整曲流
# 所以：整曲流必须用 trackId=0，其余音效/键音必须用 0xFFFF。
FIELD_MAIN = bytes.fromhex("313200004000010000000000")
FIELD_FX = bytes.fromhex("3132ffff4000010000000000")
PREVIEW_TRACK_ID = 0xFFFF         # mon/2dxTools 2dx.h：preview 的 trackId 恒为 -1
ENC_BLOCK = 256


def field_block_for(track_id: int) -> bytes:
    """生成一个条目字段块：unk1=0x3231 / trackId / unk2=64 / attenuation=1 / loop=0。"""
    return (bytes.fromhex("3132") + struct.pack("<H", track_id & 0xFFFF)
            + bytes.fromhex("40000100") + b"\x00" * 4)


def build_container(entries: list[bytes], name: bytes = b"bms2iidx",
                    field_block: bytes | None = None,
                    main_index: int = 0,
                    per_entry_fields: list[bytes] | None = None) -> bytes:
    """按 2DX9 规则拼容器；entries[i] 是采样号 i+1 的 RIFF。

    默认：第 main_index 条（0 起，默认第 1 条 = 整曲流）用 trackId=0 占音轨，
    其余全部用 trackId=0xFFFF（一次性音效，不占音轨、不掐断整曲流）。
    这条规则是实测自 31252（条目 1 trackId=0，其余 883 条 0xFFFF）。
    """
    if per_entry_fields is not None:
        fields = per_entry_fields
    elif field_block is not None:
        fields = [field_block] * len(entries)          # 显式统一指定（对照实验用）
    else:
        fields = [FIELD_MAIN if i == main_index else FIELD_FX for i in range(len(entries))]
    for fb in fields:
        if len(fb) != 12:
            raise ValueError(f"entry field block must be 12 bytes, got {len(fb)}: {fb.hex(' ')}")
    count = len(entries)
    first = 0x48 + 4 * count
    head = bytearray(0x48)
    head[0:len(name)] = name[:0x0F]
    struct.pack_into("<I", head, 0x10, first)
    struct.pack_into("<I", head, 0x14, count)
    table = bytearray()
    blob = bytearray()
    cur = first
    for riff, fb in zip(entries, fields):
        table += struct.pack("<I", cur)
        entry = (b"2DX9" + struct.pack("<I", 0x18) + struct.pack("<I", len(riff))
                 + fb + riff)
        blob += entry
        cur += len(entry)
    return bytes(head) + bytes(table) + bytes(blob)


def _cache_key(src: Path) -> str:
    st = src.stat()
    h = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}|{RATE}".encode())
    return h.hexdigest()


def transcode_to_riff(src: Path, use_cache: bool = True, channels: int = 2,
                      block_size: int | None = 256) -> bytes:
    """一步到位：任意采样文件 → MS-ADPCM RIFF（单次 ffmpeg 调用）。带磁盘缓存。"""
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp = CACHE_DIR / (CACHE_VER + _cache_key(src) + f".{channels}ch.{block_size}.riff")
        if cp.exists():
            return cp.read_bytes()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.wav"
        # -threads 1：几百个小文件并行转码时，每个 ffmpeg 只占 1 个核，
        # 否则 N 个进程 × 全部核心会互相抢线程（总吞吐反而下降）
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-threads", "1", "-filter_threads", "1", "-i", str(src),
               "-ar", str(RATE), "-ac", str(channels), "-c:a", "adpcm_ms"]
        if block_size:
            cmd += ["-block_size", str(block_size)]
        cmd.append(str(out))
        proc = cli_log.run(cmd, capture_output=True)
        if proc.returncode != 0 or not out.exists():
            raise AudioError(f"转码失败 {src.name}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        riff = _clean_riff(out.read_bytes())
    if use_cache:
        try:
            cp.write_bytes(riff)
        except OSError:
            pass
    return riff


def _transcode_task(args):
    """模块级 worker（Windows spawn 需要可 pickle）。"""
    key, src, channels, block = args
    try:
        return key, transcode_to_riff(Path(src), channels=channels, block_size=block), None
    except AudioError as e:
        return key, None, str(e)[:160]


def transcode_many(items, channels: int = 2, block_size: int = 256,
                   workers: int | None = None, on_progress=None):
    """items: [(key, Path)] → {key: riff 或 None}，多核并行。

    on_progress(done, total)：分批上报，用来做细粒度进度条（每 40 个报一次）。
    """
    from .par import pmap
    items = list(items)
    tasks = [(k, str(p), channels, block_size) for k, p in items]
    out, errs = {}, {}
    CH = 40
    done = 0
    for i in range(0, len(tasks), CH):
        part = tasks[i:i + CH]
        for key, riff, err in pmap(_transcode_task, part, workers):
            out[key] = riff
            if err:
                errs[key] = err
        done += len(part)
        if on_progress:
            try:
                on_progress(done, len(tasks))
            except Exception:                                  # noqa: BLE001
                pass
    return out, errs


def build_keysound(samples: dict[int, Path], max_sample: int | None = None,
                   warnings: list | None = None, progress=None, channels: int = 2) -> tuple[bytes, dict]:
    """samples: 采样号 → 文件路径。返回 (容器字节, 统计)。缺失的采样号用静音补齐。"""
    warn = warnings if warnings is not None else []
    n = max(samples, default=0) if max_sample is None else max_sample
    if n <= 0:
        raise AudioError("没有任何采样")
    silence = pcm_to_adpcm_riff([0.0] * int(RATE * 0.05), channels=channels)
    entries: list[bytes] = []
    missing = 0
    total_bytes = 0
    for i in range(1, n + 1):
        src = samples.get(i)
        if src is None or not Path(src).exists():
            entries.append(silence)
            missing += 1
            if missing <= 5:
                why = "BMS 没定义这个音" if src is None else f"文件缺失 {pathlib.Path(src).name}"
                warn.append(f"采样 {i} {why}，已用静音补齐")
            continue
        try:
            riff = transcode_to_riff(Path(src), channels=channels)
        except AudioError as e:
            entries.append(silence)
            missing += 1
            warn.append(f"采样 {i} 解码失败 → 静音：{e}")
            continue
        entries.append(riff)
        total_bytes += len(riff)
        if progress:
            progress(i, n)
    return build_container(entries), {"samples": n, "missing": missing, "payload_bytes": total_bytes}


def preview_from_pcm(samples, seconds: float = PREVIEW_SECONDS) -> bytes:
    """从已混好的整曲 PCM 里截前 N 秒做预览（最可靠：与真正播出来的完全一致）。

    真实 `_pre.2dx` 是 2DX9 容器、立体声 44100、正好 10 秒、trackId = -1。

    ★ samples 是 int16 数组；编码器要的是 -1~1 的浮点，必须先除以 32768，
      否则数值被当成满刻度外的巨大值 → 削平 → 听感"爆音"。
    """
    total = int(RATE * seconds)
    buf = [float(v) / 32768.0 for v in samples[:total]]
    if len(buf) < total:
        buf += [0.0] * (total - len(buf))
    fade = int(RATE * 0.2)
    for i in range(fade):
        j = total - fade + i
        if 0 <= j < total:
            buf[j] *= (fade - i) / fade
    riff = pcm_to_adpcm_riff(buf, channels=2, block_size=ENC_BLOCK)
    return build_container([riff], b"",
                           per_entry_fields=[field_block_for(PREVIEW_TRACK_ID)])


def build_preview(events: list[tuple[int, int]], resolve, seconds: float = PREVIEW_SECONDS) -> bytes:
    """把前 N 秒的所有声音混成预览音。

    events: [(毫秒, 采样号)]，按时间升序；resolve(采样号) 返回采样文件路径或 None。
    注意：events 里的采样号必须是**原始 BMS 采样号**，不能用重映射后的容器编号。
    """
    total = int(RATE * seconds)
    buf = [0.0] * total
    cache: dict[int, list[float] | None] = {}
    for ms, sample in events:
        if ms >= seconds * 1000:
            break
        if sample not in cache:
            src = resolve(sample)
            try:
                cache[sample] = decode_to_pcm(Path(src)) if src and Path(src).exists() else None
            except AudioError:
                cache[sample] = None
        clip = cache[sample]
        if not clip:
            continue
        off = int(ms * RATE / 1000)
        for i, v in enumerate(clip):
            j = off + i
            if j >= total:
                break
            buf[j] += v * 0.7
    peak = max(1e-9, max(abs(v) for v in buf))
    if peak > 1.0:
        buf = [v / peak for v in buf]
    fade = int(RATE * 0.2)
    for i in range(fade):
        j = total - fade + i
        if 0 <= j < total:
            buf[j] *= (fade - i) / fade
    # 真实 _pre.2dx 是**完整 2DX9 容器**（mon/2dxTools 2dx.h：
    #   fileHeader 72B + 4B 偏移表 + 条目；preview 的 trackId 恒为 -1；
    #   文件名域填 0（实测真实 _pre.2dx 的 0x00..0x0F 全是 0）；2 声道 44100。
    riff = pcm_to_adpcm_riff(buf, channels=2, block_size=ENC_BLOCK)
    return build_container([riff], b"",
                           per_entry_fields=[field_block_for(PREVIEW_TRACK_ID)])
