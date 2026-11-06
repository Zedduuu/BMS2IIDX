"""从 BGA 视频挑一帧当封面（按帧号取样）

为什么不能按"时间"抽帧：BMS 里的 .mpg 常常是多段拼接，容器里只有第一段的时长
（实测 bbkkbkk 的 01.mpg 报 1.1 秒，实际 14 MB）。所以按**帧号**取样。

流程：
  1. ffprobe 拿总帧数（失败则退回时长估算）
  2. 均匀取 N 个帧号，用 select 滤镜一次导出
  3. 每帧算"清晰度 + 丰富度"得分：拉普拉斯方差 + 灰度标准差
  4. 取得分最高的那帧
"""
from __future__ import annotations

import io
import pathlib
import re
import shutil
import tempfile

import numpy as np
from PIL import Image

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def _run(cmd):
    from . import cli_log
    return cli_log.run(cmd, capture_output=True)


def probe_frames(path: pathlib.Path) -> tuple[int | None, float | None]:
    """返回 (总帧数, 时长秒)"""
    frames = None
    dur = None
    r = _run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
              "-show_entries", "stream=nb_read_frames,duration", "-of", "csv=p=0",
              str(path)])
    txt = (r.stdout or b"").decode("utf-8", "replace").strip()
    if txt:
        for part in re.split(r"[,\n]", txt.replace('"', "")):
            part = part.strip()
            if not part:
                continue
            if part.isdigit() and frames is None:
                frames = int(part)
            elif dur is None and re.fullmatch(r"[\d.]+", part):
                try:
                    dur = float(part)
                except ValueError:
                    pass
    if frames is None:
        r2 = _run([FFMPEG, "-hide_banner", "-i", str(path)])
        t2 = (r2.stderr or b"").decode("utf-8", "replace")
        mm = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", t2)
        if mm:
            dur = int(mm.group(1)) * 3600 + int(mm.group(2)) * 60 + float(mm.group(3))
        mm2 = re.search(r"(\d+(?:\.\d+)?) fps", t2)
        if dur and mm2:
            frames = int(dur * float(mm2.group(1)))
    return frames, dur


def _score(img: Image.Image) -> float:
    """画面复杂度：拉普拉斯方差（细节多）+ 灰度标准差（对比度高）"""
    g = np.asarray(img.convert("L"), dtype=np.float32)
    if g.size == 0:
        return 0.0
    lap = (4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1]
           - g[1:-1, :-2] - g[1:-1, 2:])
    return float(lap.var()) + 2.0 * float(g.std())


def sample_frames(video: pathlib.Path, count: int = 12,
                  progress=None) -> list[bytes]:
    """随机抽若干帧，按"画面复杂度"从高到低返回 PNG 字节

    每次调用的帧号都是随机的（同一视频多抽几次能出不同画面）。
    """
    import random

    frames, dur = probe_frames(video)
    if progress:
        progress(f"  视频帧数={frames} 时长={dur}")
    total = frames if frames and frames > 0 else int((dur or 30) * 25)
    total = max(1, total)
    n = max(1, min(count, total))
    picks = sorted(random.sample(range(total), n))

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="thumb_"))
    try:
        expr = "+".join(f"eq(n\\,{p})" for p in picks)
        pattern = tmp / "f_%03d.png"
        _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
              "-vf", f"select={expr}", "-fps_mode", "passthrough", str(pattern)])
        scored: list[tuple[float, bytes]] = []
        for f in sorted(tmp.glob("f_*.png")):
            try:
                im = Image.open(f).convert("RGB")
            except Exception:                                  # noqa: BLE001
                continue
            sc = _score(im)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            scored.append((sc, buf.getvalue()))
        scored.sort(key=lambda x: -x[0])
        if progress:
            progress(f"  抽到 {len(scored)} 帧，最高复杂度 "
                     f"{scored[0][0]:.0f}" if scored else "  没抽到帧")
        return [b for _s, b in scored]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick_frame(video: pathlib.Path, count: int = 12,
               progress=None) -> pathlib.Path | None:
    """取最高复杂度的一帧，写成临时文件（旧接口，供转换流程用）"""
    frames = sample_frames(video, count=count, progress=progress)
    if not frames:
        return None
    out = pathlib.Path(tempfile.gettempdir()) / f"thumb_pick_{video.stem}.png"
    out.write_bytes(frames[0])
    return out
