"""谱面文本编码判别（曲库索引与转换器共用）

BMS 世界不是只有 cp932。实际遇到的还有：
  · cp949 / euc-kr —— 韩文作者（`새벽의 소녀들`）
  · euc_jp          —— 日本作者的老工具（cp932 会解成一堆半角片假名）
  · iso2022_jp      —— 带 ESC $B … ESC ( B 转义的 JIS 文本
  · utf-8 / utf-16  —— 新工具导出（可能有 BOM）

判别顺序：
  1. BOM / UTF-16 特征 / ISO-2022-JP 的 ESC 序列
  2. cp932 能干净解出、且没有"一堆半角片假名" → cp932（最常见，也最快）
  3. 否则在 cp949 / euc_jp / utf-8 里按字符类别打分取最高
"""
from __future__ import annotations

_BOM16 = (b"\xff\xfe", b"\xfe\xff")


def cls_score(s: str) -> int:
    """给解码结果打分：假名/韩文/汉字加分，半角片假名与乱码减分"""
    kana = hang = cjk = half = bad = 0
    for ch in s:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            kana += 1
        elif 0xAC00 <= o <= 0xD7A3:
            hang += 1
        elif 0x4E00 <= o <= 0x9FFF:
            cjk += 1
        elif 0xFF61 <= o <= 0xFF9F:
            half += 1
        elif o == 0xFFFD or o < 0x09:
            bad += 1
    return 2 * (kana + hang) + cjk - 3 * half - 5 * bad


def detect_encoding(raw: bytes) -> str:
    if raw[:2] in _BOM16:
        return "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    if b"\x1b$B" in raw or b"\x1b$@" in raw:
        return "iso2022_jp"
    s = raw.decode("cp932", errors="replace")
    if s.count("\ufffd") <= 1 and sum(1 for c in s if 0xFF61 <= ord(c) <= 0xFF9F) < 2:
        return "cp932"
    best, best_score = "cp932", cls_score(s)
    for enc in ("cp949", "euc_jp", "utf-8"):
        try:
            cand = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        score = cls_score(cand)
        if score > best_score:
            best, best_score = enc, score
    return best


def decode(raw: bytes, enc: str | None = None) -> str:
    """按判别结果解码整段文本（失败时退回 cp932 带替换）"""
    enc = enc or detect_encoding(raw)
    try:
        text = raw.decode(enc, errors="replace")
    except LookupError:
        text = raw.decode("cp932", errors="replace")
    return text.lstrip("\ufeff")
