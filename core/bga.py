"""BMS 的 BGA → IIDX 能播的 mp4（支持视频底、图片翻转底、以及叠加层合成）

实测结论（2026-10-23 全库普查 3272 个谱面）：
  · 04 基础层：**2192 个谱面是视频**（.mpg/.wmv/.mp4…）、1065 个是图片、43 个没有 04
  · 06/07 是叠加层（透明叠加）：509 个用 06、525 个用 07、13 个三层全有
  · **382 个谱面是「视频底 + 叠加层」**
  · 06 通道里还混着"坏判定层"（只在 POOR 时出现）→ **绝不能烧进影片**，
    判据：事件时间跨度不到全曲一半、或只有 1 个事件
  · 透明约定 = **黑色即透明**（叠加图常带大片纯黑边框）；PNG 自带 alpha 时优先用 alpha

三种渲染模式：
  A. 视频底、无叠加 → 直接转码（保留原分辨率，不裁不缩放 = 最"原汁原味"）
  B. 视频底、有叠加 → 视频按比例放进 1024×768（补边不裁），叠加层缩放后合成
  C. 图片底 → 逐帧翻转渲染（素材按铺满缩放），叠加层合成
"""
from __future__ import annotations

from . import assets, cli_log

import pathlib
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image

SIZE = (1024, 768)
FPS = 30
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
VIDEO_EXT = (".mpg", ".mpeg", ".wmv", ".avi", ".mp4", ".mkv", ".webm", ".mov")


# ----------------------------------------------------------------- 素材工具
def is_video(name: str | None) -> bool:
    return bool(name) and name.lower().endswith(VIDEO_EXT)


_FORCE_CPU = False          # NVENC 在部分驱动/显卡上会失败，失败一次就全程用 CPU


def _enc_args(fmt: str) -> list[str]:
    return assets.video_args(fmt, force_cpu=_FORCE_CPU)


def _canvas_for(wh: tuple[int, int]) -> tuple[int, int]:
    """画布 = 素材原始分辨率（不放大、不缩小、不裁切）。

    原汁原味：素材是 256×256 就输出 256×256。游戏自己会把它放进 BGA 框，
    那是游戏的缩放，不是我们改写素材。
    """
    w, h = wh
    if not w or not h:
        return SIZE
    return (w - (w % 2), h - (h % 2))       # 只保证偶数，编码器友好


def _resize(img: Image.Image, size) -> Image.Image:
    """直接缩放到画布（比例已对齐，等于不裁不补边）"""
    return img.resize(size, Image.LANCZOS)


class LayerImage:
    """已缩放好的图层：rgb(uint8) + alpha(float32 0..1)"""

    def __init__(self, path: pathlib.Path, size, overlay: bool):
        im = Image.open(path)
        has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
        im = _resize(im.convert("RGBA"), size)
        self.rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
        if has_alpha:
            self.alpha = np.asarray(im.getchannel("A"), dtype=np.float32) / 255.0
        elif overlay:
            # BMS 规范：**纯黑 #000000 = 透明**（angolmois / BMS制作wiki / bmson 规范互证）
            # 严格键控，不做任何羽化/容差：非纯黑一律不透明。
            mx = self.rgb.max(axis=2)
            self.alpha = (mx != 0).astype(np.float32)
        else:
            self.alpha = np.ones(self.rgb.shape[:2], dtype=np.float32)


def _over(base: np.ndarray, li: LayerImage | None) -> np.ndarray:
    if li is None:
        return base
    a = li.alpha[:, :, None]
    return np.clip(base.astype(np.float32) * (1.0 - a)
                   + li.rgb.astype(np.float32) * a, 0, 255).astype(np.uint8)


# ----------------------------------------------------------------- 规划
def plan(tick_chart, chart) -> dict:
    """判断渲染模式、分出叠加层与坏判定层"""
    per: dict[int, list] = {0: [], 1: [], 2: [], 3: []}
    for t, v, l in tick_chart.bga:
        per.setdefault(l, []).append((t, v))
    for k in per:
        per[k].sort()

    total = max(1, tick_chart.total_ms)
    poor = []
    overlays = []

    if per.get(0):
        base_layer = 0
        for l in (1, 2, 3):
            ev = per.get(l) or []
            if not ev:
                continue
            span = (ev[-1][0] - ev[0][0]) / total if len(ev) > 1 else 0.0
            if len(ev) < 2 or span < 0.5:
                poor.append(l)          # 坏判定层，不烧进影片
            else:
                overlays.append(l)
    else:
        base_layer = 1 if per.get(1) else (2 if per.get(2) else
                                           (3 if per.get(3) else None))
        if base_layer is None:
            raise RuntimeError("这个谱面没有任何 BGA 事件")
        overlays = [l for l in (1, 2, 3) if l != base_layer and per.get(l)]

    base_ev = per[base_layer]
    base_names = [(t, chart.bmp.get(v), v) for t, v in base_ev]
    videos = {n for _t, n, _v in base_names if is_video(n)}
    images = {n for _t, n, _v in base_names if n and not is_video(n)}

    if videos and not images:
        mode = "video"
    elif images and not videos:
        mode = "image"
    else:
        mode = "mixed"

    return {"mode": mode, "per": per, "base_layer": base_layer,
            "overlays": overlays, "poor": poor,
            "videos": sorted(videos), "images": sorted(images),
            "total_ms": total}


# ----------------------------------------------------------------- A/B：视频底
def _video_meta(path: pathlib.Path):
    """取视频宽高；stderr/stdout 都可能为空，任何异常都当作"未知"处理。"""
    try:
        r = cli_log.run([FFMPEG, "-hide_banner", "-i", str(path)],
                        capture_output=True, text=True)
    except OSError:
        return None, None
    out = (getattr(r, "stderr", None) or "") + "\n" + (getattr(r, "stdout", None) or "")
    import re
    w = h = None
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    if not w or not h:
        # ffmpeg 的输出格式因版本/容器而异，失败就用 ffprobe 再问一次
        try:
            import os
            probe = str(pathlib.Path(FFMPEG).with_name("ffprobe.exe"))
            if not pathlib.Path(probe).exists():
                probe = "ffprobe"
            r2 = cli_log.run([probe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height",
                              "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True)
            txt = (getattr(r2, "stdout", None) or "").strip().splitlines()
            if txt and "," in txt[0]:
                a, b = txt[0].split(",")[:2]
                w, h = int(float(a)), int(float(b))
        except Exception:                                      # noqa: BLE001
            pass
    return w, h


def render_video(pl: dict, chart, out_path: pathlib.Path, progress=None,
                 video_fmt: str = "mp4", on_progress=None) -> dict:
    global _FORCE_CPU
    folder = pathlib.Path(chart.path).parent
    video = folder / pl["videos"][0].replace("\\", "/")
    if len(pl["videos"]) > 1 and progress:
        progress(f"  注意：基础层有 {len(pl['videos'])} 个视频，只用第一个"
                 f"（{pl['videos'][0]}）")

    overlays = pl["overlays"]
    total_ms = pl["total_ms"]
    vw, vh = _video_meta(video)
    canvas = _canvas_for((vw, vh))

    # 预先缩放叠加层素材
    cache: dict[tuple[int, int], LayerImage | None] = {}
    missing = []
    for l in overlays:
        for _t, v in pl["per"][l]:
            key = (l, v)
            if key in cache:
                continue
            name = chart.bmp.get(v)
            src = folder / name.replace("\\", "/") if name else None
            if not src or not src.exists() or is_video(name):
                cache[key] = None
                missing.append((l, v, name))
                continue
            try:
                cache[key] = LayerImage(src, canvas, True)
            except Exception:                                  # noqa: BLE001
                cache[key] = None
                missing.append((l, v, name))

    if not overlays:
        # ---- 模式 A：直接转码，保留原分辨率 ----
        if progress:
            progress(f"  模式 A：纯视频底，直接转码 {video.name}（{vw}x{vh}）")
        cmd = ([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
               + _enc_args(video_fmt)
               + ["-r", str(FPS), "-an", str(out_path)])
        try:
            cli_log.run(cmd, check=True)
        except Exception:                                      # noqa: BLE001
            if _enc_args(video_fmt) != assets.video_args(video_fmt, force_cpu=True):
                _FORCE_CPU = True
                if progress:
                    progress("  GPU 编码失败，改用 CPU（libx264）重试")
                cmd = ([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
                       + _enc_args(video_fmt)
                       + ["-r", str(FPS), "-an", str(out_path)])
                cli_log.run(cmd, check=True)
            else:
                raise
        return {"mode": "video-passthrough", "video": video.name, "canvas": (vw, vh),
                "overlays": [], "poor_layers": pl["poor"]}

    # ---- 模式 B：视频 + 叠加层合成 ----
    if progress:
        progress(f"  模式 B：视频底 {video.name}（{vw}x{vh}）+ 叠加层 {overlays}"
                 f" 合成到 {canvas[0]}x{canvas[1]}")
    vf = f"scale={canvas[0]}:{canvas[1]},fps={FPS}"
    dec = cli_log.popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE)
    enc = cli_log.popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{canvas[0]}x{canvas[1]}",
         "-r", str(FPS), "-i", "-",
         *_enc_args(video_fmt), "-an", str(out_path)],
        stdin=subprocess.PIPE)

    frame_bytes = canvas[0] * canvas[1] * 3
    idx = {l: -1 for l in overlays}
    cur = {l: None for l in overlays}
    n = 0
    try:
        while True:
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            t = n * 1000.0 / FPS
            changed = False
            for l in overlays:
                ev = pl["per"][l]
                while idx[l] + 1 < len(ev) and ev[idx[l] + 1][0] <= t:
                    idx[l] += 1
                    cur[l] = ev[idx[l]][1]
                    changed = True
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(canvas[1], canvas[0], 3)
            if changed or n == 0:
                for l in overlays:
                    frame = _over(frame, cache.get((l, cur[l])))
                last = frame.tobytes()
            enc.stdin.write(last)
            n += 1
            if progress and n % (FPS * 20) == 0:
                progress(f"  合成 {n} 帧")
            if on_progress and n % 20 == 0:
                try:
                    on_progress(min(1.0, n / max(1, _vf_total_frames(pl))))
                except Exception:                              # noqa: BLE001
                    pass
    finally:
        dec.stdout.close()
        dec.wait()
        enc.stdin.close()
        enc.wait()

    # 输出体检：合成/编码失败时会留下 0 字节或几 KB 的残片，游戏里就是黑屏。
    # 这时退回"纯视频转码"（丢掉叠加层），至少画面能出。
    try:
        size = out_path.stat().st_size if out_path.exists() else 0
    except OSError:
        size = 0
    if size < 4096:
        _FORCE_CPU = True                  # 大概率是 GPU 编码失败，后面改用 CPU
        if progress:
            progress(f"  ⚠ 合成结果只有 {size} 字节，退回纯视频转码（叠加层丢弃）")
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        cmd = ([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
               + _enc_args(video_fmt)
               + ["-r", str(FPS), "-an", str(out_path)])
        cli_log.run(cmd, check=False)
        try:
            size = out_path.stat().st_size if out_path.exists() else 0
        except OSError:
            size = 0
        if size < 4096:
            return {"mode": "failed", "frames": n, "video": video.name,
                    "src_size": (vw, vh), "canvas": canvas, "overlays": overlays,
                    "poor_layers": pl["poor"], "missing": missing[:5],
                    "warning": "BGA 生成失败（编码输出为空）"}
        return {"mode": "video-passthrough(fallback)", "frames": n,
                "video": video.name, "src_size": (vw, vh), "canvas": canvas,
                "overlays": [], "poor_layers": pl["poor"], "missing": missing[:5],
                "warning": "叠加层合成失败，已退回纯视频"}

    return {"mode": "video+overlay", "frames": n, "video": video.name,
            "src_size": (vw, vh), "canvas": canvas,
            "overlays": overlays, "poor_layers": pl["poor"],
            "missing": missing[:5], "bytes": size}


# ----------------------------------------------------------------- C：图片底
def render_images(pl: dict, chart, out_path: pathlib.Path, progress=None,
                  video_fmt: str = "mp4", on_progress=None) -> dict:
    folder = pathlib.Path(chart.path).parent
    base_layer = pl["base_layer"]
    used = [base_layer] + pl["overlays"]
    overlay_flag = {l: (l != base_layer) for l in used}

    # 画布按基础层第一张图的比例定
    canvas = SIZE
    for _t, v in pl["per"][base_layer]:
        name = chart.bmp.get(v)
        src = folder / name.replace("\\", "/") if name else None
        if src and src.exists() and not is_video(name):
            try:
                with Image.open(src) as im:
                    canvas = _canvas_for(im.size)
                break
            except Exception:                                  # noqa: BLE001
                continue

    cache: dict[tuple[int, int], LayerImage | None] = {}
    missing = []
    for l in used:
        for _t, v in pl["per"][l]:
            key = (l, v)
            if key in cache:
                continue
            name = chart.bmp.get(v)
            src = folder / name.replace("\\", "/") if name else None
            if not src or not src.exists() or is_video(name):
                cache[key] = None
                missing.append((l, v, name))
                continue
            try:
                cache[key] = LayerImage(src, canvas, overlay_flag[l])
            except Exception:                                  # noqa: BLE001
                cache[key] = None
                missing.append((l, v, name))
    if progress:
        progress(f"  模式 C：图片底 + 层 {used}，画布 {canvas[0]}x{canvas[1]}，"
                 f"{len(cache)} 张图"
                 + (f"，缺/坏 {len(missing)} 张" if missing else ""))

    total_ms = max(pl["total_ms"], 1)
    for l in used:
        if pl["per"][l]:
            total_ms = max(total_ms, pl["per"][l][-1][0] + 1000)
    frames = int(total_ms * FPS / 1000) + 1
    frame_ms = 1000.0 / FPS

    idx = {l: -1 for l in used}
    cur = {l: None for l in used}
    enc = cli_log.popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{canvas[0]}x{canvas[1]}",
         "-r", str(FPS), "-i", "-",
         *_enc_args(video_fmt), "-an", str(out_path)],
        stdin=subprocess.PIPE)

    last = None
    wrote = 0
    try:
        for f in range(frames):
            t = f * frame_ms
            changed = False
            for l in used:
                ev = pl["per"][l]
                while idx[l] + 1 < len(ev) and ev[idx[l] + 1][0] <= t:
                    idx[l] += 1
                    cur[l] = ev[idx[l]][1]
                    changed = True
            if changed or last is None:
                frame = np.zeros((canvas[1], canvas[0], 3), dtype=np.uint8)
                for l in used:
                    frame = _over(frame, cache.get((l, cur[l])))
                last = frame.tobytes()
            enc.stdin.write(last)
            wrote += 1
            if on_progress and wrote % 20 == 0:
                try:
                    on_progress(min(1.0, wrote / max(1, frames)))
                except Exception:                              # noqa: BLE001
                    pass
            if progress and f and f % (FPS * 20) == 0:
                progress(f"  BGA 渲染 {f}/{frames} 帧")
    finally:
        enc.stdin.close()
        enc.wait()

    return {"mode": "image", "frames": wrote, "images": len(cache),
            "layers": used, "canvas": canvas, "poor_layers": pl["poor"],
            "missing": missing[:5], "seconds": round(total_ms / 1000, 1)}


def bake(chart, tick_chart, out_path: pathlib.Path, progress=None,
         video_fmt: str = "mp4", on_progress=None) -> dict:
    pl = plan(tick_chart, chart)
    if progress:
        progress(f"  BGA 模式={pl['mode']} 叠加层={pl['overlays']} "
                 f"坏判定层={pl['poor']}  黑键=严格纯黑")
    if pl["mode"] == "video":
        return render_video(pl, chart, out_path, progress, video_fmt=video_fmt,
                            on_progress=on_progress)
    if pl["mode"] == "image":
        return render_images(pl, chart, out_path, progress, video_fmt=video_fmt,
                             on_progress=on_progress)
    if pl["mode"] == "mixed":
        # 视频与图片混用：以图片路径渲染，视频事件按"缺失"处理（罕见）
        if progress:
            progress("  注意：基础层同时含视频与图片，按图片渲染，视频段会变黑")
        return render_images(pl, chart, out_path, progress, video_fmt=video_fmt)
    raise RuntimeError(f"未知模式 {pl['mode']}")


# 兼容旧接口
def main():
    import argparse

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from core import bms, timeline

    ap = argparse.ArgumentParser()
    ap.add_argument("chart")
    ap.add_argument("out")
    a = ap.parse_args()

    chart = bms.parse(pathlib.Path(a.chart))
    tc = timeline.build(chart)
    print(bake(chart, tc, pathlib.Path(a.out), progress=print))


if __name__ == "__main__":
    main()
