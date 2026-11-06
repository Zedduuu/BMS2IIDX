"""导入计划（Import Plan）：把"拖进来的 BMS 文件夹"变成 GUI 能直接渲染的数据

设计目标（对应用户方案）
------------------------
1. 列出文件夹里**所有**谱面，每条给出：文件名、#DIFFICULTY、作者等级、音符数、曲长、
   平均/峰值 nps、**推荐的 IIDX 等级**、长按数、键数、BPM、是否 DP/被拒。
2. 显示 **5 个 2DX 难度槽**（HYPER/NORMAL/ANOTHER/LEGGENDARIA/BEGINNER）。
3. 谱面 ≤ 5 → 自动进槽；> 5 → 自动按 #DIFFICULTY 先占，剩下的留给用户手工拖，
   并给出 `overflow` 数字与 `needs_manual` 标志，供界面提示"新建歌曲副本"。

本模块只读，不写任何文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import bms, chart1, level, timeline

# 槽位按"难度由低到高"排，用于自动填补空缺
SLOT_ORDER = [chart1.SLOT_SPB, chart1.SLOT_SPN, chart1.SLOT_SPH,
              chart1.SLOT_SPA, chart1.SLOT_SPL]
KIND_TO_SLOT = {2: chart1.SLOT_SPN, 3: chart1.SLOT_SPH,
                4: chart1.SLOT_SPA, 5: chart1.SLOT_SPL}   # 1=BEGINNER 按约定不导入


@dataclass
class ChartInfo:
    file: str
    kind: int | None = None          # BMS #DIFFICULTY（1-5），None=没写
    author_level: int = 0            # #PLAYLEVEL 原文（可能 >12）
    notes: int = 0
    seconds: float = 0.0
    nps: float = 0.0                 # 平均音符/秒
    peak_nps: float = 0.0            # 4 秒窗峰值
    ln: int = 0                      # 长按条数
    keys: int = 7                    # 用到的键数（5/7）
    bpm: float = 0.0
    total_ms: int = 0
    density: float = 0.0             # 综合指标
    suggest_level: int = 0           # 推荐等级 1-12
    suggest_cont: float = 0.0        # 推荐连续值
    auto_slot: str = ""              # 自动分配的槽名（空=未分配）
    dp: bool = False
    rejected: str = ""               # 被拒原因（如有）
    warning: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    folder: str = ""
    title: str = ""
    artist: str = ""
    genre: str = ""
    charts: list[ChartInfo] = field(default_factory=list)
    slots: dict = field(default_factory=dict)     # 槽名 → file（"" 表示空）
    overflow: int = 0                              # 塞不下的谱面数
    needs_manual: bool = False                     # 是否需要用户手工安排/新建副本
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["charts"] = [c.to_dict() for c in self.charts]
        return d


def _keys_used(ch) -> int:
    lanes = set()
    for n in ch.notes:
        if 0 <= n.column <= 6:
            lanes.add(n.column)
    filled = len(lanes)
    return 5 if filled <= 5 else 7


KIND_TO_SLOT_DP = {2: 7, 3: 6, 4: 8, 5: 10}    # DP NORMAL/HYPER/ANOTHER/LEGGENDARIA
SLOT_ORDER_DP = [9, 7, 6, 8, 10]               # DP 显示顺序：B N H A L


def scan(folder: Path) -> Plan:
    folder = Path(folder)
    plan = Plan(folder=str(folder))
    charts = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".bms", ".bme", ".bml"):
            continue
        info = ChartInfo(file=p.name)
        try:
            ch = bms.parse(p)
        except Exception as e:
            info.rejected = f"解析失败: {type(e).__name__}"
            plan.charts.append(info)
            continue
        info.dp = bool(ch.is_dp)
        try:
            tc = timeline.build(ch)
        except Exception as e:
            info.rejected = f"时间轴构建失败: {type(e).__name__}"
            plan.charts.append(info)
            continue
        if not tc.notes:
            info.rejected = "没有可转换音符"
            plan.charts.append(info)
            continue

        notes = len(tc.notes)
        sec = max(0.1, tc.total_ms / 1000.0)
        ticks = [n.tick for n in tc.notes]
        rec, cont, dens = level.recommend(notes, sec, ticks)
        info.kind = _kind(ch)
        info.author_level = _author_level(ch)
        info.notes = notes
        info.seconds = round(sec, 1)
        info.nps = round(notes / sec, 2)
        info.peak_nps = round(level.peak_density(ticks), 2)
        info.ln = sum(1 for n in tc.notes if n.ln_ticks)
        info.keys = _keys_used(ch)
        info.bpm = tc.initial_bpm or 0.0
        info.total_ms = tc.total_ms
        info.density = dens
        info.suggest_level, info.suggest_cont = rec, cont
        info.warning = list(tc.warnings)
        charts.append(info)
    plan.charts = charts
    plan.title = _header_of(folder, "TITLE")
    plan.artist = _header_of(folder, "ARTIST")
    plan.genre = _header_of(folder, "GENRE")
    auto_layout(plan)
    return plan


def _kind(ch) -> int | None:
    raw = (ch.header.get("DIFFICULTY") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 5:
        return int(raw)
    import re
    for rx, kind in ((r"(leg|leggendaria|spl)", 5),
                     (r"(another|_a$|\[a\]|extreme|insane|maniac|black|hardest)", 4),
                     (r"(hyper|_h$|\[h\]|hard|ex)", 3),
                     (r"(normal|_n$|\[n\]|easy|nov)", 2),
                     (r"(beginner|_b$|begin)", 1)):
        if re.search(rx, ch.path.stem, re.I):
            return kind
    return None


def _author_level(ch) -> int:
    import re
    m = re.search(r"\d+", ch.header.get("PLAYLEVEL") or "")
    return int(m.group()) if m else 0


def _header_of(folder: Path, key: str) -> str:
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in (".bms", ".bme", ".bml"):
            try:
                return (bms.parse(p).header.get(key) or "").strip()
            except Exception:
                continue
    return ""


def auto_layout(plan: Plan):
    """自动把谱面放进槽（SP 与 DP 各自一套槽位）

    规则（沿用"小于 5 才自动进槽"）：
      · 同类可用谱面 ≤ 5：按 #DIFFICULTY 精确入槽，其余按推荐等级补空槽
      · > 5：不自动分配，只给 auto_slot 参考，让用户手工拖
    """
    for c in plan.charts:
        c.auto_slot = ""

    def layout(group: list, kind_map: dict, order: list):
        usable = [c for c in group if not c.rejected]
        suggest: dict[int, ChartInfo] = {}
        for c in usable:
            if c.kind in kind_map:
                cur = suggest.get(kind_map[c.kind])
                if cur is None or c.notes > cur.notes:
                    suggest[kind_map[c.kind]] = c
        for s, c in suggest.items():
            c.auto_slot = chart1.SLOT_NAMES.get(s, str(s))

        slots: dict[int, ChartInfo] = {}
        if len(usable) <= 5:
            slots.update(suggest)
            leftover = [c for c in usable
                        if id(c) not in {id(x) for x in slots.values()}]
            leftover.sort(key=lambda c: c.suggest_cont)
            empty = [s for s in order if s not in slots]
            for c, s in zip(leftover, empty):
                slots[s] = c
                c.auto_slot = chart1.SLOT_NAMES.get(s, str(s))
        return usable, slots

    sp_group = [c for c in plan.charts if not c.dp]
    dp_group = [c for c in plan.charts if c.dp]
    sp_usable, sp_slots = layout(sp_group, KIND_TO_SLOT, SLOT_ORDER)
    dp_usable, dp_slots = layout(dp_group, KIND_TO_SLOT_DP, SLOT_ORDER_DP)

    all_slots = {**sp_slots, **dp_slots}
    usable_n = len(sp_usable) + len(dp_usable)
    biggest = max(len(sp_usable), len(dp_usable))
    plan.overflow = max(0, biggest - 5)
    plan.needs_manual = biggest > 5
    plan.slots = {chart1.SLOT_NAMES.get(s, str(s)): (all_slots[s].file if s in all_slots else "")
                  for s in list(SLOT_ORDER) + list(SLOT_ORDER_DP)}
    return plan


def render_table(plan: Plan) -> str:
    """给终端/界面用的文本表格（GUI 会自己画，这里用于自检）。"""
    lines = []
    lines.append(f"文件夹: {plan.folder}")
    lines.append(f"曲名  : {plan.title}    作者: {plan.artist}    类型: {plan.genre}")
    lines.append("")
    head = (f"{'谱面文件':42s} {'DIFF':>4} {'作者级':>5} {'物量':>5} {'时长':>6} "
            f"{'平均nps':>7} {'峰值':>6} {'推荐':>4} {'槽':>12}")
    lines.append(head)
    lines.append("-" * len(head))
    for c in plan.charts:
        if c.rejected:
            lines.append(f"{c.file[:42]:42s}  ——  {c.rejected}")
            continue
        lines.append(f"{c.file[:42]:42s} {str(c.kind or '-'):>4} {c.author_level:>5} "
                     f"{c.notes:>5} {c.seconds:>6.0f} {c.nps:>7.2f} {c.peak_nps:>6.2f} "
                     f"{c.suggest_level:>4} {c.auto_slot or '（未分配）':>12}")
    lines.append("")
    lines.append("难度槽：")
    for s in SLOT_ORDER:
        name = chart1.SLOT_NAMES.get(s, str(s))
        lines.append(f"   {name:12s} ← {plan.slots.get(name) or '（空）'}")
    if plan.needs_manual:
        lines.append("")
        lines.append(f"⚠ 还有 {plan.overflow} 张谱面塞不下（一首歌只有 5 个槽）")
        lines.append("   → 手工拖进槽位，或「新建歌曲副本」再放 5 张")
    return "\n".join(lines)
