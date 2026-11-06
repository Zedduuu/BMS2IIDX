"""BMS 曲库索引（给界面里的「BMS 曲库」页用）

目标：10 万+ 谱面也要快。做了四件事：

1. **只读文件头**：先读 2KB，找不到 `#TITLE` 再补到 64KB；不解析音符
2. **多进程解析**：解析是 CPU 瓶颈（单线程约 2200/秒），铺到多核后首次扫描快 4–6 倍
3. **目录级跳过**：缓存每个目录的 `mtime + 子目录名单`；目录没变就**不进去**，
   日常刷新只 stat 1 万来个目录（约 0.3 秒），不用枚举 500 万个文件
4. **单事务写入**：一次性 upsert，不再每 400 行提交

注意：目录 mtime 只在「增/删/改名」时变；**原地修改文件内容**不会变。
所以 `full=True` 会忽略目录缓存做完整重扫（界面上是「完整重建」）。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor

from . import bms as bms_mod
from . import enc as enc_mod

EXTS = {".bms", ".bme", ".bml", ".pms"}
HEAD_FIRST = 2 * 1024          # 绝大多数谱面前 1KB 内就有 #TITLE
HEAD_SECOND = 64 * 1024        # 没有 #TITLE 再补到 64KB
CHUNK = 300                    # 多进程一次处理多少个文件

# 一次扫描抓所有头部命令（字节级：cp932 与 utf-8 的 ASCII 部分一致，不用先解码）
_HEAD_RE = re.compile(
    rb"(?im)^[ \t]*#(TITLE|SUBTITLE|ARTIST|GENRE|PLAYLEVEL|DIFFICULTY|BPM|PLAYER|"
    rb"STAGEFILE|TOTAL|SUBARTIST)[ \t]+([^\r\n]*)")
_CH_RE = re.compile(rb"(?m)^#\d{3}([12][1-9])")
_KEY_ORDER = ("TITLE", "SUBTITLE", "ARTIST", "SUBARTIST", "GENRE", "PLAYLEVEL",
              "DIFFICULTY", "BPM", "PLAYER", "STAGEFILE", "TOTAL")


def _clean_level(raw) -> int | None:
    """#PLAYLEVEL 清洗：只接受 1–12。

    BMS 里这个字段可以是 "5"、"★10"、"-73"、空串甚至一句话；
    原样 int() 会把脏值写进索引（实测见过 -73），列表就没法看了。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    import re as _re
    m = _re.search(r"\d+", s)
    if not m:
        return None
    try:
        v = int(m.group(0))
    except ValueError:
        return None
    return v if 1 <= v <= 12 else None


def _read_raw(path: pathlib.Path) -> bytes:
    """只读文件头部字节（先 2KB，找不到 #TITLE 再补到 64KB）"""
    with open(path, "rb") as f:
        raw = f.read(HEAD_FIRST)
        if not any(k in raw.upper() for k in (b"#TITLE", b"#ARTIST")):
            raw += f.read(HEAD_SECOND - len(raw))
    return raw


def _dec_value(b: bytes) -> str:
    """只解码一个字段值：先试 utf-8（快），失败再 cp932（日文谱面主力）"""
    if not b:
        return ""
    try:
        return b.decode("utf-8").strip()
    except UnicodeDecodeError:
        return b.decode("cp932", "replace").strip()


def _extract(raw: bytes, enc: str):
    """按给定编码从字节里抓头部命令 → (字段字典, SP 通道集, P2 通道集)"""
    h = {k.lower(): "" for k in _KEY_ORDER}
    for m in _HEAD_RE.finditer(raw):
        key = m.group(1).decode("ascii").lower()
        if not h[key]:
            try:
                h[key] = m.group(2).decode(enc, errors="replace").strip()
            except LookupError:
                h[key] = _dec_value(m.group(2))
    sp, p2 = set(), set()
    for m in _CH_RE.finditer(raw):
        code = m.group(1).decode("ascii")
        (sp if code.startswith("1") else p2).add(code)
    return h, sp, p2


def _looks_broken(h: dict) -> bool:
    """值里出现替换符或一堆半角片假名 → 编码判断错了"""
    for v in h.values():
        if "\ufffd" in v:
            return True
        if sum(1 for c in v if 0xFF61 <= ord(c) <= 0xFF9F) >= 2:
            return True
    return False


def parse_head(path: pathlib.Path) -> dict | None:
    try:
        raw = _read_raw(path)
    except OSError:
        return None

    # 快速路径：cp932 覆盖 99% 的谱面；只有可疑时才做完整编码判别
    enc = "cp932"
    if (raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[:3] == b"\xef\xbb\xbf"
            or b"\x1b$B" in raw or b"\x1b$@" in raw):
        enc = enc_mod.detect_encoding(raw)

    if enc not in ("cp932", "cp949"):
        # UTF-16 / ISO-2022-JP / UTF-8：字节层抓关键字会混淆，整段解码再正则
        text = enc_mod.decode(raw, enc)
        h = {}
        for key, field in (("TITLE", "title"), ("SUBTITLE", "subtitle"),
                           ("ARTIST", "artist"), ("GENRE", "genre"),
                           ("PLAYLEVEL", "playlevel"), ("DIFFICULTY", "difficulty"),
                           ("BPM", "bpm"), ("PLAYER", "player"),
                           ("STAGEFILE", "stagefile"), ("TOTAL", "total")):
            m = re.search(rf"(?im)^[ \t]*#{key}[ \t]+([^\r\n]*)", text)
            h[field] = m.group(1).strip() if m else ""
        sp = {m.group(1) for m in re.finditer(r"(?m)^#\d{3}(1[1-9])", text)}
        p2 = {m.group(1) for m in re.finditer(r"(?m)^#\d{3}(2[1-9])", text)}
    else:
        h, sp, p2 = _extract(raw, enc)
        if _looks_broken(h):
            enc2 = enc_mod.detect_encoding(raw)
            if enc2 != enc:
                enc = enc2
                if enc in ("cp932", "cp949"):
                    h, sp, p2 = _extract(raw, enc)
                else:
                    text = enc_mod.decode(raw, enc)
                    h = {}
                    for key, field in (("TITLE", "title"), ("SUBTITLE", "subtitle"),
                                       ("ARTIST", "artist"), ("GENRE", "genre"),
                                       ("PLAYLEVEL", "playlevel"),
                                       ("DIFFICULTY", "difficulty"), ("BPM", "bpm"),
                                       ("PLAYER", "player"), ("STAGEFILE", "stagefile"),
                                       ("TOTAL", "total")):
                        m = re.search(rf"(?im)^[ \t]*#{key}[ \t]+([^\r\n]*)", text)
                        h[field] = m.group(1).strip() if m else ""
                    sp = {m.group(1) for m in re.finditer(r"(?m)^#\d{3}(1[1-9])", text)}
                    p2 = {m.group(1) for m in re.finditer(r"(?m)^#\d{3}(2[1-9])", text)}

    player = h.get("player", "")
    dp = player == "3" or bool(p2)
    n_sp = len({c for c in sp if c != "16"})       # 16 = 转盘
    keys = 5 if (n_sp and n_sp <= 5 and "18" not in sp and "19" not in sp) else 7
    if dp:
        keys = 5 if ("28" not in p2 and "29" not in p2) else 7
    try:
        level = _clean_level(h.get("playlevel"))
    except ValueError:
        level = None
    try:
        diff = int(h["difficulty"]) if h.get("difficulty") else None
    except ValueError:
        diff = None
    return {
        "title": h.get("title", "")[:200], "subtitle": h.get("subtitle", "")[:200],
        "artist": h.get("artist", "")[:200], "genre": h.get("genre", "")[:120],
        "level": level, "difficulty": diff, "bpm": h.get("bpm", ""),
        "subartist": h.get("subartist", "")[:200],
        "dp": 1 if dp else 0, "keys": keys,
        "stagefile": h.get("stagefile", "")[:200], "total": h.get("total", ""),
        "encoding": enc,
    }


def _parse_subartist(path: str) -> tuple[str, str]:
    """只读文件头拿 #SUBARTIST（升级索引后补历史数据用）"""
    try:
        h = parse_head(pathlib.Path(path))
    except Exception:                                          # noqa: BLE001
        return path, ""
    return path, (h or {}).get("subartist", "") or ""


def _parse_chunk(items: list[tuple]) -> list[tuple]:
    """工作进程：把一批 (path, root, folder, name, size, mtime) 解析成行"""
    out = []
    for path, root, folder, name, size, mtime in items:
        head = parse_head(pathlib.Path(path))
        if head is None:
            out.append((path, root, folder, name, size, mtime, None))
        else:
            out.append((path, root, folder, name, size, mtime, head))
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS charts (
    path TEXT PRIMARY KEY, root TEXT NOT NULL, folder TEXT NOT NULL, name TEXT NOT NULL,
    size INTEGER NOT NULL, mtime REAL NOT NULL,
    title TEXT, subtitle TEXT, artist TEXT, genre TEXT,
    level INTEGER, difficulty INTEGER, bpm TEXT,
    dp INTEGER, keys INTEGER, stagefile TEXT, total TEXT, subartist TEXT,
    length_ms INTEGER, mean_nps REAL, peak_nps REAL, rec_level INTEGER, stats_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_root ON charts(root);
CREATE INDEX IF NOT EXISTS idx_folder ON charts(folder);
CREATE INDEX IF NOT EXISTS idx_title_nc ON charts(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_root_title ON charts(root, title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_root_title_f JSON1_PLACEHOLDER;
CREATE TABLE IF NOT EXISTS dirs (
    root TEXT NOT NULL, folder TEXT NOT NULL, mtime REAL NOT NULL,
    children TEXT NOT NULL, PRIMARY KEY(root, folder)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

UPSERT = """INSERT INTO charts(path, root, folder, name, size, mtime,
        title, subtitle, artist, genre, level, difficulty,
        bpm, dp, keys, stagefile, total, subartist)
   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
   ON CONFLICT(path) DO UPDATE SET
        size=excluded.size, mtime=excluded.mtime, folder=excluded.folder,
        name=excluded.name, title=excluded.title, subtitle=excluded.subtitle,
        artist=excluded.artist, genre=excluded.genre, level=excluded.level,
        difficulty=excluded.difficulty, bpm=excluded.bpm, dp=excluded.dp,
        keys=excluded.keys, stagefile=excluded.stagefile, total=excluded.total,
        subartist=excluded.subartist,
        -- 文件内容变了（size/mtime 变才会走到这里），旧统计作废
        length_ms=NULL, mean_nps=NULL, peak_nps=NULL, rec_level=NULL, stats_at=NULL"""

DIFF_NAMES = {1: "BEGINNER", 2: "NORMAL", 3: "HYPER", 4: "ANOTHER", 5: "LEGENDARIA"}


_STATS_COLUMNS = (
    ("subartist", "TEXT"),
    ("length_ms", "INTEGER"),
    ("mean_nps", "REAL"),
    ("peak_nps", "REAL"),
    ("rec_level", "INTEGER"),
    ("stats_at", "TEXT"),
)


def _migrate_stats_cols(conn) -> None:
    """老索引没有统计列时补上（ALTER TABLE ADD COLUMN，不动已有数据）"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(charts)")}
    for name, typ in _STATS_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE charts ADD COLUMN {name} {typ}")
    conn.commit()


class LibraryIndex:
    def __init__(self, db_path: pathlib.Path | str):
        self.path = pathlib.Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA.replace("CREATE INDEX IF NOT EXISTS idx_root_title_f JSON1_PLACEHOLDER;", ""))
        _migrate_stats_cols(self.conn)          # 老库补统计列
        self.closed = False
        self.conn.commit()

    # ---------------------------------------------------------------- 扫描
    def refresh(self, root: pathlib.Path | str, progress=None,
                should_stop=None, full: bool = False) -> dict:
        """full=False：目录没变就跳过（快）；full=True：完整重扫"""
        root = pathlib.Path(root)
        root_s = str(root)
        t0 = time.perf_counter()

        known_files: dict[str, tuple[int, float]] = {
            p: (s, m) for p, s, m in self.conn.execute(
                "SELECT path, size, mtime FROM charts WHERE root=?", (root_s,))}
        known_dirs: dict[str, tuple[float, list[str]]] = {}
        for folder, mtime, children in self.conn.execute(
                "SELECT folder, mtime, children FROM dirs WHERE root=?", (root_s,)):
            try:
                known_dirs[folder] = (mtime, json.loads(children))
            except Exception:                                  # noqa: BLE001
                pass

        visited: set[str] = set()          # 走到的目录（含跳过的）
        changed: set[str] = set()          # 需要核对文件的目录
        new_dirs: dict[str, tuple[float, list[str]]] = {}
        candidates: list[tuple] = []       # (path, folder, name, size, mtime)
        seen_files: set[str] = set()       # 本次确认存在的文件
        failed = 0
        skipped_dirs = 0

        stack = [(root_s, "")]
        while stack:
            if should_stop and should_stop():
                break
            dirpath, rel = stack.pop()
            try:
                dm = os.stat(dirpath).st_mtime
            except OSError:
                continue
            visited.add(rel)
            kd = known_dirs.get(rel)
            if not full and kd and abs(kd[0] - dm) < 1e-6:
                # 目录没变：直接按缓存的子目录继续走，不枚举文件
                skipped_dirs += 1
                for name in kd[1]:
                    stack.append((os.path.join(dirpath, name),
                                  (rel + "/" + name).lstrip("/")))
                continue
            children: list[str] = []
            changed.add(rel)
            try:
                entries = list(os.scandir(dirpath))
            except OSError:
                continue
            for ent in entries:
                try:
                    if ent.is_dir(follow_symlinks=False):
                        if not ent.name.startswith("."):
                            children.append(ent.name)
                            stack.append((ent.path, (rel + "/" + ent.name).lstrip("/")))
                        continue
                except OSError:
                    continue
                if os.path.splitext(ent.name)[1].lower() not in EXTS:
                    continue
                try:
                    st = ent.stat()
                except OSError:
                    failed += 1
                    continue
                candidates.append((ent.path, rel, ent.name, st.st_size, st.st_mtime))
                seen_files.add(ent.path)
            new_dirs[rel] = (dm, children)

        # ---- 解析变化的文件（多进程）
        todo = [c for c in candidates
                if c[0] not in known_files
                or known_files[c[0]][0] != c[3]
                or abs(known_files[c[0]][1] - c[4]) > 1e-6]
        rows: list[tuple] = []
        if todo:
            chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
            workers = min(8, max(1, (os.cpu_count() or 4) - 2))
            done = 0
            try:
                with ProcessPoolExecutor(max_workers=workers) as ex:
                    for part in ex.map(_parse_chunk,
                                       [[(p, root_s, f, n, s, m) for p, f, n, s, m in ch]
                                        for ch in chunks]):
                        rows.extend(r for r in part if r[6] is not None)
                        done += len(part)
                        if progress:
                            progress(f"解析中 {done}/{len(todo)}…")
            except Exception:                                  # noqa: BLE001
                for ch in chunks:
                    for r in _parse_chunk([(p, root_s, f, n, s, m)
                                           for p, f, n, s, m in ch]):
                        rows.extend([r] if r[6] is not None else [])
                done = len(todo)

        # ---- 写库（单事务）
        cur = self.conn.cursor()
        if rows:
            cur.executemany(UPSERT, [
                (p, rt, f, n, s, m, h["title"], h["subtitle"], h["artist"], h["genre"],
                 h["level"], h["difficulty"], h["bpm"], h["dp"], h["keys"],
                 h["stagefile"], h["total"], h["subartist"])
                for p, rt, f, n, s, m, h in rows])
        # 变动的目录里，DB 有但本次没看到的文件 → 删掉
        removed = 0
        for rel in changed:
            existing = [p for (p,) in self.conn.execute(
                "SELECT path FROM charts WHERE root=? AND folder=?", (root_s, rel))]
            gone = [p for p in existing if p not in seen_files]
            if gone:
                cur.executemany("DELETE FROM charts WHERE path=?", [(p,) for p in gone])
                removed += len(gone)
        # 整个目录已消失
        vanished = [f for f in known_dirs if f not in visited]
        if vanished:
            cur.executemany("DELETE FROM charts WHERE root=? AND folder=?", [(root_s, f) for f in vanished])
            cur.executemany("DELETE FROM dirs WHERE root=? AND folder=?", [(root_s, f) for f in vanished])
        if new_dirs:
            cur.executemany(
                "INSERT INTO dirs(root, folder, mtime, children) VALUES(?,?,?,?) "
                "ON CONFLICT(root, folder) DO UPDATE SET mtime=excluded.mtime, "
                "children=excluded.children",
                [(root_s, f, m, json.dumps(ch, ensure_ascii=False)) for f, (m, ch) in new_dirs.items()])
        self.conn.commit()
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                          (f"scanned:{root_s}", str(int(time.time()))))
        self.conn.commit()

        ms = int((time.perf_counter() - t0) * 1000)
        added = len([r for r in rows if r[0] not in known_files])
        return {"root": root_s, "total": len(seen_files) if changed else len(known_files),
                "known": len(known_files), "added": added,
                "updated": len(rows) - added, "removed": removed,
                "failed": failed, "dirs": len(visited),
                "skipped_dirs": skipped_dirs, "ms": ms}

    # ---------------------------------------------------------------- 查询
    def count(self, root: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM charts WHERE root=?",
                                 (str(root),)).fetchone()[0]

    def facets(self, root: str) -> dict:
        root = str(root)
        folders = self.conn.execute(
            "SELECT folder, COUNT(*) FROM charts WHERE root=? GROUP BY folder "
            "ORDER BY folder", (root,)).fetchall()
        diffs = dict(self.conn.execute(
            "SELECT difficulty, COUNT(*) FROM charts WHERE root=? GROUP BY difficulty",
            (root,)).fetchall())
        dp = dict(self.conn.execute(
            "SELECT dp, COUNT(*) FROM charts WHERE root=? GROUP BY dp", (root,)).fetchall())
        lv = self.conn.execute(
            "SELECT MIN(level), MAX(level) FROM charts WHERE root=? AND level IS NOT NULL",
            (root,)).fetchone()
        return {"folders": folders, "difficulty": diffs, "dp": dp,
                "level_min": lv[0], "level_max": lv[1]}

    def query_songs(self, root: str, text: str = "", folder: str | None = None,
                    difficulty: int | None = None, dp: int | None = None,
                    keys: int | None = None,
                    order: str = "title", limit: int = 100, offset: int = 0):
        """按「歌曲」聚合：一行 = 一个文件夹（一首歌）。

        返回 row = (folder, title, artist, genre, 谱面数, 最高难度, 最高等级,
                    keys, dp, bpm, 代表谱面path, 最长时长, 最高均值, 最高峰值, 最高推荐等级)
        """
        where = ["root=?"]
        args: list = [str(root)]
        if text:
            like = f"%{text}%"
            where.append("(title LIKE ? OR artist LIKE ? OR genre LIKE ? OR name LIKE ?)")
            args += [like, like, like, like]
        if folder is not None:
            if folder == "":
                where.append("folder=''")
            else:
                where.append("(folder=? OR folder LIKE ?)")
                args += [folder, folder + "/%"]
        if difficulty is not None:
            where.append("difficulty=?")
            args.append(difficulty)
        if dp is not None:
            where.append("dp=?")
            args.append(dp)
        if keys is not None:
            where.append("keys=?")
            args.append(keys)
        ws = " AND ".join(where)

        total = self.conn.execute(
            f"SELECT COUNT(DISTINCT folder) FROM charts WHERE {ws}", args).fetchone()[0]
        col = {"title": "title COLLATE NOCASE", "artist": "artist COLLATE NOCASE",
               "folder": "folder", "charts": "n"}.get(order, "title COLLATE NOCASE")
        rows = self.conn.execute(
            f"""SELECT folder,
                       MAX(NULLIF(title, '')), MAX(NULLIF(artist, '')), MAX(NULLIF(genre, '')),
                       COUNT(*), MAX(difficulty), MAX(level),
                       MAX(keys), MAX(dp), MAX(NULLIF(bpm, '')),
                       MIN(path),
                       MAX(length_ms), MAX(mean_nps), MAX(peak_nps), MAX(rec_level)
                FROM charts WHERE {ws}
                GROUP BY folder
                ORDER BY {col} LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
        # COUNT(*) 的别名给排序用不了，单独补一列
        return [r + (r[4],) for r in rows], total

    def stats_missing(self, paths: list[str]) -> list[str]:
        """这些路径里还没统计过的"""
        if not paths:
            return []
        q = ",".join("?" * len(paths))
        sql = ("SELECT path FROM charts WHERE path IN (" + q + ") "
               "AND (stats_at IS NULL OR stats_at='')")
        return [r[0] for r in self.conn.execute(sql, list(paths))]

    def chart_paths(self, root: str, folder: str) -> list[str]:
        """这个文件夹里所有谱面的绝对路径（算「本机有成绩的谱面数」用）"""
        return [r[0] for r in self.conn.execute(
            "SELECT path FROM charts WHERE root=? AND folder=?", (str(root), folder))]

    def chart_list(self, root: str, folder: str) -> list[tuple]:
        """某个文件夹里的所有谱面：(name, level, difficulty, keys, dp,
        length_ms, mean_nps, peak_nps, rec_level)"""
        return self.conn.execute(
            """SELECT name, level, difficulty, keys, dp,
                      length_ms, mean_nps, peak_nps, rec_level, subartist
               FROM charts WHERE root=? AND folder=? ORDER BY difficulty, name""",
            (str(root), folder)).fetchall()

    def stats_pending(self, root: str, text: str = "", folder: str | None = None,
                      difficulty: int | None = None, dp: int | None = None,
                      keys: int | None = None) -> list[str]:
        """还没统计过的谱面路径（可带当前筛选条件，避免一上来就统计全库）"""
        where = ["root=?", "(stats_at IS NULL OR stats_at='')"]
        args: list = [str(root)]
        if text:
            like = f"%{text}%"
            where.append("(title LIKE ? OR artist LIKE ? OR genre LIKE ? OR name LIKE ?)")
            args += [like, like, like, like]
        if folder is not None:
            if folder == "":
                where.append("folder=''")
            else:
                where.append("(folder=? OR folder LIKE ?)")
                args += [folder, folder + "/%"]
        if difficulty is not None:
            where.append("difficulty=?")
            args.append(difficulty)
        if dp is not None:
            where.append("dp=?")
            args.append(dp)
        if keys is not None:
            where.append("keys=?")
            args.append(keys)
        rows = self.conn.execute(
            f"SELECT path FROM charts WHERE {' AND '.join(where)}", args).fetchall()
        return [r[0] for r in rows]

    def stats_count(self, root: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM charts WHERE root=? AND stats_at IS NOT NULL AND stats_at<>''",
            (str(root),)).fetchone()[0]

    def save_stats(self, rows: list[dict]) -> int:
        """把统计结果写回索引（按 path 更新）"""
        import datetime as _dt
        now = _dt.datetime.now().isoformat(timespec="seconds")
        n = 0
        for r in rows:
            if not r.get("ok"):
                continue
            cur = self.conn.execute(
                "UPDATE charts SET length_ms=?, mean_nps=?, peak_nps=?, rec_level=?,"
                " stats_at=? WHERE path=?",
                (r.get("length_ms"), r.get("mean_nps"), r.get("peak_nps"),
                 r.get("rec_level"), now, r["path"]))
            n += max(0, cur.rowcount)          # 只算真正写进去的行
        self.conn.commit()
        return n

    def query(self, root: str, text: str = "", folder: str | None = None,
              difficulty: int | None = None, dp: int | None = None,
              keys: int | None = None, level_min: int | None = None,
              level_max: int | None = None,
              order: str = "title", limit: int = 100, offset: int = 0):
        where = ["root=?"]
        args: list = [str(root)]
        if text:
            like = f"%{text}%"
            where.append("(title LIKE ? OR artist LIKE ? OR genre LIKE ? OR name LIKE ?)")
            args += [like, like, like, like]
        if folder is not None:
            if folder == "":
                where.append("folder=''")
            else:
                where.append("(folder=? OR folder LIKE ?)")
                args += [folder, folder + "/%"]
        if difficulty is not None:
            where.append("difficulty=?")
            args.append(difficulty)
        if dp is not None:
            where.append("dp=?")
            args.append(dp)
        if keys is not None:
            where.append("keys=?")
            args.append(keys)
        if level_min is not None:
            where.append("level>=?")
            args.append(level_min)
        if level_max is not None:
            where.append("level<=?")
            args.append(level_max)
        ws = " AND ".join(where)
        col = {"title": "title COLLATE NOCASE", "artist": "artist COLLATE NOCASE",
               "level": "level", "folder": "folder"}.get(order, "title COLLATE NOCASE")
        total = self.conn.execute(f"SELECT COUNT(*) FROM charts WHERE {ws}",
                                  args).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT path, folder, name, title, artist, genre, level, difficulty,"
            f" dp, keys, bpm, stagefile, size, mtime,"
              f" length_ms, mean_nps, peak_nps, rec_level FROM charts WHERE {ws}"
            f" ORDER BY {col}, name LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return rows, total

    def clear_stats_before(self, paths: list[str]) -> int:
        """这些文件重新入库前，先把旧统计清掉（内容变了，旧数据不准）"""
        n = 0
        for pth in paths:
            cur = self.conn.execute(
                "UPDATE charts SET stats_at=NULL, length_ms=NULL, mean_nps=NULL,"
                " peak_nps=NULL, rec_level=NULL WHERE path=?", (pth,))
            n += cur.rowcount
        self.conn.commit()
        return n

    def close(self):
        self.closed = True
        try:
            self.conn.close()
        except Exception:                                      # noqa: BLE001
            pass
