"""整曲混音（IIDX 原生的"单流"做法）

真实证据：现代曲目（29902/30223/31252 等）的音频都是"整曲一条流 + 一堆短音效"。
性能：用 numpy 向量化累加（纯 Python 逐采样在 149 秒的曲子上要 130 秒，
numpy 只要 1~2 秒）。
"""
from __future__ import annotations

from . import cli_log

import hashlib
import shutil
from pathlib import Path

import numpy as np

RATE = 44100
TARGET_RMS = 0.274      # 真实曲目实测 RMS（约 -11 dBFS）
PEAK_CEILING = 0.891    # -1 dBFS，给编码留余量
LIMIT_THRESHOLD = 0.70  # 限幅起始点：此值以下完全不动，保证响度不被压掉
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
CACHE = Path(__file__).resolve().parent.parent / "work" / "pcm_cache"


def _key(p: Path) -> str:
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()


def decode_pcm(p: Path, duration: float | None = None) -> np.ndarray:
    """解码成 44.1kHz 单声道 int16 numpy 数组（带磁盘缓存）。"""
    suffix = "" if duration is None else f"|{duration:.3f}"
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = CACHE / (_key(p) + suffix.replace("|", "_").replace(".", "_") + ".pcm")
    if cf.exists():
        return np.frombuffer(cf.read_bytes(), dtype=np.int16)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(p), "-ar", str(RATE), "-ac", "1", "-f", "s16le", "-"]
    r = cli_log.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"解码失败 {p.name}: {r.stderr.decode('utf-8','replace')[:160]}")
    raw = r.stdout[: len(r.stdout) // 2 * 2]
    a = np.frombuffer(raw, dtype=np.int16).copy()
    try:
        cf.write_bytes(a.tobytes())
    except OSError:
        pass
    return a


def _decode_task(args):
    sid, src = args
    try:
        return sid, decode_pcm(Path(src)).tobytes(), None
    except Exception as e:                                        # noqa: BLE001
        return sid, None, str(e)[:160]


def prefetch_pcm(sample_ids, resolve, workers: int | None = None) -> dict:
    from .par import pmap
    todo = [(sid, str(resolve(sid))) for sid in sample_ids
            if resolve(sid) and Path(resolve(sid)).exists()]
    out: dict = {}
    for sid, raw, _err in pmap(_decode_task, todo, workers):
        if raw:
            out[sid] = np.frombuffer(raw, dtype=np.int16)
    return out


def mix_events(events, resolve, total_ms: int, gain: float = 1.0,
               target_rms: float = TARGET_RMS, peak_ceiling: float = PEAK_CEILING,
               progress=None) -> np.ndarray:
    """把 [(毫秒, 采样号)] 混成一条 44.1k 单声道 int16 音轨。

    增益策略（实测自真实曲目）：
      真实 IIDX 曲目 平均 -11 dB / 峰值 -1 dB（RMS≈0.274、峰值≈0.891）。
      之前把 BGM 和键音直接相加，峰值远超满刻度被削平 → 听起来"爆音"。
      现在先求整条音轨的 RMS 与峰值：把 RMS 拉到真实水平，
      若这样峰值会超过 -1 dBFS 就改为受峰值限制，最后用软限幅兜底。
    """
    total = int(total_ms * RATE / 1000) + RATE // 2
    acc = np.zeros(total, dtype=np.int32)
    ids = sorted({s for _, s in events})
    if progress:
        progress(f"并行解码 {len(ids)} 个采样 …")
    cache = prefetch_pcm(ids, resolve)
    for ms, sid in events:
        clip = cache.get(sid)
        if clip is None or clip.size == 0:
            continue
        off = int(ms * RATE / 1000)
        n = min(clip.size, total - off)
        if n <= 0:
            continue
        acc[off:off + n] += clip[:n]

    x = acc.astype(np.float32) / 32768.0
    peak = float(np.abs(x).max()) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0

    # ① 先对齐响度（RMS ≈ 0.274 ≈ -11 dBFS，取自真实曲目）
    g = gain
    if rms > 1e-6 and target_rms > 0:
        g *= target_rms / rms
    x = x * g

    # ② 再只用限幅器压峰值：阈值以下完全不动，所以响度不会被压掉。
    #    若按峰值线性缩放，整首会掉到 -19 dB（比真实曲目轻 8 dB），不可取。
    thr = LIMIT_THRESHOLD
    if peak * g > thr:
        knee = max(1e-4, peak_ceiling - thr)
        mag = np.abs(x)
        hot = mag > thr
        if hot.any():
            x[hot] = np.sign(x[hot]) * (thr + knee * np.tanh((mag[hot] - thr) / knee))

    if progress:
        p2 = float(np.abs(x).max()) if x.size else 0.0
        r2 = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        progress(f"增益 {g:+.2f}x 限幅后（原始峰值 {peak:.3f} / RMS {rms:.3f} → "
                 f"峰值 {p2:.3f} / RMS {r2:.3f}）")

    return np.clip(x * 32767.0, -32768, 32767).astype(np.int16)


