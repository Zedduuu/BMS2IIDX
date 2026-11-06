"""打包入口：无控制台版要自己兜住 stdout 和启动异常"""
import sys
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
    sys.path.insert(0, str(ROOT))

# 打包版：把 exe 旁边的 ffmpeg\ 目录加进 PATH，
# 这样别人机器上不用装 ffmpeg 也能直接跑（转换/BGA/抽帧都要用）
if getattr(sys, "frozen", False):
    import os
    # PyInstaller 6 默认把二进制放在 _internal\ 下；两种位置都找一遍
    for _cand in (ROOT / "ffmpeg", ROOT / "_internal" / "ffmpeg"):
        if _cand.is_dir():
            os.environ["PATH"] = str(_cand) + os.pathsep + os.environ.get("PATH", "")
            break

# 无控制台模式（pythonw / --noconsole）下 sys.stdout 是 None，先兜住再导入界面
if sys.stdout is None or sys.stderr is None:
    import io
    sys.stdout = sys.stdout or io.StringIO()
    sys.stderr = sys.stderr or io.StringIO()


def _fatal(err: BaseException):
    """启动就崩的时候，至少留个记录 + 弹窗，别让用户面对"双击没反应" """
    import datetime
    log = ROOT / "启动失败.log"
    try:
        with log.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            f.write("".join(traceback.format_exception(err)))
    except OSError:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"程序启动失败：\n{err}\n\n详细信息已写入：\n{log}",
            "BMS2IIDX", 0x10)
    except Exception:                                          # noqa: BLE001
        pass


if __name__ == "__main__":
    # 打包成 exe 后，转换用的多进程会重新拉起本程序；
    # 没有这一行会变成"开一堆窗口"，必须在最前面调用。
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        from gui.ui import main
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as e:                                 # noqa: BLE001
        _fatal(e)
        raise SystemExit(1)
