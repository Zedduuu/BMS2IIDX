"""按「目标游戏代次」决定资源格式（影片容器、缩略图目录与尺寸）

依据实测（见 docs/STATUS.md）：

| 版本 | 歌曲影片 | 选曲缩略图 |
|---|---|---|
| 20–26 | `.wmv`（wmv3/VC-1，640×480 或 1024×768，30fps） | **没有缩略图** |
| 27–31 | 未知（推断 mp4 或 wmv，按目标机现有文件自动判断） | 未知（有 `graphic/thumbnail` 才写） |
| 32–34 | `.mp4`（h264） | `graphic/movie_thumbnail` 228×130（32 起） |

ffmpeg 没有 VC-1 编码器，所以旧代只能输出 **wmv2（WMV8）**；能不能被引擎接受需真机验证。
"""
from __future__ import annotations

import pathlib

# 影片编码参数
MP4_ARGS = ["-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
            "-pix_fmt", "yuv420p", "-b:v", "4500k", "-maxrate", "6000k",
            "-bufsize", "12000k", "-movflags", "+faststart"]
WMV_ARGS = ["-c:v", "wmv2", "-pix_fmt", "yuv420p", "-b:v", "4000k",
            "-maxrate", "5000k", "-bufsize", "10000k"]

# GPU（NVIDIA NVENC）编码参数：同码率画质略低于 x264，但 BGA 这种画面基本看不出，
# 换来 5–10 倍速度。只在输出 mp4（32 代以上）时可用 —— N 卡没有 WMV 编码器。
NVENC_ARGS = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
              "-b:v", "4500k", "-maxrate", "6000k", "-bufsize", "12000k",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart"]

THUMB_NEW = ("movie_thumbnail", (228, 130))     # 32+
THUMB_OLD = ("thumbnail", (278, 186))           # 27–31（若目标机有）

# 影片容器分界：26 及以前是 wmv，32 起是 mp4
WMV_MAX = 26
MP4_MIN = 32


def video_format(ver: int, contents: pathlib.Path | None = None) -> str:
    """返回 "mp4" 或 "wmv"。27–31 用目标机现有影片判断。"""
    if ver <= WMV_MAX:
        return "wmv"
    if ver >= MP4_MIN:
        return "mp4"
    if contents:
        movie = pathlib.Path(contents) / "data" / "movie"
        if movie.is_dir():
            mp4 = sum(1 for _ in movie.glob("*.mp4"))
            wmv = sum(1 for _ in movie.glob("*.wmv"))
            if mp4 or wmv:
                return "mp4" if mp4 >= wmv else "wmv"
    return "mp4"


_HAS_NVENC: bool | None = None


def has_nvenc() -> bool:
    """本机 ffmpeg 是否带 NVENC（检测一次后记住）"""
    global _HAS_NVENC
    if _HAS_NVENC is None:
        try:
            from . import cli_log
            r = cli_log.run(["ffmpeg", "-hide_banner", "-h", "encoder=h264_nvenc"],
                            capture_output=True)
            txt = ((getattr(r, "stdout", None) or b"") + (getattr(r, "stderr", None) or b""))
            if isinstance(txt, bytes):
                txt = txt.decode("utf-8", "ignore")
            _HAS_NVENC = "nvenc" in txt.lower() and "unknown encoder" not in txt.lower()
        except Exception:                                      # noqa: BLE001
            _HAS_NVENC = False
    return bool(_HAS_NVENC)


def video_args(fmt: str, force_cpu: bool = False) -> list[str]:
    """mp4 → 有 N 卡就 NVENC，否则/失败时回退 libx264；wmv 只能软件编码"""
    if fmt == "wmv":
        return list(WMV_ARGS)
    if not force_cpu and has_nvenc():
        return list(NVENC_ARGS)
    return list(MP4_ARGS)


def video_ext(fmt: str) -> str:
    return ".wmv" if fmt == "wmv" else ".mp4"


def thumbnail_spec(ver: int, contents: pathlib.Path | None = None):
    """返回 (目录名, 尺寸) 或 None（这一代没有选曲缩略图）"""
    if ver <= 26:
        return None
    if ver >= 32:
        return THUMB_NEW
    # 27–31：目标机有 thumbnail 目录才写
    if contents and (pathlib.Path(contents) / "data" / "graphic" / "thumbnail").is_dir():
        return THUMB_OLD
    if contents:
        return None
    return THUMB_OLD
