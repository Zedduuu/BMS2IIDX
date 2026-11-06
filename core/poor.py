"""坏判层（POOR 层）：借现成图形包的单图模板，换成原曲的 miss 图

做法：
  1. 在目标安装里找一个含 `tex/miss.png` 的图形包当模板（结果缓存）
  2. 解包 → 把 tex/miss.png 换成我们的图（保持模板尺寸，纯黑透明）→ 重新打包
  3. 曲库记录里打开 NGPF 图层开关：0x6B0=2、0x6B4="00"
"""
from __future__ import annotations

import json
import pathlib
import shutil
import struct

from . import dbmask
import tempfile

import numpy as np
from PIL import Image

CACHE_NAME = "poor_template.json"


def _cache_path(lib_root: pathlib.Path) -> pathlib.Path:
    return lib_root / CACHE_NAME


def _has_miss(p: pathlib.Path) -> bool:
    import ifstools
    try:
        ifs = ifstools.IFS(str(p))
        names = {str(f).replace("/", "\\") for f in ifs.tree.all_files}
        ifs.close()
        return "tex\\miss.png" in names
    except Exception:                                      # noqa: BLE001
        return False


def find_template(contents: pathlib.Path, lib_root: pathlib.Path,
                  max_scan: int = 60, progress=None) -> dict | None:
    """找一个含 tex/miss.png 的图形包；结果缓存

    顺序：
      1. 已知好用的包（13000 / 12012，都是"一张 miss.png"的结构）
      2. 同目录按体积从小到大试 max_scan 个
    """
    cache = _cache_path(lib_root)
    if cache.exists():
        try:
            info = json.loads(cache.read_text(encoding="utf-8"))
            p = pathlib.Path(info["path"])
            if p.exists():
                return info
        except Exception:                                  # noqa: BLE001
            pass

    dirs = [contents / "data_mods/omnimix/graphic", contents / "data/graphic"]
    preferred = ["13000.ifs", "12012.ifs", "13010.ifs", "13026.ifs", "13038.ifs"]
    tried: set[pathlib.Path] = set()

    def accept(p: pathlib.Path) -> dict:
        info = {"path": str(p), "entry": "tex\\miss.png"}
        lib_root.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        if progress:
            progress(f"坏判层模板：{p.parent.name}/{p.name}")
        return info

    for name in preferred:
        for d in dirs:
            p = d / name
            if p.exists() and p not in tried:
                tried.add(p)
                if _has_miss(p):
                    return accept(p)

    cand: list[pathlib.Path] = []
    for d in dirs:
        if d.is_dir():
            cand += [x for x in sorted(d.glob("*.ifs")) if x.is_file()]
    for p in [x for x in cand if x not in tried][:max_scan]:
        if _has_miss(p):
            return accept(p)
    if progress:
        progress("没找到含 tex/miss.png 的图形包模板")
    return None


def _process_miss(src: pathlib.Path, size: tuple[int, int]) -> Image.Image:
    """把 BMS 的 miss 图做成模板尺寸的 RGBA（纯黑透明）"""
    im = Image.open(src).convert("RGB")
    k = min(size[0] / im.width, size[1] / im.height)
    im2 = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                    Image.LANCZOS)
    canvas = Image.new("RGB", size, (0, 0, 0))
    canvas.paste(im2, ((size[0] - im2.width) // 2, (size[1] - im2.height) // 2))
    arr = np.asarray(canvas, dtype=np.uint8)
    alpha = (arr.max(axis=2) != 0).astype(np.uint8) * 255
    out = canvas.convert("RGBA")
    out.putalpha(Image.fromarray(alpha))
    return out


def build_pack(template: pathlib.Path, miss_src: pathlib.Path | None,
               out_ifs: pathlib.Path, progress=None) -> pathlib.Path:
    import ifstools

    out_ifs = pathlib.Path(out_ifs)
    out_ifs.parent.mkdir(parents=True, exist_ok=True)
    if miss_src is None:
        # 原曲没有 miss 图 → 直接用游戏自带那张（整包复制），坏判层照样生效
        if progress:
            progress("没有原曲 miss 图，直接沿用模板里的图")
        shutil.copy2(template, out_ifs)
        return out_ifs

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="poor_"))
    try:
        ifs = ifstools.IFS(str(template))
        ifs.extract(path=str(tmp), progress=False)
        ifs.close()

        target = tmp / "tex" / "miss.png"
        if not target.exists():
            raise RuntimeError("模板里没有 tex/miss.png")
        old = Image.open(target)
        new = _process_miss(miss_src, old.size)
        new.save(target, "PNG")

        out_ifs.parent.mkdir(parents=True, exist_ok=True)
        pack = ifstools.IFS(str(tmp))
        pack.repack(path=str(out_ifs), progress=False)
        pack.close()
        if progress:
            progress(f"坏判层图形包 → {out_ifs.name}（{out_ifs.stat().st_size/1e6:.1f} MB）")
        return out_ifs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def set_layer_flags(db_path: pathlib.Path, song_id: int, layer: int = 2,
                    afp: str = "00") -> bool:
    """在曲库里打开该曲的 NGPF 图层开关（0x6B0 / 0x6B4）"""
    _plain, _masked = dbmask.read_db(db_path)
    raw = bytearray(_plain)
    ver, avail, unk, slots = struct.unpack_from("<IHHI", raw, 4)
    base = 16 + slots * 4
    for i in range(avail):
        off = base + i * 2040
        if off + 2040 > len(raw):
            return False
        if struct.unpack_from("<I", raw, off + 0x67C)[0] != song_id:
            continue
        struct.pack_into("<H", raw, off + 0x6B0, layer)
        code = afp.encode("ascii")[:3].ljust(3, b"\x00")
        raw[off + 0x6B4: off + 0x6B4 + 4] = code + b"\x00"
        dbmask.write_db(db_path, bytes(raw), _masked)
        return True
    return False
