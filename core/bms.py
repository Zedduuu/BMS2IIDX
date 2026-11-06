"""BMS 解析器（阶段 2 第一块）

只做"把 BMS 读成结构化数据"，不碰 IIDX 格式。
支持：7 键 + 皿、BPM 变化（#BPMxx / 03 / 08）、STOP（#STOPxx / 09）、
     小节长度（#02）、长按（#LNTYPE 1 与 #LNOBJ）、BGM 通道（#01）、
     keysound 通道（#11-#19）、多难度判定所需的头部字段。
明确拒绝：DP（#PLAYER 3）、9 键 PMS、#RANDOM / #IF 分支。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# P1 通道 → IIDX 轨道列（0..7 = Key1..Key7 + Scratch）
CH_P1 = {"11": 0, "12": 1, "13": 2, "14": 3, "15": 4, "18": 5, "19": 6, "16": 7}
# P2 / DP 通道
CH_P2 = {"21": 0, "22": 1, "23": 2, "24": 3, "25": 4, "28": 5, "29": 6, "26": 7}
# 长按通道（LNTYPE 1，P1）
CH_LN_P1 = {"51": 0, "52": 1, "53": 2, "54": 3, "55": 4, "58": 5, "59": 6, "56": 7}
# 长按通道：61-69 是 P2 侧（不是"LNTYPE 2"）
CH_LN_P2 = {"61": 0, "62": 1, "63": 2, "64": 3, "65": 4, "68": 5, "69": 6, "66": 7}
# 不可见按键（会发声但不显示音符）→ 当作自动播放并入 BGM 流
CH_INVIS = {"31": 0, "32": 1, "33": 2, "34": 3, "35": 4, "38": 5, "39": 6, "36": 7}
# 注意：04/06/07 是 BGA 通道（值指向 BMP 而不是 WAV），绝不可以当 BGM 处理

CH_BGA_BASE = "04"     # BGA 基础层（值指向 #BMP 而不是 #WAV）
CH_BGA_POOR = "06"     # BGA 坏判定层（angolmois 文档：06 = POOR BGA）
CH_BGA_LAYER = "07"    # BGA 中间层（middle layer）
CH_BGA_TOP = "0A"      # BGA 顶层（top layer）；本库普查未见使用，仍支持
CH_BGM = "01"
CH_MEASURE = "02"
CH_BPM_HEX = "03"
CH_BPM_IDX = "08"
CH_STOP = "09"


def b36(s: str) -> int:
    return B36.index(s[0].upper()) * 36 + B36.index(s[1].upper())


def b36_pairs(data: str) -> list[int] | None:
    """把通道数据拆成对象号列表；遇到非法字符返回 None（真实文件里确实有脏数据）。"""
    data = data.strip()
    if len(data) % 2:
        data = data[:-1]
    out = []
    for i in range(0, len(data), 2):
        p = data[i:i + 2].upper()
        if p[0] not in B36 or p[1] not in B36:
            return None
        out.append(B36.index(p[0]) * 36 + B36.index(p[1]))
    return out


def detect_encoding(raw: bytes) -> str:
    """谱面文本编码（cp932/cp949/euc_jp/iso2022_jp/utf-8/utf-16）"""
    from . import enc as enc_mod
    return enc_mod.detect_encoding(raw)


@dataclass
class Note:
    measure: float          # 绝对小节（浮点，含小节内位置）
    column: int             # 0..7
    value: int              # BMS 对象号（WAV 号）
    ln_end_measure: float | None = None   # 长按时为结束位置
    side: int = 0           # 0=P1（单打），1=P2（DP 右侧）


@dataclass
class BGATick:
    measure: float        # 绝对小节（浮点）
    value: int            # #BMP 编号
    layer: int = 0        # 0=04 基础 1=06 坏判定 2=07 叠加


@dataclass
class BGM:
    measure: float
    value: int


@dataclass
class BMSChart:
    path: Path
    encoding: str
    header: dict = field(default_factory=dict)
    wav: dict = field(default_factory=dict)          # 对象号 → 文件名
    bmp: dict = field(default_factory=dict)          # #BMP 对象号 → 图片文件名（BGA）
    bpm_def: dict = field(default_factory=dict)      # #BPMxx
    stop_def: dict = field(default_factory=dict)     # #STOPxx
    notes: list = field(default_factory=list)        # list[Note]
    bgm: list = field(default_factory=list)
    bga: list = field(default_factory=list)   # BGA 事件（04/06/07）          # list[BGM]
    bpm_events: list = field(default_factory=list)   # [(measure, bpm)]
    stop_events: list = field(default_factory=list)  # [(measure, stop_value_192)]
    measure_len: dict = field(default_factory=dict)  # measure → 倍数
    rejected: list = field(default_factory=list)     # 拒绝原因

    @property
    def title(self):
        return self.header.get("TITLE", "")

    @property
    def artist(self):
        return self.header.get("ARTIST", "")

    @property
    def genre(self):
        return self.header.get("GENRE", "")

    @property
    def player(self):
        return int(self.header.get("PLAYER", "1").split()[0] or 1)

    @property
    def is_dp(self):
        return self.player == 3 or any(ch in self.header.get("_channels", []) for ch in CH_P2)

    @property
    def lnobj(self):
        v = self.header.get("LNOBJ", "")
        return b36(v) if re.fullmatch(r"[0-9A-Za-z]{2}", v or "") else None


LINE_HEADER = re.compile(r"^#(\w+)\s+(.*)$")
LINE_CHANNEL = re.compile(r"^#(\d{3})([0-9A-Za-z]{2}):(.*)$")


def parse(path: Path) -> BMSChart:
    raw = path.read_bytes()
    enc = detect_encoding(raw)
    text = raw.decode(enc, errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    chart = BMSChart(path=path, encoding=enc)
    channels_used = set()

    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line.startswith("#"):
            continue
        if re.match(r"^#(RANDOM|IF|ELSEIF|ELSE|ENDIF|ENDRANDOM)\b", line, re.I):
            _reject(chart, "随机分支 #RANDOM/#IF")
            continue
        m = LINE_CHANNEL.match(line)
        if m:
            measure = int(m.group(1))
            ch = m.group(2).upper()
            data = m.group(3).strip()
            channels_used.add(ch)
            # 小节长度通道用的是十进制小数（如 0.5），不是对象号
            if ch == CH_MEASURE:
                try:
                    chart.measure_len[measure] = float(data)
                except ValueError:
                    _reject(chart, f"#02 小节长度无法解析: {data!r}")
                continue
            pairs = b36_pairs(data)
            if pairs is None:
                _reject(chart, f"通道 {ch} 含非法字符")
                continue
            if not any(pairs):
                continue
            n = len(pairs)
            for i, v in enumerate(pairs):
                if v == 0:
                    continue
                pos = Fraction(measure) + Fraction(i, n)
                if ch == CH_BGM:
                    chart.bgm.append(BGM(pos, v))
                elif ch == CH_BGA_BASE:
                    chart.bga.append(BGATick(pos, v, 0))          # 04 基础图层
                elif ch == CH_BGA_POOR:
                    chart.bga.append(BGATick(pos, v, 1))          # 06 坏判定图层
                elif ch == CH_BGA_LAYER:
                    chart.bga.append(BGATick(pos, v, 2))          # 07 中间图层
                elif ch == CH_BGA_TOP:
                    chart.bga.append(BGATick(pos, v, 3))          # 0A 顶层
                elif ch in CH_INVIS:
                    # 不可见按键：有声音但不显示音符 → 并入自动播放
                    chart.bgm.append(BGM(pos, v))
                elif ch in CH_P1:
                    chart.notes.append(Note(pos, CH_P1[ch], v))
                elif ch in CH_P2:
                    chart.notes.append(Note(pos, CH_P2[ch], v, side=1))
                elif ch in CH_LN_P1:
                    chart.notes.append(Note(pos, CH_LN_P1[ch], v, ln_end_measure=-1.0))
                elif ch in CH_LN_P2:
                    chart.notes.append(Note(pos, CH_LN_P2[ch], v,
                                            ln_end_measure=-1.0, side=1))
                elif ch == CH_BPM_HEX:
                    try:
                        chart.bpm_events.append((pos, float(int(data[i * 2:i * 2 + 2], 16))))
                    except ValueError:
                        _reject(chart, f"BPM 十六进制非法: {data[i*2:i*2+2]!r}")
                elif ch == CH_BPM_IDX:
                    chart.bpm_events.append((pos, ("idx", v)))
                elif ch == CH_STOP:
                    chart.stop_events.append((pos, v))

            continue
        m = LINE_HEADER.match(line)
        if m:
            key, val = m.group(1).upper(), m.group(2).strip()
            if key == "WAV":
                pass
            if re.fullmatch(r"WAV[0-9A-Z]{2}", key):
                chart.wav[b36(key[3:5])] = val
            elif re.fullmatch(r"BMP[0-9A-Z]{2}", key):
                chart.bmp[b36(key[3:5])] = val          # 图片定义（BGA 用）
            elif re.fullmatch(r"BPM[0-9A-Z]{2}", key):
                chart.bpm_def[b36(key[3:5])] = float(val.split()[0])
            elif re.fullmatch(r"STOP[0-9A-Z]{2}", key):
                chart.stop_def[b36(key[4:6])] = float(val.split()[0])
            else:
                chart.header[key] = val
    chart.header["_channels"] = channels_used

    _resolve_ln(chart)
    return chart


def _reject(chart: BMSChart, reason: str):
    if reason not in chart.rejected:
        chart.rejected.append(reason)


def _resolve_ln(chart: BMSChart):
    """处理两种长按写法，把 ln_end_measure = -1 的占位补成真实结束位置。"""
    lnobj = chart.lnobj
    # 1) LNTYPE 1：同一列上成对出现（起点、终点交替）
    by_col = {}
    for n in chart.notes:
        if n.ln_end_measure == -1.0:
            by_col.setdefault(n.column, []).append(n)
    for col, lst in by_col.items():
        lst.sort(key=lambda n: n.measure)
        for i in range(0, len(lst) - 1, 2):
            lst[i].ln_end_measure = lst[i + 1].measure
            lst[i + 1].mark_skip = True  # type: ignore[attr-defined]
    chart.notes = [n for n in chart.notes if not getattr(n, "mark_skip", False)]

    # 2) #LNOBJ：普通轨上，等于 LNOBJ 的对象是"前一个同列音符"的终点
    if lnobj:
        normal = {}
        keep = []
        for n in sorted(chart.notes, key=lambda n: n.measure):
            if n.ln_end_measure is not None:
                keep.append(n)
                continue
            if n.value == lnobj and n.column in normal:
                normal[n.column].ln_end_measure = n.measure
                normal.pop(n.column, None)
                continue
            normal[n.column] = n
            keep.append(n)
        chart.notes = keep


