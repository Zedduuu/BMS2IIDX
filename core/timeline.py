"""BMS 位置 → IIDX tick 的时间轴引擎

关键事实（已由真实数据验证）：
  · IIDX tick 单位 = 1/1000 秒，所以 tick 数值就等于毫秒
  · BPM 事件 param=1 或 100，BPM = value/param；真实谱面里存在 BPM=3 的极端值
    （某些曲子用它制造停顿），所以"用极小 BPM 伪造 #STOP"与游戏原生做法一致
  · BMS 的 #STOPxx 单位是 1/192 小节，换算成毫秒 = value * 1250 / bpm
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .bms import BMSChart

STOP_BPM = 1.0          # 伪造停顿用，真实谱面里最低见到 3
MIN_BPM = 0.01


@dataclass
class TickNote:
    tick: int
    column: int
    sample: int
    ln_ticks: int = 0
    side: int = 0            # 0=P1 1=P2


@dataclass
class TickChart:
    notes: list = field(default_factory=list)
    bgm: list = field(default_factory=list)
    bga: list = field(default_factory=list)     # [(tick, #BMP 号, 层)]
    bpm_events: list = field(default_factory=list)
    measure_ticks: list = field(default_factory=list)
    total_ms: int = 0
    initial_bpm: float = 120.0
    warnings: list = field(default_factory=list)

    @property
    def note_count(self) -> int:
        return sum(2 if n.ln_ticks > 0 else 1 for n in self.notes)


def initial_bpm_of(chart: BMSChart) -> float:
    try:
        return float(chart.header.get("BPM", "120").split()[0])
    except (ValueError, IndexError):
        return 120.0


def build(chart: BMSChart) -> TickChart:
    bpm0 = initial_bpm_of(chart)
    out = TickChart(initial_bpm=bpm0)

    # ---- 1) 收集会改变时间流速的事件 ----
    bpm_at: dict[Fraction, float] = {}
    for pos, v in chart.bpm_events:
        b = v if isinstance(v, float) else chart.bpm_def.get(v[1])
        if b and b >= MIN_BPM:
            bpm_at[pos] = float(b)
        else:
            out.warnings.append(f"BPM 事件无效（{v}），已忽略")

    stop_at: dict[Fraction, float] = {}
    for pos, v in chart.stop_events:
        val = float(chart.stop_def.get(v, 0))
        if val > 0:
            stop_at[pos] = val
        else:
            out.warnings.append(f"STOP 引用不存在的 #STOP{v:02X}，已忽略")

    # ---- 2) 所有需要算时刻的位置 ----
    positions: set[Fraction] = set()
    for n in chart.notes:
        positions.add(n.measure)
        if n.ln_end_measure not in (None, -1.0):
            positions.add(n.ln_end_measure)
    for b in chart.bgm:
        positions.add(b.measure)
    for e in getattr(chart, "bga", []):
        positions.add(e.measure)
    positions |= set(bpm_at) | set(stop_at)
    if not positions:
        positions.add(Fraction(0))

    max_measure = max(int(p) for p in positions)
    buckets: dict[int, list[Fraction]] = {}
    for p in positions:
        buckets.setdefault(int(p), []).append(p - int(p))
    for k in buckets:
        buckets[k].sort()

    # ---- 3) 单遍推进 ----
    t = 0.0
    bpm = bpm0
    out.bpm_events.append((0, bpm))
    pos_tick: dict[Fraction, int] = {}
    measure_start = {0: 0}

    for m in range(max_measure + 1):
        beats = 4.0 * chart.measure_len.get(m, 1.0)
        last = Fraction(0)
        for frac in buckets.get(m, []):
            t += float(frac - last) * beats * 60000.0 / bpm
            pos = m + frac
            pos_tick[pos] = int(round(t))          # 同位置音符先记时刻（在停顿之前）
            for p, b in bpm_at.items():
                if p == pos:
                    bpm = b
                    out.bpm_events.append((int(round(t)), bpm))
            for p, v in stop_at.items():
                if p == pos:
                    dur_ms = v * 1250.0 / bpm
                    out.bpm_events.append((int(round(t)), STOP_BPM))
                    t += dur_ms
                    out.bpm_events.append((int(round(t)), bpm))
                    out.warnings.append(
                        f"停顿 {dur_ms:.0f}ms（小节 {m} 位置 {float(frac):.3f}）→ 用 BPM {STOP_BPM} 伪造"
                    )
            last = frac
        t += float(1 - last) * beats * 60000.0 / bpm
        if m + 1 <= max_measure:
            measure_start[m + 1] = int(round(t))

    out.measure_ticks = [measure_start[i] for i in range(max_measure + 1)]
    out.total_ms = int(round(t))

    # ---- 4) 落地音符 ----
    for n in chart.notes:
        start = pos_tick[n.measure]
        ln = 0
        if n.ln_end_measure not in (None, -1.0):
            ln = max(0, pos_tick[n.ln_end_measure] - start)
            if ln == 0:
                out.warnings.append(f"长按长度为 0（列 {n.column} tick {start}）→ 降级为单点")
        out.notes.append(TickNote(start, n.column, n.value, ln,
                          getattr(n, "side", 0)))
    for b in chart.bgm:
        out.bgm.append((pos_tick[b.measure], b.value))
    for e in getattr(chart, "bga", []):
        t = pos_tick.get(e.measure)
        if t is not None:
            out.bga.append((t, e.value, e.layer))

    out.notes.sort(key=lambda x: (x.tick, x.column))
    out.bgm.sort(key=lambda x: x[0])
    return out
