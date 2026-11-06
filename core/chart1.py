"""IIDX .1 谱面写入器

`.1` 结构（已由真实文件与 iidx-datatools 交叉确认）：
  · 12 条目录项，每条 (int32 offset, int32 length)，空槽 length=0
  · 槽位：0=SPH 1=SPN 2=SPA 3=SPB 4=SPL 6=DPH 7=DPN 8=DPA 10=DPL
  · 事件 8 字节：int32 tick / u8 type / u8 param / int16 value
  · 终止符 tick=0x7FFFFFFF
  · tick 单位 = 1 毫秒
  · 事件类型：00/01 可见音符(P1/P2)、02/03 列采样切换、04 BPM、05 拍号、
    06 曲末、07 BGM、08 判定窗、0C 小节线、10 物量
  · 长按 = 00/01 的 value 非 0（值为冻结长度 tick）
"""
from __future__ import annotations

import struct

from .timeline import TickChart

SLOT_SPH, SLOT_SPN, SLOT_SPA = 0, 1, 2
SLOT_SPB, SLOT_SPL = 3, 4          # BEGINNER / LEGGENDARIA（文件头 0x18 / 0x20）
SLOT_DPH, SLOT_DPN, SLOT_DPA = 6, 7, 8
SLOT_DPB, SLOT_DPL = 9, 10
SLOT_NAMES = {0: "HYPER", 1: "NORMAL", 2: "ANOTHER", 3: "BEGINNER", 4: "LEGGENDARIA",
              6: "DP HYPER", 7: "DP NORMAL", 8: "DP ANOTHER", 9: "DP BEGINNER",
              10: "DP LEGGENDARIA"}

# 真实谱面里判定窗事件恒为这 6 个值，照抄
JUDGE = ((0, 240), (1, 250), (2, 255), (3, 3), (4, 8), (5, 18))

# 同 tick 内的写出优先级（采样切换必须早于音符）
PRIO = {0x10: 0, 0x04: 1, 0x05: 2, 0x08: 3, 0x07: 4, 0x02: 5, 0x0C: 6, 0x00: 7, 0x06: 8}

TERM = struct.pack("<iBBh", 0x7FFFFFFF, 0, 0, 0)
I16_MAX = 32767


def ev(tick: int, ty: int, param: int, value: int) -> bytes:
    return struct.pack("<iBBh", tick, ty, param & 0xFF, value)


def encode_bpm(bpm: float) -> tuple[int, int]:
    """把 BPM 编成 (param, value)，满足 BPM = value/param 且 value 不超 int16。"""
    for param in (1, 10, 100):
        v = bpm * param
        if abs(v - round(v)) < 1e-6 and round(v) <= I16_MAX:
            return param, int(round(v))
    return 100, min(I16_MAX, int(round(bpm * 100)))


def build_slot(tc: TickChart, warnings: list | None = None) -> bytes:
    """把一首曲子的一个难度编成 `.1` 的一个槽内容。"""
    warn = warnings if warnings is not None else []
    events: list[tuple[int, int, int, int]] = []

    dp = any(getattr(n, "side", 0) for n in tc.notes)
    n_p1 = sum(1 for n in tc.notes if not getattr(n, "side", 0))
    n_p2 = sum(1 for n in tc.notes if getattr(n, "side", 0))
    # --- 头部（物量先占位，等事件全部生成后回填真实值）---
    # SP：param0=物量，param1=0；DP：param0=P1 数，param1=P2 数
    events.append((0, 0x10, 0x00, min(I16_MAX, n_p1 if dp else tc.note_count)))
    events.append((0, 0x10, 0x01, min(I16_MAX, n_p2) if dp else 0))
    if tc.initial_bpm > 0:
        p, v = encode_bpm(tc.initial_bpm)
        events.append((0, 0x04, p, v))
    events.append((0, 0x05, 4, 4))                    # 拍号 4/4
    for p, v in JUDGE:
        events.append((0, 0x08, p, v))

    # --- 小节线 ---
    for tk in tc.measure_ticks:
        events.append((tk, 0x0C, 0, 0))

    # --- 变速 ---
    last_bpm = tc.initial_bpm
    for tk, bpm in tc.bpm_events:
        if tk == 0 and abs(bpm - tc.initial_bpm) < 1e-9:
            continue                                   # 开局那条已经写过
        p, v = encode_bpm(bpm)
        events.append((tk, 0x04, p, v))
        last_bpm = bpm

    # --- BGM（整曲音轨 / 背景采样）---
    for tk, sample in tc.bgm:
        if 0 < sample <= I16_MAX:
            events.append((tk, 0x07, 0, sample))

    # 实测建议：谱面在 tick 0 给**每条用到的轨道**各写一条 0x02，把装填槽全部初始化。
    # 理由：装填槽是静态单例，没有观察到换曲时的 memset，只清"按键历史队列"。
    # 初始化后既消除跨曲残留的不确定性，也让"本轨第一个音符前的按键"行为确定。
    prior = [0] * 16                     # 0-7 = P1 各轨，8-15 = P2 各轨
    for n in sorted(tc.notes, key=lambda x: x.tick):
        lane = n.column + (8 if getattr(n, "side", 0) else 0)
        if 0 <= n.column <= 7 and n.sample > 0 and prior[lane] == 0:
            prior[lane] = n.sample
    for lane in range(16):
        if prior[lane]:
            # 0x02 = P1 装填，0x03 = P2 装填（真实 DP 谱面就是这样）
            events.append((0, 0x03 if lane >= 8 else 0x02, lane % 8, prior[lane]))
    # --- 可见音符（带轨道采样预加载）---
    # 格式结论（对实际 .1 文件的比对，2026-10-21）：
    #   · 0x02 唯一动作 = playerObj[lane*4] = 采样号（不发声、不停止、不互斥）
    #   · 按键在**按下瞬间**读该槽（压进 100 条历史队列后统一 Play）
    #   · 槽常驻；引擎没有硬性提前量要求，只要求"按下前已写入"
    FIRST_LEAD = 400          # 本轨第一个音符之前的最小装填提前量
    current = list(prior)     # tick 0 已把各轨槽初始化成该轨第一个采样
    last_switch_tick = [-10 ** 9] * 16
    prev_note_tick = [-10 ** 9] * 16
    for n in sorted(tc.notes, key=lambda x: x.tick):
        if not (0 <= n.column <= 7):
            warn.append(f"非法轨道 {n.column}，已丢弃")
            continue
        if n.sample < 0 or n.sample > I16_MAX:
            warn.append(f"非法采样号 {n.sample}，已丢弃一个音符")
            continue
        _side = getattr(n, "side", 0)
        _lane = n.column + (8 if _side else 0)
        if n.sample > 0 and current[_lane] != n.sample:      # 只在变化时装填
            if prev_note_tick[_lane] < -10 ** 8 and current[_lane]:
                st = 0                                     # tick 0 已初始化过本轨
            elif prev_note_tick[_lane] < -10 ** 8:           # 本轨第一个有声音符
                st = max(0, n.tick - FIRST_LEAD)
            else:
                # 槽里只能放一个采样，翻转时刻是取舍：
                #   翻早 → 按晚的人听到下一个音；翻晚 → 按早的人听到上一个音。
                # 取两音符中点 = 最坏误差最小（两边各承担半个间隔）。
                st = (prev_note_tick[_lane] + n.tick) // 2
            st = max(st, last_switch_tick[_lane] + 1, 0)
            st = min(st, n.tick)
            events.append((st, 0x03 if _side else 0x02, n.column, n.sample))
            current[_lane] = n.sample
            last_switch_tick[_lane] = st
        prev_note_tick[_lane] = n.tick
        ln = n.ln_ticks
        if ln > I16_MAX:
            warn.append(f"长按过长 {ln} tick，已截断到 {I16_MAX}")
            ln = I16_MAX
        events.append((n.tick, 0x01 if _side else 0x00, n.column, ln))

    # --- 曲末：DP 谱面要写两条（param 0/1 各一条）---
    events.append((tc.total_ms, 0x06, 0, 0))
    if dp:
        events.append((tc.total_ms, 0x06, 1, 0))

    # --- 排序去重 ---
    def key(e):
        tk, ty, pa, va = e
        return (tk, PRIO.get(ty, 9), pa, va)

    events.sort(key=key)
    dedup: list[tuple[int, int, int, int]] = []
    for e in events:
        if dedup and dedup[-1] == e:
            continue
        dedup.append(e)

    # 回填真实物量。约定经 942 个真实谱面验证：
    #   0x10 的值 = 普通音符数 + 长按条数（长按只算 1 个，不是 2 个）
    # 注意 iidx-datatools 的 parse_chart_notecounts.py 用的是"长按算 2 个"，
    # 那是给曲库物量字段用的另一套约定，不能拿来写 .1 里的 0x10。
    taps = sum(1 for (t, ty, _, va) in dedup
               if ty in (0x00, 0x01) and va == 0 and t != 0x7FFFFFFF)
    lanc = sum(1 for (t, ty, _, va) in dedup
               if ty in (0x00, 0x01) and va != 0 and t != 0x7FFFFFFF)
    real_count = max(0, taps + lanc)
    if real_count != min(I16_MAX, tc.note_count) and warnings is not None:
        warnings.append(f"物量修正：原 {tc.note_count} → 实际 {real_count}")
    # 无条件回填（不能只在"有变化时"改，否则调用方给的初值会漏到文件里）
    if dp:
        c1 = sum(1 for (t, ty, _, va) in dedup
                 if ty == 0x00 and t != 0x7FFFFFFF)
        c2 = sum(1 for (t, ty, _, va) in dedup
                 if ty == 0x01 and t != 0x7FFFFFFF)
        for i, e in enumerate(dedup):
            if e[1] == 0x10 and e[2] == 0:
                dedup[i] = (e[0], e[1], e[2], min(I16_MAX, c1))
            elif e[1] == 0x10 and e[2] == 1:
                dedup[i] = (e[0], e[1], e[2], min(I16_MAX, c2))
    else:
        for i, e in enumerate(dedup):
            if e[1] == 0x10 and e[2] == 0:
                dedup[i] = (e[0], e[1], e[2], min(I16_MAX, real_count))
                break

    body = b"".join(ev(*e) for e in dedup) + TERM
    return body


def assemble(slots: dict[int, bytes]) -> bytes:
    """把若干难度槽拼成一个完整 `.1` 文件。"""
    dirs = [(0, 0)] * 12
    off = 0x60
    blob = bytearray()
    for slot in sorted(slots):
        assert 0 <= slot < 12, slot
        body = slots[slot]
        dirs[slot] = (off + len(blob), len(body))
        blob += body
    head = b"".join(struct.pack("<ii", o, l) for o, l in dirs)
    return head + bytes(blob)


