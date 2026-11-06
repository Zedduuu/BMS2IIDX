"""生成 IIDX 选曲缩略图（内嵌版，打包后也能用）

做法：拿一张真实缩略图当模板，保留它的边框（黑-白-黑 + 右下小尾巴），
只把内部画面换掉。模板必须由调用方传入（打包后没有固定盘符）。
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageFilter

FRAME_PX = 6                  # 边框厚度：外黑 1 + 白 2 + 内黑 3


def _cover(pic: Image.Image, w: int, h: int) -> Image.Image:
    """等比铺满并居中裁切"""
    k = max(w / pic.width, h / pic.height)
    pic = pic.resize((max(1, round(pic.width * k)), max(1, round(pic.height * k))),
                     Image.LANCZOS)
    left = (pic.width - w) // 2
    top = (pic.height - h) // 2
    return pic.crop((left, top, left + w, top + h))


def _official_style(src: pathlib.Path, dst: pathlib.Path,
                    size: tuple[int, int]) -> pathlib.Path:
    """34 代选曲图的官方样子：整幅铺满 + 上下 1px 黑边 + 最底行透明"""
    w, h = size
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    art = _cover(Image.open(src).convert("RGB"), w, h).convert("RGBA")
    canvas.paste(art, (0, 0))
    black = Image.new("RGBA", (w, 1), (0, 0, 0, 255))
    canvas.paste(black, (0, 0))                    # 顶部黑边
    canvas.paste(black, (0, h - 2))                # 底部黑边
    bottom = Image.new("RGBA", (w, 1), (0, 0, 0, 0))
    canvas.paste(bottom, (0, h - 1))               # 最后一行透明（官方如此）
    canvas.save(dst, "PNG")
    return dst


def make(src: pathlib.Path, dst: pathlib.Path,
         template: pathlib.Path | None = None,
         size: tuple[int, int] | None = None) -> pathlib.Path:
    """34 代走官方布局；32 代/其它有模板就走模板边框"""
    dst = pathlib.Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if size == (228, 130):
        return _official_style(src, dst, size)

    if template and pathlib.Path(template).exists():
        tpl = Image.open(template).convert("RGBA")
        alpha = tpl.getchannel("A")
        inner = alpha.point(lambda v: 255 if v > 128 else 0)
        inner = inner.filter(ImageFilter.MinFilter(FRAME_PX * 2 + 1))
        bbox = inner.getbbox()
        if bbox is None:
            raise RuntimeError("模板内部区域计算失败")
        box_w, box_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

        pic = Image.open(src).convert("RGB")
        k = max(box_w / pic.width, box_h / pic.height)        # 等比铺满，居中裁切
        pic = pic.resize((max(1, round(pic.width * k)), max(1, round(pic.height * k))),
                         Image.LANCZOS)
        left, top = (pic.width - box_w) // 2, (pic.height - box_h) // 2
        pic = pic.crop((left, top, left + box_w, top + box_h))

        canvas = tpl.copy()
        art = Image.new("RGBA", tpl.size, (0, 0, 0, 0))
        art.paste(pic, (bbox[0], bbox[1]))
        canvas.paste(art, (0, 0), inner)
        canvas.save(dst, "PNG")
        return dst

    pic = Image.open(src).convert("RGB")
    if size:
        k = max(size[0] / pic.width, size[1] / pic.height)
        pic = pic.resize((max(1, round(pic.width * k)), max(1, round(pic.height * k))),
                         Image.LANCZOS)
        left = (pic.width - size[0]) // 2
        top = (pic.height - size[1]) // 2
        pic = pic.crop((left, top, left + size[0], top + size[1]))
    pic.save(dst)
    return dst
