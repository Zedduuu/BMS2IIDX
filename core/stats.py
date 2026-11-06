"""给「BMS 谱面库」补统计列：时长 / 均值 NPS / 峰值 NPS / 推荐等级。

为什么单独做：
  · 索引平时只读文件头（2 KB），所以 10 万谱面 15 秒就建好了
  · 这几个数要把整份谱面解析一遍（实测约 22–30 ms/首），必须用户点按钮才做
  · 结果写回索引，之后翻列表直接用，不再计算

复用转换器的同一套算法（core/level.py），所以「推荐等级」和转换页上看到的
一致：不会出现“列表说 9 级、转换出来 11 级”这种事。
"""
from __future__ import annotations

import pathlib

from . import bms, level, timeline

# 实测（本机，C:\BMS 随机 30 首，6 进程）：单首约 163 ms 墙钟
# ⇒ 单核成本约 1.0 秒/首（timeline 的 Fraction 运算占大头）
# 估算 = 首数 × MS_PER_CHART / 进程数
MS_PER_CHART = 980.0


def chart_stats(path: pathlib.Path) -> dict:
    """算一首歌的统计值。失败时返回 ok=False 和原因（不抛异常，交给上层汇总）。"""
    try:
        ch = bms.parse(path)
        tc = timeline.build(ch)
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "path": str(path), "error": f"{type(e).__name__}: {e}"[:120]}
    notes = len(tc.notes)
    if notes <= 0:
        return {"ok": False, "path": str(path), "error": "没有可转换音符"}
    sec = max(0.1, tc.total_ms / 1000.0)
    ticks = [n.tick for n in tc.notes]
    rec, _cont, _dens = level.recommend(notes, sec, ticks)
    return {
        "ok": True,
        "path": str(path),
        "length_ms": int(tc.total_ms),
        "mean_nps": round(notes / sec, 2),
        "peak_nps": round(level.peak_density(ticks), 2),
        "rec_level": int(rec),
    }


def _task(p: str) -> dict:
    return chart_stats(pathlib.Path(p))


def estimate_ms(count: int, workers: int = 8) -> int:
    """粗略估算总耗时（毫秒）。给界面弹窗用，按当前库的条数算。"""
    workers = max(1, min(workers, 16))
    return int(count * MS_PER_CHART / workers) + 800


def run_stats(paths: list[str], progress=None, should_stop=None,
              workers: int = 8) -> dict:
    """批量算统计：分块丢给进程池，每块完成后回报进度。"""
    from .par import pmap

    ok, failed = [], []
    total = len(paths)
    done = 0
    for i in range(0, total, 60):
        if should_stop and should_stop():
            break
        chunk = paths[i:i + 60]
        try:
            results = pmap(_task, chunk, workers)
        except Exception:                                      # noqa: BLE001
            results = [_task(p) for p in chunk]
        for r in results:
            (ok if r.get("ok") else failed).append(r)
        done += len(chunk)
        if progress:
            progress(f"[PROGRESS {int(done * 100 / max(1, total))}] "
                     f"统计 {done}/{total}（失败 {len(failed)}）")
    return {"ok": True, "done": len(ok), "failed": failed, "rows": ok}
