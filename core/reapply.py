"""增量写入 / 重应用（v20–v34 通用）

不做整份重建，只做三件事：
  1. 在记录区末尾追加一条记录（长度按版本）
  2. 把槽表对应项指向这条新记录（索引宽度按版本：u16 / i32）
  3. avail（记录数）+1

各代布局（实测）：

| 版本 | 记录长度 | 槽表元素 | song_id 偏移 | ORIGIN/version 偏移 |
|---|---|---|---|---|
| 20–21 | 800 | u16 | 0x1C8 | 0x118 |
| 22–25 | 832 | u16 | 0x1C8 | 0x118 |
| 26 | 836 | u16 | 0x1C8 | 0x118 |
| 27–31 | 1324 | u16 | 0x3B0 | 0x118 |
| 32–34 | 2040 | i32 | 0x67C | 0x3DC |

头 16 字节：`IIDX` | ver u32 | avail u16 | 对齐字段 | 槽表大小
  · v<32：槽表大小是 u16 @0x0A，0x0C 起 4 字节为 0
  · v≥32：槽表大小是 u32 @0x0C
"""
from __future__ import annotations

import dataclasses
import pathlib
import struct

from . import dbmask

HDR_MAGIC = b"IIDX"
RECORD_SIZE = 2040          # 新代记录长度（保留旧名字给外部引用）
EMPTY16 = 0xFFFF


class PatchError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Layout:
    ver: int
    idx_w: int              # 2 或 4
    rec: int                # 记录长度
    song_id_off: int        # 记录内 song_id 偏移
    gv_off: int             # 记录内 game_version / ORIGIN 字段偏移

    @property
    def empty_idx(self) -> int:
        return EMPTY16 if self.idx_w == 2 else -1


def layout_for(ver: int) -> Layout:
    if ver >= 32:
        return Layout(ver, 4, 2040, 0x67C, 0x3DC)
    if ver >= 27:
        return Layout(ver, 2, 1324, 0x3B0, 0x118)
    if ver >= 26:
        return Layout(ver, 2, 836, 0x1C8, 0x118)
    if ver >= 22:
        return Layout(ver, 2, 832, 0x1C8, 0x118)
    if ver >= 20:
        return Layout(ver, 2, 800, 0x1C8, 0x118)
    raise PatchError(f"不支持的曲库版本 v{ver}（支持 20–34）")


def _header(raw: bytes):
    """返回 (ver, avail, unk, slots)，槽表大小按版本取对字段"""
    if raw[:4] != HDR_MAGIC:
        raise PatchError("不是 IIDX 曲库文件")
    ver = struct.unpack_from("<I", raw, 4)[0]
    avail = struct.unpack_from("<H", raw, 8)[0]
    if ver >= 32:
        unk = struct.unpack_from("<H", raw, 10)[0]
        slots = struct.unpack_from("<I", raw, 12)[0]
    else:
        slots = struct.unpack_from("<H", raw, 10)[0]
        unk = struct.unpack_from("<I", raw, 12)[0]
    return ver, avail, unk, slots


def layout_of(raw: bytes) -> tuple[Layout, int, int, int]:
    """返回 (layout, avail, slots, 记录区起点)"""
    ver, avail, _unk, slots = _header(raw)
    lay = layout_for(ver)
    return lay, avail, slots, 16 + slots * lay.idx_w


def _read_idx(raw: bytes, slot: int, lay: Layout) -> int:
    if lay.idx_w == 4:
        return struct.unpack_from("<i", raw, 16 + slot * 4)[0]
    return struct.unpack_from("<H", raw, 16 + slot * 2)[0]


def _write_idx(buf: bytearray, slot: int, value: int, lay: Layout) -> None:
    if lay.idx_w == 4:
        struct.pack_into("<i", buf, 16 + slot * 4, value)
    else:
        struct.pack_into("<H", buf, 16 + slot * 2, value)


def _is_empty(idx: int, lay: Layout) -> bool:
    return idx == lay.empty_idx or idx < 0


def ids_in_db(raw: bytes) -> dict[int, int]:
    """返回 {歌曲ID: 记录索引}（只认「槽号 == 记录里的 song_id」的真实曲目）"""
    lay, _avail, slots, base = layout_of(raw)
    out: dict[int, int] = {}
    for slot in range(slots):
        idx = _read_idx(raw, slot, lay)
        if _is_empty(idx, lay):
            continue
        off = base + idx * lay.rec
        if off + lay.rec > len(raw):
            continue
        sid = struct.unpack_from("<I", raw, off + lay.song_id_off)[0]
        if sid == slot:
            out[sid] = idx
    return out


def free_slots(raw: bytes, lo: int, hi: int) -> list[int]:
    """槽号在 [lo,hi] 里可复用的位置：空槽，或指向占位/别名记录"""
    lay, _avail, slots, base = layout_of(raw)
    used = set(ids_in_db(raw))
    out: list[int] = []
    for slot in range(lo, min(hi, slots - 1) + 1):
        if slot in used:
            continue
        idx = _read_idx(raw, slot, lay)
        if _is_empty(idx, lay):
            out.append(slot)
            continue
        off = base + idx * lay.rec
        if off + lay.rec > len(raw):
            out.append(slot)          # 指向不存在的记录（空库常见：槽值写 0）→ 可用
            continue
        sid = struct.unpack_from("<I", raw, off + lay.song_id_off)[0]
        if sid != slot:
            out.append(slot)          # 占位/别名记录 → 可用
    return out


def insert_record(raw: bytes, record: bytes, slot: int,
                  game_version: int | None = None) -> tuple[bytes, int]:
    """把一条记录插进曲库，占用指定槽位。返回 (新字节, 新记录索引)"""
    lay, avail, slots, base = layout_of(raw)
    if len(record) != lay.rec:
        raise PatchError(f"v{lay.ver} 的记录长度必须是 {lay.rec}，收到 {len(record)}")
    if len(raw) != base + avail * lay.rec:
        raise PatchError(f"曲库结构异常：期望 {base + avail * lay.rec} 字节，"
                         f"实际 {len(raw)}")
    if slot >= slots:
        raise PatchError(f"槽号 {slot} 超出槽表大小 {slots}")
    if lay.idx_w == 2 and avail >= EMPTY16 - 1:
        raise PatchError("记录数已达 u16 上限")

    rec = bytearray(record)
    struct.pack_into("<I", rec, lay.song_id_off, slot)      # 记录 id 必须等于槽号
    if game_version is not None:
        struct.pack_into("<H", rec, lay.gv_off, game_version)

    new_idx = avail
    out = bytearray(raw)
    out.extend(rec)
    _write_idx(out, slot, new_idx, lay)
    struct.pack_into("<H", out, 8, avail + 1)
    return bytes(out), new_idx


def insert_many(db_path: pathlib.Path, entries: list[tuple[int, bytes]],
                game_version: int | None = None,
                prefer_original_slot: bool = True) -> dict:
    """批量插入。entries = [(期望槽号, 记录字节)]，返回实际使用的槽号映射"""
    raw, _masked = dbmask.read_db(db_path)
    ver, _avail, _unk, _slots = _header(raw)
    lo, hi = ver * 1000, ver * 1000 + 999
    result = {"used": {}, "moved": [], "skipped": []}
    for want, record in entries:
        free = free_slots(raw, lo, hi)
        slot = want if (prefer_original_slot and want in free) else (max(free) if free else None)
        if slot is None:
            result["skipped"].append(want)
            continue
        raw, _idx = insert_record(raw, record, slot, game_version)
        result["used"][want] = slot
        if slot != want:
            result["moved"].append((want, slot))
    dbmask.write_db(db_path, raw, _masked)
    return result


def remove_song(db_path: pathlib.Path, song_id: int) -> bool:
    """把一首歌从曲库摘掉：槽表置空；若正好是最后一条记录，顺带截断"""
    _plain, _masked = dbmask.read_db(db_path)
    raw = bytearray(_plain)
    lay, avail, slots, base = layout_of(bytes(raw))
    hit_idx = hit_slot = None
    for slot in range(slots):
        idx = _read_idx(bytes(raw), slot, lay)
        if _is_empty(idx, lay):
            continue
        off = base + idx * lay.rec
        if off + lay.rec > len(raw):
            continue
        if struct.unpack_from("<I", raw, off + lay.song_id_off)[0] == song_id:
            hit_idx, hit_slot = idx, slot
            break
    if hit_idx is None:
        return False

    _write_idx(raw, hit_slot, lay.empty_idx, lay)
    if hit_idx == avail - 1:
        del raw[base + hit_idx * lay.rec: base + (hit_idx + 1) * lay.rec]
        struct.pack_into("<H", raw, 8, avail - 1)
    dbmask.write_db(db_path, bytes(raw), _masked)
    return True


# ------------------------------------------------------------------ merged XML
def build_merged_video(entries: list[dict]) -> bytes:
    """entries: [{id,title,artist,flags}]"""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<mdb>"]
    for e in sorted(entries, key=lambda x: -x["id"]):
        lines += [f'  <music id="{e["id"]}">', "    <info>",
                  f'      <title_name>{_esc(e["title"])}</title_name>',
                  f'      <artist_name>{_esc(e["artist"])}</artist_name>',
                  f'      <play_video_flags>{e.get("flags", 6)}</play_video_flags>',
                  "    </info>", "  </music>"]
    lines.append("</mdb>")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def build_merged_yomi(entries: list[dict], key: str = "yomi") -> bytes:
    """entries: [{id, yomi}]，Shift-JIS 编码；编不了的条目跳过"""
    lines = ['<?xml version="1.0" encoding="Shift-JIS"?>', "<data_list>"]
    for e in sorted(entries, key=lambda x: -x["id"]):
        lines += ["  <data>",
                  f'    <index __type="s32">{e["id"]}</index>',
                  f'    <{key} __type="str">{_esc(e["yomi"])}</{key}>',
                  "  </data>"]
    lines.append("</data_list>")
    text = "\r\n".join(lines) + "\r\n"
    return text.encode("shift_jis", "replace")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
