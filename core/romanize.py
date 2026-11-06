"""韩文 → 罗马字（Revised Romanization），以及文字类别判断

为什么需要：IIDX 的主字体（DF 系列）只有 JIS 日文假名/汉字，**没有韩文字形**
（实测 DF-PopMix-W5：假名 177、韩文 0、汉字 6682）。把韩文标题原样写进曲库，
游戏里会显示成方块。所以转换时建议把韩文标题转成罗马字。

实现的是「逐音节直译」（不含连音/同化规则），足够让标题可读：
    새벽의 소녀들  →  saebyeok-ui sonyeodeul
"""
from __future__ import annotations

# 初声（19）
_INIT = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
         "j", "jj", "ch", "k", "t", "p", "h"]
# 中声（21）
_VOWEL = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
          "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
# 终声（28，第一个是"无"）
_FINAL = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "p", "l",
          "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t",
          "p", "t"]

_SYL_BASE = 0xAC00


def hangul_to_romaja(text: str) -> str:
    """把韩文音节转成罗马字；非韩文字符原样保留"""
    out = []
    prev_final = 0
    for ch in text:
        o = ord(ch)
        if _SYL_BASE <= o <= 0xD7A3:
            i = o - _SYL_BASE
            init = i // 588
            vowel = (i % 588) // 28
            final = i % 28
            if prev_final and init == 11:          # 前字有终声 + 本字以 ㅇ 开头
                out.append("-")
            out.append(_INIT[init] + _VOWEL[vowel] + _FINAL[final])
            prev_final = final
        else:
            out.append(ch)
            prev_final = 0
    s = "".join(out)
    # 去掉连字符式的空白重复
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def chinese_to_pinyin(text: str) -> str:
    """中文 → 无声调拼音；pypinyin 没装就返回空串（不影响其它功能）"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return ""
    parts = []
    for chunk in text.split(" "):
        parts.append(" ".join(lazy_pinyin(chunk)))
    s = " ".join(parts)
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def script_counts(text: str) -> dict:
    """统计标题里各类字符：韩文/假名/汉字/拉丁"""
    c = {"hangul": 0, "kana": 0, "cjk": 0, "latin": 0, "other": 0}
    for ch in text or "":
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF:
            c["hangul"] += 1
        elif 0x3040 <= o <= 0x30FF:
            c["kana"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            c["cjk"] += 1
        elif o < 0x80:
            c["latin"] += 1
        else:
            c["other"] += 1
    return c


def unrenderable(text: str) -> list[str]:
    """返回 IIDX 字体里没有、会显示成方块的字（去掉重复，保序）"""
    from . import glyphs
    seen: list[str] = []
    for ch in text or "":
        if ch in seen:
            continue
        if ch in "\r\n\t":
            continue
        if not glyphs.can_render(ch):
            seen.append(ch)
    return seen


def iidx_unsafe_reason(text: str) -> str:
    """这句标题在 IIDX 里会不会显示成方块；空字符串 = 没问题"""
    bad = unrenderable(text)
    if not bad:
        return ""
    c = script_counts(text)
    if c["hangul"]:
        return f"含韩文（{len(bad)} 个字形缺失，IIDX 字体没有韩文）"
    return f"含 {len(bad)} 个 IIDX 字体没有的字（例：{''.join(bad[:4])}）"


# 常见的"只在简体里出现"的字，用来区分简体中文与日文汉字
_SIMPLIFIED_HINTS = set(
    "东车马长门问间闻关兴举义乐习书买卖车东亚严业丛丝两丽举义乌乐乔习乡书买"
    "亏云亚产亩亲亿仅从仑仓仪们价众优会伞伟传伤伦伪体余佣侠侣侥侧侨侩侪侬俣"
    "俭债倾偻偾偿傥傧储儿兑党兰关兴养兽内冈册写军农冯冲决况冻净凉减凑凛几凤"
)


def _looks_japanese_kanji(text: str) -> bool:
    """粗判：出现简体专用字就当作中文；否则当日本汉字（能显示）"""
    return not any(ch in _SIMPLIFIED_HINTS for ch in text)


def suggest_title(text: str) -> tuple[str, str]:
    """给出 (建议标题, 原因)。原因空 = 不用改；建议为空 = 需要人自己想个英文名"""
    reason = iidx_unsafe_reason(text)
    if not reason:
        return "", ""
    c = script_counts(text)
    if c["hangul"]:
        return hangul_to_romaja(text), reason
    if c["cjk"] and not c["kana"]:
        py = chinese_to_pinyin(text)
        if py:
            return py, reason
    return "", reason
