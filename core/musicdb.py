"""曲库读写（复用 iidx-datatools 的 musicdata_tool.py，不重写格式）

设计要点：
  · 只读源曲库，产物写到输出目录
  · 新增记录 = 深拷贝一条"结构最简单"的现有记录，只改必要字段
    （未知字段全部继承真实数据，避免自己编造）
  · 新歌 ID 从当前版本块尾部（32999）往上/下找空位，并在 registry 里登记
"""
from __future__ import annotations

import copy
import struct
import sys
from pathlib import Path

# 内置副本优先（打包后没有 _ref 目录），开发时回退到 _ref
_BUNDLED = Path(__file__).resolve().parent / "_vendor"
_REF = Path(__file__).resolve().parent.parent / "_ref" / "iidx-datatools"
for _p in (_BUNDLED, _REF):
    if (_p / "musicdata_tool.py").exists():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

import musicdata_tool as mdt  # noqa: E402

SUPPORTED_VER = set(range(20, 35))   # 20–34：布局按代分段（见 core/reapply.py）
MAX_ID = 99999                     # 槽表最大支持 99999（100 代 * 1000）
ORIGIN_NAMES = [
    "1ST", "SUBSTREAM", "2ND", "3RD", "4TH", "5TH", "6TH", "7TH", "8TH", "9TH",
    "10TH", "IIDX RED", "HAPPYSKY", "DISTORTED", "GOLD", "DJ TROOPERS", "EMPRESS",
    "SIRIUS", "RESORT ANTHEM", "LINCLE", "TRICORO", "SPADA", "PENDUAL", "COPULA",
    "SINOBUZ", "CANNON BALLERS", "ROOTAGE", "HEROIC VERSE", "BISTROVER",
    "CAST HOUR", "RESIDENT", "EPOLIS", "PINKY CRUSH", "SPARKLE SHOWER", "ZINRAI",
]
ORIGIN_DEFAULT = 1          # SUBSTREAM：官方只有 12 首，最干净的可用分类


def origin_index(value) -> int:
    """接受名字（"SUBSTREAM"）或整数索引，返回合法的索引"""
    if isinstance(value, int):
        idx = value
    else:
        text = str(value or "").strip().upper()
        if text.isdigit():
            idx = int(text)
        elif text in ORIGIN_NAMES:
            idx = ORIGIN_NAMES.index(text)
        else:
            raise ValueError(f"未知的 ORIGIN 分类: {value!r}")
    if not 0 <= idx < len(ORIGIN_NAMES):
        raise ValueError(f"ORIGIN 索引越界: {idx}")
    return idx

TEXT_LIMITS = {"title": 0x100, "title_ascii": 0x40, "genre": 0x80,
               "artist": 0x100, "subtitle": 0x100}


class MusicDBError(RuntimeError):
    pass


def _trim(text: str, field: str, ver: int = 34) -> str:
    """按字段的字节上限裁剪；cp932 字段顺带剔除无法编码的字符。

    v32+ 的 title/genre/artist/subtitle 是 UTF-16；v20–31 全是 cp932 0x40。
    """
    limit = TEXT_LIMITS.get(field)
    if not limit:
        return text
    if ver < 32:
        limit = 0x40                        # 旧代四个字段都是 0x40 的 cp932
        keep, used = [], 0
        for ch in text or "":
            try:
                b = ch.encode("cp932")
            except UnicodeEncodeError:
                continue
            if used + len(b) > limit - 2:
                break
            keep.append(ch)
            used += len(b)
        return "".join(keep)
    if field in ("title", "genre", "artist", "subtitle"):
        out = text
        while out and len(out.encode("utf-16-le", "ignore")) > limit - 2:
            out = out[:-1]
        return out
    # cp932 定长字符串
    keep, used = [], 0
    for ch in text:
        try:
            b = ch.encode("cp932")
        except UnicodeEncodeError:
            continue
        if used + len(b) > limit - 2:
            break
        keep.append(ch)
        used += len(b)
    return "".join(keep)


class MusicDB:
    def __init__(self, path: Path, read_only_ok: bool = False):
        self.path = Path(path)
        if not self.path.exists():
            raise MusicDBError(f"曲库文件不存在：{self.path}")
        from . import dbmask
        try:
            plain, self.masked = dbmask.read_db(self.path)
        except ValueError as e:
            raise MusicDBError(str(e)) from e
        raw = plain
        if self.masked:
            # mdt 只吃文件路径：混淆库先落成临时明文再解析
            import os
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix=".bin")
            os.close(fd)
            tmp_path = Path(tmp)
            tmp_path.write_bytes(plain)
            self._tmp_plain = tmp_path
        else:
            self._tmp_plain = None
        try:
            self._data = mdt.extract_file(
                str(self._tmp_plain or self.path), None, in_memory=True)
        except BaseException as e:                             # noqa: BLE001
            # vendor 在未知版本时是 raise SystemExit（不是 Exception），
            # 之前漏接 → 线程直接死掉 → 队列卡住
            ver = struct.unpack_from("<I", raw, 4)[0]
            raise MusicDBError(
                f"曲库版本 v{ver} 的布局我们还解析不了（只支持 v20–v34）。"
                f"原始错误：{e}") from e
        self.version = self._data["data_ver"]
        self.writable = self.version in SUPPORTED_VER
        if not self.writable and not read_only_ok:
            raise MusicDBError(
                f"曲库版本 v{self.version} 未验证（只支持 {sorted(SUPPORTED_VER)} 写入）。"
                f"可以读来查看，但不能转换/部署到这一代。")
        self.entries = self._data["data"]
        # ★ 记住原文件的槽表大小：默认保存必须沿用，否则会把扩大的 ID 空间写回 33000
        #   v<32 是 u16 @0x0A，v>=32 是 u32 @0x0C
        if self.version >= 32:
            self.slot_total = struct.unpack_from("<I", raw, 0x0C)[0]
        else:
            self.slot_total = struct.unpack_from("<H", raw, 0x0A)[0]

    # ---------------------------------------------------------------- 查询
    @property
    def ids(self) -> set[int]:
        return {e["song_id"] for e in self.entries}

    def find(self, song_id: int) -> dict | None:
        return next((e for e in self.entries if e["song_id"] == song_id), None)

    def summary(self) -> dict:
        ids = sorted(self.ids)
        return {"version": self.version, "count": len(self.entries),
                "id_min": ids[0] if ids else None, "id_max": ids[-1] if ids else None,
                "path": str(self.path)}

    # ---------------------------------------------------------------- 模板
    def template(self) -> dict:
        """挑一条"结构最简单"的记录当模板：无 DP、无 Beginner/Leggendaria。"""
        best = None
        best_score = 1e9
        for e in self.entries:
            if e["song_id"] == 0:
                continue
            score = 0
            score += sum(1 for k in ("DPB_level", "DPN_level", "DPH_level", "DPA_level", "DPL_level") if e[k])
            score += sum(1 for k in ("SPB_level", "SPL_level") if e[k])
            if e["song_id"] == 29912:
                score -= 0.5
            if score < best_score:
                best, best_score = e, score
        if best is None:
            raise MusicDBError("曲库里找不到可用模板记录")
        return best

    # ---------------------------------------------------------------- 写入
    def remove_song(self, song_id: int) -> bool:
        """删掉一条记录（重复转换同一首歌时用）。"""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["song_id"] != song_id]
        return len(self.entries) != before

    def add_song(self, song_id: int, title: str, title_ascii: str, genre: str, artist: str,
                 levels: dict[int, int], bga_filename: str = "",
                 subtitle: str = "", version: int | None = None,
                 font_idx: int | None = None) -> dict:
        """levels: {槽号: 等级}，槽号 1=SPN 0=SPH 2=SPA。"""
        if song_id in self.ids:
            raise MusicDBError(f"歌曲 ID {song_id} 已存在")
        entry = copy.deepcopy(self.template())
        entry.update({
            "song_id": song_id,
            "title": _trim(title, "title", self.version),
            "title_ascii": _trim(title_ascii, "title_ascii", self.version),
            "genre": _trim(genre, "genre", self.version),
            "artist": _trim(artist, "artist", self.version),
            "subtitle": _trim(subtitle, "subtitle", self.version),
            "game_version": version or self.version,
            "bga_filename": bga_filename,
            "SPB_level": 0, "SPN_level": 0, "SPH_level": 0, "SPA_level": 0, "SPL_level": 0,
            "DPB_level": 0, "DPN_level": 0, "DPH_level": 0, "DPA_level": 0, "DPL_level": 0,
            # 下面这些是从模板记录继承来的，必须显式清掉：
            #   afp_flag  = 0x6B0 的坏判层开关（继承会让歌"开关开着却没图形包"）
            #   afp_data  = 0x6B4 起的附加串
            #   other_folder = 曲库文件夹位图（不该跟着模板走）
            "font_idx": (pick_font_idx(title, subtitle)
                         if font_idx is None else int(font_idx)),
            # ★ 副标题要"显示"必须带 texture_subtitle（0x3D4）：官方带版权行的 68 首里
            #   50 首是 2、13 首是 1，剩下 5 首 subtex=0 的（JASRAC 标注）压根不显示。
            #   我们之前一直写 0 → 文字在库里但游戏不画。
            "texture_subtitle": 2 if (subtitle or "").strip() else 0,
            "afp_flag": 0,
            "afp_data": ["" for _ in range(len(entry.get("afp_data") or [0] * 10))],
            "other_folder": 0,
        })
        slot_key = {1: "SPN_level", 0: "SPH_level", 2: "SPA_level",
                    3: "SPB_level", 4: "SPL_level"}
        for slot, lv in levels.items():
            key = slot_key.get(slot)
            if key:
                entry[key] = max(1, min(12, int(lv)))
        self.entries.append(entry)
        return entry

    def save(self, out_path: Path, slot_total: int | None = None):
        if not getattr(self, "writable", True):
            raise MusicDBError(
                f"曲库版本 v{self.version} 未验证，拒绝写入（只支持 {sorted(SUPPORTED_VER)}）")
        """写曲库。

        slot_total：槽表大小，默认 None = 按 `data_ver*1000+1000`（正常情况）。
        传更大的值可以让 ID 突破当前代号的 1000 个号（例如 100000 → 支持 99xxx）。
        实现技巧：第三方 writer 只会按 `ver*1000+1000` 算表长，所以用一个"假的
        data_ver"写出去，再把头部 0x04 的版本号改回真实值——记录布局在 v32+ 是同一套，
        假版本必须 >= 32 才能保证格式正确。
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        natural = self.version * 1000 + 1000
        if slot_total is None:
            # ★ 默认沿用读取时的槽表大小，避免把扩大的 ID 空间悄悄写回 33000
            slot_total = getattr(self, "slot_total", None) or natural
        if slot_total == natural:
            with open(out_path, "wb") as f:
                mdt.writer(self.version, f, self.entries)
            self._maybe_mask(out_path)
            return out_path

        if self.version < 32 and slot_total != natural:
            raise MusicDBError(
                f"v{self.version} 的槽表大小固定为 {natural}，不能扩到 {slot_total}")
        if slot_total % 1000 or slot_total < natural:
            raise MusicDBError(
                f"slot_total 必须是 1000 的倍数且 >= {natural}，收到 {slot_total}")
        fake_ver = slot_total // 1000 - 1        # fake_ver*1000+1000 == slot_total
        if fake_ver < 32:
            raise MusicDBError("slot_total 太小，对应版本号 < 32 会破坏记录布局")

        import io
        import struct
        buf = io.BytesIO()
        mdt.writer(fake_ver, buf, self.entries)
        raw = bytearray(buf.getvalue())
        struct.pack_into("<I", raw, 4, self.version)      # 版本号改回真实值
        with open(out_path, "wb") as f:
            f.write(raw)
        self._maybe_mask(out_path)
        return out_path

    def _maybe_mask(self, path: Path) -> None:
        """原库是混淆格式（omnimix）时，写回也要保持混淆，否则游戏读不了"""
        if not getattr(self, "masked", False):
            return
        from . import dbmask
        path = Path(path)
        raw = path.read_bytes()
        path.write_bytes(dbmask.mask(raw))


def id_block_of(db: MusicDB) -> tuple[int, int]:
    """本代号段的可分配范围 [下限, 上限]（v34 → 34000..34999）"""
    base = db.version * 1000
    return base, base + 999


def pick_font_idx(*texts: str) -> int:
    """按文字内容挑字体号（0=纯 ASCII 字体，2=日文字体）

    实测官方 34 库：font 0 的 1947 首几乎全是纯 ASCII 标题，
    带假名/汉字的 120+157+159+185 首分别用 1/2/3/4（都是日文字体家族）。
    我们的模板歌是纯 ASCII（font 0），所以带中文/日文的标题会缺字形 → 乱码。
    """
    for s in texts:
        if not s:
            continue
        if any(ord(c) > 0x7F for c in s):
            return 2
    return 0


def allocate_song_id(db: MusicDB, top: int | None = None,
                     block: int | None = None) -> int:
    """从号段上限往下找第一个空位。

    top=None → 用本代号段的顶（v34 就是 34999），不写死任何版本号。
    top 可显式指定（GUI 里可改），用于跨代/实验性分配。
    block 指定号段（千位）：放 SUBSTREAM 时官方歌都在 1xxx，所以按文件夹选号段
    （block=1 → 1000~1999）。不传就沿用本代号段。
    """
    if db.version < 32:
        # 旧代：槽表是 u16，空槽/占位槽很多，直接扫真实空位，从高往低取
        from . import dbmask, reapply
        raw, _masked = dbmask.read_db(db.path)
        lay, _avail, slots, _base = reapply.layout_of(raw)
        lo, hi2 = id_block_of(db)
        top_legacy = hi2 if top is None else int(top)
        if block is not None:
            lo = max(1000, int(block) * 1000)
            top_legacy = min(top_legacy, lo + 999)
        free = reapply.free_slots(raw, lo, min(top_legacy, slots - 1))
        if not free:
            raise MusicDBError(f"号段 {lo}~{top_legacy} 没有空位")
        return max(free)
    if block is not None:
        lo = max(1000, int(block) * 1000)
        natural_hi = lo + 999
    else:
        lo, natural_hi = id_block_of(db)
    hi = natural_hi if top is None else int(top)
    if hi < lo:
        raise MusicDBError(f"ID 上限 {hi} 低于本代号段起点 {lo}")
    if hi > MAX_ID:
        raise MusicDBError(f"ID 上限 {hi} 超过最大值 {MAX_ID}")
    used = db.ids
    for sid in range(hi, lo - 1, -1):
        if sid not in used:
            return sid
    raise MusicDBError(
        f"号段 {lo}~{hi} 已经没有空位；可以调高「ID 上限」到其它号段，"
        f"或手动指定一个空号")


def slot_total_for(db: MusicDB, song_id: int) -> int:
    """要让 song_id 能用，槽表至少要多大（1000 的倍数）"""
    need = (song_id // 1000 + 1) * 1000
    natural = db.version * 1000 + 1000
    return max(need, natural)


