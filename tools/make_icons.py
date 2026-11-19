"""由 assets/icon/lotus.svg 生成整套图标（PNG + ICO）。

用法：
    python tools/make_icons.py

产物（assets/icon/）：
    lotus.svg                    源文件（唯一要手改的地方）
    BMS2IIDX-<size>.png          16/24/32/48/64/128/256/512
    BMS2IIDX.ico                 多尺寸图标（给 exe / 窗口用）
    BMS2IIDX-simple-<size>.png   小尺寸简化版（≤32px 用，孔少更清楚）

说明：小尺寸下 8 个孔会糊成一团，所以 ≤32 用简化版（1 大孔 + 3 小孔）。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ICON_DIR = ROOT / "assets" / "icon"
SVG = ICON_DIR / "lotus.svg"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
ILLUS = ICON_DIR / "lotus-illustration.png"   # 手绘藕片（用户提供），优先用它
ART_MIN = 32                                   # 32px 以下用简化矢量，否则糊成一团
SMALL = (16,)

# 简化版：小尺寸用，1 大孔 + 3 小孔
SIMPLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <circle cx="128" cy="128" r="112" fill="#F4EAD6" stroke="#B08A5A" stroke-width="10"/>
  <circle cx="128" cy="128" r="30" fill="#5B4630"/>
  <circle cx="128" cy="72" r="16" fill="#5B4630"/>
  <circle cx="176" cy="150" r="16" fill="#5B4630"/>
  <circle cx="80" cy="150" r="16" fill="#5B4630"/>
</svg>"""


def render(svg_bytes: bytes, size: int) -> "QImage":
    from PySide6.QtCore import QByteArray, QSize, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg_bytes))
    img = QImage(QSize(size, size), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(p)
    p.end()
    return img


def write_ico(path: pathlib.Path, entries: list[tuple[int, pathlib.Path]]) -> None:
    """把若干 PNG 直接封进 ICO（Windows Vista+ 支持 PNG 压缩条目）。

    不用 Pillow 的多尺寸保存：那个只会存一张图再缩放，小尺寸的简化版会丢。
    """
    import struct

    blobs = [(size, p.read_bytes()) for size, p in entries]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    dirs, data = b"", b""
    for size, blob in blobs:
        b = 0 if size >= 256 else size
        dirs += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    path.write_bytes(header + dirs + data)


def art_master() -> "Image.Image | None":
    """手绘插画：保留原图的透明背景，不抠色、不改色（2026-11-20 用户要求）。

    唯一处理：按 alpha 包围盒裁到内容，再补成正方形（四周留 6% 空间），
    让图标在各种尺寸下居中；原来那样把浅色藕肉也键成透明，边缘才发毛。
    """
    if not ILLUS.exists():
        return None
    from PIL import Image

    im = Image.open(ILLUS).convert("RGBA")
    box = im.getbbox()                     # 只看 alpha，白色藕肉不会被切
    if box:
        im = im.crop(box)
    side = max(im.size)
    pad = int(side * 0.06)
    canvas = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.paste(im, ((canvas.width - im.width) // 2,
                      (canvas.height - im.height) // 2), im)
    return canvas


def art_at(master, size: int) -> "Image.Image":
    from PIL import Image
    return master.resize((size, size), Image.LANCZOS)


def main() -> int:
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:
        print("需要 PySide6")
        return 1
    app = QGuiApplication([])                       # noqa: F841  (QPainter 需要)

    icon_dir = ICON_DIR
    icon_dir.mkdir(parents=True, exist_ok=True)
    svg_bytes = SVG.read_bytes()
    made = []
    master = art_master()

    for size in SIZES:
        if master is not None and size >= ART_MIN:
            art_at(master, size).save(str(icon_dir / f"BMS2IIDX-{size}.png"))
        else:
            render(svg_bytes, size).save(str(icon_dir / f"BMS2IIDX-{size}.png"))
        made.append(f"BMS2IIDX-{size}.png")

    for size in SMALL:
        render(SIMPLE_SVG.encode("utf-8"), size).save(
            str(icon_dir / f"BMS2IIDX-simple-{size}.png"))
        made.append(f"BMS2IIDX-simple-{size}.png")

    # 多尺寸 ico：直接把各尺寸 PNG 塞进 ICO 容器（16 用简化版，其余用完整版）
    ico_entries = [(16, icon_dir / "BMS2IIDX-simple-16.png")]
    ico_entries += [(24, icon_dir / "BMS2IIDX-simple-24.png")]
    ico_entries += [(s, icon_dir / f"BMS2IIDX-{s}.png")
                    for s in (32, 48, 64, 128, 256)]
    ico = icon_dir / "BMS2IIDX.ico"
    write_ico(ico, ico_entries)
    made.append(ico.name)

    print("生成完成：")
    for m in made:
        print("  ", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
