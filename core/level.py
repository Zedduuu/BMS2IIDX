"""难度等级推荐：由"物量密度"反推 IIDX 1-12 级。

数据来源：本机 IIDX32 曲库 7459 个真实 SP 谱面（`tools/iidx_density.py` 提取，
`work/iidx_density.tsv`），按等级统计中间值得到的经验表。

指标定义
--------
- 平均密度 `avg = 音符数 / 曲长秒数`（音符数采用与 IIDX `.1` 的 0x10 一致的算法：
  普通音符 + 长按条数×1，长按尾巴不另计）
- 峰值密度 `peak = 任意 4 秒窗内的最大音符数 / 4`
- 综合指标 `score = max(avg, peak * 0.62)`
  （0.62 是把峰值折算到平均量纲的经验系数；峰值天然高于平均）

回测（对 7422 个真实谱面）：绝对误差中位 **0.38 级**，**87% 落在 ±1 级内**。
对比：只用平均密度 0.40 / 只用峰值 0.44 —— 综合指标最好。
"""

from __future__ import annotations

import math

# 每级的综合指标中位数（来自 7459 个真实 SP 谱面）
LEVEL_SCORE = {
    1: 1.240, 2: 2.015, 3: 2.790, 4: 3.675, 5: 4.460, 6: 5.233,
    7: 6.200, 8: 7.057, 9: 8.110, 10: 9.655, 11: 11.673, 12: 14.730,
}

PEAK_WINDOW_MS = 4000
PEAK_WEIGHT = 0.62


def peak_density(ticks: list[int], window_ms: int = PEAK_WINDOW_MS) -> float:
    """任意滑动窗内的最大音符密度（音符/秒）。ticks 单位毫秒。"""
    if not ticks:
        return 0.0
    ts = sorted(ticks)
    best, j = 0, 0
    for i, t in enumerate(ts):
        while ts[j] <= t - window_ms:
            j += 1
        best = max(best, i - j + 1)
    return best / (window_ms / 1000.0)


def score(notes: int, seconds: float, ticks: list[int] | None = None) -> float:
    """综合密度指标。ticks 为空时退化为只用平均密度。"""
    if seconds <= 0:
        return 0.0
    avg = notes / seconds
    if not ticks:
        return avg
    return max(avg, peak_density(ticks) * PEAK_WEIGHT)


def level_float(score_value: float) -> float:
    """综合指标 → 连续等级（对数空间分段线性插值）。"""
    lvs = sorted(LEVEL_SCORE)
    xs = [math.log(LEVEL_SCORE[l]) for l in lvs]
    x = math.log(max(1e-6, score_value))
    if x <= xs[0]:
        return float(lvs[0])
    if x >= xs[-1]:
        k = (lvs[-1] - lvs[-2]) / (xs[-1] - xs[-2])
        return min(12.0, lvs[-1] + (x - xs[-1]) * k)
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return lvs[i] + t * (lvs[i + 1] - lvs[i])
    return 12.0


def recommend(notes: int, seconds: float, ticks: list[int] | None = None) -> tuple[int, float, float]:
    """返回 (推荐等级 1-12, 连续等级, 综合密度)。"""
    sc = score(notes, seconds, ticks)
    lf = level_float(sc)
    return max(1, min(12, round(lf))), round(lf, 2), round(sc, 3)


