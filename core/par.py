"""极简并行工具：把"每个采样一次 ffmpeg"这种扇出任务铺到多核上。

注意：worker 函数必须是模块级函数（Windows 用 spawn 启动子进程）。
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

MAX_WORKERS = max(1, min(24, (os.cpu_count() or 4) - 2))


def pmap(fn, items, workers: int | None = None):
    """按顺序返回结果；items 为空时直接返回 []。"""
    items = list(items)
    if not items:
        return []
    n = workers or MAX_WORKERS
    if n <= 1 or len(items) == 1:
        return [fn(x) for x in items]
    try:
        with ProcessPoolExecutor(max_workers=min(n, len(items))) as ex:
            return list(ex.map(fn, items))
    except Exception:                                            # noqa: BLE001
        # 进程池不可用（例如从 stdin 运行、或被策略拦）→ 退回串行，不中断任务
        return [fn(x) for x in items]
