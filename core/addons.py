"""缩略图规格与生成（XML 登记已经搬到 core/reapply.py 的 merged 方案）

为什么缩略图必须有：
  · graphic/movie_thumbnail/<id>_thum.png —— 34 代选曲界面的封面，228×130
  · 32 代及以前是 graphic/thumbnail/，278×186

生成方式见 core/thumbmake.py（34 代按官方布局整幅铺满 + 1px 黑边）。
"""
from __future__ import annotations

import pathlib

THUMB_SIZE = (228, 130)          # 34 代 movie_thumbnail 的实际尺寸
THUMB_SIZE_32 = (278, 186)       # 32 代 thumbnail 的实际尺寸


def thumbnail_spec(version: int, contents: pathlib.Path | None = None) -> tuple[str, tuple[int, int]]:
    """按代次/安装返回 (目录名, 尺寸)。规则统一放在 core.assets，避免两处不一致。"""
    from .assets import thumbnail_spec as _spec
    spec = _spec(version, contents)
    if spec is None:                       # 27 代以前没有缩略图
        return "thumbnail", THUMB_SIZE_32
    return spec


# ------------------------------------------------------------------ XML 插入
# ------------------------------------------------------------------ 缩略图
def make_thumbnail(src: pathlib.Path, dst: pathlib.Path,
                   template: pathlib.Path | None = None,
                   size: tuple[int, int] = THUMB_SIZE) -> pathlib.Path:
    """生成选曲封面：默认套用模板（官方边框），模板不在就按 base 图直接缩放"""
    from PIL import Image

    dst.parent.mkdir(parents=True, exist_ok=True)
    if template and template.exists():
        from . import thumbmake
        return thumbmake.make(src, dst, template, size=size)

    pic = Image.open(src).convert("RGB")
    k = max(size[0] / pic.width, size[1] / pic.height)
    pic = pic.resize((max(1, round(pic.width * k)), max(1, round(pic.height * k))),
                     Image.LANCZOS)
    left = (pic.width - size[0]) // 2
    top = (pic.height - size[1]) // 2
    pic.crop((left, top, left + size[0], top + size[1])).save(dst)
    return dst
