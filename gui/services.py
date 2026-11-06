"""GUI 的服务层：纯函数，不依赖界面框架

新增（2026-10-24）：
  · 自动从 spicetools.xml 推断 IIDX 游戏目录
  · 5K/5B 谱面识别（不参与自动难度分配，列表置底）
  · 本机 BMS 成绩（beatoraja 的 score.db，按谱面 sha256 匹配，显示评级+分数）
  · 转换前的封面缩略图预览（用官方边框模板生成）
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import time

if getattr(sys, "frozen", False):          # PyInstaller 打包后：以 exe 所在目录为根
    ROOT = pathlib.Path(sys.executable).resolve().parent
else:
    ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import bms as bms_mod                 # noqa: E402
from core import plan as plan_mod               # noqa: E402
from core import convert as convert_mod         # noqa: E402
from core.musicdb import MusicDB                # noqa: E402

MANIFEST_NAME = "bms2iidx_deployed.json"
# 真实槽号（与 .1 目录一致）：0=HYPER 1=NORMAL 2=ANOTHER 3=BEGINNER 4=LEGGENDARIA
# DP 依次 +6：6=HYPER 7=NORMAL 8=ANOTHER 9=BEGINNER 10=LEGGENDARIA
SLOT_NAMES = {0: "SP HYPER", 1: "SP NORMAL", 2: "SP ANOTHER", 3: "SP BEGINNER",
              4: "SP LEGGENDARIA", 6: "DP HYPER", 7: "DP NORMAL", 8: "DP ANOTHER",
              9: "DP BEGINNER", 10: "DP LEGGENDARIA"}
# BMS #DIFFICULTY → 槽号：2=NORMAL 3=HYPER 4=ANOTHER 5=LEGGENDARIA（BEGINNER 不导入）
SLOT_BY_KIND = {2: 1, 3: 0, 4: 2, 5: 4}
from .help_text import ABOUT_PAGE, HELP_PAGE        # noqa: E402
# ------------------------------------------------------------------ 环境探测
def find_game_dirs() -> list[dict]:
    """辣椒配置里只有读卡器路径，不是游戏目录，所以这里恒为空。

    保留函数是为了兼容调用方；真正的目录由用户在界面上选择/拖入，
    并记在程序设置里（QSettings），下次自动带出。
    """
    return []


def detect_game_dir() -> str:
    """默认选中最近启动过的那个安装"""
    found = find_game_dirs()
    return found[0]["path"] if found else ""


def _find_beatoraja() -> pathlib.Path | None:
    """只在**显式指定**的地方找，绝不扫盘：

    1. 环境变量 BMS2IIDX_BEATORAJA（用户或我们自己设置的路径）
    2. 上一次在界面上选过的路径（存在 QSettings 里，由调用方传进来）

    两个都没有就返回 None —— 成绩列留空，不做任何猜测。
    """
    import os
    env = os.environ.get("BMS2IIDX_BEATORAJA")
    if env:
        p = pathlib.Path(env)
        if (p / "player").is_dir() and (p / "songdata.db").exists():
            return p
    return None


_RANK_STEPS = [(88.89, "AAA"), (77.78, "AA"), (66.67, "A"), (55.56, "B"),
               (44.44, "C"), (33.33, "D"), (22.22, "E")]


def _rank_of(rate: float) -> str:
    for th, name in _RANK_STEPS:
        if rate >= th:
            return name
    return "F"


class PlayerScores:
    """beatoraja 成绩（只读打开，绝不影响对方数据）"""

    def __init__(self, root: str | pathlib.Path | None = None):
        if root:
            r = pathlib.Path(root)
            self.root = r if (r / "player").is_dir() else None
            if self.root is None:
                self.error = f"这个目录不像 beatoraja：{root}"
        else:
            self.root = _find_beatoraja()
        self.songs: dict[str, dict] = {}      # sha256 → 曲目信息
        self.scores: dict[str, dict] = {}     # sha256 → 最佳成绩
        self.error = getattr(self, "error", "") or ""
        if not self.root:
            if not self.error:
                self.error = "未设置"
            return
        try:
            self._load_songs()
            self._load_scores()
        except Exception as e:                                 # noqa: BLE001
            self.error = f"{e}"

    def _load_songs(self):
        con = sqlite3.connect(f"file:{self.root/'songdata.db'}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT sha256, title, subtitle, level, difficulty FROM song")
            for sha, title, sub, level, diff in cur.fetchall():
                self.songs[sha] = {"title": title, "subtitle": sub,
                                   "level": level, "difficulty": diff}
        finally:
            con.close()

    def _load_scores(self):
        dbs = sorted((self.root / "player").glob("*/score.db"))
        for db in dbs:
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                cur = con.cursor()
                cur.execute("""SELECT sha256, mode, clear, epg, lpg, egr, lgr,
                                      notes, combo, minbp, playcount, date
                               FROM score""")
                for (sha, mode, clear, epg, lpg, egr, lgr,
                     notes, combo, minbp, playcount, date) in cur.fetchall():
                    if not notes:
                        continue
                    got = 2 * (epg or 0) + (egr or 0) + 2 * (lpg or 0) + (lgr or 0)
                    maxp = notes * 2
                    rate = min(100.0, round(got / maxp * 100, 2)) if maxp else 0.0
                    prev = self.scores.get(sha)
                    if prev and prev["score"] >= got:
                        continue
                    self.scores[sha] = {
                        "score": got, "max": maxp, "rate": rate, "rank": _rank_of(rate),
                        "clear": clear, "playcount": playcount or 0,
                        "notes": notes,
                    }
                con.close()
            except Exception:                                  # noqa: BLE001
                continue

    def lookup(self, chart_path: pathlib.Path) -> dict | None:
        if not self.scores:
            return None
        try:
            sha = hashlib.sha256(chart_path.read_bytes()).hexdigest()
        except OSError:
            return None
        return self.scores.get(sha)


_player: PlayerScores | None = None
_player_root: str | None = None


def set_player_root(root: str | None):
    """由界面调用：显式指定成绩库目录（空字符串 = 不启用）"""
    global _player, _player_root
    _player_root = root or None
    _player = PlayerScores(_player_root) if _player_root else None


def player() -> PlayerScores:
    global _player
    if _player is None:
        _player = PlayerScores(_player_root)
    return _player


# ------------------------------------------------------------------ 源侧
def _lanes_of(chart) -> dict:
    """判定键位布局：SP 标准 = 7键+1皿；DP 标准 = 14键+2皿"""
    chans = chart.header.get("_channels", set()) or set()
    p1 = {c for c in ("11", "12", "13", "14", "15", "18", "19", "16") if c in chans}
    p2 = {c for c in ("21", "22", "23", "24", "25", "28", "29", "26") if c in chans}
    keys1 = len(p1 & {"11", "12", "13", "14", "15", "18", "19"})
    keys2 = len(p2 & {"21", "22", "23", "24", "25", "28", "29"})
    scr1 = "16" in p1
    scr2 = "26" in p2
    return {
        "p1_channels": sorted(p1),
        "p2_channels": sorted(p2),
        "keys1": keys1, "keys2": keys2, "scratch1": scr1, "scratch2": scr2,
        "has_7key": keys1 >= 7,
        "is_dp": keys2 > 0,
        # 标准 SP：7 键 + 1 皿，且没有 P2 侧
        "standard_sp": keys1 == 7 and scr1 and keys2 == 0,
        # 标准 DP：两侧各 7 键 + 各 1 皿
        "standard_dp": keys1 == 7 and scr1 and keys2 == 7 and scr2,
        "five_key": 0 < keys1 < 7 and not scr2,
    }


def chart_detail(path: pathlib.Path) -> dict:
    try:
        c = bms_mod.parse(path)
    except Exception:                                          # noqa: BLE001
        return {}
    h = c.header
    layers: dict[int, int] = {}
    layer_images: dict[int, list[str]] = {}
    for e in c.bga:
        layers[e.layer] = layers.get(e.layer, 0) + 1
        nm = c.bmp.get(e.value)
        if nm:
            layer_images.setdefault(e.layer, [])
            if nm not in layer_images[e.layer]:
                layer_images[e.layer].append(nm)
    poor = [n for n in layer_images.get(1, []) + layer_images.get(2, [])
            if "miss" in n.lower()]
    # 判定要和转换流程一致：必须磁盘上真有这张图，否则等于没有坏判层
    poor_file = ""
    for name in poor:
        cand = pathlib.Path(path).parent / name.replace("\\", "/")
        if cand.exists():
            poor_file = str(cand)
            break
    lanes = _lanes_of(c)
    return {
        "subtitle": h.get("SUBTITLE", ""),
        "subartist": h.get("SUBARTIST", ""),
        "maker": h.get("MAKER", ""),
        "stagefile": h.get("STAGEFILE", ""),
        "banner": h.get("BANNER", ""),
        "backbmp": h.get("BACKBMP", ""),
        "bga_counts": layers,
        "bga_images": (layer_images.get(0) or [])[:3],
        "poor_images": poor[:4],
        "poor_file": poor_file,
        "has_poor_layer": bool(poor_file),
        "five_key": lanes["five_key"],
        "keys1": lanes["keys1"], "keys2": lanes["keys2"],
        "scratch1": lanes["scratch1"], "scratch2": lanes["scratch2"],
        "dp_lanes": lanes["is_dp"],
        "standard_sp": lanes["standard_sp"],
        "standard_dp": lanes["standard_dp"],
        "lanes": lanes["p1_channels"],
        "score": player().lookup(path),
        "header_keys": sorted(k for k in h if not k.startswith("_")),
    }


def _slot_int(name) -> int | None:
    """plan 的 auto_slot 是槽名（'HYPER' 等），转成数字；空返回 None"""
    if not name:
        return None
    s = str(name).strip().upper()
    for k, v in SLOT_NAMES.items():
        if v.replace("SP ", "").replace("DP ", "").upper() == s:
            return k
    return None


def scan_folder(folder: str, progress=None) -> dict:
    f = pathlib.Path(folder)
    if not f.is_dir():
        raise ValueError(f"不是文件夹：{folder}")
    plan = plan_mod.scan(f)
    charts = []
    for c in plan.charts:
        d = chart_detail(f / c.file) if c.file else {}
        five = bool(d.get("five_key"))
        charts.append({
            "file": c.file,
            "kind": c.kind,
            "dp": bool(c.dp),
            "author_level": c.author_level,
            "suggest_level": c.suggest_level,
            "notes": c.notes,
            "keys": c.keys,
            "ln": c.ln,
            "seconds": round(c.seconds, 1),
            "nps": round(c.nps, 2),
            "peak_nps": round(c.peak_nps, 2),
            "bpm": c.bpm,
            "rejected": list(c.rejected),
            # 5K 谱：不参与自动分配
            "auto_slot": None if five else _slot_int(c.auto_slot),
            "kind_slot": None if five else SLOT_BY_KIND.get(c.kind),
            **d,
        })
    # 7K 在前（按难度、等级），5K 全部置底
    charts.sort(key=lambda x: (bool(x.get("five_key")),
                               x.get("kind_slot") is None,
                               x.get("kind") or 9,
                               -(x.get("notes") or 0)))
    p = player()
    return {
        "folder": str(f),
        "title": plan.title, "artist": plan.artist, "genre": plan.genre,
        "charts": charts,
        "needs_manual": plan.needs_manual,
        "overflow": plan.overflow,
        "warnings": list(plan.warnings),
        "player": {"found": bool(p.root), "root": str(p.root or ""),
                   "scores": len(p.scores), "songs": len(p.songs), "error": p.error},
    }


def thumbnail_preview(folder: str, stagefile: str, version: int = 34,
                      game_dir: str = "") -> str:
    """按目标代次生成封面预览（与转换时写进游戏的那张完全一致）

    · 34+ → movie_thumbnail 228×130
    · 32- → thumbnail 278×186
    模板优先用目标安装里现成的封面（带官方边框），没有就用内置默认。
    """
    if not stagefile:
        return ""
    src = pathlib.Path(folder) / stagefile.replace("\\", "/")
    if not src.exists():
        return ""
    from core import addons
    thumb_dir, thumb_size = addons.thumbnail_spec(version)

    template = None
    if game_dir:
        contents = _contents_of(pathlib.Path(game_dir))
        for base in (contents / "data_mods/omnimix/graphic",
                     contents / "data/graphic"):
            d = base / thumb_dir
            if d.is_dir():
                cands = sorted(d.glob("*.png"))
                if cands:
                    template = cands[0]
                    break

    out = ROOT / "out/gui_preview" / f"thumb_{thumb_size[0]}x{thumb_size[1]}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    addons.make_thumbnail(src, out, template, size=thumb_size)
    return str(out)


def frame_to_thumb(raw: bytes, version: int = 34, game_dir: str = "") -> str:
    """把一帧原始画面按封面规格处理（裁切+模板边框），返回临时 PNG 路径"""
    import tempfile
    from core import addons

    thumb_dir, size = addons.thumbnail_spec(version)
    src = pathlib.Path(tempfile.gettempdir()) / "bms2iidx_frame_src.png"
    src.write_bytes(raw)

    template = None
    if game_dir:
        contents = _contents_of(pathlib.Path(game_dir))
        for base in (contents / "data_mods/omnimix/graphic", contents / "data/graphic"):
            d = base / thumb_dir
            if d.is_dir():
                cands = sorted(d.glob("*.png"))
                if cands:
                    template = cands[0]
                    break
    out = pathlib.Path(tempfile.gettempdir()) / f"bms2iidx_frame_thumb_{size[0]}.png"
    addons.make_thumbnail(src, out, template, size=size)
    return str(out)


def frame_candidates(folder: str, chart: dict, count: int = 12) -> list[bytes]:
    """从该谱面的 BGA 视频随机抽若干帧，返回 PNG 字节（复杂度从高到低）"""
    from core import thumb

    root = pathlib.Path(folder)
    raw = chart.get("bga_images") or []
    bmps = list(raw.values()) if isinstance(raw, dict) else list(raw)
    vids = [root / str(b).replace("\\", "/") for b in bmps
            if str(b).lower().endswith((".mpg", ".mpeg", ".mp4", ".avi", ".wmv", ".mkv"))]
    vids = [v for v in vids if v.exists()]
    if not vids:
        for ext in ("*.mpg", "*.mpeg", "*.mp4", "*.avi"):
            vids += sorted(root.glob(ext))
    if not vids:
        return []
    return thumb.sample_frames(vids[0], count=count)


# ------------------------------------------------------------------ 副标题（谱师署名）
def default_subtitle(charts: list[dict], picked_files: list[str],
                     prefix: str = "BMS: ", max_lines: int = 3) -> str:
    """按所选谱面的 #SUBARTIST 原文生成副标题

    规则：按谱面顺序收集、去重；每个谱面一行、前缀 BMS:；最多 3 行
    （34 代官方最多 3 行）；谱面自带的 "/" 原样保留。
    """
    by_file = {c.get("file"): c for c in charts}
    lines: list[str] = []
    for f in picked_files:
        sub = (by_file.get(f, {}).get("subartist") or "").strip()
        if not sub:
            continue
        for part in sub.replace("\r\n", "\n").split("\n"):
            part = part.strip()
            if part and f"{prefix}{part}" not in lines:
                lines.append(f"{prefix}{part}")
    return "\n".join(lines[:max_lines])


def subtitle_warning(text: str) -> str:
    """副标题里如果混进游戏字体不支持的字符（如简体中文），给个提示"""
    if not text:
        return ""
    bad = []
    for ch in text:
        if ch in "\n\r":
            continue
        try:
            ch.encode("shift_jis")
        except UnicodeEncodeError:
            bad.append(ch)
    if not bad:
        return ""
    uniq = "".join(dict.fromkeys(bad))
    return f"⚠ 有 {len(uniq)} 个字符可能画不出来：{uniq}（建议换成日文/英文）"


# ------------------------------------------------------------------ 游戏目录解析
def _contents_of(game_dir: pathlib.Path) -> pathlib.Path:
    """统一成 contents 目录；传游戏根目录也能自动补"""
    g = pathlib.Path(game_dir)
    if g.name.lower() == "contents":
        return g
    c = g / "contents"
    return c if c.is_dir() else g


def diagnose_layeredfs(game_dir: str) -> dict:
    """从游戏 log.txt 判断 layeredfs 到底做了什么。

    能看到：hook 版本 / 启动参数 / 识别到的 mod 目录 / 合并了哪些 XML /
    "Using <路径>"（真正从 data_mods 取过的文件）。

    关键判断：某个 Using 路径如果在 data/ 下也有同名文件，说明 layeredfs
    确实**覆盖**了游戏本体文件；一条 Using 都没有，说明这次启动没从 mod 读过文件
    （不能据此说“覆盖失败”，只能说“没发生”）。
    """
    contents = _contents_of(pathlib.Path(game_dir))
    log = contents / "log.txt"
    out = {"ok": False, "log": str(log), "error": "",
           "version": "", "options": "", "mod_dirs": [],
           "merged": [], "using": [], "verdict": "", "counts": {}}
    if not log.exists():
        out["error"] = "还没有 log.txt（先启动一次游戏）"
        return out
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        out["error"] = f"读不到日志：{e}"
        return out
    out["ok"] = True

    m = re.search(r"layeredfs: IFS layeredFS v([\d.]+) init", text)
    if m:
        out["version"] = m.group(1)
    m = re.search(r"layeredfs: Options: (.+)", text)
    if m:
        out["options"] = m.group(1).strip()

    idx = text.find("Detected mod folders:")
    if idx >= 0:
        for line in text[idx:idx + 1200].splitlines()[1:]:
            s = line.split("layeredfs:", 1)[-1].strip()
            if s.startswith("./data_mods/"):
                out["mod_dirs"].append(s[len("./data_mods/"):].strip("/"))
            elif s and out["mod_dirs"]:
                break

    merged = []
    for mm in re.finditer(r"layeredfs: Merging into (\./data_mods/[^\r\n]+)", text):
        pair = mm.group(1)
        if pair not in merged:
            merged.append(pair)
    out["merged"] = merged

    using = []
    for mm in re.finditer(r"layeredfs: Using ([^\r\n]+)", text):
        raw = mm.group(1).strip()
        if raw not in using:
            using.append(raw)
    rows = []
    for raw in using:
        rel = raw.replace("\\", "/")
        mod = ""
        for d in (out["mod_dirs"] or ["omnimix", "bms2iidx"]):
            tag = f"./data_mods/{d}/"
            if rel.startswith(tag):
                mod, rel = d, rel[len(tag):]
                break
        base = contents / "data" / rel
        rows.append({"raw": raw, "mod": mod, "rel": rel,
                     "overrides_base": base.exists()})
    out["using"] = rows

    out["counts"] = {
        "using": len(rows),
        "override": sum(1 for r in rows if r["overrides_base"]),
        "merged": len(merged),
    }
    if not out["version"]:
        out["verdict"] = "没看到 layeredfs 初始化：hook 没加载（或这次没启动游戏）"
    elif not rows:
        out["verdict"] = ("hook 已加载，但没有「Using」记录 —— "
                          "这次启动没从 data_mods 读过文件，不能据此判断覆盖能力")
    elif out["counts"]["override"]:
        out["verdict"] = (f"覆盖有效：{out['counts']['override']} 个文件从 mod 目录取到，"
                          "且本体里存在同名文件")
    else:
        out["verdict"] = ("只发生了「新增」（取到的文件本体里没有同名文件）；"
                          "要确认能否覆盖本体，得放一个同名文件再启动一次")
    return out


def read_env(game_dir: str) -> dict:
    """从 contents/log.txt 里读出运行环境（补丁层/文件层/钩子/挂载点/缩略图目录）"""
    contents = _contents_of(pathlib.Path(game_dir))
    log = contents / "log.txt"
    out = {"log": str(log), "log_exists": log.exists(), "hooks": [],
           "mod_folders": [], "omnifix": "", "layeredfs": "",
           "game_version": "", "thumbnail_dir": ""}
    if not log.exists():
        return out
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    m = re.search(r"omnifix[\s_/-]*v?(\d[\d.]*)", text, re.I)
    if m:
        out["omnifix"] = m.group(1)
    m = re.search(r"(?:ifs_)?layeredfs[\s_/-]*v?(\d[\d.]*)", text, re.I)
    if m:
        out["layeredfs"] = m.group(1)

    HOOKish = ("ifs_hook", "omnifix", "altfix", "spice", "d3d9", "dxgi", "winmm")
    for dll in re.findall(r"([A-Za-z0-9_.-]+\.dll)", text):
        low = dll.lower()
        if not any(k in low for k in HOOKish):
            continue
        if low not in out["hooks"]:
            out["hooks"].append(low)

    for f in re.findall(r"data_mods[/\\]([A-Za-z0-9_.-]+)[/\\]", text):
        if f.startswith("_") or f == "_cache":
            continue
        if f not in out["mod_folders"]:
            out["mod_folders"].append(f)

    # ---- 启动参数段：spice2x 会把实际参数逐行打出来 ----
    out["launch_args"] = []
    m = re.search(r"arguments:\s*\n(.*?)(?:\n\[|\Z)", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line.startswith("-"):
                out["launch_args"].append(line)

    # ---- 实际加载成功的 hook（比参数更可靠）----
    out["loaded_hooks"] = []
    for name in re.findall(r"loading hook DLL ([A-Za-z0-9_.-]+)", text, re.I):
        low = name.lower()
        if low not in out["loaded_hooks"]:
            out["loaded_hooks"].append(low)

    # ---- layeredfs 实际用到的 mod 目录（取第一个非 _ 的）----
    used = re.findall(r"layeredfs: Using \./data_mods/([A-Za-z0-9_.-]+)/", text)
    out["mod_dir"] = next((u for u in used if not u.startswith("_")), "")

    hooks_all = out["loaded_hooks"] or out["hooks"]
    out["ifs_hook_loaded"] = any("ifs_hook" in h for h in hooks_all)
    out["omnifix_loaded"] = any("omnifix" in h for h in hooks_all)

    # ---- 部署目标：由"有没有 omnifix"决定文件名，由"实际 mod 目录"决定目录 ----
    #   omnifix 存在  → 游戏读 music_omni.bin，目标沿用 omni 目录（现状，已验证）
    #   omnifix 不存在 → 游戏读 music_data.bin，目标是我们自己的 bms2iidx 目录
    _has_omni_db = any(
        (contents / f"data_mods/omnimix/info/{s}/music_omni.bin").exists()
        for s in (0, 1)) or (contents / "data/info/0/music_omni.bin").exists()
    if out["omnifix_loaded"] and _has_omni_db:
        out["music_db_name"] = "music_omni.bin"
        out["deploy_mod_dir"] = out["mod_dir"] or "omnimix"
        out["deploy_mode"] = "omni"
    else:
        # 注意：只要日志里能确认 omnifix 没加载，就按原版处理，
        # 即使 data_mods/omnimix 还在（那种情况游戏读的是 music_data.bin）
        out["music_db_name"] = "music_data.bin"
        out["deploy_mod_dir"] = "bms2iidx"
        out["deploy_mode"] = "vanilla"

    m = re.search(r"LDJ:[A-Z0-9:]+", text)
    if m:
        out["game_version"] = m.group(0)
    if "movie_thumbnail/" in text:
        out["thumbnail_dir"] = "graphic/movie_thumbnail"
    elif "graphic/thumbnail/" in text:
        out["thumbnail_dir"] = "graphic/thumbnail"
    return out


def id_block(db_path: str) -> dict:
    """当前代的 ID 号段与已用量"""
    from core import reapply

    raw = _db_plain(db_path)
    ver, avail, unk, slots = reapply._header(raw)
    lo, hi = ver * 1000, ver * 1000 + 999
    have = reapply.ids_in_db(raw)
    used = [s for s in have if lo <= s <= hi]
    free = [s for s in reapply.free_slots(raw, lo, min(hi, slots - 1))]
    return {"version": ver, "block_lo": lo, "block_hi": hi,
            "next_id": max(free) if free else None,
            "count": len(have), "slot_total": slots, "used_in_block": len(used)}


# ------------------------------------------------------------------ 曲库读取
def _db_plain(path) -> bytes:
    """读曲库为明文（omnimix 有些包把前 64 字节异或过）"""
    from core import dbmask
    raw, _masked = dbmask.read_db(path)
    return raw


def _read_db_ids(path: pathlib.Path) -> dict[int, str]:
    """曲库里 {歌曲ID: 标题}（按版本布局读，旧代也支持）"""
    from core.musicdb import MusicDB
    try:
        db = MusicDB(path, read_only_ok=True)
    except Exception:                                          # noqa: BLE001
        return {}
    return {int(e["song_id"]): (e.get("title") or "") for e in db.entries}


def _block_usage(db_path: pathlib.Path) -> dict:
    """本代号段占用：只有「槽号 == 记录里的 ID」才算真占用（按版本布局读）"""
    from core import reapply
    b = _db_plain(db_path)
    lay, _avail, slots, _base = reapply.layout_of(b)
    ids = set(reapply.ids_in_db(b))
    lo = lay.ver * 1000
    hi = min(lay.ver * 1000 + 999, slots - 1)
    used = [s for s in range(lo, hi + 1) if s in ids]
    free = [s for s in range(lo, hi + 1) if s not in ids]
    return {"version": lay.ver, "slot_total": slots, "lo": lo, "hi": hi,
            "used": used, "free": free}


def id_usage(game_dir: str) -> dict:
    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    if not db:
        return {"ok": False, "error": "找不到曲库文件"}
    u = _block_usage(db)
    return {"ok": True, "version": u["version"], "slot_total": u["slot_total"],
            "block_lo": u["lo"], "block_hi": u["hi"],
            "block_used": len(u["used"]), "block_free": len(u["free"]),
            "next_free_id": max(u["free"]) if u["free"] else None}


# ------------------------------------------------------------------ 目标检测
def probe_target(game_dir: str) -> dict:
    """读目标安装：曲库、环境、号段、部署记录、问题清单"""
    contents = _contents_of(pathlib.Path(game_dir))
    omni = contents / "data_mods/omnimix"
    _dbinfo = _game_db_info(contents)
    db = _dbinfo.get("path")
    env = read_env(str(contents))
    out = {
        "game_dir": str(contents.parent if contents.name.lower() == "contents"
                        else contents),
        "contents": str(contents),
        "omnimix_dir": str(omni),
        "omnimix_installed": omni.is_dir(),
        "db_path": str(db) if db else "",
        "db_exists": bool(db and db.exists()),
        "db_size": db.stat().st_size if db and db.exists() else 0,
        "db_version": None, "db_slots": None, "db_songs": None,
        "env": env, "ids": {}, "manifest": None, "deployed": [],
        "problems": [],
    }
    if not db:
        for b in _dbinfo.get("bad") or []:
            out["problems"].append(
                f"曲库文件格式不对：{b.get('rel')}（{b.get('bad')}，"
                f"前 8 字节 {b.get('head_hex', '?')}）")
        out["problems"].append("找不到可用的曲库文件（data_mods/omnimix 或 data/info/*）")
        return out

    raw = _db_plain(db)
    from core import reapply
    try:
        ver, avail, unk, slots = reapply._header(raw)
    except Exception as e:                                     # noqa: BLE001
        out["problems"].append(f"曲库文件读不出来：{e}")
        return out
    out["db_version"] = ver
    out["db_slots"] = slots
    out["deploy_mode"] = env.get("deploy_mode", "omni")
    out["deploy_mod_dir"] = env.get("deploy_mod_dir", "omnimix")
    out["music_db_name"] = env.get("music_db_name", "music_omni.bin")
    out["ifs_hook_loaded"] = env.get("ifs_hook_loaded", False)
    for b in _dbinfo.get("bad") or []:
        out["problems"].append(
            f"跳过格式不对的文件：{b.get('rel')}（{b.get('bad')}，"
            f"前 8 字节 {b.get('head_hex', '?')}）")
    if _dbinfo.get("kind") == "base" and any(
            b.get("kind") == "omni" for b in _dbinfo.get("bad") or []):
        out["problems"].append("omni 曲库读不出来，已退回本体曲库（只读基线）")
    _slotinfo = _game_db_info(contents)
    out["db_slot"] = _slotinfo.get("slot")
    out["db_rel"] = _slotinfo.get("rel")
    out["db_candidates"] = [
        {"rel": c["rel"], "version": c["version"], "avail": c["avail"]}
        for c in _slotinfo.get("candidates", [])
    ]
    out["db_songs"] = len(reapply.ids_in_db(raw))
    out["ids"] = id_block(str(db))

    man_path = omni / MANIFEST_NAME
    if man_path.exists():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            man = {}
        songs = man.get("songs") or []
        out["manifest"] = {"path": str(man_path), "count": len(songs),
                           "slot_total": man.get("slot_total"),
                           "updated_at": man.get("updated_at", "")}
        for s in songs:
            sid = int(s.get("id") or 0)
            files = _song_files(contents, sid)
            issues = []
            if not files["chart"]:
                issues.append("缺谱面")
            if not files["audio"]:
                issues.append("缺键音")
            out["deployed"].append({"id": sid, "title": s.get("title", ""),
                                    "ok": not issues, "issues": issues})
    return out


LIB_ROOT = pathlib.Path(os.environ.get("BMS2IIDX_LIB")
                        or (ROOT / "library"))


def _install_key(contents: pathlib.Path) -> str:
    """每套安装一个独立库目录，避免 32/34 互相污染"""
    norm = str(contents).replace("/", "\\").lower()
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    title = contents.parent.name if contents.name.lower() == "contents" else contents.name
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", title)[:32] or "install"
    return f"{safe}_{h}"


def lib_root_for(game_dir: str) -> pathlib.Path:
    return LIB_ROOT / _install_key(_contents_of(pathlib.Path(game_dir)))


def _contents_from_db(db_path: pathlib.Path) -> pathlib.Path:
    """从曲库路径反推 contents"""
    parts = list(pathlib.Path(db_path).parts)
    low = [x.lower() for x in parts]
    for marker in ("data_mods", "data"):
        if marker in low:
            return pathlib.Path(*parts[:low.index(marker)])
    return pathlib.Path(db_path).parent


def _songlib(game_dir: str):
    from core.songlib import SongLib
    return SongLib(lib_root_for(game_dir))


def _db_head(path: pathlib.Path) -> dict:
    """读曲库头：版本 / 曲目数 / 文件时间。

    不是 IIDX 格式（magic 不对、太短、读不了）时返回 {}，
    并把原因放在 bad 字段里，供界面提示（不再把坏文件当成"能读"）。
    """
    import struct as _struct
    try:
        from core import dbmask
        raw_all = path.read_bytes()
        raw16 = raw_all[:16]
        if len(raw16) >= 16 and raw16[:4] != b"IIDX" and dbmask.is_masked(raw_all):
            raw16 = dbmask.unmask(raw_all)[:16]
        with path.open("rb") as f:
            head = raw16
        if len(head) < 16:
            return {"bad": f"文件太短（{len(head)} 字节）",
                    "head_hex": head.hex(" "), "size": path.stat().st_size}
        if head[:4] != b"IIDX":
            return {"bad": "头部不是 IIDX", "head_hex": head[:8].hex(" "),
                    "size": path.stat().st_size}
        _sig, ver, avail, _unk, slots = _struct.unpack_from("<4sIHHI", head, 0)
        return {"version": ver, "avail": avail, "slots": slots,
                "mtime": path.stat().st_mtime, "size": path.stat().st_size}
    except Exception as e:                                     # noqa: BLE001
        return {"bad": f"读不了：{type(e).__name__}", "size": 0}


def info_slot_of(version: int) -> int:
    """官方规则：数据版本偶数放 info/0，奇数放 info/1（iidx-datatools 同款）"""
    return 0 if int(version) % 2 == 0 else 1


def _db_candidates(contents: pathlib.Path) -> list[dict]:
    """所有可能被游戏读取的曲库，按"更可能是当前代"排序"""
    out, bad = [], []
    for slot in (0, 1):
        for rel, kind in ((f"data_mods/omnimix/info/{slot}/music_omni.bin", "omni"),
                          (f"data/info/{slot}/music_data.bin", "base")):
            q = contents / rel
            if not q.exists():
                continue
            head = _db_head(q)
            if not head:
                continue
            item = {"path": q, "rel": rel, "kind": kind, "slot": slot, **head}
            if item.get("bad"):
                bad.append(item)
                continue
            out.append(item)
    # 排序：omni 优先（游戏读的是它）→ 版本高 → 曲目多 → 文件新
    out.sort(key=lambda d: (d["kind"] != "omni", -d["version"], -d["avail"], -d["mtime"]))
    for d in out:
        d["bad"] = None
    return out + bad


def _game_db_info(contents: pathlib.Path) -> dict:
    """当前应使用的曲库 + 其它候选（供界面显示）"""
    cands = _db_candidates(contents)
    if not cands:
        return {"path": None, "version": None, "slot": None, "kind": None,
                "rel": None, "candidates": [], "bad": []}
    good = [c for c in cands if not c.get("bad")]
    bad = [c for c in cands if c.get("bad")]
    if not good:
        return {"path": None, "version": None, "slot": None, "kind": None,
                "rel": None, "candidates": [], "bad": bad}
    best = good[0]
    return {"path": best["path"], "version": best["version"], "slot": best["slot"],
            "kind": best["kind"], "rel": best["rel"],
            "candidates": good[1:], "bad": bad}


def _game_db(contents: pathlib.Path) -> pathlib.Path | None:
    return _game_db_info(contents)["path"]


def _backup_mgr(game_dir: str):
    from core.backup import BackupManager
    contents = _contents_of(pathlib.Path(game_dir))
    return BackupManager(LIB_ROOT / "backups" / _install_key(contents), contents)


# ------------------------------------------------------------------ 备份
def backup_set_note(game_dir: str, backup_id: str, note: str) -> dict:
    """给某个备份写一句备注（存在它的 manifest.json 里）"""
    contents = _contents_of(pathlib.Path(game_dir))
    m = _backup_mgr(game_dir)
    root = m.root
    d = root / backup_id
    man = d / "manifest.json"
    if not man.exists():
        return {"ok": False, "error": "找不到这个备份"}
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": str(e)}
    data["note"] = str(note or "").strip()
    man.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


def backup_list(game_dir: str) -> list[dict]:
    return _backup_mgr(game_dir).list()


def backup_create(game_dir: str, label: str = "", progress=None) -> dict:
    return _backup_mgr(game_dir).snapshot(label=label, progress=progress)


def backup_restore(game_dir: str, backup_id: str, progress=None) -> dict:
    return _backup_mgr(game_dir).restore(backup_id, progress=progress)


def backup_delete(game_dir: str, backup_id: str) -> bool:
    return _backup_mgr(game_dir).delete(backup_id)


# ------------------------------------------------------------------ 文件核查
def _song_files(contents: pathlib.Path, sid: int) -> dict:
    """一首歌在磁盘上有哪些文件"""
    def find(bases, rel):
        for b in bases:
            q = b / rel
            if q.exists():
                return q
        return None

    sounds = [r / "sound" for r in _mod_roots(contents)] + [contents / "data/sound"]
    pad = f"{sid:05d}"                      # IIDX 一律 5 位补零：01999/01999.1
    names = (pad,) if pad == str(sid) else (pad, str(sid))   # 兼容早期不补零的部署
    sound = None
    for n in names:
        sound = find(sounds, n)
        if sound:
            break
    chart = None
    for n in names:
        chart = find(sounds, f"{n}/{n}.1")
        if chart:
            break
    audio = None
    if sound and sound.is_dir():
        cands = sorted(sound.glob(f"{pad}*.2dx")) or sorted(sound.glob(f"{sid}*.2dx"))
        audio = cands[0] if cands else None
    movie = None
    for ext in (".mp4", ".wmv"):
        movie = find([r / "movie" for r in _mod_roots(contents)]
                     + [contents / "data/movie"], f"{pad}{ext}")
        if movie:
            break
    thumb = None
    thumb_dirs = []
    for r in _mod_roots(contents):
        thumb_dirs += [r / "graphic/movie_thumbnail", r / "graphic/thumbnail"]
    thumb_dirs += [contents / "data/graphic/movie_thumbnail",
                   contents / "data/graphic/thumbnail"]
    for b in thumb_dirs:
        q = b / f"{pad}_thum.png"
        if q.exists():
            thumb = q
            break
    ifs = find([r / "graphic" for r in _mod_roots(contents)]
               + [contents / "data/graphic"], f"{pad}.ifs")
    return {"sound": sound, "chart": chart, "audio": audio, "movie": movie,
            "thumb": thumb, "ifs": ifs}


def library_status(game_dir: str) -> dict:
    """本机库 vs 当前曲库"""
    from core import reapply

    lib = _songlib(game_dir)
    songs = lib.songs()
    out = {"count": len(songs), "ids": sorted(songs), "missing_in_db": [],
           "incompatible": [], "present": [], "file_report": {},
           "db": "", "version": None, "lib_dir": str(lib.root)}
    if not songs:
        return out
    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    if not db:
        out["missing_in_db"] = sorted(songs)
        return out
    raw = db.read_bytes()
    ver = reapply._header(raw)[0]
    out["db"], out["version"] = str(db), ver
    have = reapply.ids_in_db(raw)

    report = {"sound_dir": [], "chart": [], "audio": [], "movie": [], "thumbnail": []}
    for sid in sorted(songs):
        meta = songs[sid]
        if meta.get("game_version") not in (None, ver):
            out["incompatible"].append(sid)
            continue
        if sid not in have:
            out["missing_in_db"].append(sid)
        f = _song_files(contents, sid)
        if not f["sound"]:
            report["sound_dir"].append(sid)
        else:
            if not f["chart"]:
                report["chart"].append(sid)
            if not f["audio"]:
                report["audio"].append(sid)
        if meta.get("bga") and not f["movie"]:
            report["movie"].append(sid)
        if not f["thumb"]:
            report["thumbnail"].append(sid)
        if sid not in out["missing_in_db"]:
            out["present"].append(sid)
    out["file_report"] = {k: v for k, v in report.items() if v}
    return out


# ------------------------------------------------------------------ 曲库全景
def _xml_titles(contents: pathlib.Path) -> dict[int, str]:
    """从包内 XML 反查曲名（残留歌用）"""
    out: dict[int, str] = {}
    info = _omni_info_dir(contents)
    for name in ("video_music_omni.xml", "video_music_list.xml"):
        q = info / name
        if not q.exists():
            continue
        try:
            txt = q.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        for m in re.finditer(
                r'<music id="(\d+)">\s*<info>\s*<title_name>(.*?)</title_name>',
                txt, re.S):
            out.setdefault(int(m.group(1)), m.group(2).strip())
        break
    return out


def _dir_size(p: pathlib.Path) -> int:
    if p is None:
        return 0
    p = pathlib.Path(p)
    if p.is_file():
        return p.stat().st_size
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return 0


def library_overview(game_dir: str) -> dict:
    """曲库全景：本程序部署 / KONAMI / omnimix / 可恢复残留 / 其它残留"""
    from core import reapply

    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    out = {"ok": False, "version": None, "db_songs": 0, "groups": [],
           "ours": [], "orphans": [], "recoverable": [], "others": [],
           "error": ""}
    if not db:
        out["error"] = "找不到曲库文件"
        return out

    raw = db.read_bytes()
    ver = reapply._header(raw)[0]
    ids = reapply.ids_in_db(raw)
    titles = _read_db_ids(db)
    base_db = _base_info_dir(contents) / "music_data.bin"
    base_ids = set(_read_db_ids(base_db)) if base_db.exists() else set()
    lib = _songlib(game_dir)
    ours_meta = lib.songs()

    def row(sid: int, title: str, source: str) -> dict:
        f = _song_files(contents, sid)
        missing = []
        if not f["chart"]:
            missing.append("chart")
        if not f["audio"]:
            missing.append("audio")
        meta = ours_meta.get(sid, {})
        if meta.get("bga") and not f["movie"]:
            missing.append("movie")
        if not f["thumb"]:
            missing.append("thumb")
        size = sum(_dir_size(f[k]) for k in ("sound", "movie", "thumb", "ifs"))
        return {"id": sid, "title": title, "source": source,
                "in_db": sid in ids, "missing": missing, "size": size,
                "files": {k: (str(v) if v else "") for k, v in f.items()}}

    man_ids: dict[int, str] = {}
    man_path = _mod_manifest_path(contents)
    if man_path.exists():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
            for s in man.get("songs", []):
                try:
                    man_ids[int(s["id"])] = s.get("title", "")
                except (KeyError, ValueError, TypeError):
                    continue
        except Exception:                                      # noqa: BLE001
            pass

    ours = []
    for sid in sorted(set(ours_meta) | set(man_ids)):
        meta = ours_meta.get(sid, {})
        item = row(sid, meta.get("title") or man_ids.get(sid, ""), "ours")
        item["has_library_record"] = sid in ours_meta
        ours.append(item)

    official, third = [], []
    for sid, title in sorted(titles.items()):
        if sid in ours_meta or sid in man_ids:
            continue
        grp = "official" if sid in base_ids else "omnimix"
        (official if grp == "official" else third).append(
            {"id": sid, "title": title, "source": grp, "in_db": True,
             "missing": [], "size": 0, "files": {}})

    xml_titles = _xml_titles(contents)
    orphans = []
    seen = set(ids) | set(ours_meta) | set(man_ids)
    for base in ([r / "sound" for r in _mod_roots(contents)]
                 + [contents / "data/sound"]):
        if not base.is_dir():
            continue
        for sub in base.iterdir():
            if not sub.is_dir() or not sub.name.isdigit():
                continue
            sid = int(sub.name)
            if sid in seen:
                continue
            seen.add(sid)
            orphans.append({"id": sid,
                            "title": ours_meta.get(sid, {}).get("title")
                            or xml_titles.get(sid) or "（曲名未知）",
                            "size": _dir_size(sub),
                            "has_record": lib.record(sid) is not None,
                            "from_xml": bool(xml_titles.get(sid))})
    scan_pairs = []
    for r in _mod_roots(contents):
        scan_pairs += [(r / "movie", "*.mp4", "（只有影片）"),
                       (r / "graphic/movie_thumbnail", "*_thum.png", "（只有封面）")]
    for base, pat, label in scan_pairs:
        if not base.is_dir():
            continue
        for f in base.glob(pat):
            stem = f.name.split(".")[0].replace("_thum", "")
            if not stem.isdigit():
                continue
            sid = int(stem)
            if sid in seen:
                continue
            seen.add(sid)
            orphans.append({"id": sid, "title": label, "size": f.stat().st_size,
                            "has_record": lib.record(sid) is not None,
                            "from_xml": False})

    recoverable = [o for o in orphans if o.get("has_record")]
    others = [o for o in orphans if not o.get("has_record")]
    out.update({
        "ok": True, "version": ver, "db_songs": len(ids),
        "groups": [
            {"key": "ours", "label": f"本程序部署（{len(ours)}）", "items": ours,
             "collapsed": False},
            {"key": "official", "label": f"KONAMI 本番（{len(official)}）",
             "items": official, "collapsed": True},
            {"key": "omnimix", "label": f"omnimix 追加（{len(third)}）",
             "items": third, "collapsed": True},
            {"key": "recoverable",
             "label": f"⚠ 本机库有记录、曲库里没有（{len(recoverable)}）",
             "items": recoverable, "collapsed": False},
            {"key": "others",
             "label": f"其它：目录里有、本机库无记录（{len(others)}）",
             "items": others, "collapsed": True},
        ],
        "ours": ours, "orphans": recoverable + others,
        "recoverable": recoverable, "others": others,
    })
    return out


# ------------------------------------------------------------------ 单曲操作
def reapply_songs(game_dir: str, progress=None) -> dict:
    """把本机库里的记录增量写回曲库，并重建 merged XML"""
    from core import reapply

    lib = _songlib(game_dir)
    songs = lib.songs()
    if not songs:
        return {"ok": True, "restored": {}, "count": 0, "note": "库里没有歌"}
    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    if not db:
        raise ValueError("找不到曲库文件")

    raw = db.read_bytes()
    ver = reapply._header(raw)[0]
    entries, skipped = [], []
    for sid in sorted(songs):
        rec = lib.record(sid)
        gv = songs[sid].get("game_version")
        if gv not in (None, ver):
            skipped.append((sid, gv))
            continue
        if rec:
            entries.append((sid, rec))
    if skipped and progress:
        progress(f"跳过 {len(skipped)} 首跨代记录：{skipped}")
    res = reapply.insert_many(db, entries, game_version=ver)
    _write_merged(contents, songs)
    if progress:
        progress(f"写回完成：{res['used']}")
    return {"ok": True, "restored": res, "count": len(entries), "version": ver,
            "skipped_version": skipped}


def reapply_one(game_dir: str, song_id: int, progress=None) -> dict:
    from core import reapply

    lib = _songlib(game_dir)
    rec = lib.record(song_id)
    if not rec:
        return {"ok": False, "error": "本机库里没有这首歌的记录"}
    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    if not db:
        return {"ok": False, "error": "找不到曲库文件"}
    ver = reapply._header(_db_plain(db))[0]
    res = reapply.insert_many(db, [(song_id, rec)], game_version=ver)
    _write_merged(contents, lib.songs())
    return {"ok": True, "used": res["used"]}


def _write_merged(contents: pathlib.Path, songs: dict):
    """重建三个 merged XML（layeredfs 运行时合并，不覆盖原始文件）"""
    from core import reapply

    info = _mod_info_dir(contents)
    info.mkdir(parents=True, exist_ok=True)
    vname = "video_music_omni" if (info / "video_music_omni.xml").exists() \
        else "video_music_list"
    vids = [{"id": sid, "title": s.get("title", ""), "artist": s.get("artist", ""),
             "flags": 6} for sid, s in songs.items() if s.get("bga")]
    if vids:
        (info / f"{vname}.merged.xml").write_bytes(reapply.build_merged_video(vids))
    for kind, field in (("title", "title"), ("artist", "artist")):
        base = (f"music_{kind}_omni" if (info / f"music_{kind}_omni.xml").exists()
                else f"music_{kind}_yomi")
        ents = [{"id": sid, "yomi": s.get(field, "")} for sid, s in songs.items()
                if s.get(field)]
        if ents:
            (info / f"{base}.merged.xml").write_bytes(reapply.build_merged_yomi(ents))


def delete_song_files(game_dir: str, song_id: int, progress=None) -> dict:
    """彻底删除这首歌的资源（目录递归删、文件 unlink）。

    和 trash_song 共用同一个「找文件」实现（song_file_paths），
    避免两边规则不一致——之前 delete_song_files 用 _song_files、
    trash_song 用 song_file_paths，就出过“删了记录却没删对文件”的 bug。
    """
    contents = _contents_of(pathlib.Path(game_dir))
    removed = []
    for q in song_file_paths(contents, song_id):
        try:
            rel = q.relative_to(contents).as_posix()
        except ValueError:
            rel = str(q)
        try:
            if q.is_dir():
                shutil.rmtree(q, ignore_errors=True)
            else:
                q.unlink(missing_ok=True)
            removed.append(rel)
        except OSError:
            continue
    return {"ok": True, "removed": removed}


def remove_song(game_dir: str, song_id: int, drop_library: bool = True,
                drop_files: bool = False, progress=None) -> dict:
    from core import reapply

    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    if not db:
        return {"ok": False, "error": "找不到曲库文件"}
    ok = False
    if drop_library:                                # ★ 只有要求删记录时才动曲库
        ok = reapply.remove_song(db, song_id)
    removed = []
    if drop_files:
        removed = delete_song_files(game_dir, song_id)["removed"]
    if drop_library:
        _songlib(game_dir).remove_song(song_id)
        _drop_from_manifest(contents, song_id)      # 部署清单也要同步
    try:
        _write_merged(contents, _songlib(game_dir).songs())
    except Exception:                                          # noqa: BLE001
        pass
    return {"ok": ok, "removed": removed}


def _drop_from_manifest(contents: pathlib.Path, song_id: int):
    """从部署清单里摘掉一首歌（清单只记我们装过什么，不参与备份）"""
    man_path = _mod_manifest_path(contents)
    if not man_path.exists():
        return
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return
    songs = [s for s in man.get("songs", []) if int(s.get("id", -1)) != int(song_id)]
    if len(songs) == len(man.get("songs", [])):
        return
    man["songs"] = songs
    man["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")


def orphan_preview(game_dir: str, song_id: int) -> dict:
    """给残留歌抽一帧影片画面，帮助认歌"""
    from core import thumb

    contents = _contents_of(pathlib.Path(game_dir))
    movie = None
    for base in (contents / "data_mods/omnimix/movie", contents / "data/movie"):
        q = base / f"{song_id}.mp4"
        if q.exists():
            movie = q
            break
    if movie is None:
        return {"ok": False, "error": "这首歌没有影片，无法抽帧"}
    frames = thumb.sample_frames(movie, count=6)
    if not frames:
        return {"ok": False, "error": "抽帧失败"}
    import tempfile
    out = pathlib.Path(tempfile.gettempdir()) / f"bms2iidx_orphan_{song_id}.png"
    out.write_bytes(frames[0])
    return {"ok": True, "path": str(out), "dir": str(movie.parent)}


# ------------------------------------------------------------------ 转换 / 部署
def convert_song(folder: str, db_path: str, staging: str, picks: list[dict],
                 origin_folder: str = "SUBSTREAM",
                 mod_dir: str = "omnimix",
                 music_db_name: str = "music_omni.bin",
                 font_index: int | None = None,
                 title: str | None = None, artist: str | None = None,
                 genre: str | None = None, song_id: int | None = None,
                 with_preview: bool = True, with_bga: bool = True,
                 audio_mode: str = "keysound", level_mode: str = "density",
                 id_top: int | None = None, subtitle: str = "",
                 lib_root: str | None = None, bake_poor: bool = False,
                 thumb_override: str | None = None, progress=None) -> dict:
    """转换一首歌到暂存区（不动游戏目录）"""
    st = pathlib.Path(staging)
    if st.exists():
        shutil.rmtree(st, ignore_errors=True)
    st.mkdir(parents=True, exist_ok=True)

    # 必须用数字槽号：槽名 "ANOTHER" 在 SP/DP 里都有，会串到 SP 段
    pick_spec = [f"{int(p['slot'])}={p['file']}" for p in picks]

    dbp = pathlib.Path(db_path)
    contents = _contents_from_db(dbp)
    meta_root = dbp.parents[2] if len(dbp.parents) > 2 else dbp.parent

    rep = convert_mod.convert(
        folder=pathlib.Path(folder), db_path=dbp, out_dir=st,
        song_id=song_id, title=title, artist=artist, genre=genre,
        with_preview=with_preview, audio_mode=audio_mode,
        level_mode=level_mode, pick_spec=pick_spec,
        bake_bga=with_bga, id_top=id_top, meta_root=meta_root,
        subtitle=subtitle or "",
        lib_root=pathlib.Path(lib_root) if lib_root else lib_root_for(str(contents)),
        bake_poor=bake_poor,
        origin_version=_origin_index(origin_folder),
        font_index=font_index,
        mod_dir=mod_dir,
        music_db_name=music_db_name,
        thumb_source=pathlib.Path(thumb_override) if thumb_override else None,
        progress=progress or (lambda *a, **k: None))
    out = rep.to_dict()
    if picks and all(int(x["slot"]) >= 6 for x in picks):
        out["warnings"].append(
            "只有 DP 谱面：曲库里 SP 等级会是 0，游戏 SP 模式下这首歌可能不可选"
            "（官方 840 个谱面里没有这种先例，属于未验证组合）")
    return {"report": out, "staging": str(st)}


def _already_deployed(db: pathlib.Path, song_id, staging: pathlib.Path,
                      contents: pathlib.Path) -> bool:
    """曲库里已经有这首歌、且记录与暂存区一致 → 视为重复部署，不报冲突"""
    if not song_id:
        return False
    from core import reapply

    def rec_of(raw: bytes) -> bytes | None:
        try:
            lay, _av, _sl, base = reapply.layout_of(raw)
            ids = reapply.ids_in_db(raw)
            idx = ids.get(int(song_id))
            if idx is None:
                return None
            off = base + idx * lay.rec
            return raw[off:off + lay.rec]
        except Exception:                                      # noqa: BLE001
            return None

    try:
        found = rec_of(_db_plain(db))
        if not found:
            return False
        for staged in staging.rglob("*.bin"):
            if staged.name not in ("music_omni.bin", "music_data.bin"):
                continue
            other = rec_of(_db_plain(staged))
            if other is not None:
                return other == found
        return False
    except Exception:                                          # noqa: BLE001
        return False


def stage_preview(staging: str) -> list[str]:
    st = pathlib.Path(staging)
    if not st.is_dir():
        return []
    return sorted(str(f.relative_to(st)).replace("\\", "/")
                  for f in st.rglob("*") if f.is_file())


def _mod_roots(contents: pathlib.Path) -> list[pathlib.Path]:
    """可能存放我们文件的 mod 目录，部署目录排最前。

    原版环境写在 data_mods/bms2iidx，omni 环境写在 data_mods/omnimix；
    剩下的都列上是为了兼容“装过又卸了 omni”这类历史状态。
    """
    deployed, _dbn = _deploy_target(contents)
    names = [deployed, "omnimix", "bms2iidx"]
    out = []
    for n in names:
        q = contents / "data_mods" / n
        if q not in out:
            out.append(q)
    return out


def _mod_manifest_path(contents: pathlib.Path) -> pathlib.Path:
    """部署清单放在“当前部署目录”下（原版 = data_mods/bms2iidx）"""
    deployed, _dbn = _deploy_target(contents)
    return contents / "data_mods" / deployed / MANIFEST_NAME


def _deploy_target(contents: pathlib.Path) -> tuple[str, str]:
    """返回 (mod 目录名, 曲库文件名)。

    优先级：
      1) 日志里实际加载了 omnifix → omni 模式（music_omni.bin，目录沿用日志里的 mod 名）
      2) 否则如果机器上确实存在 music_omni.bin（omni 内容在）→ 仍按 omni 处理
      3) 都没有 → 原版模式（music_data.bin，写到我们自己的 bms2iidx 目录）
    """
    env = read_env(str(contents))
    has_omni_db = any(
        (contents / f"data_mods/omnimix/info/{s}/music_omni.bin").exists()
        for s in (0, 1)) or (contents / "data/info/0/music_omni.bin").exists()
    if env.get("omnifix_loaded") and has_omni_db:
        return env.get("deploy_mod_dir") or "omnimix", "music_omni.bin"
    if env.get("omnifix_loaded") and not has_omni_db:
        # 日志是上一次启动留下的（omni 目录被改名/删掉）→ 不能写 omni 库
        return "bms2iidx", "music_data.bin"

    # 日志能读到 hook 列表时，以“omnifix 有没有加载”为准：
    # 只关掉 omnifix、omni 目录还在的情况（模拟原版）也必须走 music_data.bin，
    # 否则会出现“程序写 omni 库、游戏读本体库”，部署完看不到歌。
    hooks_known = bool(env.get("loaded_hooks") or env.get("hooks"))
    if hooks_known:
        return "bms2iidx", "music_data.bin"

    # 读不到日志（还没启动过游戏）时才退回“看文件在不在”
    has_omni = any((contents / f"data_mods/omnimix/info/{s}/music_omni.bin").exists()
                   for s in (0, 1)) or (contents / "data/info/0/music_omni.bin").exists()
    if has_omni:
        return "omnimix", "music_omni.bin"
    return "bms2iidx", "music_data.bin"


def deploy(staging: str, game_dir: str, report: dict | None = None,
           force: bool = False, progress=None) -> dict:
    """部署到游戏目录：先校验包是否被换过，再自动备份，最后复制"""
    st = pathlib.Path(staging)
    contents = _contents_of(pathlib.Path(game_dir))
    deployed_mod, _dbn = _deploy_target(contents)
    base = st / "data_mods" / deployed_mod
    if not base.is_dir():                       # 兼容旧的暂存区
        base = st / "data_mods/omnimix"
    if not base.is_dir():
        raise ValueError("暂存区里没有转换产物")

    expect = (report or {}).get("base_db_sha256") or ""
    db = _game_db(contents)
    if expect and db and db.exists() and not force:
        now = hashlib.sha256(_db_plain(db)).hexdigest()
        already = _already_deployed(db, (report or {}).get("song_id"),
                                   st, contents)
        if now != expect and not already:
            return {"ok": False, "conflict": {
                "reason": "曲库在转换之后被改过（多半是 omni 包更新了）",
                "expected": expect, "actual": now, "db": str(db)}}

    try:
        bk = backup_create(str(contents), label="部署前自动备份")
        if progress:
            progress(f"已自动备份：{bk['id']}（{len(bk['files'])} 个文件）")
    except Exception as e:                                     # noqa: BLE001
        if progress:
            progress(f"自动备份失败（继续部署）：{e}")

    _dbi = _game_db_info(contents)
    slot_target = info_slot_of(_dbi["version"]) if _dbi.get("version") else 0
    copied = []
    for src in sorted(base.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(st)
        # 暂存区固定写在 info/0；如果目标库是奇数代（在 info/1），这里换槽
        rel_posix = rel.as_posix()
        target_posix = rel_posix
        if slot_target != 0 and "info/0/" in rel_posix:
            target_posix = rel_posix.replace("info/0/", f"info/{slot_target}/")
        if target_posix != rel_posix:
            rel = pathlib.PurePosixPath(target_posix)
            dst = contents.joinpath(*rel.parts)
        else:
            dst = contents / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        base_same = contents / "data" / pathlib.PurePosixPath(target_posix).relative_to(
            pathlib.PurePosixPath(f"data_mods/{deployed_mod}")) \
            if target_posix.startswith(f"data_mods/{deployed_mod}/") else None
        shutil.copy2(src, dst)
        copied.append(str(rel).replace("\\", "/"))
        if base_same is not None and base_same.exists() and progress:
            # 2026-11-06 实测：覆盖同名文件是否生效取决于“读取路径”——
            #   34 的 mp4 走 hook（覆盖有效）；wmv 播放绕过 hook（覆盖无效）。
            progress(f"⚠ 覆盖本体已有文件：{base_same.relative_to(contents)}"
                     "（能不能生效取决于游戏怎么读这个文件）")
        if progress:
            progress(f"复制 {rel}")

    man_path = _mod_manifest_path(contents)
    man = {"version": 1, "songs": []}
    if man_path.exists():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            pass
    sid = (report or {}).get("song_id")
    if sid:
        man["songs"] = [s for s in man.get("songs", []) if int(s["id"]) != int(sid)]
        man["songs"].append({
            "id": int(sid), "title": (report or {}).get("title", ""),
            "files": copied,
            "deployed_at": datetime.datetime.now().isoformat(timespec="seconds")})
    if db:
        try:
            mdb = MusicDB(db)
            man["slot_total"] = getattr(mdb, "slot_total", None)
            man["db_version"] = mdb.version
        except Exception:                                      # noqa: BLE001
            pass
    man["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "copied": copied, "manifest": str(man_path), "song_id": sid}


def deploy_one(staging: str, game_dir: str, song_id: int, progress=None) -> dict:
    """只部署一首（队列里"部署选中"用）。

    与 deploy() 的区别：**不用暂存区那份整库覆盖游戏曲库**，而是
      1) 从暂存区复制这首歌的资源（sound/<id>/、graphic/<id>.ifs、
         graphic/movie_thumbnail/<id>_thum.png、movie/<id>.*）
      2) 用 reapply 把它的一条记录并入游戏曲库（保留现有其它歌）
      3) 重建三个 merged XML
    """
    st = pathlib.Path(staging)
    contents = _contents_of(pathlib.Path(game_dir))
    deployed_mod, _dbn = _deploy_target(contents)
    base = st / "data_mods" / deployed_mod
    if not base.is_dir():
        base = st / "data_mods/omnimix"
    if not base.is_dir():
        return {"ok": False, "error": "暂存区里没有转换产物"}

    sid = int(song_id)
    pat = f"{sid:05d}"
    copied = []
    for src in sorted(base.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(st)
        posix = rel.as_posix()
        parts = rel.parts
        keep = (
            # rel 形如 data_mods/omnimix/<area>/…，area 在 parts[2]
            (len(parts) >= 5 and parts[2] == "sound" and parts[3] == pat)          # sound/<id>/…
            or (len(parts) == 4 and parts[2] == "graphic" and parts[3] == f"{pat}.ifs")
            or (len(parts) >= 5 and parts[2] == "graphic" and parts[3] == "movie_thumbnail"
                and parts[4].startswith(f"{pat}_"))
            or (len(parts) >= 4 and parts[2] == "movie" and parts[3].startswith(pat))
        )
        if not keep:
            continue
        dst = contents.joinpath(*parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(posix)
        if progress:
            progress(f"复制 {posix}")

    res = reapply_one(str(contents), sid)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "写回曲库失败"), "copied": copied}
    return {"ok": True, "song_id": sid, "copied": copied, "used": res.get("used")}


# ------------------------------------------------------------------ spice2x 配置
SPICE2X_GAME = "Beatmania IIDX"


def spice2x_config_path(contents: pathlib.Path) -> pathlib.Path:
    """spice2x 配置文件位置：-cfgpath > 便携(spicetools.xml 在 exe 旁) > %APPDATA%"""
    env = read_env(str(contents))
    for arg in env.get("launch_args") or []:
        m = re.match(r"-cfgpath\s+(.+)$", arg, re.I)
        if m:
            return pathlib.Path(m.group(1).strip().strip('"'))
    exe_dir = contents if (contents / "spice64.exe").exists() else contents
    portable = exe_dir / "spicetools.xml"
    if portable.exists():
        return portable
    return pathlib.Path(os.environ.get("APPDATA", "")) / "spicetools.xml"


def _spice2x_section(text: str) -> tuple[int, int] | None:
    """返回 IIDX 段在文本里的 [起, 止) 偏移"""
    m = re.search(r'<game name="' + re.escape(SPICE2X_GAME) + r'"', text)
    if not m:
        return None
    start = m.start()
    nxt = re.search(r'<game name="', text[start + 1:])
    end = start + 1 + nxt.start() if nxt else len(text)
    return start, end


def spice2x_hook_state(contents: pathlib.Path) -> dict:
    """读配置里 IIDX 段的 k 值，以及它和"日志里实际生效的 hooks"是否一致"""
    path = spice2x_config_path(contents)
    env = read_env(str(contents))
    out = {"path": str(path), "exists": path.exists(), "value": "",
           "has_ifs_hook": False, "ifs_hook_first": False,
           "config_driven": False, "loaded": bool(env.get("ifs_hook_loaded"))}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8", errors="replace")
    sec = _spice2x_section(text)
    if not sec:
        return out
    seg = text[sec[0]:sec[1]]
    m = re.search(r'<option name="k" value="([^"]*)"', seg)
    val = (m.group(1) if m else "").split()
    out["value"] = " ".join(val)
    low = [v.lower() for v in val]
    out["has_ifs_hook"] = any("ifs_hook" in v for v in low)
    out["ifs_hook_first"] = bool(low) and "ifs_hook" in low[0]
    out["config_driven"] = out["has_ifs_hook"] or (out["loaded"] and not
                                                   any("-k " in a for a in env.get("launch_args") or []))
    return out


def _spice2x_running() -> list[str]:
    """spice2x / SpiceCfg 是否在运行（在运行绝不能改配置，退出时会回写覆盖）"""
    import subprocess
    names = []
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or b"").decode("mbcs", errors="replace")
        for line in out.splitlines():
            low = line.lower()
            for key in ("spice64.exe", "spice.exe", "spicecfg.exe"):
                if f'"{key}"' in low or f",{key}," in low:
                    names.append(key)
    except Exception:                                          # noqa: BLE001
        pass
    return sorted(set(names))


def spice2x_ensure_hook(contents: pathlib.Path, dll: str = "ifs_hook.dll") -> dict:
    """把 dll 放到 IIDX 段 k 的最前面（去重、保留其它项），改前备份"""
    path = spice2x_config_path(contents)
    st = spice2x_hook_state(contents)
    if not path.exists():
        return {"ok": False, "error": f"找不到 spice2x 配置文件：{path}"}
    running = _spice2x_running()
    if running:
        return {"ok": False, "error": f"spice2x 正在运行（{', '.join(running)}），"
                                     f"请先关掉再改配置，否则退出时会被覆盖"}

    text = path.read_text(encoding="utf-8")
    sec = _spice2x_section(text)
    if not sec:
        return {"ok": False, "error": f"配置里没有 {SPICE2X_GAME} 段"}
    seg = text[sec[0]:sec[1]]
    m = re.search(r'(<option name="k" value=")([^"]*)(")', seg)
    before = m.group(2) if m else ""
    items = [x for x in before.split() if "ifs_hook" not in x.lower()]
    new_items = [dll] + items
    new_val = " ".join(new_items)
    if m:
        new_seg = seg[:m.start()] + m.group(1) + new_val + m.group(3) + seg[m.end():]
    else:
        new_seg = seg.replace("</options>",
                              f'<option name="k" value="{new_val}"/></options>', 1)
    if new_seg == seg:
        if re.search(r'<option name="k" value="[^"]*"', seg):
            return {"ok": True, "unchanged": True, "path": str(path),
                    "before": before, "after": new_val}
        return {"ok": False, "error": "没找到 <options> 段，未改动"}

    backup = path.with_name(path.name + f".bms2iidx-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(path, backup)
    path.write_text(text[:sec[0]] + new_seg + text[sec[1]:], encoding="utf-8")
    return {"ok": True, "path": str(path), "before": before, "after": new_val,
            "backup": str(backup)}


# ------------------------------------------------------------------ ifs_hook 安装
HOOK_DLL_64 = "ifs_hook.dll"
HOOK_DLL_32 = "ifs_hook_32bit.dll"


def bundled_hook_dir() -> pathlib.Path:
    """随包的 ifs_hook 目录（打包后在 _MEIPASS/vendor/ifs_hook）"""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).parent))
        return base / "vendor" / "ifs_hook"
    return pathlib.Path(__file__).resolve().parent.parent / "vendor" / "ifs_hook"


def hook_installed(contents: pathlib.Path) -> bool:
    return (contents / "modules" / HOOK_DLL_64).exists() or \
           (contents / "modules" / "ifs_hook.dll").exists()


def install_ifshook(contents: pathlib.Path) -> dict:
    """把包内的 ifs_hook.dll 复制到 contents/modules/（已存在则不覆盖）"""
    src_dir = bundled_hook_dir()
    dst_dir = contents / "modules"
    dst = dst_dir / HOOK_DLL_64
    if dst.exists():
        return {"ok": True, "unchanged": True, "path": str(dst),
                "size": dst.stat().st_size}
    src = src_dir / HOOK_DLL_64
    if not src.exists():
        return {"ok": False, "error": f"包里没有 {HOOK_DLL_64}（{src_dir}）"}
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"ok": True, "path": str(dst), "size": dst.stat().st_size,
            "version": "4.1", "source": str(src)}


# ------------------------------------------------------------------ 回收区（删除前移走）
TRASH_DIRNAME = "bms2iidx_trash"


def trash_root(contents: pathlib.Path) -> pathlib.Path:
    """回收区：和 contents 同级（方便整体还原/清理，也不进游戏加载路径）"""
    return contents.parent / TRASH_DIRNAME


def song_file_paths(contents: pathlib.Path, song_id: int, bga: str = "") -> list[pathlib.Path]:
    """这首歌在游戏目录里可能占用的资源（存在的才返回）"""
    sid = f"{int(song_id):05d}"
    out: list[pathlib.Path] = []
    for rel in (f"sound/{sid}", f"sound/{sid}.ifs",
                f"graphic/{sid}.ifs",
                f"graphic/movie_thumbnail/{sid}_thum.png",
                f"graphic/thumbnail/{sid}.png"):
        for r in _mod_roots(contents):
            q = r / rel
            if q.exists() and q not in out:
                out.append(q)
    for rel in (f"data/sound/{sid}", f"data/sound/{sid}.ifs",
                f"data/graphic/{sid}.ifs",
                f"data/graphic/movie_thumbnail/{sid}_thum.png",
                f"data/graphic/thumbnail/{sid}.png"):
        q = contents / rel
        if q.exists() and q not in out:
            out.append(q)
    # 影片按曲库 bga 字段
    names = {bga, str(int(song_id)), sid}
    movie_dirs = [contents / "data/movie"] + [r / "movie" for r in _mod_roots(contents)]
    for d in movie_dirs:
        if not d.is_dir():
            continue
        for ptrn in names:
            if not ptrn:
                continue
            for ext in (".mp4", ".wmv", ".m4v"):
                q = d / f"{ptrn}{ext}"
                if q.exists():
                    out.append(q)
    # omni 目录里的同名资源也算
    for rel in (f"data_mods/omnimix/sound/{sid}",
                f"data_mods/omnimix/graphic/{sid}.ifs"):
        q = contents / rel
        if q.exists():
            out.append(q)
    return out


def trash_song(game_dir: str, song_id: int, drop_record: bool = True,
               drop_files: bool = True, progress=None) -> dict:
    """删除一首歌：资源移到回收区、可选从曲库删记录。

    移动目标：<contents 同级>/bms2iidx_trash/<时间戳>_<id>_<动作>/<原相对路径>
    并写 manifest.json（记录原路径），便于一键还原。
    """
    from core import reapply

    contents = _contents_of(pathlib.Path(game_dir))
    db = _game_db(contents)
    if not db:
        return {"ok": False, "error": "找不到曲库文件"}
    raw = db.read_bytes()
    ids = reapply.ids_in_db(raw)
    sid = int(song_id)
    # 取 bga 名（用于找影片）
    bga = ""
    title = ""
    lay, _av, _sl, base = reapply.layout_of(raw)
    rec_bytes = b""
    if sid in ids:
        off = base + ids[sid] * lay.rec
        rec_bytes = raw[off:off + lay.rec]
        if lay.ver >= 32:
            title = rec_bytes[0:0x100].decode("utf-16-le", "ignore").split("\x00")[0]
            bga = rec_bytes[0x690:0x6B0].split(b"\x00")[0].decode("cp932", "ignore").strip()
        else:
            title = rec_bytes[0:0x40].decode("cp932", "ignore").split("\x00")[0]
            bga = ""                    # 旧代 bga 字段位置未验证，改名靠 5 位补零找片

    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "all" if (drop_record and drop_files) else ("record" if drop_record else "files")
    target = trash_root(contents) / f"{stamp}_{sid:05d}_{tag}"
    target.mkdir(parents=True, exist_ok=True)     # ★ 必须先建目录：
    #   这首歌没有可移动资源时（例如测试歌/文件已丢），下面写 record.bin 会 FileNotFoundError
    moved: list[dict] = []
    if drop_files:
        for src in song_file_paths(contents, sid, bga):
            rel = src.relative_to(contents)
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append({"rel": rel.as_posix(), "size": dst.stat().st_size})
            if progress:
                progress(f"移动 {rel.as_posix()}")

    removed_record = False
    if drop_record and sid in ids:
        removed_record = reapply.remove_song(db, sid)
        if removed_record:
            _drop_from_manifest(contents, sid)
            try:
                _write_merged(contents, _songlib(game_dir).songs())
            except Exception:                                  # noqa: BLE001
                pass

    if rec_bytes:
        (target / "record.bin").write_bytes(rec_bytes)
    man = {"song_id": sid, "title": title, "bga": bga, "at": stamp,
           "record_removed": removed_record, "record_saved": bool(rec_bytes),
           "files": moved}
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "trash": str(target), "files": len(moved),
            "record_removed": removed_record, "title": title}


def trash_list(contents: pathlib.Path) -> list[dict]:
    """回收区里的条目"""
    root = trash_root(contents)
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        man = {}
        mf = d / "manifest.json"
        if mf.exists():
            try:
                man = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:                                  # noqa: BLE001
                man = {}
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        out.append({"name": d.name, "dir": str(d), "size": size,
                    "song_id": man.get("song_id"), "title": man.get("title") or "",
                    "record_removed": man.get("record_removed"),
                    "files": len(man.get("files") or [])})
    return out


def trash_purge(contents: pathlib.Path, name: str) -> dict:
    """彻底删除回收区里的某一条（不可还原）。只允许删回收区内部的目录。"""
    root = trash_root(contents).resolve()
    target = (root / name).resolve()
    if root not in target.parents:
        return {"ok": False, "error": "路径不在回收区里，拒绝删除"}
    if not target.is_dir():
        return {"ok": False, "error": f"回收区里没有 {name}"}
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    try:
        shutil.rmtree(target)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "freed": size}


def trash_songs(game_dir: str, song_ids, drop_record: bool = True,
                drop_files: bool = True, progress=None) -> dict:
    """批量删歌（逐首走 trash_song）。返回成功/失败明细。"""
    ok, failed = [], []
    ids = [int(x) for x in song_ids]
    for i, sid in enumerate(ids, 1):
        if progress:
            progress(f"[PROGRESS {int(i * 100 / max(1, len(ids)))}] 正在删除 {sid}（{i}/{len(ids)}）")
        try:
            r = trash_song(game_dir, sid, drop_record, drop_files)
        except Exception as e:                                 # noqa: BLE001
            failed.append((sid, str(e)))
            continue
        if r.get("ok"):
            ok.append(sid)
        else:
            failed.append((sid, r.get("error") or "未知错误"))
    return {"ok": not failed, "done": len(ok), "ids": ok, "failed": failed}


def purge_songs(game_dir: str, song_ids, drop_record: bool = True,
                drop_files: bool = True, progress=None) -> dict:
    """批量彻底删除（不进回收区）。drop_record / drop_files 可单独开关。"""
    ok, failed = [], []
    ids = [int(x) for x in song_ids]
    for i, sid in enumerate(ids, 1):
        if progress:
            progress(f"[PROGRESS {int(i * 100 / max(1, len(ids)))}] 正在彻底删除 {sid}（{i}/{len(ids)}）")
        try:
            r = remove_song(game_dir, sid, drop_record, drop_files)
        except Exception as e:                                 # noqa: BLE001
            failed.append((sid, str(e)))
            continue
        if r:
            ok.append(sid)
        else:
            failed.append((sid, "这首歌不在曲库里，且没有可删文件"))
    return {"ok": not failed, "done": len(ok), "ids": ok, "failed": failed}


def trash_restore(contents: pathlib.Path, name: str, progress=None) -> dict:
    """把回收区里某个条目按原路径搬回去（已存在则不覆盖）"""
    root = trash_root(contents)
    src_dir = root / name
    if not src_dir.is_dir():
        return {"ok": False, "error": f"回收区里没有 {name}"}
    mf = src_dir / "manifest.json"
    man = {}
    if mf.exists():
        try:
            man = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            man = {}
    restored, skipped = [], []
    for item in man.get("files") or []:
        rel = item.get("rel")
        if not rel:
            continue
        src = src_dir / rel
        dst = contents / rel
        if not src.exists():
            continue
        if dst.exists():
            skipped.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        restored.append(rel)
        if progress:
            progress(f"还原 {rel}")
    record_restored = False
    rec_file = src_dir / "record.bin"
    sid = man.get("song_id")
    if rec_file.exists() and sid:
        from core import reapply
        db = _game_db(contents)
        if db:
            raw = db.read_bytes()
            ver = reapply._header(raw)[0]
            try:
                # 必须写回原槽号（= 歌曲 ID）：insert_many 只会挑当前号段的空槽，
                # 老曲会被挪到 34xxx，槽号就错位了
                raw_new, _idx = reapply.insert_record(
                    raw, rec_file.read_bytes(), int(sid), ver)
                db.write_bytes(raw_new)
                record_restored = True
                try:
                    _write_merged(contents, _songlib(str(contents.parent)).songs())
                except Exception:                              # noqa: BLE001
                    pass
            except Exception as e:                             # noqa: BLE001
                return {"ok": False, "error": f"写回曲库失败：{e}",
                        "restored": restored}
    return {"ok": True, "restored": restored, "skipped": skipped,
            "record_removed": man.get("record_removed"),
            "record_restored": record_restored, "song_id": sid}


def _origin_index(name) -> int:
    """ORIGIN 分类名（或索引）→ 合法索引；不认识就用默认 SUBSTREAM"""
    from core.musicdb import ORIGIN_DEFAULT, origin_index
    try:
        return origin_index(name)
    except ValueError:
        return ORIGIN_DEFAULT


def _active_slot(contents: pathlib.Path) -> int:
    """当前代曲库所在槽（0 或 1）。读不到就按 0 处理。"""
    import inspect
    if inspect.stack()[1].function in ("_omni_info_dir", "_base_info_dir"):
        pass
    info = _game_db_info(contents)
    if info.get("version"):
        return info_slot_of(info["version"])
    return 0


def _mod_info_dir(contents: pathlib.Path) -> pathlib.Path:
    """当前部署目录的 info/<槽>；原版环境（bms2iidx）也能用"""
    deployed, _dbn = _deploy_target(contents)
    slot = _active_slot(contents)
    p = contents / "data_mods" / deployed / "info" / str(slot)
    if not p.is_dir():
        alt = contents / "data_mods" / deployed / "info" / "0"
        if alt.is_dir():
            return alt
    return p


def _omni_info_dir(contents: pathlib.Path) -> pathlib.Path:
    """data_mods/omnimix/info/<槽>；该槽不存在时退回 0"""
    slot = _active_slot(contents)
    p = contents / "data_mods/omnimix/info" / str(slot)
    return p if p.is_dir() else contents / "data_mods/omnimix/info/0"


def _base_info_dir(contents: pathlib.Path) -> pathlib.Path:
    """data/info/<槽>；该槽不存在时退回 0"""
    slot = _active_slot(contents)
    p = contents / "data/info" / str(slot)
    return p if p.is_dir() else contents / "data/info/0"
