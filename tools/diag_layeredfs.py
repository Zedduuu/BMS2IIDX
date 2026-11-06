"""开发用：从游戏 log.txt 看 layeredfs 到底做了什么。

不属于用户功能，只是排障脚本。

用法：
    python tools/diag_layeredfs.py "<游戏根目录>"
    python tools/diag_layeredfs.py "…\\contents" --dump   # 连 Using 明细一起打

要看逐文件记录（Using 行），先让 hook 开 verbose：
  在 spice2x（spicecfg）里找到「传给游戏的命令行参数 / Command Line Args Override」，
  填上 --layered-verbose=1，重启游戏一次。（平时别开，日志会很大）
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gui import services            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="layeredfs 日志诊断")
    ap.add_argument("game_dir", help="游戏根目录，或 …\\contents")
    ap.add_argument("--dump", action="store_true", help="打印 Using 明细")
    args = ap.parse_args()

    r = services.diagnose_layeredfs(args.game_dir)
    if not r.get("ok"):
        print("读取失败：", r.get("error"))
        print("日志路径：", r.get("log"))
        return 1

    print("日志：", r["log"])
    print("hook 版本：", r["version"] or "（未检出）")
    print("启动参数：", r["options"] or "（无）")
    print("mod 目录：", "、".join(r["mod_dirs"]) or "（无）")
    print("合并 XML：", r["counts"]["merged"], "个")
    for x in r["merged"]:
        print("    ", x)
    print("Using 记录：", r["counts"]["using"],
          "（其中覆盖本体", r["counts"]["override"], "条）")
    print()
    print("结论：", r["verdict"])
    print()

    if args.dump:
        for row in r["using"]:
            flag = "覆盖" if row["overrides_base"] else "新增"
            print(f"  [{row['mod'] or '?'}] {row['rel']}  → {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
