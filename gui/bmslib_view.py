"""「BMS 谱面库」页：浏览本机所有 BMS 谱面（索引缓存在 SQLite）

性能要点：
  · 索引只读文件头（8–64KB），首次全扫约 1–2 分钟（10 万谱面），之后只扫变化的
  · 表格是**分页模型**：rowCount 是总数，滚动到哪页才查哪页（每页 200 行）
  · 搜索/筛选走 SQL，防抖 250ms；扫描在独立线程，不卡界面
"""
from __future__ import annotations

import os
import pathlib

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTableView, QVBoxLayout, QWidget,
)

from core.bmslib import DIFF_NAMES, LibraryIndex

COLS = ["曲名", "艺术家", "流派", "文件夹", "谱面数", "时长", "本机成绩"]
COL_KEYS = ["title", "artist", "genre", "folder", "charts", "length", "score"]


class ChartModel(QAbstractTableModel):
    PAGE = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.idx: LibraryIndex | None = None
        self.root: str = ""
        self.filters: dict = {}
        self.total = 0
        self._cache: dict[int, list] = {}
        self.score_fn = None            # 由主窗口注入：路径 → 成绩文本
        self.score_ready = False        # 有没有配置成绩库（没配就显示 “—”）
        self._score_cache: dict[str, str] = {}

    def set_score_fn(self, fn):
        self.score_fn = fn
        self.clear_scores()
        self.layoutChanged.emit()

    def clear_scores(self):
        self._score_cache.clear()

    # ------------------------------------------------------------------ 数据源
    def set_source(self, idx: LibraryIndex, root: str):
        self.idx, self.root = idx, root
        self.reload()

    def set_filters(self, **kw):
        self.filters = {k: v for k, v in kw.items() if v not in (None, "", "全部")}
        self.reload()

    def reload(self):
        self.beginResetModel()
        self._cache.clear()
        self.total = 0
        if self.idx and self.root and not getattr(self.idx, "closed", False):
            _rows, total = self.idx.query_songs(self.root, limit=1, offset=0, **self.filters)
            self.total = total
        self.endResetModel()

    # ------------------------------------------------------------------ Qt 接口
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else self.total

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLS[section]
        return None

    def _page(self, page: int) -> list:
        rows = self._cache.get(page)
        if rows is None:
            if not self.idx:
                return []
            rows, _ = self.idx.query_songs(self.root, limit=self.PAGE,
                                           offset=page * self.PAGE, **self.filters)
            self._cache[page] = rows
        return rows

    def row(self, r: int) -> tuple | None:
        rows = self._page(r // self.PAGE)
        k = r % self.PAGE
        return rows[k] if 0 <= k < len(rows) else None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter) if c in (4, 5, 6) else None
        if role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        row = self.row(r)
        if not row:
            return None
        # row = (folder, title, artist, genre, 谱面数, 最高难度, 最高等级,
        #        keys, dp, bpm, 代表谱面path, 最长时长, 最高均值, 最高峰值, 最高推荐等级)
        if c == 0:
            return row[1] or row[0]
        if c == 1:
            return row[2]
        if c == 2:
            return row[3]
        if c == 3:
            return row[0] or "（根目录）"
        if c == 4:
            return str(row[4] or 0)
        if c == 5:
            ms = row[11]
            if not ms:
                return "—"
            s = int(ms // 1000)
            return f"{s // 60}:{s % 60:02d}"
        if c == 6:
            n, total = self._score_count(row[0])
            if not self.score_ready:
                return "—"
            if role == Qt.ToolTipRole:
                return f"这首歌 {total} 个谱面里有 {n} 个存了成绩"
            return str(n)
        return None

    def set_score_ready(self, ok: bool):
        self.score_ready = bool(ok)
        self.clear_scores()
        self.layoutChanged.emit()

    def _score_count(self, folder: str) -> tuple[int, int]:
        """(有几个谱面存了成绩, 这个文件夹一共几个谱面)"""
        if not self.score_fn or not self.score_ready:
            return 0, 0
        hit = self._score_cache.get("cnt:" + folder)
        if hit is not None:
            return hit
        try:
            paths = self.idx.chart_paths(self.root, folder) if self.idx else []
        except Exception:                                      # noqa: BLE001
            paths = []
        n = 0
        for p in paths:
            try:
                txt = self.score_fn(pathlib.Path(p))
            except Exception:                                  # noqa: BLE001
                txt = None
            if txt and txt != "—":
                n += 1
        out = (n, len(paths))
        self._score_cache["cnt:" + folder] = out
        return out

    def _score_of(self, path: str) -> str:
        """成绩按需算（只算屏幕上出现过的），结果缓存"""
        if not self.score_fn:
            return "—"
        hit = self._score_cache.get(path)
        if hit is not None:
            return hit
        try:
            txt = self.score_fn(pathlib.Path(path)) or "—"
        except Exception:                                      # noqa: BLE001
            txt = "—"
        self._score_cache[path] = txt
        return txt


class ScanWorker(QThread):
    progress = Signal(str)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path, root, full=False, parent=None):
        super().__init__(parent)
        self.db_path, self.root = db_path, root
        self.full = full
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            idx = LibraryIndex(self.db_path)
            stat = idx.refresh(self.root, progress=self.progress.emit,
                               should_stop=lambda: self._stop, full=self.full)
            idx.close()
            self.done.emit(stat)
        except BaseException as e:                              # noqa: BLE001
            self.failed.emit(str(e))


class StatsWorker(QThread):
    """把「统计 NPS」放到后台线程，主线程只收进度"""
    progress = Signal(str)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path, root, paths, workers=8, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.root = root
        self.paths = list(paths)
        self.workers = workers
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            from core import stats as statsmod
            res = statsmod.run_stats(self.paths, progress=self.progress.emit,
                                     should_stop=lambda: self._stop,
                                     workers=self.workers)
            idx = LibraryIndex(self.db_path)
            try:
                saved = idx.save_stats(res["rows"]) if res.get("rows") else 0
            finally:
                idx.close()
            res["saved"] = saved
            self.done.emit(res)
        except BaseException as e:                             # noqa: BLE001
            import traceback as _tb
            tail = _tb.format_exc().strip().splitlines()[-1]
            self.failed.emit(f"{type(e).__name__}: {e}\n{tail}")


class BmsLibraryPage(QWidget):
    requestLoad = Signal(str)          # 让主窗口在「转换」页打开这个文件夹
    scoreRootChanged = Signal(str)     # 成绩库换了（主窗口同步到转换页/设置页）

    def __init__(self, db_path: pathlib.Path, settings, parent=None):
        super().__init__(parent)
        self.db_path = pathlib.Path(db_path)
        self.settings = settings
        self.idx = self._open_index()
        self.model = ChartModel(self)
        self.worker: ScanWorker | None = None
        self.stats_worker = None          # 统计 NPS 用（和扫描线程分开，别互相覆盖）
        self._closed = False

        lay = QVBoxLayout(self)

        # ---- 第一行：曲库根目录
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("BMS 谱面库（本机 BMS 文件夹）"))
        self.edRoot = QLineEdit(str(self.settings.value("bms_root", "", str)))
        self.edRoot.setPlaceholderText("选一个装着 BMS 的根目录（可以是整个盘）…")
        row1.addWidget(self.edRoot, 1)
        b = QPushButton("浏览…")
        b.clicked.connect(self.pick_root)
        row1.addWidget(b)
        self.btnRefresh = QPushButton("更新索引")
        self.btnRefresh.setToolTip("只 stat 目录：没变的文件夹直接跳过（约 1 秒）")
        self.btnRefresh.clicked.connect(self.refresh_index)
        row1.addWidget(self.btnRefresh)
        self.btnFull = QPushButton("完整重建")
        self.btnFull.setToolTip("忽略目录缓存，重新读所有谱面的头部（约 35 秒）\n"
                                "原地改过谱面内容、又没动过目录名时用这个")
        self.btnFull.clicked.connect(self.rebuild_index)
        row1.addWidget(self.btnFull)
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 0)
        self.pbar.setFixedWidth(90)
        self.pbar.setVisible(False)
        row1.addWidget(self.pbar)
        lay.addLayout(row1)

        # ---- 成绩库（和转换页、设置页共用同一个设置）
        srow = QHBoxLayout()
        srow.addWidget(QLabel("成绩库"))
        self.edScore = QLineEdit(str(self.settings.value("player_root", "", str)))
        self.edScore.setPlaceholderText("可选：beatoraja 根目录（设了才显示「本机成绩」列）")
        self.edScore.editingFinished.connect(self._on_score_edited)
        srow.addWidget(self.edScore, 1)
        b_s = QPushButton("选择…")
        b_s.clicked.connect(self.pick_score_root)
        srow.addWidget(b_s)
        b_c = QPushButton("不用了")
        b_c.clicked.connect(self.clear_score_root)
        srow.addWidget(b_c)
        lay.addLayout(srow)

        # ---- 第二行：筛选
        flt = QHBoxLayout()
        self.edSearch = QLineEdit()
        self.edSearch.setPlaceholderText("搜索曲名 / 艺术家 / 流派 / 文件名（250ms 防抖）")
        self.edSearch.setClearButtonEnabled(True)
        flt.addWidget(self.edSearch, 2)

        flt.addWidget(QLabel("文件夹"))
        self.cbFolder = QComboBox()
        self.cbFolder.setMinimumWidth(160)
        flt.addWidget(self.cbFolder)
        flt.addWidget(QLabel("难度"))
        self.cbDiff = QComboBox()
        for k, v in (("全部", None), ("BEGINNER", 1), ("NORMAL", 2), ("HYPER", 3),
                     ("ANOTHER", 4), ("LEGENDARIA", 5)):
            self.cbDiff.addItem(k, v)
        flt.addWidget(self.cbDiff)
        flt.addWidget(QLabel("键数"))
        self.cbKeys = QComboBox()
        for k, v in (("全部", None), ("7K", 7), ("5K", 5)):
            self.cbKeys.addItem(k, v)
        flt.addWidget(self.cbKeys)
        flt.addWidget(QLabel("模式"))
        self.cbMode = QComboBox()
        for k, v in (("全部", None), ("SP", 0), ("DP", 1)):
            self.cbMode.addItem(k, v)
        flt.addWidget(self.cbMode)
        b2 = QPushButton("清空筛选")
        b2.clicked.connect(self.clear_filters)
        flt.addWidget(b2)
        lay.addLayout(flt)

        self.lblStat = QLabel("还没有索引：选好目录后点「更新索引」（首次约 2 分钟，之后只扫变化）")
        self.lblStat.setStyleSheet("color:#888")
        lay.addWidget(self.lblStat)

        # ---- 表格 + 预览
        split = QSplitter(Qt.Horizontal)      # 左列表 / 右预览
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setSortingEnabled(False)
        self.table.setWordWrap(False)
        self.table.setColumnHidden(5, True)     # 时长：统计完再显示
        hdr = self.table.horizontalHeader()
        for i, w in enumerate((240, 150, 110, 200, 62, 58, 74)):
            hdr.resizeSection(i, w)
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)      # 曲名占满剩余
        split.addWidget(self.table)

        prev = QGroupBox("选中项")
        prev.setMinimumWidth(480)
        pv = QVBoxLayout(prev)
        pv.setContentsMargins(10, 8, 10, 8)
        pv.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.thumb = QLabel("（没有 #STAGEFILE）")
        self.thumb.setFixedSize(200, 134)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("border:1px solid #444;color:#777;border-radius:4px")
        top.addWidget(self.thumb, 0, Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.lblTitle = QLabel("")
        self.lblTitle.setStyleSheet("font-size:15px;font-weight:600")
        self.lblTitle.setWordWrap(True)
        info.addWidget(self.lblTitle)
        self.lblMeta = QLabel("")
        self.lblMeta.setWordWrap(True)
        self.lblMeta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(self.lblMeta)
        self.lblPath = QLabel("")
        self.lblPath.setStyleSheet("color:#888")
        self.lblPath.setWordWrap(True)
        self.lblPath.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(self.lblPath)
        info.addStretch(1)
        top.addLayout(info, 1)
        pv.addLayout(top)

        self.tblCharts = QTableWidget(0, 7)
        self.tblCharts.setHorizontalHeaderLabels(
            ["谱面文件", "难度", "等级", "谱面作者", "均值 NPS", "峰值 NPS", "推荐"])
        self.tblCharts.verticalHeader().setVisible(False)
        self.tblCharts.verticalHeader().setDefaultSectionSize(22)
        self.tblCharts.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblCharts.setSelectionMode(QAbstractItemView.NoSelection)
        _h = self.tblCharts.horizontalHeader()
        _h.setStretchLastSection(False)
        for _i, _w in enumerate((176, 58, 42, 132, 66, 66, 40)):
            _h.resizeSection(_i, _w)
            _h.setSectionResizeMode(_i, QHeaderView.Interactive)
        pv.addWidget(self.tblCharts, 1)

        rowb = QHBoxLayout()
        self.btnUse = QPushButton("送到转换页")
        self.btnUse.clicked.connect(self.use_in_convert)
        rowb.addWidget(self.btnUse)
        self.btnStatsSong = QPushButton("统计 NPS")
        self.btnStatsSong.setToolTip("只算这首歌的谱面（每个约 1 秒），结果显示在上面的表格里")
        self.btnStatsSong.clicked.connect(self.run_stats)
        rowb.addWidget(self.btnStatsSong)
        b3 = QPushButton("打开文件夹")
        b3.clicked.connect(self.open_folder)
        rowb.addWidget(b3)
        rowb.addStretch(1)
        pv.addLayout(rowb)
        split.addWidget(prev)
        split.setSizes([860, 720])
        lay.addWidget(split, 1)

        # ---- 事件
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.apply_filters)
        self.edSearch.textChanged.connect(lambda *_: self._debounce.start())
        for cb in (self.cbFolder, self.cbDiff, self.cbKeys, self.cbMode):
            cb.currentIndexChanged.connect(self.apply_filters)
        self.table.selectionModel().selectionChanged.connect(self.on_select)
        self.table.doubleClicked.connect(lambda *_: self.use_in_convert())
        self.edSearch.returnPressed.connect(self.use_in_convert)
        self.edRoot.editingFinished.connect(self.on_root_changed)

        # 首次：如果根目录有索引，直接加载
        root = self.edRoot.text().strip()
        if not root and pathlib.Path("C:/BMS").is_dir():
            self.edRoot.setText("C:/BMS")
            root = "C:/BMS"
        if root and pathlib.Path(root).is_dir():
            self.model.set_source(self.idx, root)
            self.reload_facets()
            self.update_stat()
            self._refresh_stats_columns()
            self._refresh_stats_columns()

    # ------------------------------------------------------------------ 动作
    def _alive(self) -> bool:
        return not getattr(self, "_closed", False) and not getattr(self.idx, "closed", False)

    def _on_score_edited(self):
        self.set_score_root(self.edScore.text().strip())

    def pick_score_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择 beatoraja 根目录",
                                             self.edScore.text() or "C:\\")
        if d:
            self.edScore.setText(d)
            self.set_score_root(d)

    def set_score_root(self, path: str, emit: bool = True):
        """成绩库路径（空 = 不启用）。会写进全局设置并通知主窗口。"""
        path = (path or "").strip()
        if self.edScore.text() != path:
            self.edScore.setText(path)
        self.settings.setValue("player_root", path)
        try:
            from . import services
            services.set_player_root(path or None)
        except Exception:                                      # noqa: BLE001
            pass
        self.model.clear_scores()
        self.model.layoutChanged.emit()
        if emit:
            self.scoreRootChanged.emit(path)

    def clear_score_root(self):
        self.set_score_root("")

    # ------------------------------------------------------------------ 统计 NPS
    def _show_stats_columns(self, on: bool):
        """「时长」依赖统计结果，没统计过就先藏起来"""
        self.table.setColumnHidden(5, not on)

    def _refresh_stats_columns(self):
        try:
            n = self.idx.stats_count(self.root_text()) if self.root_text() else 0
        except Exception:                                      # noqa: BLE001
            n = 0
        self._show_stats_columns(n > 0)

    def current_filters(self) -> dict:
        """把界面上的筛选条件转成索引查询用的参数"""
        out = {}
        text = self.edSearch.text().strip()
        if text:
            out["text"] = text
        folder = self.cbFolder.currentData()
        if folder:
            out["folder"] = folder
        for key, cb in (("difficulty", self.cbDiff), ("keys", self.cbKeys),
                        ("dp", self.cbMode)):
            v = cb.currentData()
            if v is not None:
                out[key] = v
        return out

    def root_text(self) -> str:
        return self.edRoot.text().strip()

    def run_stats(self):
        """只统计当前选中这首歌的谱面（结果显示在预览表格里）"""
        row = self.current_row()
        if not row:
            QMessageBox.information(self, "先选一首歌", "在左边列表里点一首歌，再点这个按钮。")
            return
        root = self.root_text()
        if not root or not pathlib.Path(root).is_dir():
            return
        try:
            if self.stats_worker is not None and self.stats_worker.isRunning():
                return
        except RuntimeError:                                   # 线程对象已被回收
            self.stats_worker = None
        folder = row[0]
        try:
            paths = self.idx.chart_paths(root, folder)
            missing = self.idx.stats_missing(paths)
        except Exception as e:                                 # noqa: BLE001
            QMessageBox.warning(self, "读不到索引", str(e))
            return
        if not missing:
            self.on_select()                       # 已有统计：直接刷预览
            self.set_status_ok("这首歌已经统计过了，直接看表格")
            return
        self.btnStatsSong.setEnabled(False)
        self.pbar.setVisible(True)
        self.pbar.setRange(0, 0)
        self._stats_song = folder
        self.stats_worker = StatsWorker(self.db_path, root, missing, workers=4, parent=self)
        self.stats_worker.progress.connect(self._on_stats_progress)
        self.stats_worker.done.connect(self._on_stats_done)
        self.stats_worker.failed.connect(self._on_stats_failed)
        self.stats_worker.start()

    def set_status_ok(self, msg: str):
        self.lblStat.setText(msg)

    def _apply_stats_rows(self, rows: list):
        """把刚算出的统计值直接写进预览表格（按文件名匹配）"""
        if not rows or not self.tblCharts.rowCount():
            return
        by_name = {}
        for r in rows:
            if r.get("ok"):
                by_name[pathlib.Path(r["path"]).name] = r
        for i in range(self.tblCharts.rowCount()):
            cell = self.tblCharts.item(i, 0)
            if not cell:
                continue
            hit = by_name.get(cell.text())
            if not hit:
                continue
            for col, key, fmt in ((4, "mean_nps", "{:.2f}"), (5, "peak_nps", "{:.2f}"),
                                  (6, "rec_level", "{}")):
                v = hit.get(key)
                item = self.tblCharts.item(i, col) or QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignCenter)
                item.setText(fmt.format(v) if v is not None else "—")
                self.tblCharts.setItem(i, col, item)

    def _on_stats_progress(self, msg: str):
        if isinstance(msg, str) and msg.startswith("[PROGRESS "):
            try:
                n = int(msg.split("]", 1)[0].split()[1])
                self.pbar.setRange(0, 100)
                self.pbar.setValue(n)
                self.lblStat.setText(msg.split("]", 1)[-1].strip())
            except (ValueError, IndexError):
                pass

    def _on_stats_done(self, res: dict):
        if hasattr(self, "btnStatsSong"):
            self.btnStatsSong.setEnabled(True)
        self.pbar.setVisible(False)
        self.pbar.setRange(0, 0)
        saved = res.get("saved", 0)
        failed = res.get("failed") or []
        msg = f"这首歌统计完成：{saved} 个谱面"
        if failed:
            msg += f"，{len(failed)} 个失败（多为无音符/解析失败）"
        self.lblStat.setText(msg)
        self.stats_worker = None
        # 主线程这条连接可能还停在旧快照上，结束掉隐形事务再读，保证读到刚写进去的统计
        try:
            self.idx.conn.commit()
        except Exception:                                      # noqa: BLE001
            pass
        self.model.clear_scores()
        self.model.reload()
        self._refresh_stats_columns()
        try:
            if hasattr(self, "_stats_song") and self._stats_song:
                self._reselect_folder(self._stats_song)
        except Exception as e:                                 # noqa: BLE001
            self.lblStat.setText(f"重选行失败（不影响统计结果）：{e}")
        # 用一条新连接读一次再填：主连接可能还停在旧快照上
        folder = getattr(self, "_stats_song", "") or ""
        if folder:
            try:
                tmp = LibraryIndex(self.db_path)
                try:
                    charts = tmp.chart_list(self.model.root, folder)
                finally:
                    tmp.close()
                self._fill_charts(charts, folder)
            except Exception:                                  # noqa: BLE001
                self._apply_stats_rows(res.get("rows") or [])

    def _on_stats_failed(self, msg: str):
        self.stats_worker = None
        if hasattr(self, "btnStatsSong"):
            self.btnStatsSong.setEnabled(True)
        self.pbar.setVisible(False)
        self.pbar.setRange(0, 0)
        QMessageBox.warning(self, "统计失败", msg)
        self.lblStat.setText("统计失败：" + msg[:120])

    def _open_index(self) -> LibraryIndex:
        """索引文件坏掉时不要让程序起不来：备份坏文件、重建一个空索引"""
        try:
            return LibraryIndex(self.db_path)
        except Exception as e:                                 # noqa: BLE001
            import time as _t
            bad = self.db_path.with_suffix(f".bad-{_t.strftime('%Y%m%d-%H%M%S')}.sqlite")
            try:
                self.db_path.replace(bad)
            except OSError:
                pass
            QMessageBox.warning(
                None, "索引文件损坏，已重建",
                f"读取 BMS 索引时出错：{e}\n\n"
                f"坏的索引已改名保存：{bad.name}\n"
                "程序会新建一个空索引，请点「完整重建」（只需一次）。")
            return LibraryIndex(self.db_path)

    def pick_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择 BMS 谱面库根目录",
                                             self.edRoot.text() or "C:\\")
        if d:
            self.edRoot.setText(d)
            self.on_root_changed()

    def on_root_changed(self):
        if not self._alive():
            return
        root = self.edRoot.text().strip()
        self.settings.setValue("bms_root", root)
        if not root or not pathlib.Path(root).is_dir():
            return
        self.model.set_source(self.idx, root)
        self.reload_facets()
        self.update_stat()

    def refresh_index(self):
        self._start_scan(False)

    def rebuild_index(self):
        self._start_scan(True)

    def _start_scan(self, full: bool):
        root = self.edRoot.text().strip()
        if not root or not pathlib.Path(root).is_dir():
            self.lblStat.setText("先选一个存在的目录")
            return
        if self.worker and self.worker.isRunning():
            return
        self.btnRefresh.setEnabled(False)
        self.btnFull.setEnabled(False)
        self.pbar.setVisible(True)
        self.lblStat.setText("完整重建中…（只读文件头，不会改动你的曲库）" if full
                             else "更新索引中…（没变的文件夹直接跳过）")
        self.worker = ScanWorker(self.db_path, root, full=full, parent=self)
        self.worker.progress.connect(lambda s: self.lblStat.setText(s))
        self.worker.done.connect(self.on_scanned)
        self.worker.failed.connect(self.on_scan_failed)
        self.worker.start()

    def on_scanned(self, stat: dict):
        self.btnRefresh.setEnabled(True)
        self.btnFull.setEnabled(True)
        self.pbar.setVisible(False)
        self._last_stat = stat
        self.settings.setValue("bms_root", stat["root"])
        self.model.set_source(self.idx, stat["root"])
        self.reload_facets()
        self.update_stat(stat)

    def on_scan_failed(self, msg: str):
        self.btnRefresh.setEnabled(True)
        self.btnFull.setEnabled(True)
        self.pbar.setVisible(False)
        self.lblStat.setText(f"扫描失败：{msg}")

    def update_stat(self, stat: dict | None = None):
        if not self._alive():
            return
        root = self.edRoot.text().strip()
        n = self.idx.count(root) if root else 0
        extra = ""
        if stat:
            extra = (f" · 新增 {stat['added']}、更新 {stat['updated']}"
                     f"、清理 {stat['removed']}，用时 {stat['ms']/1000:.1f}s"
                     f"（跳过 {stat.get('skipped_dirs', 0)}/{stat.get('dirs', 0)} 个未变目录）")
        msg = (f"索引 {n:,} 个谱面 · 当前筛选 {self.model.total:,} 首歌曲{extra}"
               "　（一行 = 一首歌，双击送到「转换」页挑谱面）")
        if self.model.total == 0 and n:
            msg += "　—　没有匹配的歌曲，试试「清空筛选」或换个关键词"
        self.lblStat.setText(msg)

    def reload_facets(self):
        if not self._alive():
            return
        root = self.edRoot.text().strip()
        if not root:
            return
        f = self.idx.facets(root)
        cur = self.cbFolder.currentData()
        self.cbFolder.blockSignals(True)
        self.cbFolder.clear()
        self.cbFolder.addItem("全部文件夹", None)
        for folder, n in f["folders"][:400]:
            self.cbFolder.addItem(f"{folder or '（根目录）'}  ·{n}", folder)
        i = self.cbFolder.findData(cur)
        self.cbFolder.setCurrentIndex(max(0, i))
        self.cbFolder.blockSignals(False)

    def clear_filters(self):
        self.edSearch.clear()
        for cb in (self.cbFolder, self.cbDiff, self.cbKeys, self.cbMode):
            cb.setCurrentIndex(0)
        self.apply_filters()

    def apply_filters(self):
        if not self._alive():
            return
        self.model.set_filters(
            text=self.edSearch.text().strip(),
            folder=self.cbFolder.currentData(),
            difficulty=self.cbDiff.currentData(),
            keys=self.cbKeys.currentData(),
            dp=self.cbMode.currentData(),
        )
        self.update_stat()

    # ------------------------------------------------------------------ 选中
    def current_row(self) -> tuple | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.row(rows[0].row())

    def _reselect_folder(self, folder: str):
        """统计完把同一首歌重新选上（选中行没变时不会触发 on_select，故单独重填）"""
        for r in range(min(self.model.total, 600)):
            row = self.model.row(r)
            if row and row[0] == folder:
                self.table.selectRow(r)
                self.table.scrollTo(self.model.index(r, 0))
                return

    def _fill_charts(self, charts: list, folder: str):
        """把某首歌的谱面填进预览表格（on_select 与统计完成后共用）"""
        self.tblCharts.setRowCount(len(charts))
        for r, ch in enumerate(charts):
            name, level, diff, ckeys, cdp, length, mean, peak, rec, subartist = ch
            if not subartist:
                # 索引里没有就现读文件头（只是一首歌的几个文件，很快）
                try:
                    from core.bmslib import parse_head as _ph
                    q = pathlib.Path(self.model.root) / folder / name
                    subartist = (_ph(q) or {}).get("subartist", "") or ""
                except Exception:                              # noqa: BLE001
                    subartist = ""
            cells = [
                name,
                DIFF_NAMES.get(diff, "—") if diff else "—",
                str(level) if isinstance(level, int) and 0 < level <= 12 else "—",
                subartist or "—",
                (f"{mean:.2f}" if mean is not None else "—"),
                (f"{peak:.2f}" if peak is not None else "—"),
                (str(rec) if rec else "—"),
            ]
            for c, v in enumerate(cells):
                item = QTableWidgetItem(str(v))
                if c in (1, 2, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignCenter)
                if c == 3 and not subartist:
                    item.setToolTip("这个谱面没有写 #SUBARTIST")
                self.tblCharts.setItem(r, c, item)


    def on_select(self, *_a):
        row = self.current_row()
        if not row:
            self.lblTitle.setText("")
            self.lblMeta.setText("")
            self.lblPath.setText("")
            self.thumb.setText("（没有 #STAGEFILE）")
            self.thumb.setPixmap(QPixmap())
            self.tblCharts.setRowCount(0)
            return
        # row = (folder, title, artist, genre, 谱面数, 最高难度, 最高等级,
        #        keys, dp, bpm, 代表谱面path, 最长时长, 最高均值, 最高峰值, 最高推荐等级)
        folder, title, artist, genre, ncharts, maxdiff, maxlevel, keys, dp, bpm, path             = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10]
        self.lblTitle.setText(title or folder or "（无标题）")
        bits = []
        if artist:
            bits.append(artist)
        if genre:
            bits.append(genre)
        bits.append(f"{ncharts} 个谱面")
        try:
            from core import romanize as _rom
            why = _rom.iidx_unsafe_reason(title or "")
            if why:
                bits.append(f"⚠ {why}")
        except Exception:                                      # noqa: BLE001
            pass
        self.lblMeta.setText("  ·  ".join(bits))
        self.lblPath.setText(folder or "（根目录）")

        # 谱面清单
        try:
            charts = self.idx.chart_list(self.model.root, folder) if self.model.root else []
        except Exception:                                      # noqa: BLE001
            charts = []
        self._fill_charts(charts, folder)

        # 封面：同一首歌里第一个带 #STAGEFILE 的谱面
        pix = QPixmap()
        if path:
            try:
                from core import bmslib as _bl
                head = _bl.parse_head(pathlib.Path(path))
                stage = (head or {}).get("stagefile") or ""
                if stage:
                    q = pathlib.Path(path).parent / stage.replace("\\", "/")
                    if q.is_file():
                        pix = QPixmap(str(q))
            except Exception:                                  # noqa: BLE001
                pass
        if pix.isNull():
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("（没有 #STAGEFILE）")
        else:
            self.thumb.setText("")
            self.thumb.setPixmap(pix.scaled(self.thumb.size(), Qt.KeepAspectRatio,
                                            Qt.SmoothTransformation))

    def song_dir(self, row) -> str:
        """歌曲行 → 绝对文件夹路径（row[0] 是相对根目录的 folder）"""
        root = pathlib.Path(self.model.root or self.edRoot.text().strip())
        rel = (row or [""])[0] or ""
        return str(root / rel) if rel else str(root)

    def use_in_convert(self):
        row = self.current_row()
        if not row:
            return
        self.requestLoad.emit(self.song_dir(row))

    def open_folder(self):
        row = self.current_row()
        if not row:
            return
        folder = self.song_dir(row)
        try:
            os.startfile(folder)                       # noqa: S606
        except OSError:
            pass

    def closeEvent(self, e):
        self._closed = True
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        try:
            if self.stats_worker is not None and self.stats_worker.isRunning():
                self.stats_worker.stop()
                self.stats_worker.wait(3000)
        except RuntimeError:
            pass
        self.idx.close()
        super().closeEvent(e)
