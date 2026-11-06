"""BMS2IIDX 界面（重构版）

布局：
  标签页：转换 / 曲库与资源 / 备份与恢复 / 帮助 / 关于
  转换页：左 = BMS 谱面表（可排序、可拖动）；右 = 难度槽(B/N/H/A/L) + 封面预览(窄) + 曲目信息
  底部：左 = 日志，右 = CLI 调用记录
"""
from __future__ import annotations

import pathlib
import json
import sys

from PySide6.QtCore import QMimeData, QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QMenu,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QProxyStyle, QPushButton, QScrollArea, QSplitter,
    QStyle, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget)

from gui import services
from gui.bmslib_view import BmsLibraryPage

if getattr(sys, "frozen", False):
    ROOT = pathlib.Path(sys.executable).resolve().parent
else:
    ROOT = pathlib.Path(__file__).resolve().parent.parent
SLOT_ORDER = [3, 1, 0, 2, 4]          # SP：B N H A L（IIDX 槽号）
SLOT_ORDER_DP = [9, 7, 6, 8, 10]      # DP：B N H A L
SLOT_TAG = {3: "B", 1: "N", 0: "H", 2: "A", 4: "L",
            9: "B", 7: "N", 6: "H", 8: "A", 10: "L"}
# services 给的 kind_slot 是 SP 槽号，DP 谱面要换成对应 DP 槽
# BMS #DIFFICULTY (2=N 3=H 4=A 5=L) → SP 槽号（0=H 1=N 2=A 4=L）
KIND_TO_SLOT = {2: 1, 3: 0, 4: 2, 5: 4}
# SP 槽号 → DP 槽号（H 0→6, N 1→7, A 2→8, B 3→9, L 4→10）
KIND_TO_SLOT_DP = {0: 6, 1: 7, 2: 8, 3: 9, 4: 10}
COLUMNS = ["", "谱面文件", "类型", "建议槽", "作者等级", "推荐", "物量", "时长",
           "均值NPS", "峰值NPS", "本机成绩"]


class StreamCatcher:
    """把 sys.stdout / sys.stderr 的写入转成界面信号（线程安全用 Qt 信号）"""

    def __init__(self, sink):
        self._sink = sink
        self._buf = ""

    def write(self, s):
        if not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._sink(line.rstrip())

    def flush(self):
        if self._buf.strip():
            self._sink(self._buf.rstrip())
        self._buf = ""


class Worker(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self):
        try:
            import inspect
            if "progress" in inspect.signature(self.fn).parameters:
                self.kwargs["progress"] = self.progress.emit
            self.done.emit(self.fn(*self.args, **self.kwargs))
        except BaseException as e:                             # noqa: BLE001
            # SystemExit 也要接住：vendored 工具在未知版本时会 raise SystemExit，
            # 不接就会直接把 QThread 打死，队列卡住不动
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc(limit=3)}")


class ThumbLabel(QLabel):
    """预览图：单击=抽帧（有图时双击重抽）"""
    clicked = Signal(int)          # 1=单击, 2=双击

    def __init__(self):
        super().__init__()
        self._clicks = 0
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(228, 130)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("无预览图")
        self.setStyleSheet("background:transparent;color:#888;border:1px dashed #bbb;"
                           "border-radius:4px")

    def mousePressEvent(self, e):
        self._clicks += 1
        from PySide6.QtCore import QTimer
        QTimer.singleShot(220, self._emit)

    def _emit(self):
        n = self._clicks
        self._clicks = 0
        self.clicked.emit(2 if n >= 2 else 1)


class NumItem(QTableWidgetItem):
    """按 UserRole 里的数值排序（否则 "12" 会排在 "7" 前面）"""

    def __lt__(self, other):
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole) if isinstance(other, QTableWidgetItem) else None
        try:
            return float(a) < float(b)
        except (TypeError, ValueError):
            return self.text() < other.text()


class ChartTable(QTableWidget):
    """整行可以拖出去（拖到难度槽里）；鼠标经过时通知外部，便于高亮对应难度槽"""
    hovered = Signal(str)
    assigned = Signal(int, str)

    def __init__(self):
        super().__init__(0, len(COLUMNS))
        self.setHorizontalHeaderLabels(COLUMNS)
        _tips = {
            2: "类型：由谱面内容判断（5K/7K、SP/DP）",
            3: "建议槽：按谱面文件名里的难度词猜的槽位（B/N/H/A/L）",
            4: "作者等级：谱面里 #PLAYLEVEL 写的等级",
            5: "推荐：本程序按物量密度算出的建议等级（1-12）",
            6: "物量：可击打音符数（不含长条中间点）",
            7: "时长：整曲长度",
            8: "均值 NPS：平均每秒音符数",
            9: "峰值 NPS：最密 1 秒内的音符数",
            10: "本机成绩：来自 beatoraja（没设置就显示 —）",
        }
        for _c, _t in _tips.items():
            self.horizontalHeaderItem(_c).setToolTip(_t)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)   # 关键：不然拖不起来
        self.setDefaultDropAction(Qt.CopyAction)
        self.setMouseTracking(True)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.viewport().setMouseTracking(True)

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        it = self.itemAt(e.position().toPoint())
        if it is not None:
            name = self.item(it.row(), 1)
            if name:
                self.hovered.emit(name.text())

    def leaveEvent(self, e):
        self.hovered.emit("")

    def contextMenuEvent(self, e):
        it = self.itemAt(e.pos())
        if it is None:
            return
        name = self.item(it.row(), 1)
        if not name:
            return
        menu = QMenu(self)
        head = menu.addAction(f"把 {name.text()[:36]} 放到槽：")
        head.setEnabled(False)
        # 建议槽（来自 BMS #DIFFICULTY）放最前面
        sug_item = self.item(it.row(), 3)
        sug_txt = sug_item.text() if sug_item else "-"
        sug_val = sug_item.data(Qt.UserRole) if sug_item else None
        if sug_txt not in ("-", "", None) and isinstance(sug_val, int):
            act = menu.addAction(f"　放入建议槽（{sug_txt}）")
            act.setData(sug_val)
            menu.addSeparator()
        for tag, sid in (("B", 3), ("N", 1), ("H", 0), ("A", 2), ("L", 4)):
            act = menu.addAction(f"　{tag}")
            act.setData(sid)
        menu.addSeparator()
        clear = menu.addAction("　清空该谱面占用的槽")
        clear.setData(-1)
        chosen = menu.exec(e.globalPos())          # exec 返回被点中的 action
        if chosen is None or chosen.data() is None:
            return
        sid = int(chosen.data())
        self.assigned.emit(sid, name.text())

    def mousePressEvent(self, e):
        """记住鼠标按下的那一行：拖拽时以它为准（currentRow 可能是别处的选中行）"""
        try:
            self._press_row = self.rowAt(int(e.position().toPoint().y()))
        except Exception:                                      # noqa: BLE001
            self._press_row = -1
        super().mousePressEvent(e)

    def startDrag(self, actions):
        row = getattr(self, "_press_row", -1)
        if row < 0 or row >= self.rowCount():
            row = self.currentRow()
        if row < 0:
            return
        item = self.item(row, 1)
        if not item:
            return
        drag = QDrag(self)
        drag.setPixmap(QPixmap(1, 1))
        md = QMimeData()
        md.setData("application/x-bms-chart", item.text().encode("utf-8"))
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)


class SlotBox(QFrame):
    """IIDX 难度槽：正方形彩色徽标（B绿 N蓝 H黄 A红 L紫），有内容时整槽染色"""
    dropped = Signal(int, str)
    hover_changed = Signal(int, bool)

    # SP: 3=B 1=N 0=H 2=A 4=L；DP: 9=B 7=N 6=H 8=A 10=L
    COLORS = {3: "#2f9e44", 1: "#1c7ed6", 0: "#e8a90c", 2: "#e03131", 4: "#9c36b5",
              9: "#2f9e44", 7: "#1c7ed6", 6: "#e8a90c", 8: "#e03131", 10: "#9c36b5"}

    def __init__(self, slot_id: int):
        super().__init__()
        self.slot_id = slot_id
        self.file = ""
        self.color = self.COLORS.get(slot_id, "#666")
        self.setObjectName("slot")
        self.setAcceptDrops(True)
        self.setMinimumHeight(48)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(8)

        self.badge = QLabel(SLOT_TAG.get(slot_id, "?"))
        self.badge.setFixedSize(30, 30)
        self.badge.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.badge)

        self.lblName = QLabel("拖谱面到这里")
        lay.addWidget(self.lblName, 1)

        self.btnClear = QPushButton("×")
        self.btnClear.setFixedWidth(22)
        self.btnClear.setVisible(False)
        self.btnClear.clicked.connect(lambda: self.dropped.emit(slot_id, ""))
        lay.addWidget(self.btnClear)

        self.set_file("")

    def _dark(self) -> bool:
        from PySide6.QtGui import QPalette
        return _is_dark()

    def _badge_style(self):
        return (f"background:{self.color};color:#ffffff;font-weight:bold;"
                f"font-size:15px;border-radius:6px;border:none;")

    def _frame_style(self, filled: bool, hover: bool = False):
        dark = self._dark()
        empty_bg = "#3a3a3a" if dark else "#f7f7f7"
        empty_bd = "#5f5f5f" if dark else "#c4c4c4"
        text = "#d0d0d0" if dark else "#999999"
        fill_bg = self._tint(0.22 if dark else 0.12)
        if hover:
            bg = fill_bg if filled else empty_bg
            return (f"QFrame#slot{{background:{bg};"
                    f"border:3px solid {self.color};border-radius:6px}}")
        if filled:
            return (f"QFrame#slot{{background:{fill_bg};"
                    f"border:1px solid {self.color};border-radius:6px}}")
        return (f"QFrame#slot{{background:{empty_bg};"
                f"border:1px dashed {empty_bd};border-radius:6px}}")

    def _tint(self, alpha: float) -> str:
        """把槽的颜色和当前底色混一下，得到浅/深的填充色"""
        from PySide6.QtGui import QColor, QPalette
        app = QApplication.instance()
        base = app.palette().color(QPalette.Window)
        c = QColor(self.color)
        mix = QColor(
            round(c.red() * alpha + base.red() * (1 - alpha)),
            round(c.green() * alpha + base.green() * (1 - alpha)),
            round(c.blue() * alpha + base.blue() * (1 - alpha)))
        return mix.name()

    def set_file(self, name: str):
        self.file = name or ""
        self.badge.setStyleSheet(self._badge_style())
        if name:
            self.lblName.setText(name)
            self.lblName.setStyleSheet(
                "color:#e8e8e8;background:transparent;" if self._dark()
                else "color:#222;background:transparent;")
            self.btnClear.setVisible(True)
            self.setStyleSheet(self._frame_style(True))
        else:
            self.lblName.setText("拖谱面到这里")
            self.lblName.setStyleSheet(
                "color:#9a9a9a;background:transparent;" if self._dark()
                else "color:#999;background:transparent;")
            self.btnClear.setVisible(False)
            self.setStyleSheet(self._frame_style(False))

    def flash(self, on: bool):
        self.setStyleSheet(self._frame_style(bool(self.file), hover=on))
        if self.file:
            weight = "bold" if on else "normal"
            colour = "#e8e8e8" if self._dark() else "#222"
            self.lblName.setStyleSheet(f"color:{colour};background:transparent;"
                                       f"font-weight:{weight}")

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat("application/x-bms-chart"):
            e.acceptProposedAction()
            self.flash(True)

    def dragLeaveEvent(self, e):
        self.flash(False)

    def dropEvent(self, e):
        name = bytes(e.mimeData().data("application/x-bms-chart")).decode("utf-8")
        self.dropped.emit(self.slot_id, name)
        self.flash(False)
        e.acceptProposedAction()

    def enterEvent(self, e):
        self.hover_changed.emit(self.slot_id, True)

    def leaveEvent(self, e):
        self.hover_changed.emit(self.slot_id, False)


class Main(QMainWindow):

    # CLI 面板只能由主线程写；工作线程发这个信号
    _cli_signal = Signal(str)
    _stdout_signal = Signal(str)

    def __init__(self):
        super().__init__()
        try:
            from core.version import APP_TITLE as _title
        except Exception:                                      # noqa: BLE001
            _title = "BMS2IIDX"
        self.setWindowTitle(_title)
        self.settings = QSettings("bms2iidx", "bms2iidx")
        self.scan: dict | None = None
        self.target: dict | None = None
        self.staging: pathlib.Path | None = None
        self.last_report: dict | None = None
        self.slots: dict[int, str] = {s: "" for s in SLOT_ORDER}
        self._warned_lib = False
        self._row_of_file: dict[str, int] = {}
        self.frames: list[bytes] = []            # 原始帧字节（内存，最多 99 张）
        self.frames_view: list[QPixmap] = []     # 处理后的预览（与上面一一对应）
        self.frame_idx: int = -1
        self.MAX_FRAMES = 99

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        self.queue: list[dict] = []
        self._queue_running = False
        self._queue_db: pathlib.Path | None = None
        self._queue_last_report: dict | None = None
        self.build_convert_tab()
        self.build_queue_tab()
        self._queue_load()
        self.refresh_queue_table()
        self.build_bmslib_tab()
        self.build_lib_tab()
        self.build_backup_tab()
        self.build_help_tab()
        self.build_settings_tab()
        self.build_about_tab()

        # 状态不再用底部横条显示，全部写进下面的日志（带底色）

        self.logPanel = QWidget()
        _lp = QVBoxLayout(self.logPanel)
        _lp.setContentsMargins(0, 0, 0, 0)
        _lp.setSpacing(2)
        _head = QHBoxLayout()
        self.btnLogToggle = QPushButton("日志 ▾")
        self.btnLogToggle.setFlat(True)
        self.btnLogToggle.setFixedWidth(72)
        self.btnLogToggle.setToolTip("展开/收起下方日志（Ctrl+L）")
        self.btnLogToggle.clicked.connect(self.toggle_log)
        _head.addWidget(self.btnLogToggle)
        self.lblLastStatus = QLabel("")
        self.lblLastStatus.setStyleSheet("color:#666")
        self.lblLastStatus.setTextInteractionFlags(Qt.TextSelectableByMouse)
        _head.addWidget(self.lblLastStatus, 1)
        _lp.addLayout(_head)
        self.logWrap = QWidget()
        _lw = QVBoxLayout(self.logWrap)
        _lw.setContentsMargins(0, 0, 0, 0)

        logs = QTabWidget()
        self.txtLog = QPlainTextEdit()
        self.txtLog.setReadOnly(True)
        self.txtLog.setMaximumBlockCount(self.settings.value("log_lines", 5000, int))
        self.txtCli = QPlainTextEdit()
        self.txtCli.setReadOnly(True)
        self.txtCli.setMaximumBlockCount(self.settings.value("log_lines", 5000, int))
        self.txtCli.setStyleSheet("font-family:Consolas,monospace;font-size:11px")
        logs.addTab(self.txtLog, "运行日志")
        logs.addTab(self.txtCli, "CLI 调用")
        logs.setMaximumHeight(210)
        _lw.addWidget(logs)
        _lp.addWidget(self.logWrap, 1)
        outer.addWidget(self.logPanel)
        self._log_open = bool(self.settings.value("log_open", True, bool))
        self._apply_log_open()

        from core import cli_log
        cli_log.set_listener(self.on_cli)
        self._cli_signal.connect(self.on_stdout)      # 主线程里落到控件上
        self._open_log_file()

        # 把 print / 异常输出接到 CLI 面板（pythonw 下没有控制台也能看到）
        import sys as _sys
        out_sig = self._stdout_signal
        catcher_out = StreamCatcher(lambda s: out_sig.emit(s))
        catcher_err = StreamCatcher(lambda s: out_sig.emit("[stderr] " + s))
        _sys.stdout = catcher_out
        _sys.stderr = catcher_err

        def _hook(exc_type, exc, tb):
            import traceback
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            self.info(f"✗ 未处理异常：{exc_type.__name__}: {exc}\n{text}")
            self.set_status(
                f"✗ 未处理异常（{exc_type.__name__}），日志文件：{self.log_file_path()}", "err")
        _sys.excepthook = _hook

        self._stdout_signal.connect(self.on_stdout)
        self.update_frame_buttons()
        self.tabs.setCurrentIndex(0)
        self._style_buttons()
        self._restore_window()
        self._install_shortcuts()
        self.info("启动中…")
        self.autodetect()

    # ---------------------------------------------------------------- 通用
    def _style_buttons(self):
        """给关键按钮分主次：主操作=实心蓝，危险操作=红描边，其余保持默认"""
        primary = {"开始转换", "部署到游戏", "立即备份", "添加到队列", "更新索引",
                   "转换到暂存区", "转换并部署", "安装 ifs_hook.dll"}
        danger = {"删除选中", "移除选中", "清空队列", "恢复默认设置", "删除这些残留文件（永久）",
                  "从曲库移除并删除文件", "都删除（记录 + 资源，移到回收区）"}
        qss = ("QPushButton[role=\"primary\"]{font-weight:700}"
               "QPushButton[role=\"danger\"]{color:#c0392b;font-weight:600}"
               "QPushButton[role=\"danger\"]:hover{color:#e03131}")
        self.setStyleSheet(self.styleSheet() + qss)
        for b in self.findChildren(QPushButton):
            txt = (b.text() or "").strip()
            role = "primary" if txt in primary else ("danger" if txt in danger else "")
            if not role:
                continue
            b.setProperty("role", role)
            b.style().unpolish(b)
            b.style().polish(b)

    def _restore_window(self):
        """恢复上次的窗口大小/位置"""
        geo = self.settings.value("win_geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:                                  # noqa: BLE001
                pass

    def _install_shortcuts(self):
        """常用快捷键：Ctrl+O 选文件夹、Ctrl+Enter 加入队列、F5 刷新索引、Ctrl+L 日志"""
        from PySide6.QtGui import QKeySequence, QShortcut

        def add(seq, fn):
            QShortcut(QKeySequence(seq), self).activated.connect(fn)

        add("Ctrl+O", self.pick_source)
        add("Ctrl+Enter", self.add_to_queue)
        add("Ctrl+L", self.toggle_log)

        def refresh_lib():
            page = getattr(self, "pageBms", None)
            if page is not None:
                self.tabs.setCurrentWidget(page)
                page.refresh_index()

        add("F5", refresh_lib)

    def _wrap(self, title: str, w: QWidget) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(title)
        lab.setStyleSheet("color:#666;font-weight:bold")
        lay.addWidget(lab)
        lay.addWidget(w, 1)
        return box

    def toggle_log(self):
        self._log_open = not getattr(self, "_log_open", True)
        self.settings.setValue("log_open", self._log_open)
        self._apply_log_open()

    def _apply_log_open(self):
        opened = getattr(self, "_log_open", True)
        if hasattr(self, "logWrap"):
            self.logWrap.setVisible(opened)
        if hasattr(self, "btnLogToggle"):
            self.btnLogToggle.setText("日志 ▾" if opened else "日志 ▸")

    def _on_progress_msg(self, s: str):
        """转换器发 [PROGRESS n] 时更新进度条；其它消息照常进日志"""
        if isinstance(s, str) and s.startswith("[PROGRESS "):
            try:
                n = int(s.split("]", 1)[0].split()[1])
            except (ValueError, IndexError):
                return
            if hasattr(self, "pbar") and getattr(self, "_queue_running", False):
                done = getattr(self, "_queue_index", 0)
                self.pbar.setValue(done * 100 + max(0, min(100, n)))
            if hasattr(self, "lblLastStatus"):
                self.lblLastStatus.setText(str(s).split("]", 1)[-1].strip() or "处理中…")
            return
        self.info(s)

    def _log_to_file(self, text: str):
        """同一条日志同时写进文件：出问题时能事后翻，用户也能直接把文件发过来"""
        fh = getattr(self, "_log_fh", None)
        if fh is None:
            return
        try:
            import datetime as _dt
            fh.write(f"[{_dt.datetime.now():%H:%M:%S}] {text}\n")
            fh.flush()
        except Exception:                                      # noqa: BLE001
            self._log_fh = None

    def log_file_path(self) -> str:
        return str(getattr(self, "_log_path", "") or "")

    def _open_log_file(self):
        """只留一个日志文件：每次启动覆盖重写（用户要求，不累计）"""
        try:
            import datetime as _dt
            logs = services.LIB_ROOT / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            path = logs / "app.log"
            # 顺手清掉早期按日期分的旧日志
            for old in logs.glob("app-*.log"):
                try:
                    old.unlink()
                except OSError:
                    pass
            self._log_fh = open(path, "w", encoding="utf-8")
            self._log_path = path
            self._log_fh.write(f"===== {_dt.datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
            self._log_fh.flush()
        except Exception as e:                                 # noqa: BLE001
            self._log_fh = None
            self._log_path = None
            print(f"[日志] 无法创建日志文件：{e}")

    def info(self, msg: str):
        self._log_to_file(str(msg))
        self._append_colored(str(msg), "plain")
        sb = self.txtLog.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _append_colored(self, text: str, kind: str = "info"):
        """往日志里插一行带底色的文字（成功绿 / 失败红 / 警告黄 / 普通蓝）"""
        from PySide6.QtGui import QTextBlockFormat, QTextCharFormat, QTextCursor
        dark = _is_dark()
        palette = {
            "ok":   (("#1f3d24", "#d9f5dd") if dark else ("#d7f5dc", "#14532d")),
            "err":  (("#4a1b1b", "#ffd7d7") if dark else ("#ffe0e0", "#7f1d1d")),
            "warn": (("#4a3d12", "#ffe9a8") if dark else ("#fff4cc", "#7a5b00")),
            "info": (("#2b3a4a", "#dce9f7") if dark else ("#e8f0fb", "#1e3a5f")),
        }
        cur = self.txtLog.textCursor()
        cur.movePosition(QTextCursor.End)
        bf = QTextBlockFormat()
        cf = QTextCharFormat()
        if kind == "plain":
            bf.setBackground(QBrush(Qt.NoBrush))        # 明确"无底色"，避免继承上一行
        else:
            bg, fg = palette.get(kind, palette["info"])
            bf.setBackground(QBrush(QColor(bg)))
            cf.setForeground(QBrush(QColor(fg)))
        # 只有一行空白时复用它，别在顶部留个黑块
        doc = self.txtLog.document()
        reuse = (doc.blockCount() == 1 and not doc.firstBlock().text())
        cur.insertBlock(bf) if not reuse else cur.setBlockFormat(bf)
        cur.insertText(str(text), cf)
        self.txtLog.setTextCursor(cur)
        self.txtLog.ensureCursorVisible()

    def set_status(self, text: str, kind: str = "info"):
        # 进行中的提示不上色（只在日志里留一行，避免满屏黄条）
        self._append_colored(text, "plain" if kind == "busy" else kind)
        if hasattr(self, "lblLastStatus"):
            color = {"ok": "#1a7f37", "err": "#c0392b", "warn": "#a06a00"}.get(kind, "#555")
            self.lblLastStatus.setStyleSheet(f"color:{color}")
            self.lblLastStatus.setText(str(text).splitlines()[0][:160] if text else "")

    def on_stdout(self, line: str):
        self.txtCli.appendPlainText(line)
        sb = self.txtCli.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, e):
        """关窗时等后台任务收尾，避免 QThread 还在跑就被销毁"""
        try:
            self.settings.setValue("win_geometry", self.saveGeometry())
        except Exception:                                      # noqa: BLE001
            pass
        w = getattr(self, "worker", None)
        if w is not None:
            try:
                if w.isRunning():
                    w.requestInterruption()
                    w.quit()
                    if not w.wait(8000):          # 给正在跑的任务一点时间收尾
                        w.terminate()             # 还不停就强制结束，避免退出崩溃
                        w.wait(1000)
            except Exception:                                  # noqa: BLE001
                pass
        super().closeEvent(e)

    def on_cli(self, entry: dict):
        cmd = entry["cmd"]
        if len(cmd) > 320:
            cmd = cmd[:320] + " …"
        line = f"[{entry['at']}] code={entry['code']} {entry['ms']}ms  {cmd}"
        if entry["output"]:
            tail = entry["output"].strip().splitlines()
            line += "\n      " + "\n      ".join(tail[-3:])
        # 工作线程里不能直接碰控件（会闪退），发信号交给主线程
        self._cli_signal.emit(line)

    def run_bg(self, fn, on_done, *args, on_error=None, **kwargs):
        """跑一个后台任务。

        注意：队列会连续发起多次任务，所以线程/worker 必须用列表各自持有到结束，
        不能共用一个 self.thread —— 否则上一个 QThread 还在跑就被覆盖销毁。
        """
        thread = QThread(self)
        worker = Worker(fn, *args, **kwargs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress_msg)
        worker.done.connect(on_done)
        if on_error is None:
            worker.failed.connect(self.on_failed)
        else:
            worker.failed.connect(lambda m: (self.on_failed(m), on_error(m)))
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)

        live = getattr(self, "_live_tasks", None)
        if live is None:
            live = self._live_tasks = []
        live.append((thread, worker))

        def _cleanup(*_a):
            try:
                live.remove((thread, worker))
            except ValueError:
                pass
            thread.deleteLater()
            worker.deleteLater()

        # 只能等线程真正结束再放手，否则会出现
        # "QThread: Destroyed while thread is still running"
        thread.finished.connect(_cleanup)
        thread.start()

    def on_failed(self, msg: str):
        text = str(msg or "").strip()
        # 第一行写进状态条（折叠日志时也能看到），全文仍然进日志
        first = text.splitlines()[0] if text else "未知错误"
        self.info(f"✗ 出错：{text}")
        self.set_status(f"✗ 出错：{first[:120]}", "err")
        if not getattr(self, "_log_open", True):
            self._log_open = True
            self.settings.setValue("log_open", True)
            self._apply_log_open()

    # ---------------------------------------------------------------- 转换页
    def build_convert_tab(self):
        page = QWidget()
        self.pageConvert = page
        self.tabs.addTab(page, "转换")
        lay = QVBoxLayout(page)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)

        # 左：源 + 表
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        row = QHBoxLayout()
        self.edSource = QLineEdit()
        self.edSource.setPlaceholderText("拖入歌曲文件夹，或点「选择」…")
        b = QPushButton("选择")
        b.clicked.connect(self.pick_source)
        r = QPushButton("读取")
        r.clicked.connect(self.do_scan)
        row.addWidget(self.edSource, 1)
        row.addWidget(b)
        row.addWidget(r)
        lv.addLayout(row)

        prow = QHBoxLayout()
        self.edPlayer = QLineEdit()
        self.edPlayer.setPlaceholderText("可选：beatoraja 根目录（设了才显示本机成绩）")
        pb = QPushButton("选择成绩库")
        pb.clicked.connect(self.pick_player)
        pc = QPushButton("清除")
        pc.clicked.connect(self.clear_player)
        prow.addWidget(QLabel("成绩库"))
        prow.addWidget(self.edPlayer, 1)
        prow.addWidget(pb)
        prow.addWidget(pc)
        lv.addLayout(prow)

        self.lblScan = QLabel("尚未读取")
        lv.addWidget(self.lblScan)
        hintrow = QHBoxLayout()
        hint = QLabel("勾选要导入的谱面；把行拖到右边难度槽可指定槽位；点表头排序。")
        hint.setStyleSheet("color:#777")
        self.btnSp = QPushButton("SP")
        self.btnDp = QPushButton("DP")
        for b in (self.btnSp, self.btnDp):
            b.setCheckable(True)
            b.setMinimumWidth(48)
            b.setMinimumHeight(26)
        self.btnSp.setChecked(True)
        self.btnSp.clicked.connect(lambda: self.set_mode("SP"))
        self.btnDp.clicked.connect(lambda: self.set_mode("DP"))
        hintrow.addWidget(self.btnSp)
        hintrow.addWidget(self.btnDp)

        self.ckHide5k = QCheckBox("隐藏非标准谱面")
        self.ckHide5k.setChecked(self.settings.value("def_hide5k", True, bool))
        self.ckHide5k.stateChanged.connect(lambda *_: self.apply_filter())
        hintrow.addWidget(hint, 1)
        hintrow.addWidget(self.ckHide5k)
        lv.addLayout(hintrow)

        self.table = ChartTable()
        self.table.assigned.connect(self.on_slot_drop)
        self.table.hovered.connect(self.on_table_hover)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.itemSelectionChanged.connect(self.on_row_selected)
        for col, w in ((0, 28), (2, 48), (3, 46), (4, 62), (5, 48), (6, 62),
                       (7, 60), (8, 74), (9, 74), (10, 110)):
            self.table.setColumnWidth(col, w)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, hdr.ResizeMode.Stretch)
        hdr.sortIndicatorChanged.connect(lambda *_: self.invalidate_row_cache())
        lv.addWidget(self.table, 1)
        split.addWidget(left)

        # 右
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)

        top = QHBoxLayout()
        self.slotbox = QGroupBox("难度槽（可直接拖动谱面进来）")
        outer = QHBoxLayout(self.slotbox)
        self.slotLayout = outer
        self.slotWidgets: dict[int, SlotBox] = {}
        self.slotColumns: dict[str, QVBoxLayout] = {}
        self.slotHeaders: dict[str, QLabel] = {}
        for mode, sids in (("SP", SLOT_ORDER), ("DP", SLOT_ORDER_DP)):
            col = QVBoxLayout()
            head = QLabel(mode)
            head.setAlignment(Qt.AlignCenter)
            head.setStyleSheet("color:#888;font-weight:bold")
            col.addWidget(head)
            self.slotHeaders[mode] = head
            self.slotColumns[mode] = col
            for sid in sids:
                w = SlotBox(sid)
                w.dropped.connect(self.on_slot_drop)
                w.hover_changed.connect(self.on_slot_hover)
                col.addWidget(w)
                self.slotWidgets[sid] = w
            col.addStretch(1)
            outer.addLayout(col)
        self.mode = "SP"
        self.slots = {sid: "" for sid in self.slotWidgets}
        top.addWidget(self.slotbox, 1)
        rv.addLayout(top)

        fg = QGridLayout()
        for i, (label, attr, ph) in enumerate((
                ("曲名", "edTitle", ""), ("曲风", "edGenre", ""),
                ("作曲人", "edArtist", ""),
                ("歌曲 ID", "edSongId", "留空 = 自动分配（跟文件夹走：SUBSTREAM→1xxx、ZINRAI→34xxx）"))):
            fg.addWidget(QLabel(label), i, 0)
            ed = QLineEdit()
            if ph:
                ed.setPlaceholderText(ph)
            setattr(self, attr, ed)
            if attr == "edTitle":
                # 曲名：输入框 + 「用罗马音」+ 下方黄色警告（只有字体缺字形时出现）
                box = QWidget()
                bv = QVBoxLayout(box)
                bv.setContentsMargins(0, 0, 0, 0)
                bv.setSpacing(2)
                bh = QHBoxLayout()
                bh.setContentsMargins(0, 0, 0, 0)
                bh.setSpacing(6)
                bh.addWidget(ed, 1)
                self.btnRomanize = QPushButton("自动转写")
                self.btnRomanize.setFixedWidth(86)
                self.btnRomanize.setToolTip(
                    "IIDX 字体没有韩文/简体字形：韩文→罗马音、中文→拼音")
                self.btnRomanize.setVisible(False)
                self.btnRomanize.clicked.connect(self.apply_romanized_title)
                bh.addWidget(self.btnRomanize)
                bv.addLayout(bh)
                self.lblTitleWarn = QLabel("")
                self.lblTitleWarn.setStyleSheet("color:#c98a00")
                self.lblTitleWarn.setWordWrap(True)
                self.lblTitleWarn.setVisible(False)
                bv.addWidget(self.lblTitleWarn)
                fg.addWidget(box, i, 1)
                ed.textChanged.connect(self._title_changed)
            else:
                fg.addWidget(ed, i, 1)

        # ---- 副标题：压成一行，放在歌曲信息最下面（同一网格，保证对齐）----
        subrow_i = 4
        fg.addWidget(QLabel("副标题"), subrow_i, 0)
        subwrap = QWidget()
        sh = QHBoxLayout(subwrap)
        sh.setContentsMargins(0, 0, 0, 0)
        self.edSubtitle = QPlainTextEdit()
        self.edSubtitle.setFixedHeight(28)
        self.edSubtitle.textChanged.connect(lambda: self._title_changed())
        self.edSubtitle.setPlaceholderText(
            "谱师署名（≤3 行）")
        sh.addWidget(self.edSubtitle, 1)
        self.ckSubAuto = QCheckBox("自动")
        self.ckSubAuto.setToolTip("从 #SUBARTIST 自动生成（可手动改）")
        self.ckSubAuto.setChecked(True)
        self.ckSubAuto.stateChanged.connect(lambda *_: self.refill_subtitle())
        sh.addWidget(self.ckSubAuto)
        b_recalc = QPushButton("重算")
        b_recalc.setFixedWidth(54)
        b_recalc.clicked.connect(self.refill_subtitle)
        b_clear = QPushButton("清空")
        b_clear.setFixedWidth(54)
        b_clear.clicked.connect(self.edSubtitle.clear)
        sh.addWidget(b_recalc)
        sh.addWidget(b_clear)
        self.lblSubHint = QLabel("")
        self.lblSubHint.setStyleSheet("color:#888")
        self.lblSubHint.setMinimumWidth(150)
        sh.addWidget(self.lblSubHint)
        fg.addWidget(subwrap, subrow_i, 1)
        fg.setColumnStretch(1, 1)
        rv.addLayout(fg)

        THUMB_H = 238
        subrow = QHBoxLayout()

        # 左：游戏环境（可滚动，与右侧预览图同高）
        envbox = QGroupBox("游戏环境")
        ev = QVBoxLayout(envbox)
        envbox.setFixedHeight(THUMB_H)
        envscroll = QScrollArea()
        envscroll.setWidgetResizable(True)
        envscroll.setFrameShape(QFrame.NoFrame)
        self.lblEnv = QLabel("（未检测）")
        self.lblEnv.setWordWrap(True)
        self.lblEnv.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lblEnv.setTextInteractionFlags(Qt.TextSelectableByMouse)
        envscroll.setWidget(self.lblEnv)
        ev.addWidget(envscroll, 1)
        subrow.addWidget(envbox, 1)

        thumbbox = QGroupBox("预览图")
        thumbbox.setFixedHeight(THUMB_H)
        tv = QVBoxLayout(thumbbox)
        tv.setContentsMargins(8, 6, 8, 6)
        tv.setSpacing(2)
        help_text = ("如果 BMS 本身含预览图，会直接展示。\n"
                     "如果不含，请点击从视频 BGA 中抽帧。")
        hrow = QHBoxLayout()
        hrow.addStretch(1)
        self.lblThumbHelp = QPushButton("?")
        self.lblThumbHelp.setFixedSize(20, 20)
        self.lblThumbHelp.setFlat(True)
        self.lblThumbHelp.setCursor(Qt.WhatsThisCursor)
        self.lblThumbHelp.setToolTip(help_text)
        self.lblThumbHelp.setStyleSheet(
            "QPushButton{border:1px solid #8a8a8a;border-radius:10px;"
            "color:#9a9a9a;font-weight:bold;font-size:12px;padding:0}"
            "QPushButton:hover{background:#3d3d3d;color:#ffffff;"
            "border-color:#cfcfcf}")
        self.lblThumbHelp.clicked.connect(
            lambda: self.show_help_at(self.lblThumbHelp, help_text))
        hrow.addWidget(self.lblThumbHelp)
        tv.addLayout(hrow)
        self.thumb = ThumbLabel()
        self.thumb.clicked.connect(self.on_thumb_click)
        tv.addWidget(self.thumb)
        brow = QHBoxLayout()
        self.btnPrev = QPushButton("‹")
        self.btnPrev.setFixedWidth(26)
        self.btnPrev.clicked.connect(lambda: self.step_frame(-1))
        self.btnNext = QPushButton("›")
        self.btnNext.setFixedWidth(26)
        self.btnNext.clicked.connect(lambda: self.step_frame(1))
        self.btnDraw = QPushButton("抽帧")
        self.btnDraw.clicked.connect(lambda: self.on_thumb_click(2))
        brow.addWidget(self.btnPrev)
        brow.addWidget(self.btnDraw, 1)
        brow.addWidget(self.btnNext)
        tv.addLayout(brow)
        self.lblThumbHint = QLabel("单击抽帧 / 双击重抽")
        self.lblThumbHint.setStyleSheet("color:#888;font-size:11px")
        self.lblThumbHint.setAlignment(Qt.AlignCenter)
        tv.addWidget(self.lblThumbHint)
        thumbbox.setFixedWidth(264)
        subrow.addWidget(thumbbox, 0)
        rv.addLayout(subrow)

        opt = QHBoxLayout()
        self.ckPreview = QCheckBox("试听")
        self.ckThumb = QCheckBox("封面")
        self.ckBga = QCheckBox("BGA")
        self.ckPoor = QCheckBox("坏判层")
        self.lblOrigin = QLabel("放入 IIDX 文件夹")
        self.cbOrigin = QComboBox()
        from core.musicdb import ORIGIN_DEFAULT, ORIGIN_NAMES
        self.cbOrigin.addItems(ORIGIN_NAMES)
        self.cbOrigin.setCurrentIndex(ORIGIN_DEFAULT)      # 默认 SUBSTREAM
        self.cbOrigin.setToolTip(
            "转换后放进游戏里哪个文件夹（选曲界面按代数分的那个列表）\n"
            "SUBSTREAM = 官方只有 12 首，最干净；ZINRAI = 34 代\n"
            "歌曲 ID 的号段会跟着这里走：SUBSTREAM → 1xxx，ZINRAI → 34xxx")
        self.cbOrigin.currentIndexChanged.connect(
            lambda *_: self.settings.setValue("def_origin", self.cbOrigin.currentText()))
        self._poor_label = "坏判层"
        for c, key, dflt in ((self.ckPreview, "def_preview", True),
                             (self.ckThumb, "def_thumb", True),
                             (self.ckBga, "def_bga", True),
                             (self.ckPoor, "def_poor", True)):
            c.setChecked(self.settings.value(key, dflt, bool))
            opt.addWidget(c)
        opt.addSpacing(12)
        opt.addWidget(self.lblOrigin)
        opt.addWidget(self.cbOrigin)
        opt.addStretch(1)
        rv.addLayout(opt)

        act = QHBoxLayout()
        self.btnQueue = QPushButton("添加到队列")
        self.btnQueue.setToolTip("把当前勾选和设置打包成一个任务；\n"
                                 "转换 / 部署都在「队列」页执行（转换不写游戏目录）")
        self.btnQueue.clicked.connect(self.add_to_queue)
        self.lblQueueHint = QLabel("下一步在「队列」页：先转换，再部署")
        self.lblQueueHint.setStyleSheet("color:#8a8a8a")
        act.addWidget(self.btnQueue, 1)
        act.addWidget(self.lblQueueHint, 2)
        rv.addLayout(act)

        tgt = QGroupBox("目标游戏目录")
        tgl = QVBoxLayout(tgt)
        grow = QHBoxLayout()
        self.edGame = QLineEdit()
        hookrow = QHBoxLayout()
        self.lblHook = QLabel("")
        self.lblHook.setWordWrap(True)
        self.lblHook.setStyleSheet("color:#c98a00")
        self.btnFixHook = QPushButton("让 spice2x 加载 ifs_hook")
        self.btnFixHook.setVisible(False)
        self.btnFixHook.clicked.connect(self.fix_hook)
        hookrow.addWidget(self.lblHook, 1)
        hookrow.addWidget(self.btnFixHook)
        tgl.addLayout(hookrow)

        self.edGame.setPlaceholderText("选择 …\\contents 目录（含 data/ 与 data_mods/）")
        gb = QPushButton("选择")
        gb.clicked.connect(self.pick_game)
        grow.addWidget(self.edGame, 1)
        grow.addWidget(gb)
        tgl.addLayout(grow)
        self.edGame.editingFinished.connect(self.do_probe)
        hint = QLabel("统一以 <b>contents</b> 目录为准。选好即自动检测。")
        hint.setStyleSheet("color:#888")
        hint.setWordWrap(True)
        tgl.addWidget(hint)
        rv.insertWidget(0, tgt)      # 第一步就是选目标目录，放最上面
        rv.addStretch(1)

        split.addWidget(right)
        split.setSizes([770, 610])

    # ---------------------------------------------------------------- 曲库页
    def build_queue_tab(self):
        page = QWidget()
        self.pageQueue = page
        self.tabs.addTab(page, "队列")
        lay = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.btnQStart = QPushButton("开始转换")
        self.btnQStart.clicked.connect(self.queue_start)
        self.btnQDeploy = QPushButton("部署到游戏")
        self.btnQDeploy.clicked.connect(self.queue_deploy)
        self.btnQRemove = QPushButton("移除选中")
        self.btnQRemove.clicked.connect(self.queue_remove)
        self.btnQClear = QPushButton("清空")
        self.btnQClear.clicked.connect(self.queue_clear)
        self.btnQCancel = QPushButton("取消")
        self.btnQCancel.setEnabled(False)
        self.btnQCancel.clicked.connect(self.queue_cancel)
        self.btnQDeployOne = QPushButton("部署选中")
        self.btnQDeployOne.clicked.connect(self.queue_deploy_selected)
        bar.addWidget(self.btnQStart)
        bar.addWidget(self.btnQCancel)
        bar.addWidget(self.btnQDeploy)
        bar.addWidget(self.btnQDeployOne)
        bar.addWidget(self.btnQRemove)
        bar.addWidget(self.btnQClear)
        self.lblQState = QLabel("队列为空")
        self.lblQState.setStyleSheet("color:#8a8a8a")
        bar.addSpacing(10)
        bar.addWidget(self.lblQState, 1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)

        # 左：转换队列
        left = QGroupBox("转换队列")
        lv = QVBoxLayout(left)
        self.tblQueue = QTableWidget(0, 5)
        self.tblQueue.setHorizontalHeaderLabels(["状态", "曲名", "ID", "放入", "谱面"])
        self.tblQueue.verticalHeader().setVisible(False)
        self.tblQueue.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblQueue.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblQueue.setDragDropMode(QAbstractItemView.InternalMove)
        self.tblQueue.setDefaultDropAction(Qt.MoveAction)
        self.tblQueue.setDragEnabled(True)
        self.tblQueue.horizontalHeader().setStretchLastSection(True)
        self.tblQueue.setColumnWidth(0, 72)
        self.tblQueue.setColumnWidth(1, 220)
        self.tblQueue.setColumnWidth(2, 70)
        self.tblQueue.setColumnWidth(3, 110)
        lv.addWidget(self.tblQueue, 1)
        split.addWidget(left)

        # 右：曲库已有
        right = QGroupBox("曲库已有（游戏里现在有哪些歌，避免重复加）")
        rv = QVBoxLayout(right)
        filt = QHBoxLayout()
        self.ckShowOfficial = QCheckBox("显示 IIDX 本家")
        self.ckShowOmnimix = QCheckBox("显示 omnimix")
        self.ckShowLeftover = QCheckBox("显示残留文件")
        self.ckShowLeftover.setToolTip("目录里有文件、但曲库里没有对应记录的孤儿文件")
        for c, key in ((self.ckShowOfficial, "show_official"),
                       (self.ckShowOmnimix, "show_omnimix"),
                       (self.ckShowLeftover, "show_leftover")):
            c.setChecked(self.settings.value(key, False, bool))   # 默认不勾
            c.toggled.connect(self._on_queue_lib_filter)
            filt.addWidget(c)
        filt.addStretch(1)
        rv.addLayout(filt)
        self.tblQueueLib = QTableWidget(0, 3)
        self.tblQueueLib.setHorizontalHeaderLabels(["ID", "曲名", "来源"])
        self.tblQueueLib.verticalHeader().setVisible(False)
        self.tblQueueLib.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblQueueLib.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblQueueLib.horizontalHeader().setStretchLastSection(True)
        self.tblQueueLib.setColumnWidth(0, 70)
        self.tblQueueLib.setColumnWidth(1, 240)
        self.tblQueueLib.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tblQueueLib.customContextMenuRequested.connect(self.on_queuelib_menu)
        rv.addWidget(self.tblQueueLib, 1)
        split.addWidget(right)
        split.setSizes([640, 560])

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.pbar.setFormat("%p%")
        self.pbar.setMaximumHeight(16)
        lay.addWidget(self.pbar)
        try:
            self.tblQueue.model().rowsMoved.connect(self._on_queue_rows_moved)
        except Exception:                                  # noqa: BLE001
            pass
        self.tblQueue.doubleClicked.connect(self.on_queue_double_click)
        lay.addWidget(QLabel(
            "提示：先把歌「添加到队列」，再点「开始转换」；产物集中在暂存区，"
            "确认无误后点「部署到游戏」。左栏可拖动行排序。"))
        self.refresh_queue_table()
        self.refresh_queue_lib()

    def build_bmslib_tab(self):
        """BMS 曲库浏览（索引在 library/bms_index.sqlite）"""
        self.pageBms = BmsLibraryPage(ROOT / "library" / "bms_index.sqlite", self.settings)
        self.pageBms.model.set_score_fn(self._score_text)
        try:
            self.pageBms.model.set_score_ready(services.player() is not None)
        except Exception:                                      # noqa: BLE001
            pass
        self.pageBms.scoreRootChanged.connect(
            lambda path: self.apply_score_root(path, src="bms"))
        self.pageBms.requestLoad.connect(self.open_folder_in_convert)
        self.tabs.insertTab(1, self.pageBms, "BMS 谱面库")

    def open_folder_in_convert(self, folder: str):
        """曲库页双击/按钮 → 回到转换页并读这个文件夹"""
        folder = str(folder)
        self.tabs.setCurrentIndex(0)
        self.edSource.setText(folder)
        self.do_scan()

    def build_lib_tab(self):
        page = QWidget()
        self.pageLib = page
        self.tabs.addTab(page, "IIDX 曲库与资源")
        lay = QVBoxLayout(page)

        self.lblUsage = QLabel("检测后显示")
        self.lblUsage.setWordWrap(True)
        lay.addWidget(self.lblUsage)
        legend = QLabel(
            "名词：<b>曲库</b> = 游戏里的选曲列表（music_omni.bin）；"
            "<b>文件</b> = 音源/谱面/影片/封面；"
            "<b>回收区</b> = 删掉的文件先放这里，可还原。"
            "　列表支持 Ctrl / Shift 多选，多选后右键可批量删除。")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#777")
        lay.addWidget(legend)

        self.splitLib = QSplitter(Qt.Horizontal)
        self.treeLib = QTreeWidget()
        self.treeLib.setHeaderLabels(["ID", "曲名", "文件状态", "占用"])
        self.treeLib.setColumnWidth(0, 100)
        self.treeLib.setColumnWidth(2, 210)
        self.treeLib.setColumnWidth(3, 90)
        hdr = self.treeLib.header()
        hdr.setSectionResizeMode(1, hdr.ResizeMode.Stretch)
        self.treeLib.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.treeLib.setContextMenuPolicy(Qt.CustomContextMenu)
        self.treeLib.customContextMenuRequested.connect(self.on_tree_menu)
        self.treeLib.itemSelectionChanged.connect(self._update_lib_detail)
        self.splitLib.addWidget(self.treeLib)

        # ---- 右侧详情：选中哪首就显示它的资源情况 ----
        self.boxLibDetail = QGroupBox("选中曲目")
        dv = QVBoxLayout(self.boxLibDetail)
        self.lblLibTitle = QLabel("（没有选中）")
        self.lblLibTitle.setStyleSheet("font-size:14px;font-weight:600")
        self.lblLibTitle.setWordWrap(True)
        dv.addWidget(self.lblLibTitle)
        self.lblLibMeta = QLabel("")
        self.lblLibMeta.setWordWrap(True)
        self.lblLibMeta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dv.addWidget(self.lblLibMeta)
        self.lblLibFiles = QLabel("")
        self.lblLibFiles.setWordWrap(True)
        self.lblLibFiles.setStyleSheet("color:#666")
        self.lblLibFiles.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dv.addWidget(self.lblLibFiles)
        dv.addStretch(1)
        self._lib_detail_btns = QHBoxLayout()
        self.btnLibOpen = QPushButton("打开目录")
        self.btnLibOpen.clicked.connect(self._lib_open_selected)
        self.btnLibReapply = QPushButton("写回曲库")
        self.btnLibReapply.clicked.connect(self._lib_reapply_selected)
        self._lib_detail_btns.addWidget(self.btnLibOpen)
        self._lib_detail_btns.addWidget(self.btnLibReapply)
        self._lib_detail_btns.addStretch(1)
        dv.addLayout(self._lib_detail_btns)
        self.splitLib.addWidget(self.boxLibDetail)
        self.splitLib.setSizes([760, 360])
        self.boxLibDetail.setMinimumWidth(280)
        lay.addWidget(self.splitLib, 1)

        self.lblLib = QLabel("")
        self.lblLib.setWordWrap(True)
        self.lblLib.setVisible(False)
        lay.addWidget(self.lblLib)

        row = QHBoxLayout()
        b1 = QPushButton("刷新")
        b1.clicked.connect(self.refresh_lib)
        self.btnTrash = QPushButton("回收区…")
        self.btnTrash.clicked.connect(self.show_trash)
        row.addWidget(self.btnTrash)
        b2 = QPushButton("把本机库里缺的记录写回曲库")
        b2.clicked.connect(self.do_reapply)
        self.btnReapply = b2
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)

        self.boxDeploy = QGroupBox("部署记录")
        dv = QVBoxLayout(self.boxDeploy)
        self.lblDeploy = QLabel("（无）")
        self.lblDeploy.setWordWrap(True)
        dv.addWidget(self.lblDeploy)
        lay.addWidget(self.boxDeploy)

    def _current_lib_item(self):
        it = self.treeLib.currentItem()
        if it is None:
            return None, {}
        data = it.data(0, Qt.UserRole)
        if isinstance(data, tuple) and data[0] != "group":
            return data[0], data[1]
        return None, {}

    def _group_label(self, key: str) -> str:
        return {"ours": "本程序部署", "official": "KONAMI 本家", "omnimix": "omnimix 追加",
                "recoverable": "本机库有记录、曲库里没有", "others": "其它"}.get(key, key or "")

    def _update_lib_detail(self):
        if not hasattr(self, "lblLibMeta"):
            return
        sel = self._selected_lib_items()
        if len(sel) > 1:
            total = sum((x[1].get("size") or 0) for x in sel)
            groups = {}
            for g, _i in sel:
                groups[self._group_label(g)] = groups.get(self._group_label(g), 0) + 1
            self.lblLibTitle.setText(f"已选中 {len(sel)} 首")
            self.lblLibMeta.setText(
                f"合计占用 {total / 1e6:.1f} MB　·　"
                + "　".join(f"{k} {v}" for k, v in groups.items())
                + "\n右键可以批量移到回收区 / 彻底删除")
            self.lblLibFiles.setText("")
            self.btnLibOpen.setEnabled(False)
            self.btnLibReapply.setEnabled(False)
            self.btnLibReapply.setText("把记录写回曲库")
            return
        gkey, it = self._current_lib_item()
        if not it:
            self.lblLibTitle.setText("（没有选中）")
            self.lblLibMeta.setText("在左边点一首歌，这里会显示它的资源情况。")
            self.lblLibFiles.setText("")
            self.btnLibOpen.setEnabled(False)
            self.btnLibReapply.setEnabled(False)
            self.btnLibReapply.setText("把记录写回曲库")
            return
        self.lblLibTitle.setText(f"{it.get('id')}　{it.get('title', '')}")
        stat = self._file_status_text(it) if gkey in ("ours", "recoverable") else "—"
        size = it.get("size") or 0
        self.lblLibMeta.setText(
            f"来源：{self._group_label(gkey)}　状态：{stat}　占用：{size / 1e6:.1f} MB")
        f = it.get("files") or {}
        contents = services._contents_of(pathlib.Path(self.target["game_dir"])) if self.target else None

        def _rel(v):
            try:
                return pathlib.Path(v).relative_to(contents).as_posix()
            except (ValueError, TypeError, AttributeError):
                return str(v)

        rows, tips = [], []
        for k, name in (("sound", "音源目录"), ("chart", "谱面"), ("audio", "键音"),
                        ("movie", "影片"), ("thumb", "封面"), ("ifs", "图层包")):
            v = f.get(k)
            rows.append(f"{name}：{_rel(v) if v else '（缺）'}")
            if v:
                tips.append(f"{name}：{v}")
        self.lblLibFiles.setText("\n".join(rows))
        self.lblLibFiles.setToolTip("\n".join(tips))       # 完整路径放悬浮里
        self.btnLibOpen.setEnabled(bool(f.get("sound")))
        # 记录已在曲库里 → 不用写回；只有"本机库有记录但曲库里没有"才需要
        in_db = bool(it.get("in_db"))
        self.btnLibReapply.setEnabled(gkey in ("ours", "recoverable") and not in_db)
        self.btnLibReapply.setText("已在曲库里" if in_db else "把记录写回曲库")
        self.btnLibReapply.setToolTip(
            "omnigix/omnimix 包更新会覆盖 music_omni.bin，我们加的歌会从曲库里消失；\n"
            "音源/影片文件一般还在。这时点它能把这个歌的曲库记录补回去（不用重转）。"
            if not in_db else "这首歌的曲库记录已经在里面了，不用写回")

    def _lib_open_selected(self):
        _gkey, it = self._current_lib_item()
        p = (it.get("files") or {}).get("sound")
        if not p:
            self.set_status("这首歌没有音源目录", "warn")
            return
        import os
        try:
            os.startfile(str(p))                               # noqa: S606
        except OSError as e:
            self.set_status(f"打不开目录：{e}", "err")

    def _lib_reapply_selected(self):
        gkey, it = self._current_lib_item()
        if gkey not in ("ours", "recoverable") or not self.target:
            return
        self.run_bg(services.reapply_one, self.on_one_reapplied,
                    self.target["game_dir"], int(it["id"]))

    # ---------------------------------------------------------------- 曲库树
    def show_trash(self):
        """回收区：列出被删到 contents 同级 bms2iidx_trash 的条目，可还原"""
        if not self.target:
            QMessageBox.information(self, "先检测游戏目录", "请先选好游戏目录并点「检测」。")
            return
        contents = services._contents_of(pathlib.Path(self.target["game_dir"]))
        items = services.trash_list(contents)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"回收区（{services.trash_root(contents)}）")
        dlg.resize(760, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("删除在这里的歌曲资源可以还原回游戏目录；"
                           "记录已删的需要重新转换才能回到曲库。"))
        tb = QTableWidget(len(items), 5, dlg)
        tb.setHorizontalHeaderLabels(["时间_ID_动作", "ID", "曲名", "文件数", "占用"])
        tb.verticalHeader().setVisible(False)
        tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tb.setSelectionBehavior(QAbstractItemView.SelectRows)
        for r, it in enumerate(items):
            vals = [it["name"], str(it.get("song_id") or ""), it.get("title") or "",
                    str(it.get("files") or 0), f"{it['size'] / 1e6:.1f} MB"]
            for c, val in enumerate(vals):
                tb.setItem(r, c, QTableWidgetItem(val))
        tb.horizontalHeader().setStretchLastSection(True)
        tb.setColumnWidth(0, 260)
        tb.setColumnWidth(2, 200)
        v.addWidget(tb, 1)

        btns = QHBoxLayout()
        b_restore = QPushButton("还原选中（放回游戏目录）")
        b_purge = QPushButton("彻底删除选中（不可还原）")
        b_purge.setProperty("role", "danger")
        b_open = QPushButton("打开该目录")
        b_close = QPushButton("关闭")

        def do_restore():
            rows = sorted({i.row() for i in tb.selectedIndexes()})
            if not rows:
                return
            it = items[rows[0]]
            r = services.trash_restore(contents, it["name"])
            if not r.get("ok"):
                QMessageBox.warning(dlg, "还原失败", r.get("error", ""))
                return
            msg = f"已还原 {len(r['restored'])} 个文件"
            if r.get("skipped"):
                msg += f"，跳过已存在 {len(r['skipped'])} 个"
            if r.get("record_removed"):
                msg += "\n注意：这条歌的曲库记录之前被删了，需要重新转换/写回"
            QMessageBox.information(dlg, "还原完成", msg)
            self.set_status(msg.replace("\n", "　"), "ok")
            dlg.accept()
            self.refresh_lib()
            self.do_probe()

        def do_open():
            rows = sorted({i.row() for i in tb.selectedIndexes()})
            if not rows:
                return
            import os
            os.startfile(items[rows[0]]["dir"])                # noqa: S606

        def do_purge():
            rows = sorted({i.row() for i in tb.selectedIndexes()})
            if not rows:
                return
            it = items[rows[0]]
            size = it.get("size") or 0
            if QMessageBox.question(
                    dlg, "彻底删除",
                    f"永久删除回收区里的这一条？\n\n{it['name']}\n"
                    f"占用 {size / 1e6:.1f} MB\n\n"
                    "删除后**无法还原**（不进回收站，直接消失）。") != QMessageBox.Yes:
                return
            r = services.trash_purge(contents, it["name"])
            if not r.get("ok"):
                QMessageBox.warning(dlg, "删除失败", r.get("error", ""))
                return
            QMessageBox.information(dlg, "已删除",
                                    f"已释放 {r.get('freed', 0) / 1e6:.1f} MB")
            dlg.accept()
            self.show_trash()          # 重新打开，列表刷新

        b_restore.clicked.connect(do_restore)
        b_purge.clicked.connect(do_purge)
        b_open.clicked.connect(do_open)
        b_close.clicked.connect(dlg.reject)
        btns.addWidget(b_restore)
        btns.addWidget(b_purge)
        btns.addWidget(b_open)
        btns.addStretch(1)
        btns.addWidget(b_close)
        v.addLayout(btns)
        dlg.exec()

    def refresh_lib(self):
        if not self.target:
            return
        self.fill_id_usage(self.target["game_dir"])
        self.check_library(self.target["game_dir"])
        try:
            u = services.library_overview(self.target["game_dir"])
        except Exception as e:                                 # noqa: BLE001
            self.info(f"曲库读取失败：{e}")
            return
        if not u.get("ok"):
            return
        self.treeLib.clear()
        for g in u["groups"]:
            top = QTreeWidgetItem([g["label"], "", "", ""])
            top.setData(0, Qt.UserRole, ("group", g["key"]))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            if g["key"] == "recoverable" and g["items"]:
                top.setForeground(0, QBrush(QColor("#e03131")))
            for it in g["items"]:
                stat = self._file_status_text(it) if g["key"] in (
                    "ours", "recoverable") else ""
                size = it.get("size") or 0
                size_txt = f"{size/1e6:.1f} MB" if size else ""
                child = QTreeWidgetItem([str(it["id"]), it.get("title", ""),
                                         stat, size_txt])
                child.setData(0, Qt.UserRole, (g["key"], it))
                if g["key"] == "recoverable":
                    child.setForeground(1, QBrush(QColor("#e03131")))
                elif g["key"] == "ours" and (it.get("missing")
                                            or not it.get("in_db")):
                    child.setForeground(2, QBrush(QColor("#e03131")))
                top.addChild(child)
            self.treeLib.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)      # 必须在加入树之后
            top.setExpanded(not g.get("collapsed"))
        self._update_lib_detail()
        self.info("曲库清单已刷新")

    @staticmethod
    def _file_status_text(it: dict) -> str:
        if not it.get("in_db"):
            return "不在曲库"
        miss = it.get("missing") or []
        if not miss:
            return "齐全"
        names = {"chart": "谱面", "audio": "键音", "movie": "影片", "thumb": "封面"}
        return "缺 " + "、".join(names.get(m, m) for m in miss)

    def build_song_menu(self, sid: int, it: dict, gkey: str):
        """右键菜单：按「曲库记录 / 文件 / 两者」分组，名字写清后果

        术语：
          · 曲库 = 游戏里的选曲列表（music_omni.bin / music_data.bin）
          · 文件 = 音源/谱面/影片/封面（sound/、movie/、graphic/）
          · 回收区 = 文件先移走、可还原；彻底删除 = 不可还原
        """
        menu = QMenu(self)
        head = menu.addAction(f"{sid}　{it.get('title', '')[:30]}")
        head.setEnabled(False)
        acts = {}
        is_ours = gkey in ("ours", "recoverable")
        is_official = gkey in ("official", "omnimix")

        menu.addSection("曲库记录（游戏里的选曲列表）")
        if is_ours:
            acts["reapply"] = menu.addAction("把本机保存的记录写回曲库")
            acts["reapply"].setToolTip("omni 包更新覆盖曲库后，用它把这首歌补回去，不用重转")
        if is_official:
            a = menu.addAction("本家/omnimix 自带，不建议删记录")
            a.setEnabled(False)
        else:
            acts["trash_record"] = menu.addAction("从曲库移除（文件名/音源都保留）")
            acts["trash_record"].setToolTip("只是让它不在选曲列表里出现，文件不动")

        menu.addSection("文件（音源 / 谱面 / 影片 / 封面）")
        if is_official:
            a = menu.addAction("本家/omnimix 自带，删了这首歌就废了")
            a.setEnabled(False)
        else:
            acts["trash_files"] = menu.addAction("把文件移到回收区（可还原）")
            acts["trash_files"].setToolTip("移动到 contents 同级的 bms2iidx_trash，可在「回收区」还原")
            acts["delete_files"] = menu.addAction("彻底删除文件（不可还原）")
            acts["delete_files"].setToolTip("直接删除，不进回收区")

        if not is_official:
            menu.addSection("记录和文件一起处理")
            acts["trash_all"] = menu.addAction("从曲库移除 + 文件移到回收区")
            acts["trash_all"].setToolTip("最稳妥的「删掉这首歌」：都能还原")
            acts["delete_all"] = menu.addAction("从曲库移除 + 彻底删除文件")
            acts["delete_all"].setToolTip("彻底清掉，不可还原")

        menu.addSection("其它")
        acts["open_dir"] = menu.addAction("打开它的文件目录")
        acts["copy_id"] = menu.addAction("复制歌曲 ID")
        if gkey in ("recoverable", "others"):
            acts["peek"] = menu.addAction("看影片首帧（帮我认这是哪首歌）")
        return menu, acts

    def _selected_lib_items(self):
        """当前选中的所有歌曲：[(gkey, it)]，组节点会被忽略"""
        out = []
        for node in self.treeLib.selectedItems():
            data = node.data(0, Qt.UserRole)
            if isinstance(data, tuple) and data[0] != "group":
                out.append((data[0], data[1]))
        return out

    def _bulk_menu(self, sel):
        """多选时的右键菜单"""
        menu = QMenu(self)
        head = menu.addAction(f"已选中 {len(sel)} 首")
        head.setEnabled(False)
        deletable = [x for x in sel if x[0] not in ("official", "omnimix")]
        acts = {}
        if deletable:
            n = len(deletable)
            menu.addSection(f"只动曲库记录（{n} 首，文件保留）")
            acts["bulk_trash_record"] = menu.addAction(f"从曲库移除这 {n} 首（文件保留）")
            menu.addSection(f"只动文件（{n} 首，曲库记录保留）")
            acts["bulk_trash_files"] = menu.addAction(f"把这 {n} 首的文件移到回收区（可还原）")
            acts["bulk_purge_files"] = menu.addAction(f"彻底删除这 {n} 首的文件（不可还原）")
            menu.addSection(f"记录和文件一起（{n} 首）")
            acts["bulk_trash"] = menu.addAction(f"从曲库移除 + 文件移到回收区")
            acts["bulk_purge"] = menu.addAction(f"从曲库移除 + 彻底删除文件")
        menu.addSection("其它")
        acts["bulk_copy"] = menu.addAction("复制这些 ID")
        return menu, acts, [int(it["id"]) for _g, it in deletable]

    def on_tree_menu(self, pos):
        sel = self._selected_lib_items()
        if len(sel) > 1:
            menu, acts, ids = self._bulk_menu(sel)
            chosen = menu.exec(self.treeLib.viewport().mapToGlobal(pos))
            if chosen is None:
                return
            gd = self.target["game_dir"] if self.target else None
            if chosen is acts.get("bulk_copy"):
                QApplication.clipboard().setText("、".join(str(i) for i in ids))
                self.set_status(f"已复制 {len(ids)} 个 ID", "ok")
                return
            plans = {
                "bulk_trash_record": ("从曲库移除（文件保留）", True, False, False,
                                      f"把 {len(ids)} 首从曲库摘掉？文件都还在。"),
                "bulk_trash_files": ("文件移到回收区（记录保留）", False, True, False,
                                     f"把 {len(ids)} 首的文件移到回收区？曲库记录保留，可还原。"),
                "bulk_purge_files": ("彻底删除文件（记录保留）", False, True, True,
                                     f"彻底删除 {len(ids)} 首的文件？不可还原，曲库记录保留。"),
                "bulk_trash": ("记录 + 文件移到回收区", True, True, False,
                               f"把 {len(ids)} 首移出曲库、文件移到回收区？可从「回收区」还原。"),
                "bulk_purge": ("彻底删除记录 + 文件", True, True, True,
                               f"彻底删除 {len(ids)} 首（记录 + 文件）？不可还原。"),
            }
            for key, (title, drop_rec, drop_files, permanent, ask) in plans.items():
                if chosen is not acts.get(key):
                    continue
                if QMessageBox.question(self, title, ask) != QMessageBox.Yes:
                    return
                fn = services.purge_songs if permanent else services.trash_songs
                self.run_bg(fn, self.on_bulk_done, gd, ids, drop_rec, drop_files)
                return
            return
        item = self.treeLib.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        kind, payload = data
        if kind == "group":
            return
        gkey, it = kind, payload
        sid = int(it["id"])
        menu, acts = self.build_song_menu(sid, it, gkey)
        chosen = menu.exec(self.treeLib.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        for key, act in acts.items():
            if chosen is act:
                self.on_song_action(key, sid, it, gkey)
                return

    def on_bulk_done(self, r: dict):
        n = r.get("done", 0)
        failed = r.get("failed") or []
        if failed:
            self.set_status(f"批量操作完成 {n} 首，失败 {len(failed)} 首", "warn")
            for sid, why in failed[:5]:
                self.info(f"  ✗ {sid}: {why}")
        else:
            self.set_status(f"✅ 批量操作完成：{n} 首", "ok")
        self.refresh_lib()
        self.do_probe()

    def on_song_action(self, action: str, sid: int, it: dict, gkey: str):
        gd = self.target["game_dir"]
        if action == "copy_id":
            QApplication.clipboard().setText(str(sid))
            self.set_status(f"已复制 ID {sid}", "ok")
            return
        if action == "open_dir":
            import os
            p = it.get("files", {}).get("sound") or ""
            if p and pathlib.Path(p).exists():
                os.startfile(p)                                # noqa: S606
            else:
                self.info(f"{sid} 没有音源目录")
            return
        if action == "reapply":
            self.run_bg(services.reapply_one, self.on_one_reapplied, gd, sid)
            return
        if action == "delete_record":
            if QMessageBox.question(
                    self, "从曲库移除",
                    f"把 {sid} 从曲库里摘掉（文件保留）？\n\n"
                    "可以之后用「删除残留文件」清理，或重新写回。") == QMessageBox.Yes:
                self.run_bg(services.remove_song, self.on_song_removed,
                            gd, sid, True, False)
            return
        if action == "delete_all":
            if QMessageBox.question(
                    self, "移除并删除文件",
                    f"把 {sid} 从曲库摘掉，并删除它的音源/影片/封面？\n"
                    "此操作不可撤销（但可以用「备份与恢复」还原曲库）。") == QMessageBox.Yes:
                self.run_bg(services.remove_song, self.on_song_removed,
                            gd, sid, True, True)
            return
        if action in ("trash_record", "trash_files", "trash_all"):
            drop_rec = action in ("trash_record", "trash_all")
            drop_files = action in ("trash_files", "trash_all")
            what = ("歌单 + 资源" if (drop_rec and drop_files)
                    else ("只删歌单" if drop_rec else "只删资源"))
            if QMessageBox.question(
                    self, "删除到回收区",
                    f"即将删除：{what}\nID {sid}　{it.get('title', '')}\n\n"
                    "资源会移动到 contents 同级的 bms2iidx_trash，可随时还原。\n继续？"
            ) != QMessageBox.Yes:
                return
            self.set_status(f"删除中（{what}）：{sid}", "busy")
            self.run_bg(services.trash_song, self.on_trashed,
                        gd, sid, drop_rec, drop_files)
            return
        if action == "peek":
            self.run_bg(services.orphan_preview, self.on_orphan_preview, gd, sid)
            return
        if action == "delete_files":
            if QMessageBox.question(
                    self, "删除残留文件",
                    f"删除 {sid} 在游戏目录里的文件？\n"
                    "注意：如果这是游戏本体/omnimix 自带的资源，删了这首歌就废了。") \
                    == QMessageBox.Yes:
                self.run_bg(services.delete_song_files, self.on_files_deleted, gd, sid)

    def on_one_reapplied(self, r: dict):
        if r.get("ok"):
            self.set_status(f"已写回曲库：槽位 {r.get('used')}", "ok")
        else:
            self.info("写回失败：" + str(r.get("error")))
        self.refresh_lib()

    def on_trashed(self, r: dict):
        if not r.get("ok"):
            self.set_status(f"✗ 删除失败：{r.get('error')}", "err")
            return
        self.set_status(
            f"已移到回收区：{r.get('title') or ''}（{r.get('files')} 个资源"
            + ("，记录已删" if r.get("record_removed") else "")
            + f"）→ {pathlib.Path(r['trash']).name}", "ok")
        self.refresh_lib()
        if self.target:
            self.do_probe()

    def on_song_removed(self, r: dict):
        self.set_status(f"已从曲库移除（删除文件 {len(r.get('removed') or [])} 项）", "ok")
        self.refresh_lib()

    def on_orphan_preview(self, r: dict):
        if not r.get("ok"):
            self.info(str(r.get("error")))
            return
        box = QMessageBox(self)
        box.setWindowTitle("影片首帧")
        box.setText(f"这是该曲 BGA 的一帧（{r['dir']}）")
        pix = QPixmap(r["path"])
        box.setIconPixmap(pix.scaled(360, 270, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation))
        box.exec()

    def on_files_deleted(self, r: dict):
        self.set_status(f"已删除 {len(r.get('removed') or [])} 项文件", "ok")
        self.refresh_lib()

    def build_backup_tab(self):
        page = QWidget()
        self.tabs.addTab(page, "备份与恢复")
        lay = QVBoxLayout(page)
        tip = QLabel("只备份/恢复<b>本程序会写的那几个数据文件</b>：曲库 music_omni.bin、"
                     "三个 XML（含 .merged 增量）、以及本体位置的 music_data.bin 等。\n"
                     "<b>歌曲资源（音源/影片/封面）不在备份范围内</b>，"
                     "它们由「IIDX 曲库与资源」页单独管理。")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.splitBackup = QSplitter(Qt.Horizontal)
        self.tblBackup = QTableWidget(0, 6)
        self.tblBackup.setHorizontalHeaderLabels(
            ["时间", "类型", "文件数", "大小", "ID", "备注（可双击改）"])
        self.tblBackup.verticalHeader().setVisible(False)
        self.tblBackup.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblBackup.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblBackup.setSelectionMode(QAbstractItemView.SingleSelection)
        hb = self.tblBackup.horizontalHeader()
        hb.setStretchLastSection(False)
        for c, w in enumerate((150, 140, 74, 90, 170, 200)):
            self.tblBackup.setColumnWidth(c, w)
        self.tblBackup.itemSelectionChanged.connect(self._update_backup_detail)
        self.tblBackup.itemChanged.connect(self._on_backup_note_edited)
        self.splitBackup.addWidget(self.tblBackup)

        self.boxBackupDetail = QGroupBox("这个备份包含")
        bd = QVBoxLayout(self.boxBackupDetail)
        self.lblBackupDetail = QLabel("（在左边选一个备份）")
        self.lblBackupDetail.setWordWrap(True)
        self.lblBackupDetail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bd.addWidget(self.lblBackupDetail)
        self.listBackupFiles = QListWidget()
        bd.addWidget(self.listBackupFiles, 1)
        self.splitBackup.addWidget(self.boxBackupDetail)
        self.splitBackup.setSizes([700, 420])
        lay.addWidget(self.splitBackup, 1)

        row = QHBoxLayout()
        for text, fn in (("立即备份", self.do_backup), ("刷新", self.refresh_backups),
                         ("恢复选中", self.do_restore), ("删除选中", self.do_delete_backup)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.lblBackup = QLabel("")
        self.lblBackup.setWordWrap(True)
        lay.addWidget(self.lblBackup)

    def build_help_tab(self):
        page = QWidget()
        self.tabs.addTab(page, "帮助")
        lay = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.lblLogPath = QLabel("")
        self.lblLogPath.setStyleSheet("color:#777")
        try:
            self.lblLogPath.setText(str(services.LIB_ROOT / "logs"))
        except Exception:                                      # noqa: BLE001
            pass
        self.lblLogPath.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(QLabel("运行日志："))
        bar.addWidget(self.lblLogPath, 1)
        b_open_log = QPushButton("打开日志文件")
        b_open_log.clicked.connect(self.open_log_file)
        b_open_dir = QPushButton("打开日志文件夹")
        b_open_dir.clicked.connect(self.open_log_dir)
        bar.addWidget(b_open_log)
        bar.addWidget(b_open_dir)
        lay.addLayout(bar)
        txt = QTextBrowser()
        txt.setHtml(services.HELP_PAGE)
        lay.addWidget(txt, 1)

    def open_log_file(self):
        p = self.log_file_path()
        if not p or not pathlib.Path(p).exists():
            QMessageBox.information(self, "还没有日志", "这次运行还没有产生日志文件。")
            return
        try:
            os.startfile(p)                                    # noqa: S606
        except OSError as e:
            QMessageBox.warning(self, "打不开", str(e))

    def open_log_dir(self):
        d = pathlib.Path(self.log_file_path() or (services.LIB_ROOT / "logs"))
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))                               # noqa: S606
        except OSError as e:
            QMessageBox.warning(self, "打不开", str(e))

    def build_settings_tab(self):
        page = QWidget()
        self.tabs.addTab(page, "设置")
        outer = QVBoxLayout(page)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        outer.addLayout(grid)

        box1 = QGroupBox("外观")
        g1 = QGridLayout(box1)
        g1.addWidget(QLabel("主题"), 0, 0)
        self.cmbTheme = QComboBox()
        self.cmbTheme.addItem("跟随系统", "system")
        self.cmbTheme.addItem("浅色", "light")
        self.cmbTheme.addItem("深色", "dark")
        cur = self.settings.value("theme", "system", str)
        idx = max(0, self.cmbTheme.findData(cur))
        self.cmbTheme.setCurrentIndex(idx)
        self.cmbTheme.currentIndexChanged.connect(self.on_theme_changed)
        self.cmbTheme.setFixedWidth(220)
        g1.addWidget(self.cmbTheme, 0, 1)
        note = QLabel("切换后界面立即生效（保存到本机设置）")
        note.setStyleSheet("color:#888")
        g1.addWidget(note, 1, 0, 1, 2)
        grid.addWidget(box1, 0, 0)

        box_font = QGroupBox("字体（标题/副标题字形缺失时用）")
        gf = QGridLayout(box_font)
        gf.addWidget(QLabel("日文字体"), 0, 0)
        self.cmbFont = QComboBox()
        self.cmbFont.addItem("自动（纯 ASCII → 0，含日文 → 2）", "auto")
        for i, name in ((0, "0 · 拉丁默认"), (1, "1 · 和文A"), (2, "2 · 和文B（默认）"),
                        (3, "3 · 明朝/楷书"), (4, "4 · POP")):
            self.cmbFont.addItem(name, str(i))
        cur_f = str(self.settings.value("font_idx", "auto", str))
        self.cmbFont.setCurrentIndex(max(0, self.cmbFont.findData(cur_f)))
        self.cmbFont.currentIndexChanged.connect(
            lambda *_: self.settings.setValue("font_idx", self.cmbFont.currentData()))
        self.cmbFont.setFixedWidth(220)
        gf.addWidget(self.cmbFont, 0, 1)
        note_f = QLabel("官方 34 库里的用法：font 0 给纯 ASCII 标题（2997 首为主），"
                        "1/2/3/4 给带假名/汉字的标题。选错字体时字会变方块或乱码。")
        note_f.setStyleSheet("color:#888")
        note_f.setWordWrap(True)
        gf.addWidget(note_f, 1, 0, 1, 2)
        grid.addWidget(box_font, 0, 1)

        box_id = QGroupBox("ID 分配")
        gid = QGridLayout(box_id)
        gid.addWidget(QLabel("自动分配上限"), 0, 0)
        self.edIdTopCfg = QLineEdit()
        self.edIdTopCfg.setPlaceholderText("留空 = 本代×1000+999（34 代 = 34999）")
        self.edIdTopCfg.setFixedWidth(220)
        self.edIdTopCfg.setText(self.settings.value("id_top", "", str))
        self.edIdTopCfg.textChanged.connect(
            lambda s: self.settings.setValue("id_top", s.strip()))
        gid.addWidget(self.edIdTopCfg, 0, 1)
        note_id = QLabel("只影响「留空自动分配」时挑哪个区间的空号；"
                         "一般不用改。填 34999 之类即可。")
        note_id.setStyleSheet("color:#888")
        note_id.setWordWrap(True)
        gid.addWidget(note_id, 1, 0, 1, 2)
        grid.addWidget(box_id, 1, 0)

        box2 = QGroupBox("默认勾选项（读取谱面后）")
        g2 = QHBoxLayout(box2)
        self.ckDefPreview = QCheckBox("试听")
        self.ckDefThumb = QCheckBox("封面")
        self.ckDefBga = QCheckBox("BGA")
        self.ckDefPoor = QCheckBox("坏判层")
        for w, key, dflt in ((self.ckDefPreview, "def_preview", True),
                             (self.ckDefThumb, "def_thumb", True),
                             (self.ckDefBga, "def_bga", True),
                             (self.ckDefPoor, "def_poor", True)):
            w.setChecked(self.settings.value(key, dflt, bool))
            w.stateChanged.connect(lambda _v, k=key, x=w: self.settings.setValue(k, x.isChecked()))
            g2.addWidget(w)
        g2.addStretch(1)
        grid.addWidget(box2, 1, 1)

        box3 = QGroupBox("列表")
        g3 = QGridLayout(box3)
        g3.addWidget(QLabel("读取后默认隐藏 5K"), 0, 0)
        self.ckDefHide5k = QCheckBox()
        self.ckDefHide5k.setChecked(self.settings.value("def_hide5k", True, bool))
        self.ckDefHide5k.stateChanged.connect(
            lambda _v: self.settings.setValue("def_hide5k", self.ckDefHide5k.isChecked()))
        g3.addWidget(self.ckDefHide5k, 0, 1)
        g3.addWidget(QLabel("日志保留行数"), 1, 0)
        self.edLogLines = QLineEdit(str(self.settings.value("log_lines", 5000, int)))
        self.edLogLines.setFixedWidth(120)
        self.edLogLines.editingFinished.connect(self.on_log_lines)
        g3.addWidget(self.edLogLines, 1, 1)
        grid.addWidget(box3, 2, 0)

        box4 = QGroupBox("路径")
        g4 = QVBoxLayout(box4)
        self.lblSetLib = QLabel("")
        self.lblSetLib.setWordWrap(True)
        self.lblSetLib.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g4.addWidget(self.lblSetLib)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("成绩库（beatoraja）"))
        self.edScoreCfg = QLineEdit(self.settings.value("player_root", "", str))
        self.edScoreCfg.setPlaceholderText("可选：用于显示本机成绩")
        self.edScoreCfg.editingFinished.connect(
            lambda: self.apply_score_root(self.edScoreCfg.text(), src="settings"))
        srow.addWidget(self.edScoreCfg, 1)
        sb1 = QPushButton("选择…")
        sb1.clicked.connect(self.pick_player)
        sb2 = QPushButton("不用了")
        sb2.clicked.connect(self.clear_player)
        srow.addWidget(sb1)
        srow.addWidget(sb2)
        g4.addLayout(srow)
        b = QPushButton("打开本机库目录")
        b.clicked.connect(self.open_lib_dir)
        g4.addWidget(b, 0, Qt.AlignLeft)
        grid.addWidget(box4, 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        outer.addStretch(1)

        row = QHBoxLayout()
        rst = QPushButton("恢复默认设置")
        rst.clicked.connect(self.reset_settings)
        row.addWidget(rst)
        row.addStretch(1)
        outer.addLayout(row)
        self.refresh_settings_labels()

    def refresh_settings_labels(self):
        self.lblSetLib.setText(
            "本机库（自制曲记录 + 备份）：\n" + str(services.LIB_ROOT)
            + "\n\n游戏目录请统一选择 …\\contents 目录。")

    def open_lib_dir(self):
        import os
        p = services.LIB_ROOT
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))                                   # noqa: S606

    def on_theme_changed(self):
        mode = self.cmbTheme.currentData()
        self.settings.setValue("theme", mode)
        apply_theme(QApplication.instance(), mode)
        self.repolish()
        self.refresh_slots()

    def repolish(self):
        app = QApplication.instance()
        for w in app.allWidgets():
            w.style().unpolish(w)
            w.style().polish(w)
        app.processEvents()

    def refresh_slots(self):
        for sid, w in self.slotWidgets.items():
            w.set_file(w.file)
        if self.scan:
            self.apply_filter()

    def on_log_lines(self):
        try:
            n = max(500, int(self.edLogLines.text()))
        except ValueError:
            n = 5000
        self.edLogLines.setText(str(n))
        self.settings.setValue("log_lines", n)
        self.txtLog.setMaximumBlockCount(n)
        self.txtCli.setMaximumBlockCount(n)

    def reset_settings(self):
        for k in ("theme", "def_preview", "def_thumb", "def_bga", "def_poor",
                  "def_hide5k", "log_lines"):
            self.settings.remove(k)
        self.cmbTheme.setCurrentIndex(0)
        self.ckDefPreview.setChecked(True)
        self.ckDefThumb.setChecked(True)
        self.ckDefBga.setChecked(True)
        self.ckDefPoor.setChecked(True)
        self.ckDefHide5k.setChecked(True)
        self.edLogLines.setText("5000")
        self.on_log_lines()

    def build_about_tab(self):
        page = QWidget()
        self.tabs.addTab(page, "关于")
        lay = QVBoxLayout(page)
        txt = QTextBrowser()
        txt.setHtml(services.ABOUT_PAGE)
        lay.addWidget(txt)

    # ---------------------------------------------------------------- 拖拽入口
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = pathlib.Path(url.toLocalFile())
            if not p.exists():
                continue
            if p.is_dir():
                if (p / "contents").is_dir() or p.name.lower() == "contents":
                    self.edGame.setText(str(p))
                    self.do_probe()
                else:
                    self.edSource.setText(str(p))
                    self.do_scan()
            break

    # ---------------------------------------------------------------- 源/目标
    def pick_source(self):
        p = QFileDialog.getExistingDirectory(self, "选择 BMS 歌曲文件夹")
        if p:
            self.edSource.setText(p)
            self.do_scan()

    def pick_player(self):
        p = QFileDialog.getExistingDirectory(self, "选择 beatoraja 根目录")
        if p:
            self.apply_score_root(p, src="convert")
            if self.scan:
                self.do_scan()

    def clear_player(self):
        self.apply_score_root("", src="convert")

    def apply_score_root(self, path: str, src: str = ""):
        """成绩库改动的唯一入口：三个页面 + 全局设置一起同步"""
        path = (path or "").strip()
        if hasattr(self, "edPlayer") and self.edPlayer.text() != path:
            self.edPlayer.setText(path)
        if hasattr(self, "edScoreCfg") and self.edScoreCfg.text() != path:
            self.edScoreCfg.setText(path)
        if hasattr(self, "pageBms") and src != "bms" and hasattr(self.pageBms, "set_score_root"):
            self.pageBms.set_score_root(path, emit=False)
        self.settings.setValue("player_root", path)
        services.set_player_root(path or None)
        if hasattr(self, "pageBms") and hasattr(self.pageBms, "model"):
            try:
                ready = services.player() is not None
            except Exception:                                  # noqa: BLE001
                ready = False
            self.pageBms.model.set_score_ready(ready)

    def _score_text(self, path: pathlib.Path) -> str:
        """给 BMS 谱面库的「本机成绩」列用"""
        try:
            pl = services.player()
        except Exception:                                      # noqa: BLE001
            return "—"
        if not pl or not getattr(pl, "scores", None):
            return "—"
        s = pl.lookup(path)
        if not s:
            return "—"
        return f"{s.get('rank', '')} {s.get('score', 0)}"

    def pick_game(self):
        p = QFileDialog.getExistingDirectory(self, "选择游戏根目录（含 contents）")
        if p:
            self.edGame.setText(p)
            self.do_probe()

    def autodetect(self):
        proot = self.settings.value("player_root", "", str)
        if proot and pathlib.Path(proot).is_dir():
            self.edPlayer.setText(proot)
            services.set_player_root(proot)
        try:
            self.pageBms.model.set_score_ready(services.player() is not None)
        except Exception:                                      # noqa: BLE001
            pass
        gd = self.settings.value("game_dir", "", str)
        if not (gd and pathlib.Path(gd).is_dir()):
            gd = services.detect_game_dir()
        if gd:
            self.edGame.setText(gd)
            self.do_probe()
        else:
            self.info("请点「选择」或把游戏文件夹拖进来")

    # ---------------------------------------------------------------- 读取谱面
    def do_scan(self):
        folder = self.edSource.text().strip()
        if not folder:
            return
        self.set_status("正在读取谱面…", "busy")
        self.run_bg(services.scan_folder, self.on_scanned, folder)

    def on_scanned(self, d: dict):
        self.scan = d
        self.rebuild_slots()          # 按当前 SP/DP 模式重置 5 个槽
        charts = d["charts"]
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self._row_of_file = {}
        for i, c in enumerate(charts):
            self.table.insertRow(i)
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                         | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
            chk.setCheckState(Qt.Unchecked)          # 不默认勾选
            self.table.setItem(i, 0, chk)

            name = QTableWidgetItem(c["file"])
            name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                          | Qt.ItemIsDragEnabled)
            self.table.setItem(i, 1, name)
            self._row_of_file[c["file"]] = i

            kind = "5K" if c.get("five_key") else ("DP" if c.get("dp") else "SP")
            self._set(i, 2, kind, kind)
            slot_id = next((v for v in (c.get("auto_slot"), c.get("kind_slot"))
                        if v is not None), None)
            if c.get("dp") and slot_id in KIND_TO_SLOT_DP:
                slot_id = KIND_TO_SLOT_DP[slot_id]      # SP 槽号 → DP 槽号
            tag = SLOT_TAG.get(slot_id, "-")
            if c.get("dp") and tag != "-":
                tag = "D" + tag
            self._set(i, 3, tag, 99 if slot_id is None else slot_id)
            self._set(i, 4, c.get("author_level") or "-", c.get("author_level") or -1)
            self._set(i, 5, c.get("suggest_level") or "-", c.get("suggest_level") or -1)
            self._set(i, 6, c.get("notes") or "-", c.get("notes") or 0)
            secs = c.get("seconds")
            self._set(i, 7, f"{int(secs // 60)}:{int(secs % 60):02d}" if secs else "-",
                      secs or 0)
            self._set(i, 8, f"{c['nps']:.2f}" if c.get("nps") else "-", c.get("nps") or 0)
            self._set(i, 9, f"{c['peak_nps']:.2f}" if c.get("peak_nps") else "-",
                      c.get("peak_nps") or 0)
            sc = c.get("score") or {}
            self._set(i, 10, sc.get("text") or "-", sc.get("rate") or -1)
            if c.get("five_key"):                     # 5K 整行灰底，一眼区分
                from PySide6.QtGui import QBrush
                for col in range(self.table.columnCount()):
                    cell = self.table.item(i, col)
                    if cell:
                        cell.setBackground(QBrush(QColor("#ececec")))
                        cell.setForeground(QBrush(QColor("#8a8a8a")))
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self.invalidate_row_cache()
        self.apply_filter()

        self.edTitle.setText(d.get("title", ""))
        self._title_changed()
        self.edGenre.setText(d.get("genre", ""))
        self.edArtist.setText(d.get("artist", ""))
        self.lblScan.setText(f"{pathlib.Path(d['folder']).name}：{len(charts)} 个谱面"
                             f"（5K {sum(1 for c in charts if c.get('five_key'))} 个）")
        self.frames = []
        self.frames_view = []
        self.frame_idx = -1
        self.refresh_poor_state()
        self.update_frame_buttons()
        self.refill_subtitle()
        self.update_thumb()
        pend = getattr(self, "_pending_queue_item", None)
        if pend and pathlib.Path(str(pend.get("folder", ""))) == pathlib.Path(str(d.get("folder", ""))):
            self._apply_pending_queue_item()
        else:
            self.set_status(f"已读取 {len(charts)} 个谱面，请勾选要导入的", "ok")

    # ---------------------------------------------------------------- SP / DP
    def rebuild_slots(self):
        """槽位现在是固定的 SP/DP 两列，这里只清空已放内容"""
        for sid, w in self.slotWidgets.items():
            w.set_file("")
            self.slots[sid] = ""

    def set_mode(self, mode: str):
        if mode == self.mode:
            self.btnSp.setChecked(mode == "SP")
            self.btnDp.setChecked(mode == "DP")
            return
        self.mode = mode
        self.btnSp.setChecked(mode == "SP")
        self.btnDp.setChecked(mode == "DP")
        bright = "#e6e6e6" if _is_dark() else "#222"
        dim = "#7a7a7a" if _is_dark() else "#999"
        for m, head in self.slotHeaders.items():
            on = (m == mode)
            head.setStyleSheet(
                f"color:{bright if on else dim};font-weight:bold;"
                f"font-size:{'13px' if on else '12px'}")
        self.apply_filter()
        self.refresh_poor_state()
        n = sum(1 for r in range(self.table.rowCount())
                if not self.table.isRowHidden(r))
        self.set_status(f"已切到 {mode}（显示 {n} 个谱面）", "ok")

    def chart_matches_mode(self, c: dict) -> bool:
        std = c.get("standard_sp") if self.mode == "SP" else c.get("standard_dp")
        return bool(std)

    def apply_filter(self):
        """隐藏非标准谱面：SP 只留 7键+1皿，DP 只留 14键+2皿"""
        hide = self.ckHide5k.isChecked()
        charts = {x["file"]: x for x in (self.scan or {}).get("charts", [])}
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 1)
            if not name:
                continue
            c = charts.get(name.text(), {})
            ok = self.chart_matches_mode(c)
            self.table.setRowHidden(row, hide and not ok)

    def invalidate_row_cache(self):
        self._row_cache = None

    def find_row(self, filename: str) -> int:
        """按文件名找当前显示行（排序会改变行号，缓存按键表失效）

        缓存只在"填表/排序"之后重建一次，之后是 O(1) 查字典。
        """
        if not filename:
            return -1
        cache = getattr(self, "_row_cache", None)
        if cache is None:
            cache = {}
            for r in range(self.table.rowCount()):
                it = self.table.item(r, 1)
                if it:
                    cache.setdefault(it.text(), r)
            self._row_cache = cache
        return cache.get(filename, -1)

    def _set(self, row: int, col: int, text, sortval):
        it = NumItem()
        it.setData(Qt.DisplayRole, text)
        it.setData(Qt.UserRole, sortval)
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
        self.table.setItem(row, col, it)

    # ---------------------------------------------------------------- 勾选/槽位
    def on_item_changed(self, item: QTableWidgetItem):
        if item.column() == 0:
            self.refill_subtitle()

    def checked_files(self) -> list[str]:
        out = []
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 0)
            if it and it.checkState() == Qt.Checked:
                name = self.table.item(row, 1)
                if name:
                    out.append(name.text())
        return out

    def on_slot_drop(self, slot_id: int, filename: str):
        if slot_id not in self.slotWidgets:            # 右键菜单的「清空」传 -1
            for sid, w in self.slotWidgets.items():
                if w.file == filename:
                    w.set_file("")
                    self.slots[sid] = ""
            self.refill_subtitle()
            self.refresh_poor_state()
            return

        # 先校验（放在"挪槽"之前，否则被拒绝时原内容已经被清掉了）
        chart = next((x for x in (self.scan or {}).get("charts", [])
                      if x["file"] == filename), {})
        is_dp_slot = slot_id >= 6
        if chart and bool(chart.get("dp")) != is_dp_slot:
            self.set_status(
                f"✗ {filename} 是 {'DP' if chart.get('dp') else 'SP'} 谱面，"
                f"要放到 {'右' if chart.get('dp') else '左'}边那一列", "err")
            return

        # 同一张谱面只能占一个槽：先把它从别的槽里摘掉
        for sid, w in self.slotWidgets.items():
            if w.file == filename and sid != slot_id:
                w.set_file("")
                self.slots[sid] = ""
        self.slots[slot_id] = filename
        self.slotWidgets[slot_id].set_file(filename)
        # 旧代存不了的难度：放进去时就提示（23 有 BEGINNER 但存不进，26 之后才会保存；
        # LEGGENDARIA 从 27 代起才有字段）
        try:
            ver = (self.target or {}).get("version")
            if filename and ver:
                if slot_id == 4 and int(ver) < 27:
                    self.set_status(f"⚠ 本代 v{ver} 没有 LEGGENDARIA，L 谱面会保存不进去", "warn")
                elif slot_id == 3 and int(ver) < 26:
                    self.set_status(f"⚠ 本代 v{ver} 保存不了 BEGINNER，B 谱面会保存不进去", "warn")
        except Exception:                                      # noqa: BLE001
            pass
        if filename:
            row = self.find_row(filename)
            if row >= 0:
                it = self.table.item(row, 0)
                if it:
                    it.setCheckState(Qt.Checked)
            self.set_status(
                f"已把 {filename} 放到 {self.mode} {SLOT_TAG[slot_id]} 槽", "ok")
        else:
            self.set_status("已清空该槽", "ok")
        self.refill_subtitle()
        self.refresh_poor_state()
        self.update_frame_buttons()

    def on_slot_hover(self, slot_id: int, on: bool):
        name = self.slots.get(slot_id, "")
        if not name:
            return
        row = self.find_row(name)
        if row is None:
            return
        self.table.selectRow(row if on else -1)

    def on_table_hover(self, name: str):
        for w in self.slotWidgets.values():
            w.flash(bool(name) and w.file == name)

    def on_row_selected(self):
        rows = {i.row() for i in self.table.selectedItems()}
        for sid, w in self.slotWidgets.items():
            w.flash(bool(w.file) and self.find_row(w.file) in rows)
        if rows:
            self.update_thumb()

        self.refresh_poor_state()

    def collect_picks(self) -> list[dict]:
        """勾选的谱面 + 它们的目标槽位"""
        picks = []
        owner = {v: k for k, v in self.slots.items() if v}
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 0)
            if not (it and it.checkState() == Qt.Checked):
                continue
            name = self.table.item(row, 1).text()
            c = next((x for x in (self.scan or {}).get("charts", [])
                      if x["file"] == name), {})
            slot = next((v for v in (owner.get(name), c.get("auto_slot"),
                                     c.get("kind_slot"), 2) if v is not None), 2)
            picks.append({"file": name, "slot": int(slot)})
        return picks

    # ---------------------------------------------------------------- 副标题/封面
    def refresh_poor_state(self):
        """按当前要导入的谱面决定坏判层开关能不能勾

        BMS 没有 miss 图 → 这首歌做不出坏判层，开关变灰并标注"无坏判层"。
        """
        charts = (self.scan or {}).get("charts") or []
        files = []
        for w in getattr(self, "slotWidgets", {}).values():
            if w.file:
                files.append(w.file)
        if not files:                                  # 还没放槽 → 看勾选的行
            rows = {i.row() for i in self.table.selectedItems()}
            for r in rows:
                it = self.table.item(r, 1)
                if it:
                    files.append(it.text())
        picked = [c for c in charts if c.get("file") in files] if files else []
        if not picked:
            # 还没选谱面：整首都没有 miss 图就提前标灰，省得用户勾了才发现没用
            if charts and not any(c.get("has_poor_layer") for c in charts):
                self.ckPoor.setEnabled(False)
                self.ckPoor.setChecked(False)
                self.ckPoor.setText("无坏判层")
                self.ckPoor.setToolTip("这个 BMS 没有 miss 图，做不出坏判层")
            else:
                self.ckPoor.setEnabled(True)
                self.ckPoor.setText(self._poor_label)
                self.ckPoor.setToolTip("")
            return
        has = [c for c in picked if c.get("has_poor_layer")]
        if has:
            self.ckPoor.setEnabled(True)
            self.ckPoor.setText(self._poor_label)
            missing = [c["file"] for c in picked if not c.get("has_poor_layer")]
            self.ckPoor.setToolTip(
                "这些谱面有 miss 图，可以生成坏判层" if not missing
                else "部分谱面没有 miss 图：\n" + "\n".join(missing[:6]))
        else:
            self.ckPoor.setEnabled(False)
            self.ckPoor.setChecked(False)
            self.ckPoor.setText("无坏判层")
            self.ckPoor.setToolTip("这个 BMS 没有 miss 图，做不出坏判层")

    def _title_changed(self, *_a):
        """曲名改动 → 检查有没有 IIDX 字体缺的字"""
        if not hasattr(self, "lblTitleWarn"):
            return
        from core import romanize
        text = self.edTitle.text().strip()
        if not text:
            self.lblTitleWarn.setVisible(False)
            self.btnRomanize.setVisible(False)
            return
        sub = self.edSubtitle.toPlainText().strip() if hasattr(self, "edSubtitle") else ""
        bad_sub = romanize.unrenderable(sub) if sub else []
        sug, reason = romanize.suggest_title(text)
        if not reason and bad_sub:
            reason = (f"副标题里有 {len(bad_sub)} 个 IIDX 字体没有的字"
                      f"（例：{''.join(bad_sub[:4])}），游戏里会显示成空白")
        if reason:
            self.lblTitleWarn.setText(f"⚠ {reason}" + ("；可点右边转写" if sug else "，建议改成英文"))
            self.lblTitleWarn.setVisible(True)
        else:
            self.lblTitleWarn.setText("")
            self.lblTitleWarn.setVisible(False)
        self.btnRomanize.setVisible(bool(sug))

    def apply_romanized_title(self):
        from core import romanize
        sug, _reason = romanize.suggest_title(self.edTitle.text().strip())
        if sug:
            self.edTitle.setText(sug)
            self.set_status(f"曲名已转写：{sug}", "ok")

    def refill_subtitle(self):
        if not self.scan or not self.ckSubAuto.isChecked():
            return
        files = self.checked_files()
        text = services.default_subtitle(self.scan["charts"], files)
        self.edSubtitle.setPlainText(text)
        n_lines = len(text.splitlines()) if text else 0
        warn = services.subtitle_warning(text) if text else ""
        if warn:
            self.lblSubHint.setStyleSheet("color:#a00")
            self.lblSubHint.setText(warn)
        else:
            self.lblSubHint.setStyleSheet("color:#888")
            self.lblSubHint.setText(f"{n_lines} 行 / {len(text)} 字符（上限 3 行、128 字符）")

    def selected_chart(self) -> dict | None:
        rows = {i.row() for i in self.table.selectedItems()}
        charts = (self.scan or {}).get("charts", [])
        for row in rows:
            it = self.table.item(row, 1)
            if not it:
                continue
            c = next((x for x in charts if x["file"] == it.text()), None)
            if c:
                return c
        return charts[0] if charts else None

    def on_thumb_click(self, clicks: int):
        """单击：没有图就抽；双击/按钮：抽一批新的候选（随机）"""
        if not self.scan:
            return
        if clicks < 2 and self.frames:
            return                                    # 已有候选，单击不重抽
        c = self.selected_chart()
        if c is None:
            return
        self.lblThumbHint.setText("正在抽帧…")
        QApplication.processEvents()
        try:
            batch = services.frame_candidates(self.scan["folder"], c, count=12)
        except Exception as e:                                 # noqa: BLE001
            self.info(f"抽帧失败：{e}")
            batch = []
        if not batch:
            self.lblThumbHint.setText("没有可用视频素材")
            return
        ver = (self.target or {}).get("db_version") or 34
        game = (self.target or {}).get("game_dir", "")
        for raw in batch:                              # 只在内存里留，不写盘
            if len(self.frames) >= self.MAX_FRAMES:
                break
            try:
                preview = services.frame_to_thumb(raw, ver, game)
            except Exception:                          # noqa: BLE001
                preview = None
            pix = QPixmap(preview) if preview else QPixmap()
            if pix.isNull():
                pix.loadFromData(raw, "PNG")
            if not pix.isNull():
                self.frames.append(raw)
                self.frames_view.append(pix)
        self.frame_idx = max(0, len(self.frames_view) - len(batch))
        self.show_frame()
        self.lblThumbHint.setText("‹ › 切换 / 点「抽帧」再抽一批")

    def step_frame(self, delta: int):
        if not self.frames_view:
            return
        self.frame_idx = (self.frame_idx + delta) % len(self.frames_view)
        self.show_frame()

    def show_frame(self):
        if not self.frames_view:
            self.show_thumb(None)
            self.update_frame_buttons()
            return
        self.frame_idx = max(0, min(self.frame_idx, len(self.frames_view) - 1))
        self.set_thumb_pixmap(self.frames_view[self.frame_idx])
        self.thumb.setStyleSheet("background:transparent;border:none")
        self.lblThumbHint.setText(f"第 {self.frame_idx + 1} / {len(self.frames_view)} 张"
                                  f"（本机最多存 {self.MAX_FRAMES} 张）")
        self.update_frame_buttons()

    def update_frame_buttons(self):
        has = bool(self.frames_view)
        self.btnPrev.setEnabled(has)
        self.btnNext.setEnabled(has)

    def chosen_thumb_file(self) -> str | None:
        """把当前选中的那帧写到临时文件，交给转换使用（不留在项目目录）"""
        if not self.frames or self.frame_idx < 0:
            return None
        import tempfile
        f = pathlib.Path(tempfile.gettempdir()) / "bms2iidx_thumb_choice.png"
        f.write_bytes(self.frames[self.frame_idx])     # 原始帧，交给转换流程做封面
        return str(f)

    W_THUMB = 180

    @staticmethod
    def show_help_at(widget, text: str):
        """在控件下方立刻弹出说明（点击「?」用）"""
        from PySide6.QtWidgets import QToolTip
        QToolTip.hideText()                  # 先收掉悬停已经弹出的提示
        pos = widget.mapToGlobal(widget.rect().bottomLeft())
        QToolTip.showText(pos, text, widget, widget.rect(),
                          msecShowTime=6000)

    def set_thumb_pixmap(self, pix: QPixmap):
        """按原图尺寸 1:1 显示（228×130 或 278×186），整框完整不裁切"""
        if pix.isNull():
            return
        self.thumb.setFixedSize(pix.width(), pix.height())
        self.thumb.setPixmap(pix)               # 不缩放：边框一根线都不丢
        self.thumb.setStyleSheet("background:transparent;border:none")

    def show_thumb(self, path: str | None):
        if path and pathlib.Path(path).exists():
            self.set_thumb_pixmap(QPixmap(path))
        else:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("无预览图")
            ver = (self.target or {}).get("db_version") or 34
            size = (278, 186) if ver <= 32 else (228, 130)
            self.thumb.setFixedSize(size[0], size[1])
            self.thumb.setStyleSheet(
                "background:transparent;color:#888;border:1px dashed #bbb;border-radius:4px")

    def update_thumb(self):
        if not self.scan:
            return
        c = None
        rows = {i.row() for i in self.table.selectedItems()}
        for row in rows:
            name = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            c = next((x for x in self.scan["charts"] if x["file"] == name), None)
            if c:
                break
        if not c:
            c = next((x for x in self.scan["charts"] if x.get("stagefile")), None)
        if not c:
            return
        ver = (self.target or {}).get("db_version") or 34
        game = (self.target or {}).get("game_dir", "")
        p = services.thumbnail_preview(self.scan["folder"], c.get("stagefile", ""),
                                       version=ver, game_dir=game)
        self.show_thumb(p if (p and pathlib.Path(p).exists()) else None)

    # ---------------------------------------------------------------- 目标检测
    def update_hook_hint(self, d: dict):
        """原版模式：告诉用户怎么让 spice2x 加载 ifs_hook；能改配置就给按钮"""
        if not hasattr(self, "lblHook"):
            return
        if d.get("deploy_mode") != "vanilla" or d.get("ifs_hook_loaded"):
            self.lblHook.setText("")
            self.btnFixHook.setVisible(False)
            return
        try:
            st = services.spice2x_hook_state(
                services._contents_of(pathlib.Path(d["game_dir"])))
        except Exception as e:                                  # noqa: BLE001
            self.lblHook.setText(f"⚠ 读不到 spice2x 配置：{e}")
            self.btnFixHook.setVisible(False)
            return
        contents0 = services._contents_of(pathlib.Path(d["game_dir"]))
        if not services.hook_installed(contents0):
            self.lblHook.setText(
                "当前是原版模式，且游戏里还没有 ifs_hook.dll —— "
                "没有它我们写的内容游戏读不到。\n"
                "点右边按钮，从本程序内置的 v4.1 复制一份到 contents\\modules。")
            self.btnFixHook.setText("安装 ifs_hook.dll")
            self.btnFixHook.setVisible(True)
            return

        if st.get("has_ifs_hook") and st.get("ifs_hook_first"):
            self.lblHook.setText(
                f"spice2x 配置里已有 ifs_hook.dll（{st.get('path')}），"
                "但这次启动没加载——请重开游戏再看")
            self.btnFixHook.setVisible(False)
            return
        args = (d.get("env") or {}).get("launch_args") or []
        cli_user = any(a.startswith("-k ") for a in args)
        if st.get("exists") and cli_user:
            # 命令行/快捷方式传参的用户：改配置对他无效，只给说明
            self.lblHook.setText(
                "当前是原版模式，你是用命令行/快捷方式带参数启动的。\n"
                "请在启动参数里把 ifs_hook.dll 放到最前，例如：\n"
                "    -k ifs_hook.dll omnifix.dll\n"
                "（启动参数我们已经能从日志读到，改完重开游戏即可）")
            self.btnFixHook.setVisible(False)
        elif st.get("exists"):
            self.lblHook.setText(
                "当前是原版模式：需要让 spice2x 加载 ifs_hook.dll，"
                "否则我们写进 data_mods\bms2iidx 的内容游戏读不到。")
            self.btnFixHook.setText("写入 spice2x 配置")
            self.btnFixHook.setVisible(True)
        else:
            self.lblHook.setText(
                "当前是原版模式，且找不到 spice2x 配置文件。\n"
                "请在启动参数里加： -k ifs_hook.dll （放在最前）\n"
                "例： spice64.exe -k ifs_hook.dll -url http://…")

    def fix_hook(self):
        """把 ifs_hook.dll 写到配置里 IIDX 段的 k 最前面（先备份）"""
        if not self.target:
            return
        contents = services._contents_of(pathlib.Path(self.target["game_dir"]))
        if not services.hook_installed(contents):
            res = services.install_ifshook(contents)
            if not res.get("ok"):
                QMessageBox.warning(self, "装不上", res.get("error", "未知错误"))
                return
            if res.get("unchanged"):
                self.info(f"ifs_hook.dll 已存在：{res['path']}")
            else:
                self.info(f"已安装 ifs_hook.dll v{res.get('version', '4.1')} → {res['path']}")
            self.set_status("已放入 contents\modules\ifs_hook.dll，下一步配加载方式", "ok")
            self.do_probe()
            return
        r = services.spice2x_ensure_hook(contents)
        if not r.get("ok"):
            QMessageBox.warning(self, "没改成", r.get("error", "未知错误"))
            return
        self.info(f"已写入 spice2x 配置：{r['after']}")
        self.info(f"已备份原配置：{r['backup']}")
        self.set_status("已把 ifs_hook.dll 放到启动 hook 第一位，重开游戏生效", "ok")
        self.do_probe()

    def do_probe(self):
        gd = self.edGame.text().strip()
        if not gd:
            return
        self.set_status("正在检测游戏目录…", "busy")
        self.run_bg(services.probe_target, self.on_probed, gd)

    def on_probed(self, d: dict):
        self.target = d
        root = pathlib.Path(d["game_dir"])
        if root.name.lower() == "contents":
            root = root.parent
        self.settings.setValue("game_dir", str(root))

        env = d.get("env") or {}
        ids = d.get("ids") or {}
        size = d.get("db_size") or 0
        hooks = env.get("hooks") or []
        lines = [
            f"omnimix：{'✅ 已安装' if d.get('omnimix_installed') else '❌ 未安装'}",
            f"曲库：{pathlib.Path(d.get('db_path') or '').name}　{size / 1e6:.1f} MB　"
            f"版本 v{d.get('db_version')}　槽表 {d.get('db_slots')}　"
            f"曲目 {d.get('db_songs')}",
            f"部署到：data_mods/{d.get('deploy_mod_dir')}/"
            f"（{'omni' if d.get('deploy_mode') == 'omni' else '原版'} 模式）",
            f"曲库文件：{d.get('music_db_name')}",
            f"曲库位置：{d.get('db_rel') or '—'}",
            f"槽位：info/{d.get('db_slot')}（{'偶数代' if d.get('db_slot') == 0 else '奇数代'}）",
            f"运行环境：补丁 {env.get('omnifix') or '无'} ／ 文件层 "
            f"{env.get('layeredfs') or '无'} ／ 钩子 {', '.join(hooks) if hooks else '无'}",
        ]
        if env.get("mod_folders"):
            lines.append(f"挂载点：{', '.join(env['mod_folders'])}")
        if env.get("game_version"):
            lines.append(f"游戏版本：{env['game_version']}　缩略图目录："
                         f"{env.get('thumbnail_dir') or '—'}")
        if ids:
            lines.append(f"ID 号段：{ids.get('block_lo')}~{ids.get('block_hi')}　"
                         f"下一个可用：{ids.get('next_id')}　槽表 {ids.get('slot_total')}")
        for cand in (d.get("db_candidates") or []):
            lines.append(
                f"另有曲库：{cand.get('rel')}　v{cand.get('version')}　"
                f"{cand.get('avail')} 曲（未使用）")
        if not d.get("ifs_hook_loaded"):
            lines.append("⚠ 没检测到 ifs_hook（layeredfs）：写进去游戏不会读，"
                         "需要 spice2x 勾上 Inject DLL Hooks")
        if d.get("problems"):
            lines.append("⚠ " + "；".join(map(str, d["problems"])))
        self.lblEnv.setText("\n".join(lines))
        self.set_status(f"检测完成：v{d.get('db_version')}　槽 {d.get('db_slots')}　"
                        f"曲目 {d.get('db_songs')}　下一个空号 {ids.get('next_id')}",
                        "ok")
        self.update_hook_hint(d)
        self.fill_id_usage(d["game_dir"])
        self.check_library(d["game_dir"])
        self.refresh_lib()
        self.refresh_queue_lib()          # 队列页右侧「曲库已有」跟着刷新
        self.refresh_backups()
        self.fill_deploy(d)

    def fill_deploy(self, d: dict):
        man = d.get("manifest") or {}
        songs = d.get("deployed") or []
        if not man:
            self.lblDeploy.setText("无（本工具还没往这个目录部署过）")
            return
        txt = [f"记录文件：{man.get('path')}",
               f"共 {man.get('count', len(songs))} 首　更新于 {man.get('updated_at', '—')}"]
        for s in songs[-12:]:
            bad = "" if s.get("ok", True) else f"　⚠ {', '.join(s.get('issues') or [])}"
            txt.append(f"　ID {s.get('id')}　{s.get('title', '')}{bad}")
        self.lblDeploy.setText("\n".join(txt))

    def fill_id_usage(self, game_dir: str):
        try:
            u = services.id_usage(game_dir)
        except Exception as e:                                 # noqa: BLE001
            self.lblUsage.setText(f"读取失败：{e}")
            return
        if not u.get("ok"):
            self.lblUsage.setText(u.get("error", "读取失败"))
            return
        self.lblUsage.setText(
            f"v{u['version']}　槽表 {u['slot_total']}　本代 {u['block_lo']}~{u['block_hi']}："
            f"已用 {u['block_used']} / 空闲 {u['block_free']}　"
            f"下一个空号 {u['next_free_id']}")

    def check_library(self, game_dir: str):
        try:
            st = services.library_status(game_dir)
        except Exception as e:                                 # noqa: BLE001
            self.lblLib.setVisible(True)
            self.lblLib.setText(f"库状态读取失败：{e}")
            return
        n = st.get("count", 0)
        if not n:
            self.lblLib.setVisible(False)
            self.btnReapply.setVisible(False)
            return
        miss = st.get("missing_in_db", [])
        bad = st.get("incompatible", [])
        files = st.get("file_report") or {}
        LABEL = {"sound_dir": "音源目录", "chart": "谱面(.1)", "audio": "键音(.2dx)",
                 "movie": "影片(.mp4)", "thumbnail": "封面"}
        detail = [f"　{LABEL.get(k, k)} 缺 {len(v)}（{', '.join(map(str, v[:5]))}"
                  f"{'…' if len(v) > 5 else ''}）" for k, v in files.items()]
        if miss or bad:
            self.lblLib.setStyleSheet(
                "color:#a00;background:#fff0f0;border:1px solid #e0a0a0;padding:6px")
            lines = []
            if miss:
                lines.append(f"⚠ 本机库 {n} 首，当前曲库缺 {len(miss)}："
                             f"{', '.join(map(str, miss[:8]))}")
            if bad:
                lines.append(f"⚠ {len(bad)} 首属于其它代（不会写进当前曲库）："
                             f"{', '.join(map(str, bad[:6]))}")
            if detail:
                lines.append("实际核对到的缺失文件：")
                lines += detail
            self.lblLib.setText("\n".join(lines))
            self.btnReapply.setText(f"把这 {len(miss)} 首重新写回曲库")
            self.btnReapply.setVisible(bool(miss))
            self.lblLib.setVisible(True)
            if miss and not self._warned_lib:
                self._warned_lib = True
                QMessageBox.warning(
                    self, "自制曲的曲库记录不见了",
                    f"本机库存着 {n} 首，当前曲库缺 {len(miss)} 首。\n\n"
                    f"缺的 ID：{', '.join(map(str, miss[:8]))}\n\n"
                    + ("文件核对：\n" + "\n".join(detail) if detail else "文件都还在。")
                    + "\n\n点「重新写回曲库」可以只补记录，不动包里的其它歌。")
        else:
            self.lblLib.setStyleSheet(
                "color:#2a6;background:#f2fff5;border:1px solid #a0e0b0;padding:4px")
            extra = ("；文件有缺：" + "、".join(f"{LABEL.get(k, k)} {len(v)}"
                                            for k, v in files.items())) if files else ""
            self.lblLib.setText(f"✅ 本机库 {n} 首都在当前曲库里{extra}")
            self.btnReapply.setVisible(False)
            self.lblLib.setVisible(True)

    def do_reapply(self):
        if not self.target:
            return
        self.set_status("正在把记录写回曲库…", "busy")
        self.run_bg(services.reapply_songs, self.on_reapplied, self.target["game_dir"])

    def on_reapplied(self, r: dict):
        if r.get("note"):
            self.info(r["note"])
        else:
            self.info(f"重应用完成：写回 {r.get('count')} 首，"
                      f"槽位 {r.get('restored', {}).get('used')}")
        if r.get("skipped_version"):
            self.info(f"跳过跨代记录：{r['skipped_version']}")
        self.set_status("曲库记录已补回", "ok")
        if self.target:
            self.do_probe()

    # ---------------------------------------------------------------- 队列
    def _queue_path(self) -> pathlib.Path:
        base = services.LIB_ROOT / "queue.json"
        return base

    def _queue_save(self):
        try:
            self._queue_path().parent.mkdir(parents=True, exist_ok=True)
            data = [{k: v for k, v in it.items() if k != "report"} for it in self.queue]
            self._queue_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.set_status(f"队列保存失败：{e}", "warn")

    def _queue_load(self):
        try:
            raw = json.loads(self._queue_path().read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.queue = raw
        except (OSError, ValueError):
            self.queue = []

    def _persist_thumb(self, src: str | None) -> str | None:
        """把选中的缩略图复制到 library/thumbs/，队列里存稳定路径"""
        if not src:
            return None
        p = pathlib.Path(src)
        if not p.exists():
            return None
        try:
            import hashlib
            dst_dir = services.LIB_ROOT / "thumbs"
            dst_dir.mkdir(parents=True, exist_ok=True)
            data = p.read_bytes()
            name = hashlib.sha256(data).hexdigest()[:16] + ".png"
            dst = dst_dir / name
            if not dst.exists():
                dst.write_bytes(data)
            return str(dst)
        except OSError:
            return src          # 失败就用原路径，别丢功能

    def font_index_value(self):
        """设置页里选的字体号；auto → None（由转换器按标题自动挑）"""
        try:
            v = str(self.settings.value("font_idx", "auto", str))
        except Exception:                                      # noqa: BLE001
            return None
        return int(v) if v.isdigit() else None

    def id_top_value(self):
        """设置页里配置的 ID 上限（留空 = None，由转换器按本代算）"""
        try:
            s = (self.settings.value("id_top", "", str) or "").strip()
        except Exception:                                      # noqa: BLE001
            return None
        return int(s) if s.isdigit() else None

    def add_to_queue(self):
        """把当前配置（歌曲文件夹 + 谱面→槽 + 曲目信息 + 选项）加进队列"""
        if not self.scan or not self.target:
            QMessageBox.information(self, "先检测目标目录", "请先选好游戏目录并点「检测」。")
            return
        picks = self.collect_picks()
        if not picks:
            QMessageBox.information(self, "还没勾选谱面", "请在左边表格里勾选要导入的谱面。")
            return
        folder = self.scan["folder"]
        edit_idx = getattr(self, "_editing_queue_index", None)
        if edit_idx is not None and 0 <= edit_idx < len(self.queue):
            old = self.queue[edit_idx]
            old.update({
                "folder": folder,
                "title": self.edTitle.text().strip() or self.scan.get("name", ""),
                "artist": self.edArtist.text().strip(),
                "genre": self.edGenre.text().strip(),
                "song_id": (int(self.edSongId.text()) if self.edSongId.text().strip().isdigit()
                            else None),
                "subtitle": self.edSubtitle.toPlainText().strip(),
                "origin": self.cbOrigin.currentText(),
                "picks": picks,
                "with_preview": self.ckPreview.isChecked(),
                "with_bga": self.ckBga.isChecked(),
                "bake_poor": self.ckPoor.isEnabled() and self.ckPoor.isChecked(),
                "id_top": self.id_top_value(),
                "status": "待转换",
            })
            self._editing_queue_index = None
            self._queue_save()
            self.refresh_queue_table()
            self.set_status(f"已更新队列里的「{old['title']}」", "ok")
            self.tabs.setCurrentWidget(self.pageQueue)
            return
        for it in self.queue:
            if it.get("folder") == folder:
                QMessageBox.information(self, "已在队列里", "这首歌的文件夹已经在队列里了。")
                return
        item = {
            "key": f"q{len(self.queue) + 1}_{pathlib.Path(folder).name[:20]}",
            "folder": folder,
            "title": self.edTitle.text().strip() or self.scan.get("name", ""),
            "artist": self.edArtist.text().strip(),
            "genre": self.edGenre.text().strip(),
            "song_id": (int(self.edSongId.text()) if self.edSongId.text().strip().isdigit()
                        else None),
            "subtitle": self.edSubtitle.toPlainText().strip(),
            "origin": self.cbOrigin.currentText(),
            "picks": picks,
            "with_preview": self.ckPreview.isChecked(),
            "with_bga": self.ckBga.isChecked(),
            "bake_poor": self.ckPoor.isEnabled() and self.ckPoor.isChecked(),
            "thumb": self._persist_thumb(self.chosen_thumb_file()),
            "id_top": self.id_top_value(),
            "status": "待转换",
        }
        self.queue.append(item)
        self._queue_save()
        self.refresh_queue_table()
        self.set_status(f"已加入队列：{item['title']}（{len(picks)} 个谱面，"
                     f"放入 {item['origin']}）", "ok")
        self.tabs.setCurrentWidget(self.pageQueue)      # 自动切到队列页（按控件找，不按序号）

    def on_queuelib_menu(self, pos):
        """队列页右侧「曲库已有」的右键菜单"""
        row = self.tblQueueLib.rowAt(pos.y())
        if row < 0:
            return
        idcell = self.tblQueueLib.item(row, 0)
        if not idcell:
            return
        sid = int(idcell.text())
        menu = QMenu(self)
        a1 = menu.addAction("在「IIDX 曲库与资源」里定位")
        a2 = menu.addAction("复制歌曲 ID")
        menu.addSeparator()
        a3 = menu.addAction("打开它的文件目录")
        chosen = menu.exec(self.tblQueueLib.viewport().mapToGlobal(pos))
        if chosen is a1:
            self.select_song_in_lib(sid)
        elif chosen is a2:
            QApplication.clipboard().setText(str(sid))
            self.set_status(f"已复制 ID {sid}", "ok")
        elif chosen is a3:
            try:
                items = services.library_overview(self.target["game_dir"]) if self.target else {}
                for g in items.get("groups", []):
                    for it in g.get("items", []):
                        if int(it.get("id", -1)) == sid:
                            p = (it.get("files") or {}).get("sound")
                            if p:
                                import os
                                os.startfile(str(p))               # noqa: S606
                                return
                self.set_status("这首歌没有音源目录", "warn")
            except Exception as e:                             # noqa: BLE001
                self.set_status(f"打开失败：{e}", "err")

    def select_song_in_lib(self, sid: int):
        """在「IIDX 曲库与资源」页选中指定 ID"""
        if not hasattr(self, "pageLib"):
            return
        self.tabs.setCurrentWidget(self.pageLib)
        self.refresh_lib()

        def walk(node):
            for i in range(node.childCount()):
                ch = node.child(i)
                data = ch.data(0, Qt.UserRole)
                if isinstance(data, tuple) and data[0] != "group" \
                        and int(data[1].get("id", -1)) == sid:
                    self.treeLib.setCurrentItem(ch)
                    self.treeLib.scrollToItem(ch)
                    return True
                if walk(ch):
                    return True
            return False

        for i in range(self.treeLib.topLevelItemCount()):
            top = self.treeLib.topLevelItem(i)
            top.setExpanded(True)
            if walk(top):
                return
        self.set_status(f"曲库里没找到 {sid}（可能已删）", "warn")

    def on_queue_double_click(self, index):
        """双击队列条目 → 回到转换页，把这首歌的设置全部回填，便于调整后重存"""
        if not index.isValid():
            return
        row = index.row()
        if not (0 <= row < len(self.queue)):
            return
        it = self.queue[row]
        self._editing_queue_index = row
        self.edSource.setText(it.get("folder", ""))
        self.tabs.setCurrentWidget(self.pageConvert)
        self.set_status(f"正在载入队列条目：{it.get('title')}…", "busy")
        self._pending_queue_item = it
        self.do_scan()

    def _apply_pending_queue_item(self):
        """扫描完成后，把队列条目里的设置灌回界面"""
        it = getattr(self, "_pending_queue_item", None)
        if not it:
            return
        if (self.scan or {}).get("folder") and pathlib.Path(str(it.get("folder", ""))) != pathlib.Path(str(self.scan.get("folder", ""))):
            return                      # 这次扫描不是它，等下一次
        self._pending_queue_item = None
        self.edTitle.setText(it.get("title", "") or "")
        self.edArtist.setText(it.get("artist", "") or "")
        self.edGenre.setText(it.get("genre", "") or "")
        self.edSongId.setText(str(it["song_id"]) if it.get("song_id") else "")
        self.ckSubAuto.setChecked(False)
        self.edSubtitle.setPlainText(it.get("subtitle", "") or "")
        i = self.cbOrigin.findText(it.get("origin", "") or "")
        if i >= 0:
            self.cbOrigin.setCurrentIndex(i)
        self.ckPreview.setChecked(bool(it.get("with_preview", True)))
        self.ckThumb.setChecked(bool(it.get("thumb")))
        self.ckBga.setChecked(bool(it.get("with_bga", True)))
        self.ckPoor.setChecked(bool(it.get("bake_poor", False)))
        n = 0
        for pick in it.get("picks") or []:
            fname = pick.get("file", "")
            slot = int(pick.get("slot", 0))
            row = self.find_row(fname)
            if row < 0:
                continue
            cell = self.table.item(row, 0)
            if cell:
                cell.setCheckState(Qt.Checked)
            self.on_slot_drop(slot, fname)
            n += 1
        self._title_changed()
        self.set_status(f"已载入队列条目：{it.get('title')}（{n} 个谱面）；改完点「添加到队列」会更新这一条", "ok")

    def refresh_queue_table(self):
        if not hasattr(self, "tblQueue"):
            return
        self.tblQueue.setRowCount(len(self.queue))
        for r, it in enumerate(self.queue):
            slots = "·".join(SLOT_TAG.get(int(p["slot"]), str(p["slot"]))
                             for p in it.get("picks") or [])
            vals = [it.get("status", "待转换"), it.get("title", ""),
                    str(it.get("song_id") or it.get("report", {}).get("song_id") or "自动"),
                    it.get("origin", ""), slots or "—"]
            for c, v in enumerate(vals):
                self.tblQueue.setItem(r, c, QTableWidgetItem(str(v)))
        if hasattr(self, "lblQueueEmpty"):
            self.lblQueueEmpty.setVisible(not self.queue)
        ready = sum(1 for it in self.queue if it.get("status") == "已转换")
        self.lblQState.setText(f"共 {len(self.queue)} 首，已转换 {ready} 首"
                               if self.queue else "队列为空")

    def refresh_queue_lib(self):
        if not hasattr(self, "tblQueueLib") or not self.target:
            return
        try:
            ov = services.library_overview(self.target["game_dir"])
        except Exception as e:                                  # noqa: BLE001
            self.set_status(f"读取曲库失败：{e}", "warn")
            return
        show_official = getattr(self, "ckShowOfficial", None) and self.ckShowOfficial.isChecked()
        show_omnimix = getattr(self, "ckShowOmnimix", None) and self.ckShowOmnimix.isChecked()
        show_leftover = getattr(self, "ckShowLeftover", None) and self.ckShowLeftover.isChecked()
        rows = []
        for g in ov.get("groups") or []:
            key = g.get("key")
            if key == "official" and not show_official:
                continue
            if key == "omnimix" and not show_omnimix:
                continue
            if key in ("others", "recoverable") and not show_leftover:
                continue
            for it in g.get("items") or []:
                rows.append((it.get("id"), it.get("title", ""), g.get("label", "")))
        rows.sort(key=lambda r: (r[0] or 0))
        self.tblQueueLib.setRowCount(len(rows))
        for r, (sid, title, src) in enumerate(rows):
            for c, v in enumerate((str(sid), title, src)):
                self.tblQueueLib.setItem(r, c, QTableWidgetItem(str(v)))

    def _on_queue_lib_filter(self, *_a):
        self.settings.setValue("show_official", self.ckShowOfficial.isChecked())
        self.settings.setValue("show_omnimix", self.ckShowOmnimix.isChecked())
        self.settings.setValue("show_leftover", self.ckShowLeftover.isChecked())
        self.refresh_queue_lib()

    def queue_remove(self):
        rows = sorted({i.row() for i in self.tblQueue.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.queue):
                self.queue.pop(r)
        self._queue_save()
        self.refresh_queue_table()

    def queue_clear(self):
        if self.queue and QMessageBox.question(
                self, "清空队列", f"确定清空 {len(self.queue)} 首？") != QMessageBox.Yes:
            return
        self.queue.clear()
        self._queue_save()
        self.refresh_queue_table()

    def queue_start(self):
        if self._queue_running:
            return
        pending = [it for it in self.queue if it.get("status") != "已转换"]
        if not pending:
            QMessageBox.information(self, "没有待转换的", "队列里的歌都转换过了。")
            return
        if not self.target:
            QMessageBox.information(self, "先检测目标目录", "请先选好游戏目录并点「检测」。")
            return
        self.staging = ROOT / "out" / "staging"
        self._queue_db = pathlib.Path(self.target["db_path"])
        self._queue_running = True
        self.btnQStart.setEnabled(False)
        self._queue_pending = pending
        self._queue_index = 0
        self._queue_cancel = False
        self.btnQCancel.setEnabled(True)
        self.pbar.setRange(0, max(1, len(pending)) * 100)
        self.pbar.setFormat("%p%")
        self.pbar.setValue(0)
        self.set_status(f"开始转换队列：{len(pending)} 首", "busy")
        self._queue_next()

    def _queue_next(self):
        if self._queue_cancel or self._queue_index >= len(self._queue_pending):
            if self._queue_cancel:
                left = [it for it in self._queue_pending[self._queue_index:]]
                for it in left:
                    it["status"] = "待转换"
                self.set_status(f"已取消：还剩 {len(left)} 首没转", "warn")
            self._queue_running = False
            self.btnQStart.setEnabled(True)
            self.btnQCancel.setEnabled(False)
            self._queue_save()
            self.refresh_queue_table()
            self.refresh_queue_lib()
            self.set_status("队列转换结束", "ok")
            return
        it = self._queue_pending[self._queue_index]
        it["status"] = "转换中"
        self.pbar.setValue(self._queue_index)
        self.refresh_queue_table()
        self.set_status(f"转换中（{self._queue_index + 1}/{len(self._queue_pending)}）："
                     f"{it.get('title')}", "busy")
        picks = [{"file": p["file"], "slot": p["slot"]} for p in it.get("picks") or []]
        self.run_bg(services.convert_song, self._queue_item_done,
                    it["folder"], str(self._queue_db), str(self.staging), picks,
                    it.get("origin", "SUBSTREAM"),
                    self.target.get("deploy_mod_dir", "omnimix"),
                    self.target.get("music_db_name", "music_omni.bin"),
                    self.font_index_value(),
                    title=it.get("title", ""), artist=it.get("artist", ""),
                    genre=it.get("genre", ""), song_id=it.get("song_id"),
                    with_preview=it.get("with_preview", True),
                    with_bga=it.get("with_bga", True),
                    id_top=it.get("id_top"),
                    subtitle=it.get("subtitle", ""),
                    bake_poor=it.get("bake_poor", False),
                    on_error=self._queue_item_failed,
                    thumb_override=it.get("thumb"))

    def _queue_item_failed(self, msg):
        """队列里某一首抛异常 → 记失败并继续下一首（不能把队列卡死）"""
        if not getattr(self, "_queue_pending", None):
            return
        it = self._queue_pending[min(self._queue_index, len(self._queue_pending) - 1)]
        it["status"] = "失败"
        it["error"] = str(msg)[:400]
        self.info(f"✗ {it.get('title')} 转换失败：{msg}")
        self.set_status(f"✗ {it.get('title')} 转换失败（继续下一首）", "err")
        self._queue_index += 1
        self.pbar.setValue(self._queue_index)
        self.refresh_queue_table()
        self._queue_next()

    def _drop_thumb_file(self, item):
        """转换完成后清掉临时抽帧文件（已经烘进暂存区了）"""
        try:
            thumb = (item or {}).get("thumb")
            if thumb:
                p = pathlib.Path(str(thumb))
                if p.exists() and p.name.startswith("bms2iidx_thumb"):
                    p.unlink()
        except Exception:                                      # noqa: BLE001
            pass

    def _queue_item_done(self, r: dict):
        it = self._queue_pending[self._queue_index]
        rep = (r or {}).get("report") or {}
        it["status"] = "已转换" if rep.get("ok") else "失败"
        it["report"] = rep
        sid = rep.get("song_id")
        for w in rep.get("warnings", []):
            self.set_status("· " + w, "warn")
        if rep.get("ok"):
            self.set_status(f"✅ {it.get('title')} → ID {sid}", "ok")
            staged = self.staging / "data_mods/omnimix/info/0/music_omni.bin"
            if staged.exists():
                self._queue_db = staged          # 下一首在它上面继续加
            self._queue_last_report = rep
            self._drop_thumb_file(it)
        else:
            self.set_status(f"✗ {it.get('title')} 转换失败", "err")
        self._queue_index += 1
        self.pbar.setValue(self._queue_index)
        self.refresh_queue_table()
        self._queue_next()

    def queue_deploy(self):
        if self._queue_running:
            return
        done = [it for it in self.queue if it.get("status") == "已转换"]
        if not done:
            QMessageBox.information(self, "还没有转换好的", "先点「开始转换」。")
            return
        if not self.staging or not self.target:
            return
        self._queue_running = True
        self.btnQDeploy.setEnabled(False)
        self.btnQCancel.setEnabled(True)
        self.pbar.setRange(0, 0)          # 忙碌态
        self.set_status(f"部署 {len(done)} 首到游戏…（会先自动备份）", "busy")
        self.run_bg(services.deploy, self._queue_deploy_done,
                    str(self.staging), self.target["game_dir"],
                    report=self._queue_last_report)

    def _queue_deploy_done(self, r: dict):
        self._queue_running = False
        self.btnQDeploy.setEnabled(True)
        self.btnQCancel.setEnabled(False)
        self.pbar.setRange(0, 1)
        self.pbar.setValue(1)
        if r.get("ok"):
            for it in self.queue:
                if it.get("status") == "已转换":
                    it["status"] = "已部署"
            self._queue_save()
            self.refresh_queue_table()
            self.refresh_queue_lib()
            self.set_status(f"✅ 部署完成：{len(r.get('copied') or [])} 个文件", "ok")
            self.do_probe()
        else:
            self.set_status("✗ 部署失败，详见日志", "err")

    def _on_queue_rows_moved(self, *_a):
        """拖动排序后，把表格里的顺序同步回 self.queue"""
        order = []
        for r in range(self.tblQueue.rowCount()):
            it0 = self.tblQueue.item(r, 1)
            if it0 is None:
                continue
            title = it0.text()
            for idx, q in enumerate(self.queue):
                if q.get("title") == title and idx not in [o[0] for o in order]:
                    order.append((idx, q))
                    break
        if len(order) == len(self.queue):
            self.queue = [q for _i, q in order]
            self._queue_save()
            self.refresh_queue_table()

    def queue_cancel(self):
        """取消队列：当前这首会转完（不能安全中止），剩下的留在队列里"""
        if not self._queue_running:
            return
        self._queue_cancel = True
        self.set_status("已请求取消：当前这首转完后停止", "warn")
        self.btnQCancel.setEnabled(False)

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.tblQueue.selectedIndexes()})

    def queue_deploy_selected(self):
        """只部署左栏选中的那几首（不动整库，只并入这几条记录）"""
        if self._queue_running:
            return
        rows = self._selected_rows()
        items = [self.queue[r] for r in rows if 0 <= r < len(self.queue)]
        items = [it for it in items if it.get("report", {}).get("song_id")]
        if not items:
            QMessageBox.information(self, "先选要部署的", "在左栏选中已转换的歌（要看得到 ID）。")
            return
        if not self.staging or not self.target:
            return
        if QMessageBox.question(
                self, "部署选中",
                "只把选中的 %d 首并进游戏（会先自动备份）。继续？" % len(items)
        ) != QMessageBox.Yes:
            return
        self._queue_running = True
        self.btnQDeployOne.setEnabled(False)
        self.pbar.setRange(0, 0)
        self.set_status(f"部署选中 {len(items)} 首…", "busy")
        self._deploy_one_queue = list(items)
        self._deploy_one_i = 0
        self._deploy_one_ok = 0
        self._deploy_one_next()

    def _deploy_one_next(self):
        if self._deploy_one_i >= len(self._deploy_one_queue):
            sid_list = ", ".join(str(it["report"]["song_id"]) for it in self._deploy_one_queue)
            self._queue_running = False
            self.btnQDeployOne.setEnabled(True)
            self.pbar.setRange(0, 1)
            self.pbar.setValue(1)
            for it in self._deploy_one_queue:
                if it.get("status") == "已转换":
                    it["status"] = "已部署"
            self._queue_save()
            self.refresh_queue_table()
            self.refresh_queue_lib()
            self.set_status(f"✅ 已部署：{sid_list}", "ok")
            self.do_probe()
            return
        it = self._deploy_one_queue[self._deploy_one_i]
        sid = it["report"]["song_id"]
        self.set_status(f"部署 {it.get('title')}（ID {sid}）…", "busy")
        self.run_bg(services.deploy_one, self._deploy_one_done,
                    str(self.staging), self.target["game_dir"], sid)

    def _deploy_one_done(self, r: dict):
        if r.get("ok"):
            self._deploy_one_ok += 1
            self.set_status(f"✅ ID {r.get('song_id')}：{len(r.get('copied') or [])} 个文件", "ok")
        else:
            self.set_status(f"✗ 部署失败：{r.get('error')}", "err")
        self._deploy_one_i += 1
        self._deploy_one_next()

    # ---------------------------------------------------------------- 转换/部署
    def do_convert(self):
        if not self.scan or not self.target:
            QMessageBox.information(self, "先检测目标目录", "请先选好游戏目录并点「检测」。")
            return
        picks = self.collect_picks()
        if not picks:
            QMessageBox.information(self, "还没勾选谱面", "请在左边表格里勾选要导入的谱面。")
            return
        staging = ROOT / "out" / "staging"
        self.staging = staging
        self._queue_running = True
        self.set_status(f"正在转换 {len(picks)} 个谱面…（BGA 可能要好几十秒）", "busy")
        self.run_bg(services.convert_song, self.on_converted,
                    self.scan["folder"], self.target["db_path"], str(staging), picks,
                    self.cbOrigin.currentText(),
                    self.target.get("deploy_mod_dir", "omnimix"),
                    self.target.get("music_db_name", "music_omni.bin"),
                    self.font_index_value(),
                    title=self.edTitle.text().strip(),
                    artist=self.edArtist.text().strip(),
                    genre=self.edGenre.text().strip(),
                    song_id=(int(self.edSongId.text()) if self.edSongId.text().strip().isdigit()
                             else None),
                    with_preview=self.ckPreview.isChecked(),
                    with_bga=self.ckBga.isChecked(),
                    id_top=self.id_top_value(),
                    subtitle=self.edSubtitle.toPlainText().strip(),
                    bake_poor=self.ckPoor.isEnabled() and self.ckPoor.isChecked(),
                    thumb_override=self.chosen_thumb_file())

    def on_converted(self, r: dict):
        rep = r.get("report") or {}
        self.last_report = rep
        for w in rep.get("warnings", []):
            self.info("· " + w)
        sid = rep.get("song_id")
        ok = rep.get("ok")
        if ok:
            self._drop_thumb_file({"thumb": self.chosen_thumb_file()})
            files = services.stage_preview(str(self.staging))
            self.set_status(f"✅ 转换完成：{self.edTitle.text()} → ID {sid}"
                            f"（{len(files)} 个文件在暂存区，可部署）", "ok")
            QMessageBox.information(
                self, "转换完成",
                f"✅ {self.edTitle.text()}\n\n歌曲 ID：{sid}\n"
                f"暂存文件：{len(files)} 个\n\n下一步点「增量部署到游戏」。")
        else:
            self.set_status("✗ 转换失败，详见日志", "err")

    def do_deploy(self):
        if not self.staging or not self.target:
            return
        if hasattr(self, "btnDeploy"):
            self.btnDeploy.setEnabled(False)
        self.set_status("正在部署…（会先自动备份）", "busy")
        self.run_bg(services.deploy, self.on_deploy_result,
                    str(self.staging), self.target["game_dir"],
                    report=self.last_report)

    def on_deploy_result(self, r: dict):
        if r.get("ok"):
            self.on_deployed(r)
            return
        c = r.get("conflict") or {}
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("曲库在转换后被改过")
        box.setText("目标曲库的哈希和转换时不一致（多半是 omni 包更新了）。\n\n"
                    "直接部署会用我们转换时那份旧曲库覆盖新包，包里新增的歌会消失。\n"
                    "建议取消后重新转换一次。")
        box.setInformativeText(f"当前曲库：{c.get('db', '')}")
        b_cancel = box.addButton("取消（推荐）", QMessageBox.RejectRole)
        b_force = box.addButton("仍然部署", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is b_force:
            self.run_bg(services.deploy, self.on_deployed,
                        str(self.staging), self.target["game_dir"],
                        report=self.last_report, force=True)

    def on_deployed(self, r: dict):
        self.info(f"部署完成：复制 {len(r['copied'])} 个文件")
        self._warned_lib = False
        self.set_status(f"✅ 部署完成：{len(r['copied'])} 个文件已写入游戏目录", "ok")
        if hasattr(self, "btnDeploy"):
            self.btnDeploy.setEnabled(False)
        self.do_probe()

    # ---------------------------------------------------------------- 备份
    def refresh_backups(self):
        if not self.target:
            return
        try:
            items = services.backup_list(self.target["game_dir"])
        except Exception as e:                                 # noqa: BLE001
            self.lblBackup.setText(f"读取备份失败：{e}")
            return
        self._backup_items = {b["id"]: b for b in items}
        self.tblBackup.blockSignals(True)      # 填表时别触发"备注已改"
        self.tblBackup.setRowCount(len(items))
        for r, b in enumerate(items):
            files = b.get("files") or []
            size = sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))
            vals = [str(b.get("created_at", "")).replace("T", " "),
                    b.get("label", ""), str(len(files)), f"{size / 1e6:.1f} MB", b["id"],
                    b.get("note", "")]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                if c == 4:
                    cell.setData(Qt.UserRole, b["id"])
                if c != 5:                      # 只有备注可编辑
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.tblBackup.setItem(r, c, cell)
        self.tblBackup.blockSignals(False)
        self.lblBackup.setText(f"共 {len(items)} 个备份" if items
                               else "还没有备份：点「立即备份」给当前曲库/XML 拍一份快照")
        if items:
            self.tblBackup.selectRow(0)
        else:
            self._update_backup_detail()

    def _update_backup_detail(self):
        if not hasattr(self, "listBackupFiles"):
            return
        row = self.tblBackup.currentRow() if hasattr(self, "tblBackup") else -1
        items = getattr(self, "_backup_items", {})
        bid = None
        if row >= 0:
            cell = self.tblBackup.item(row, 4)
            bid = cell.data(Qt.UserRole) if cell else None
        self.listBackupFiles.clear()
        b = items.get(bid) if bid else None
        if not b:
            self.lblBackupDetail.setText("（在左边选一个备份，这里会列出它包含的文件）")
            return
        files = b.get("files") or []
        self.lblBackupDetail.setText(
            f"{b['id']}　{b.get('label', '')}\n"
            f"{len(files)} 个文件，合计 "
            f"{sum(int(f.get('size') or 0) for f in files) / 1e6:.1f} MB")
        for f in files:
            self.listBackupFiles.addItem(str(f.get("rel") or f.get("path") or f))

    def _on_backup_note_edited(self, item):
        if item is None or item.column() != 5 or not self.target:
            return
        idcell = self.tblBackup.item(item.row(), 4)
        bid = idcell.data(Qt.UserRole) if idcell else None
        if not bid:
            return
        r = services.backup_set_note(self.target["game_dir"], bid, item.text())
        if r.get("ok"):
            self.set_status(f"已保存备注：{bid}", "ok")
        else:
            self.set_status(f"备注保存失败：{r.get('error', '')}", "err")

    def do_backup(self):
        if not self.target:
            self.lblBackup.setText("先选好游戏目录并检测")
            return
        self.run_bg(services.backup_create, self.on_backuped,
                    self.target["game_dir"], "手动备份")

    def on_backuped(self, m: dict):
        self.set_status(f"✅ 已备份 {len(m['files'])} 个文件 → {m['id']}", "ok")
        self.refresh_backups()

    def do_restore(self):
        row = self.tblBackup.currentRow()
        if row < 0 or not self.target:
            self.set_status("先在表格里选一个备份", "warn")
            return
        cell = self.tblBackup.item(row, 4)
        bid = cell.data(Qt.UserRole) if cell else None
        if not bid:
            return
        if QMessageBox.question(
                self, "确认恢复",
                f"把游戏文件恢复成备份 {bid}？\n\n"
                "· 曲库 / XML / 部署记录会被覆盖回备份时\n"
                "· 备份之后新增的歌曲资源会被删除\n"
                "· 恢复前会自动再拍一份快照") != QMessageBox.Yes:
            return
        self.run_bg(services.backup_restore, self.on_restored,
                    self.target["game_dir"], bid)

    def on_restored(self, r: dict):
        self.set_status(f"✅ 已恢复 {len(r['restored'])} 个文件"
                        f"（删除 {len(r['removed'])} 项资源）", "ok")
        self.refresh_backups()
        self.do_probe()

    def do_delete_backup(self):
        row = self.tblBackup.currentRow()
        if row < 0 or not self.target:
            self.set_status("先在表格里选一个备份", "warn")
            return
        cell = self.tblBackup.item(row, 4)
        bid = cell.data(Qt.UserRole) if cell else None
        if not bid:
            return
        if QMessageBox.question(self, "删除备份", f"删除备份 {bid}？") == QMessageBox.Yes:
            services.backup_delete(self.target["game_dir"], bid)
            self.refresh_backups()


class FastTipStyle(QProxyStyle):
    """把 Qt 默认的 tooltip 唤醒延迟（700ms）压到 250ms

    默认值正好落在"用户怀疑没反应、于是去点一下"的区间，
    缩短后悬停立刻就能看到说明。
    """

    WAKE_MS = 50
    ASLEEP_MS = 50

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        from PySide6.QtWidgets import QStyle
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return self.WAKE_MS
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:
            return self.ASLEEP_MS
        return super().styleHint(hint, option, widget, returnData)


def _is_dark() -> bool:
    """当前是不是深色（看应用调色板的窗口色亮度）"""
    from PySide6.QtGui import QPalette
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.Window).lightness() < 128


def dark_palette():
    """一套干净的深色调色板（Fusion 下效果好）"""
    from PySide6.QtGui import QColor, QPalette
    p = QPalette()
    bg = QColor(45, 45, 45)
    base = QColor(30, 30, 30)
    text = QColor(228, 228, 228)
    p.setColor(QPalette.Window, bg)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, QColor(50, 50, 50))
    p.setColor(QPalette.ToolTipBase, QColor(60, 60, 60))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor(53, 53, 53))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(255, 90, 90))
    p.setColor(QPalette.Link, QColor(90, 160, 240))
    p.setColor(QPalette.Highlight, QColor(42, 122, 226))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(130, 130, 130))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(130, 130, 130))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(130, 130, 130))
    return p


def light_palette():
    from PySide6.QtGui import QColor, QPalette
    p = QPalette()
    p.setColor(QPalette.Window, QColor(240, 240, 240))
    p.setColor(QPalette.WindowText, QColor(20, 20, 20))
    p.setColor(QPalette.Base, QColor(255, 255, 255))
    p.setColor(QPalette.AlternateBase, QColor(245, 245, 245))
    p.setColor(QPalette.Text, QColor(20, 20, 20))
    p.setColor(QPalette.Button, QColor(240, 240, 240))
    p.setColor(QPalette.ButtonText, QColor(20, 20, 20))
    p.setColor(QPalette.Highlight, QColor(42, 122, 226))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(150, 150, 150))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(150, 150, 150))
    return p


def apply_theme(app, mode: str):
    """mode: system / light / dark —— 一律用 Fusion，勾选框就不会有淡入动画"""
    from PySide6.QtWidgets import QApplication
    app.setStyle(FastTipStyle("Fusion"))
    # 提示气泡不要弹出动画、也不要淡入淡出（直接出现、直接消失）
    app.setEffectEnabled(Qt.UI_AnimateTooltip, False)
    app.setEffectEnabled(Qt.UI_FadeTooltip, False)
    if mode == "dark":
        app.setPalette(dark_palette())
    elif mode == "light":
        app.setPalette(light_palette())
    else:
        # 跟随系统：用 Qt 的系统配色方案判断，再套我们自己的调色板
        try:
            from PySide6.QtCore import Qt as _Qt
            scheme = app.styleHints().colorScheme()
            dark = scheme == _Qt.ColorScheme.Dark
        except Exception:                                  # noqa: BLE001
            dark = False
        app.setPalette(dark_palette() if dark else light_palette())


def _check_icon() -> pathlib.Path:
    """画一个 15×15 的白色勾，供 QSS 使用（一次生成，避免运行时依赖资源）"""
    from PIL import Image, ImageDraw
    d = ROOT / "out" / "_ui"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "check.png"
    if not f.exists():
        im = Image.new("RGBA", (15, 15), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        dr.line([(4, 8), (7, 11), (12, 4)], fill=(255, 255, 255, 255), width=2)
        im.save(f)
    return f


def check_stylesheet() -> str:
    """静态勾选框：不加动画，去掉原生指示器的 hover 重绘问题"""
    icon = _check_icon().as_posix()
    return f"""
    QToolTip {{
        background: #2f2f2f; color: #f0f0f0; border: 1px solid #6a6a6a;
        padding: 4px 6px;
    }}
    QTableWidget::indicator, QTableView::indicator {{
        width: 15px; height: 15px; border-radius: 3px;
    }}
    QTableWidget::indicator:unchecked, QTableView::indicator:unchecked {{
        border: 1px solid #9aa0a6; background: #ffffff;
    }}
    QTableWidget::indicator:checked, QTableView::indicator:checked {{
        border: 1px solid #1c7ed6; background: #1c7ed6; image: url({icon});
    }}
    """


def main():
    app = QApplication.instance() or QApplication([])
    s = QSettings("bms2iidx", "bms2iidx")
    apply_theme(app, s.value("theme", "system", str))
    app.setStyleSheet(check_stylesheet())
    w = Main()
    w.resize(1500, 980)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
