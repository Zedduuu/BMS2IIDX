"""BMS to IIDX omnimix song package: orchestration layer.

input : one BMS song folder + a music_omni.bin (read only)
output: <out>/data_mods/omnimix/sound/<ID>/ and .../info/0/music_omni.bin + report

audio models (audio_mode):
  keysound : one container entry per sample (legacy IIDX keysound model)
  stream   : whole song mixed into ONE stream, notes silenced (modern IIDX model)
  hybrid   : stream carries BGM channel only, notes keep their own keysounds
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, bms, chart1, level, mix as mixer, par, timeline
from . import assets, cli_log
from .musicdb import MusicDB, allocate_song_id, slot_total_for

CHART_EXTS = (".bms", ".bme", ".bml", ".bmx")
PMS_EXTS = (".pms",)

NAME_KEYWORDS = [
    (re.compile(r"(leg|leggendaria|spl)", re.I), 5),
    (re.compile(r"(another|_a$|\[a\]|extreme|insane|maniac|black|hardest)", re.I), 4),
    (re.compile(r"(hyper|_h$|\[h\]|hard|ex)", re.I), 3),
    (re.compile(r"(normal|_n$|\[n\]|easy|nov)", re.I), 2),
    (re.compile(r"(beginner|_b$|begin)", re.I), 1),
]


@dataclass
class DifficultyPick:
    path: Path
    slot: int
    label: str
    level: int              # 最终写进曲库的等级（1-12）
    kind: int
    reason: str
    author_level: int = 0   # BMS #PLAYLEVEL 原文（可能 >12 或 0）
    suggest_level: int = 0  # 按物量密度推荐的等级（1-12）
    suggest_cont: float = 0.0   # 推荐连续值（如 7.38）
    density: float = 0.0    # 综合密度指标
    notes: int = 0
    seconds: float = 0.0


@dataclass
class Report:
    folder: str = ""
    title: str = ""
    artist: str = ""
    genre: str = ""
    song_id: int = 0
    difficulties: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    audio: dict = field(default_factory=dict)
    output: str = ""
    base_db_sha256: str = ""      # 转换时目标曲库的哈希（部署前用来判断包是否被换过）
    ok: bool = False

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["difficulties"] = [
            {"file": p.path.name, "slot": p.slot, "label": p.label,
             "level": p.level, "reason": p.reason,
             "author_level": p.author_level, "suggest_level": p.suggest_level,
             "suggest_cont": p.suggest_cont, "density": p.density,
             "notes": p.notes, "seconds": p.seconds} for p in self.difficulties
        ]
        return d


def scan_folder(folder: Path):
    rep = Report(folder=str(folder))
    charts = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        low = p.suffix.lower()
        if low in PMS_EXTS:
            rep.rejected.append(f"{p.name}: PMS (9 keys) not supported")
            continue
        if low not in CHART_EXTS:
            continue
        try:
            c = bms.parse(p)
        except Exception as e:                                     # noqa: BLE001
            rep.rejected.append(f"{p.name}: parse failed {e}")
            continue
        hard = [r for r in c.rejected if "DP" in r or "RANDOM" in r or "\u968f\u673a" in r]
        if hard:
            rep.rejected.append(f"{p.name}: {'/'.join(sorted(set(hard)))}")
            continue
        if not c.notes:
            rep.rejected.append(f"{p.name}: no notes")
            continue
        charts.append(c)
    return charts, rep


def _guess_kind(c):
    raw = (c.header.get("DIFFICULTY") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 5:
        return int(raw)
    for rx, kind in NAME_KEYWORDS:
        if rx.search(c.path.stem):
            return kind
    return None


def _level(c) -> int:
    raw = (c.header.get("PLAYLEVEL") or "").strip()
    m = re.search(r"\d+", raw)
    lv = int(m.group()) if m else 0
    return max(1, min(12, lv)) if lv else 1


def pick_difficulties(charts, rep, level_mode: str = "density"):
    """把一首歌的所有谱面挑进 IIDX 的 5 个难度槽。

    BMS 的 #DIFFICULTY：1=BEGINNER 2=NORMAL 3=HYPER 4=ANOTHER 5=INSANE(≡LEGGENDARIA)
    IIDX 槽：0=SPH 1=SPN 2=SPA 3=SPB 4=SPL（文件头偏移 0x00/0x08/0x10/0x18/0x20）
    ★ BEGINNER（kind 1）按用户要求**不导入**；INSANE（kind 5）导入 **LEGGENDARIA**。
    """
    scored = [(_guess_kind(c), _level(c), c) for c in charts]
    by_kind = {}
    for kind, lv, c in scored:
        if kind is None:
            continue
        cur = by_kind.get(kind)
        if cur is None or len(c.notes) > len(cur.notes):
            by_kind[kind] = c
    picks = []
    if by_kind:
        for kind, slot in ((3, chart1.SLOT_SPH), (2, chart1.SLOT_SPN), (4, chart1.SLOT_SPA),
                           (5, chart1.SLOT_SPL)):
            if kind in by_kind:
                c = by_kind[kind]
                picks.append(DifficultyPick(c.path, slot, chart1.SLOT_NAMES[slot],
                                            _level(c), kind, f"#DIFFICULTY={kind}"))
        extra = [k for k in by_kind if k not in (2, 3, 4, 5)]
        if extra:
            rep.warnings.append(f"unused difficulty kinds: {extra}（BEGINNER 按约定不导入）")
    else:
        ordered = sorted(scored, key=lambda t: t[1])
        n = len(ordered)
        if n == 1:
            kind, lv, c = ordered[0]
            picks.append(DifficultyPick(c.path, chart1.SLOT_SPH, "HYPER", lv, 3,
                                        "single chart -> HYPER"))
        else:
            take = ordered[:3]
            slots = {1: [chart1.SLOT_SPN], 2: [chart1.SLOT_SPN, chart1.SLOT_SPA],
                     3: [chart1.SLOT_SPN, chart1.SLOT_SPH, chart1.SLOT_SPA]}[len(take)]
            labels = {chart1.SLOT_SPN: "NORMAL", chart1.SLOT_SPH: "HYPER",
                      chart1.SLOT_SPA: "ANOTHER"}
            for (_kind, lv, c), slot in zip(take, slots):
                picks.append(DifficultyPick(c.path, slot, labels[slot], lv, 0,
                                            "ordered by #PLAYLEVEL"))
        rep.warnings.append("no #DIFFICULTY, difficulties inferred from levels")
    _attach_density(picks, level_mode)
    return picks


def _attach_density(picks, level_mode: str = "density"):
    """给每个难度补上"按物量密度推荐的等级"，并按 level_mode 决定最终等级。"""
    for p in picks:
        try:
            ch = bms.parse(p.path)
            tc = timeline.build(ch)
        except Exception:
            continue
        if not tc.notes:
            continue
        notes = len(tc.notes)                       # 与 .1 的 0x10 同口径：普通音 + 长按×1
        sec = max(0.1, tc.total_ms / 1000.0)
        ticks = [n.tick for n in tc.notes]
        rec, cont, dens = level.recommend(notes, sec, ticks)
        p.notes, p.seconds = notes, round(sec, 1)
        p.author_level = p.level
        p.suggest_level, p.suggest_cont, p.density = rec, cont, dens
        if level_mode == "density":
            p.level = max(1, min(12, int(rec) or 1))     # 兜底：曲库不接受空/0
        else:
            p.level = max(1, min(12, p.author_level or rec))


def _link_or_copy(src: Path, dst: Path, mode: str):
    """把音频容器放到新歌目录：硬链接（省空间）或复制（默认，最稳）。"""
    import os
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    if mode == "hardlink":
        try:
            os.link(src, dst)
            return "hardlink"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copy"


def _probe_task(args) -> tuple[int, float]:
    """模块级 worker：探测一个采样的时长（并行用）。"""
    old, path = args
    from pathlib import Path as _P
    return old, _probe_seconds(_P(path))


def _probe_seconds(f: Path) -> float:
    """探测采样时长（秒）。任何失败都返回 0.0，绝不中断转换。

    ffprobe 优先（输出干净），再退回 ffmpeg 的 Duration 行；stdout/stderr 都可能为 None。
    """
    cmds = (
        (["ffprobe", "-v", "error", "-show_entries", "format=duration",
          "-of", "default=nw=1:nk=1", str(f)], "float"),
        (["ffmpeg", "-hide_banner", "-i", str(f), "-f", "null", "-"], "duration"),
    )
    for cmd, mode in cmds:
        try:
            r = cli_log.run(cmd, capture_output=True, text=True)
        except OSError:
            break
        out = (getattr(r, "stdout", None) or "") + "\n" + (getattr(r, "stderr", None) or "")
        if mode == "float":
            for line in out.splitlines():
                line = line.strip()
                if line and line.replace(".", "", 1).isdigit():
                    return float(line)
        else:
            m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0.0


def _warn_glyphs(rep, subtitle: str):
    """标题/副标题里有没有 IIDX 字体显示不了的字（方块/空白）"""
    try:
        from . import romanize as _rom
        if (subtitle or "").strip():
            bad = _rom.unrenderable(subtitle)
            if bad:
                rep.warnings.append(
                    f"副标题里有 {len(bad)} 个 IIDX 字体没有的字（例：{''.join(bad[:4])}），"
                    "游戏里会显示成空白/省略号；建议只保留日文或英文")
        why = _rom.iidx_unsafe_reason(rep.title)
        if why:
            sug, _r = _rom.suggest_title(rep.title)
            rep.warnings.append(
                f"标题在 IIDX 里可能显示成方块：{why}"
                + (f"；建议改成：{sug}" if sug else "；建议改成英文名"))
    except Exception:                                          # noqa: BLE001
        pass


def _build_poor_layer(rep, src_chart, folder: Path, song_id: int, mod_root: Path,
                      info_db_path: Path, meta_root, lib_root, progress):
    """坏判层：从原曲 #BMP 里找 miss 图，借现成图形包模板生成 graphic/<id>.ifs"""
    try:
        from . import poor as poor_mod
        names = [n for n in (src_chart.bmp.get(e.value) for e in src_chart.bga
                             if e.layer in (1, 2)) if n and "miss" in n.lower()]
        miss_src = None
        for n in names:
            q = folder / n.replace("\\", "/")
            if q.exists():
                miss_src = q
                break
        if miss_src is None:
            rep.warnings.append("坏判层：这首歌没有 miss 图，跳过（界面会显示无坏判层）")
            return
        lib = Path(lib_root) if lib_root else Path.cwd() / "library"
        info = poor_mod.find_template(
            Path(meta_root).parent.parent if meta_root else folder,
            lib, progress=lambda m: progress(f"  {m}"))
        if not info:
            rep.warnings.append("坏判层：找不到可用的图形包模板，跳过")
            return
        out_ifs = mod_root / "graphic" / f"{song_id:05d}.ifs"
        poor_mod.build_pack(Path(info["path"]), miss_src, out_ifs,
                            progress=lambda m: progress(f"  {m}"))
        ok = poor_mod.set_layer_flags(info_db_path, song_id, 2, "00")
        rep.warnings.append(
            f"坏判层：已用原曲 miss 图生成 {out_ifs.name}"
            + ("，记录开关已打开" if ok else "，但记录开关写入失败"))
    except Exception as e:                                     # noqa: BLE001
        rep.warnings.append(f"坏判层生成失败：{str(e)[:120]}")


def _write_side_files(meta_root: Path, out_dir: Path, song_id: int, title: str,
                      artist: str, subtitle: str, genre: str, level_map: dict,
                      picking: list[dict], source_folder: str, bga: bool,
                      stagefile: str, chart_dir: Path, with_video: bool,
                      db_path: Path, lib_root: Path, video: Path | None = None,
                      thumb_source: Path | None = None,
                      fallback_bmp: str = "",
                      mod_dir: str = "omnimix",
                      progress=print) -> list[str]:
    """写：我们自己的库记录 + 三个 merged XML + 选曲封面（全部在暂存区）"""
    from . import addons, reapply, songlib

    root = meta_root                      # …/data_mods/omnimix 或 …/contents/data
    stage_root = out_dir / "data_mods" / mod_dir
    notes: list[str] = []

    lib = songlib.SongLib(lib_root)
    # ---- 1) 从刚写好的曲库里取出这首歌的原始记录，存进我们自己的库 ----
    from . import dbmask
    raw, _masked = dbmask.read_db(db_path)
    lay, avail, slots, base = reapply.layout_of(raw)
    ver = lay.ver
    rec_bytes = None
    for i in range(avail):
        off = base + i * lay.rec
        if off + lay.rec > len(raw):
            break
        if int.from_bytes(raw[off + lay.song_id_off: off + lay.song_id_off + 4], "little") == song_id:
            rec_bytes = raw[off: off + lay.rec]
            break
    if rec_bytes:
        lib.save_song(song_id, rec_bytes, {
            "id": song_id, "title": title, "artist": artist,
            "subtitle": subtitle, "genre": genre,
            "levels": level_map, "picks": picking,
            "source_folder": source_folder, "bga": bga,
            "game_version": ver,
            "base_db_sha256": _sha256_of(db_path),
            "written_at": _now(),
        })
        notes.append(f"记录已存入本机库（{song_id}.rec）")

    # ---- 2) 三个 merged XML：用库里所有歌重建，交给 layeredfs 合并 ----
    songs = lib.songs()

    def _slot_has(name: str) -> Path | None:
        """在 info/0、info/1 里找第一个存在的同名文件（官方 XML 按版本槽放）"""
        for slot in (0, 1):
            q = root / f"info/{slot}/{name}"
            if q.exists():
                return q
        return None

    vname = "video_music_omni" if _slot_has("video_music_omni.xml") \
        else "video_music_list"
    if with_video or any(s.get("bga") for s in songs.values()):
        entries = [{"id": sid, "title": s.get("title", ""), "artist": s.get("artist", ""),
                    "flags": 6}
                   for sid, s in songs.items() if s.get("bga")]
        if entries:
            dst = stage_root / "info/0" / f"{vname}.merged.xml"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(reapply.build_merged_video(entries))
            notes.append(f"{vname}.merged.xml（{len(entries)} 条）")

    for kind, field in (("title", "title"), ("artist", "artist")):
        base_name = (f"music_{kind}_omni" if _slot_has(f"music_{kind}_omni.xml")
                     else f"music_{kind}_yomi")
        entries = [{"id": sid, "yomi": s.get(field, "")} for sid, s in songs.items()
                   if s.get(field)]
        if entries:
            dst = stage_root / "info/0" / f"{base_name}.merged.xml"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(reapply.build_merged_yomi(entries))
            notes.append(f"{base_name}.merged.xml（{len(entries)} 条）")

    # ---- 3) 选曲封面 ----
    #     来源优先级：用户挑的帧 → STAGEFILE → BGA 视频抽帧 → 第一张 BMP
    src_pic = chart_dir / stagefile.replace("\\", "/") if stagefile else None
    if thumb_source and Path(thumb_source).exists():
        src_pic = Path(thumb_source)              # 界面上挑中的那一帧优先
    if (src_pic is None or not src_pic.exists()) and video and video.exists():
        from . import thumb as thumb_mod
        if progress:
            progress("  没有 STAGEFILE，从 BGA 视频抽帧挑封面")
        src_pic = thumb_mod.pick_frame(video, count=8,
                                       progress=lambda m: progress(m))
    if (src_pic is None or not src_pic.exists()) and fallback_bmp:
        cand = chart_dir / fallback_bmp.replace("\\", "/")
        if cand.exists():
            src_pic = cand
    if src_pic is not None and src_pic.exists():
        _contents = (db_path.parents[3] if len(db_path.parents) > 3 else None)
        _spec = assets.thumbnail_spec(ver, _contents)
        if _spec is None:
            notes.append(f"封面跳过（v{ver} 没有选曲缩略图）")
            return notes
        thumb_dir, thumb_size = _spec
        tpl_dir = root / "graphic" / thumb_dir
        tpl = None
        if tpl_dir.is_dir():
            cands = sorted(tpl_dir.glob("*.png"))
            tpl = cands[0] if cands else None
        out_png = stage_root / "graphic" / thumb_dir / f"{song_id:05d}_thum.png"
        addons.make_thumbnail(src_pic, out_png,
                              tpl if tpl and tpl.exists() else None,
                              size=thumb_size)
        notes.append(f"封面 {thumb_dir}/{song_id:05d}_thum.png"
                     f"（{thumb_size[0]}×{thumb_size[1]}）")
    return notes


RECORD_SIZE = 2040


def _sha256_of(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def convert(folder: Path, db_path: Path, out_dir: Path, song_id=None,
            title=None, artist=None, genre=None, bga: str = "29cy",
            with_preview: bool = True, sample_order: str = "long-first",
            audio_mode: str = "keysound", channels: int = 2,
            fx_track: str = "oneshot", level_mode: str = "density",
            pick_spec: list[str] | None = None, level_spec: list[str] | None = None,
            shared_audio: bool = False, reuse_audio: int | None = None,
            copy_mode: str = "copy", bake_bga: bool = True,
            id_top: int | None = None, meta_root: Path | None = None,
            subtitle: str = '', lib_root: Path | None = None,
            bake_poor: bool = False, thumb_source: Path | None = None,
    origin_version: int = 1,          # ORIGIN 分类索引：1 = SUBSTREAM
    font_index: int | None = None,    # None = 按标题自动选（纯 ASCII → 0，含日文 → 2）
    mod_dir: str = "omnimix",         # data_mods/<这个名字>/（omni 环境=omnimix，原版=bms2iidx）
    music_db_name: str = "music_omni.bin",   # omni 环境读 music_omni.bin，原版读 music_data.bin
            progress=print) -> Report:
    import time as _tm
    if progress is None:                       # 允许调用方显式传 None 关掉日志
        progress = lambda *_a, **_k: None
    _t0 = _tm.time()
    _phase = {"t": _t0}

    def _mark(msg):
        now = _tm.time()
        progress(f"[{now-_t0:6.1f}s] {msg}")
        _phase["t"] = now

    def _pct(p, msg=""):
        """统一发 "[PROGRESS n] 说明"，界面按百分比画进度条"""
        try:
            progress(f"[PROGRESS {max(0, min(100, int(p)))}] {msg}")
        except Exception:                                      # noqa: BLE001
            pass

    charts, rep = scan_folder(folder)
    _pct(3, "谱面解析完成")
    _mark("解析完成")
    if not charts:
        rep.warnings.append("no convertible chart in this folder")
        return rep
    picks = pick_difficulties(charts, rep, level_mode)
    if pick_spec:
        by_name = {c.path.name.lower(): c for c in charts}
        chosen = []
        for spec in pick_spec:
            slot_name, _, fname = spec.partition("=")
            slot_name = slot_name.strip()
            if slot_name.isdigit():
                slot = int(slot_name)                 # 直接给槽号，避免名字歧义
            else:
                slot = next((s for s, n in chart1.SLOT_NAMES.items()
                             if n.replace("DP ", "") == slot_name.upper()), None)
            if slot is None:
                rep.warnings.append(f"--pick 槽名无法识别: {slot_name}")
                continue
            c = by_name.get(fname.strip().lower())
            if c is None:
                rep.warnings.append(f"--pick 找不到谱面: {fname}")
                continue
            old = next((p for p in picks if p.path == c.path), None)
            lv = old.level if old else _level(c)
            chosen.append(DifficultyPick(c.path, slot, chart1.SLOT_NAMES.get(slot, str(slot)),
                                         lv, _guess_kind(c) or 0, "--pick 指定"))
        if chosen:
            picks = chosen
            _attach_density(picks, level_mode)     # 手工挑的谱面也要算推荐等级
    if level_spec:
        for spec in level_spec:
            slot_name, _, val = spec.partition("=")
            try:
                want = max(1, min(12, int(val)))
            except ValueError:
                rep.warnings.append(f"--level 数值非法: {spec}")
                continue
            for p in picks:
                if p.label.replace("DP ", "").upper() == slot_name.strip().upper():
                    p.level = want
    if not picks:
        rep.warnings.append("no usable difficulty")
        return rep

    # 旧代能存哪些难度：实测 23 代有 BEGINNER 但存不进曲库，26 之后才会保存；
    # LEGGENDARIA（槽 4）从 27 代起才有字段。目标库版本不够就跳过并提示。
    try:
        from . import dbmask as _dm, reapply as _ra
        _raw, _ = _dm.read_db(Path(db_path))
        _lay, _a, _s, _b = _ra.layout_of(_raw)
        _ver = _lay.ver
    except Exception:                                          # noqa: BLE001
        _ver = None
    if _ver is not None and _ver < 27:
        bad = [p for p in picks if p.slot == 4]
        if bad:
            picks = [p for p in picks if p.slot != 4]
            rep.warnings.append(
                f"v{_ver} 没有 LEGGENDARIA 字段：已跳过 {len(bad)} 个 L 谱面")
    if _ver is not None and _ver < 26:
        bad = [p for p in picks if p.slot == 3]
        if bad:
            picks = [p for p in picks if p.slot != 3]
            rep.warnings.append(
                f"v{_ver} 保存不了 BEGINNER：已跳过 {len(bad)} 个 B 谱面")
    if not picks:
        rep.warnings.append("该曲库版本存不了任何选中的难度")
        return rep

    rep.difficulties = picks

    timelines = {}
    for pick in picks:
        c = next(x for x in charts if x.path == pick.path)
        tc = timeline.build(c)
        timelines[pick.slot] = (tc, c)
        rep.warnings.extend(f"[{pick.label}] {w}" for w in tc.warnings)

    src_chart = next(x for x in charts if x.path == picks[0].path)

    # 目录只扫一次，建"小写文件名 → 路径"和"小写主名 → 路径"两张索引。
    # 之前每个采样找不到就直接扫整个目录（1176 个文件 × 1220 次 ≈ 39 秒）。
    by_name: dict[str, Path] = {}
    by_stem: dict[str, Path] = {}
    for f in folder.iterdir():
        if f.is_file():
            by_name.setdefault(f.name.lower(), f)
            by_stem.setdefault(f.stem.lower(), f)

    def resolve(old_id):
        name = src_chart.wav.get(old_id)
        if not name:
            return None
        rel = name.replace("\\", "/")
        p = folder / rel
        hit = by_name.get(p.name.lower())
        if hit:
            return hit
        return by_stem.get(p.stem.lower())

    # ★ 预览事件必须在**采样号重映射之前**快照，否则会拿到容器编号，解码成别的东西
    _prev_slot = max(timelines, key=lambda s: len(timelines[s][0].notes))
    _tc_prev = timelines[_prev_slot][0]
    preview_events = sorted(
        [(t, s) for t, s in _tc_prev.bgm if s > 0]
        + [(n.tick, n.sample) for n in _tc_prev.notes if n.sample > 0])
    mixed_pcm = None

    used = set()
    for tc, _ in timelines.values():
        used |= {n.sample for n in tc.notes if n.sample > 0}
        used |= {s for _, s in tc.bgm if s > 0}
    used = sorted(used)
    if sample_order == "long-first":
        # 逐个探测时长也是一个采样一个 ffmpeg 进程 → 必须并行，否则这一步就要一两分钟
        pairs = [(o, resolve(o)) for o in used]
        todo = [(o, str(p)) for o, p in pairs if p is not None]
        durs = par.pmap(_probe_task, todo)
        dur = dict(durs)
        for o, _p in pairs:
            dur.setdefault(o, 0.0)
        used.sort(key=lambda o: (-dur.get(o, 0.0), o))
        if used:
            progress(f"long-first sample order, longest {dur.get(used[0], 0):.1f}s (old id {used[0]})")
    remap = {old: i + 1 for i, old in enumerate(used)}
    for tc, _ in timelines.values():
        for n in tc.notes:
            n.sample = remap.get(n.sample, 0)
        tc.bgm = [(t, remap.get(s, 0)) for t, s in tc.bgm]
        tc.bgm = [(t, s) for t, s in tc.bgm if s > 0]

    sample_files = {remap[old]: resolve(old) for old in used}
    missing = [old for old in used if resolve(old) is None]
    if missing:
        rep.warnings.append(f"{len(missing)} samples missing (silence): {missing[:8]}")

    total_ms = max(tc.total_ms for tc, _ in timelines.values())
    main_slot = max(timelines, key=lambda s: len(timelines[s][0].notes))

    # --shared-audio：采样号按"整个文件夹所有谱面的并集"编号。
    # 这样同一文件夹的多个歌曲副本（--reuse-audio）才能共用同一个 .2dx：
    # 即使第二首复用了音频、跳过了容器生成，也必须用**同一张** id 映射。
    shared_ids: list[int] | None = None
    shared_map: dict[int, int] | None = None
    if shared_audio and audio_mode in ("stream", "hybrid"):
        shared_ids = sorted({n.value for c in charts for n in c.notes if n.value > 0})
        shared_map = {old: i + 2 for i, old in enumerate(shared_ids)}

    if reuse_audio is not None:
        # 「新建歌曲副本」：音频与另一首完全相同，直接复制/硬链接，不重新混音转码。
        # 前提：两首必须用同一份容器（即 --shared-audio 生成的并集编号）。
        keys_bytes = None
        if shared_map is not None:
            for tc, _ in timelines.values():
                for n in tc.notes:
                    n.sample = shared_map.get(n.sample, 2)
                tc.bgm = [(0, 1)]
        progress(f"跳过音频生成，复用歌曲 {reuse_audio:05d} 的容器（{copy_mode}）")
        _mark("复用音频")
        rep.audio = {"mode": f"reuse:{reuse_audio:05d}", "copy_mode": copy_mode}
    elif audio_mode == "schedule":
        # ===== 真实 IIDX 的发声模型 =====
        # 实测 31252：每个音符的同一 tick 上都有一个 0x07 事件，音符附近没有 0x02。
        # 也就是：所有声音由谱面"定时播放"（0x07），按键本身不出声。
        # 这样无论玩家何时按键，声音都与谱面对齐，不会错位。
        all_ids = sorted({n.sample for tc, _ in timelines.values() for n in tc.notes if n.sample > 0}
                         | {s for tc, _ in timelines.values() for _, s in tc.bgm if s > 0})
        id_map = {old: i + 1 for i, old in enumerate(all_ids)}      # 1 基，无"流"条目
        for tc, _ in timelines.values():
            sched = [(n.tick, id_map.get(n.sample, 0)) for n in tc.notes if n.sample > 0]
            sched += [(t, id_map.get(s, 0)) for t, s in tc.bgm if s > 0]
            sched = sorted((t, s) for t, s in sched if s > 0)
            tc.bgm = sched
            for n in tc.notes:
                n.sample = 0                                     # 音符静音
        todo = [(o, sample_files[o]) for o in all_ids if sample_files.get(o)]
        progress(f"转码 {len(todo)} 个采样（{par.MAX_WORKERS} 进程并行）…")
        got, errs = audio.transcode_many(
            todo, channels=channels, block_size=256,
            on_progress=lambda d, n: _pct(3 + 52 * d / max(1, n), f"键音转码 {d}/{n}"))
        for sid, msg in list(errs.items())[:5]:
            rep.warnings.append(f"采样 {sid} 转码失败（用静音）：{msg}")
        silence_riff = audio.pcm_to_adpcm_riff([0.0] * int(audio.RATE * 0.05),
                                               channels=channels, block_size=256)
        entries = [got.get(o) or silence_riff for o in all_ids]
        if fx_track == "column":
            col_of = {}
            for tc, _ in timelines.values():
                for n in tc.notes:
                    col_of.setdefault(n.sample, n.column)
            fields = [audio.field_block_for((col_of.get(o, 0) % 7) + 1) for o in all_ids]
        else:
            fields = [audio.FIELD_FX] * len(entries)             # 全部一次性音效
        keys_bytes = audio.build_container(entries, per_entry_fields=fields)
        _pct(58, "键音容器拼装完成")
        _mark("容器拼装完成")
        rep.audio = {"mode": "schedule", "entries": len(entries),
                     "events": sum(len(tc.bgm) for tc, _ in timelines.values())}
    elif audio_mode in ("stream", "hybrid"):
        tc_main = timelines[main_slot][0]
        if audio_mode == "stream":
            ev = [(t, s) for t, s in tc_main.bgm] + [(n.tick, n.sample) for n in tc_main.notes]
        else:
            ev = [(t, s) for t, s in tc_main.bgm]
        ev.sort()
        progress(f"mixing whole song ({audio_mode}, {len(ev)} events, {total_ms/1000:.1f}s)")
        pcm = mixer.mix_events(ev, lambda s: sample_files.get(s), total_ms, progress=progress)
        mixed_pcm = pcm
        _mark("混音完成")
        stream_riff = audio.s16_to_adpcm_riff(pcm.tobytes(), channels=channels, block_size=256)
        silence_riff = audio.pcm_to_adpcm_riff([0.0] * int(audio.RATE * 0.05), channels=channels, block_size=256)
        fields = None
        if audio_mode == "stream":
            entries = [stream_riff, silence_riff]
            for tc, _ in timelines.values():
                for n in tc.notes:
                    n.sample = 2
                tc.bgm = [(0, 1)]
        else:
            # --shared-audio：采样号按"整个文件夹所有谱面的并集"编号，
            # 这样同一文件夹的多个歌曲副本可以共用同一个 .2dx（配合 --reuse-audio）。
            if shared_ids is not None:
                note_ids = shared_ids
                id_map = shared_map
            else:
                note_ids = sorted({n.sample for tc, _ in timelines.values()
                                   for n in tc.notes if n.sample > 0})
                id_map = {old: i + 2 for i, old in enumerate(note_ids)}
            for tc, _ in timelines.values():
                for n in tc.notes:
                    n.sample = id_map.get(n.sample, 2)
                tc.bgm = [(0, 1)]
            todo = [(o, sample_files[o]) for o in sorted(note_ids) if sample_files.get(o)]
            progress(f"转码 {len(todo)} 个采样（{par.MAX_WORKERS} 进程并行）…")
            got, errs = audio.transcode_many(
            todo, channels=channels, block_size=256,
            on_progress=lambda d, n: _pct(3 + 52 * d / max(1, n), f"键音转码 {d}/{n}"))
            for sid, msg in list(errs.items())[:5]:
                rep.warnings.append(f"采样 {sid} 转码失败（用静音）：{msg}")
            entries = [stream_riff] + [
                got.get(o) or silence_riff for o in sorted(note_ids)]
            # 键音组号（trackId）：实测"同组新声掐旧声，0xFF 不互斥"。
            # 默认 0xFFFF = 不互斥 —— 这是真实 IIDX 曲目的做法（31252 里除整曲流外
            # 883 条全是 0xFFFF），也就是引擎默认不掐断任何采样，让作曲/谱师的意图完整播出。
            # `--fx-track lane` 才切成"按轨 1..8 强制互斥"，仅供对照实验。
            col_of = {}
            for tc, _ in timelines.values():
                for n in tc.notes:
                    col_of.setdefault(n.sample, n.column)
            if fx_track != "lane":
                fields = None                                      # 全部 0xFFFF = 不互斥
            else:
                fields = [audio.FIELD_MAIN] + [
                    audio.field_block_for(col_of.get(o, 0) + 1)     # 轨 0..7 → 组 1..8
                    for o in sorted(note_ids)]
        if fields:
            keys_bytes = audio.build_container(entries, per_entry_fields=fields)
        else:
            keys_bytes = audio.build_container(entries)
        _pct(58, "键音容器拼装完成")
        _mark("容器拼装完成")
        rep.audio = {"mode": audio_mode, "entries": len(entries),
                     "stream_seconds": round(len(pcm) / audio.RATE, 2)}
    else:
        progress(f"packing keysounds: {len(used)} samples")
        keys_bytes, rep.audio = audio.build_keysound(
            {k: v for k, v in sample_files.items() if v}, max_sample=len(used),
            warnings=rep.warnings,
            progress=lambda i, n: _pct(3 + 52 * i / max(1, n), f"键音打包 {i}/{n}")
            if i % 100 == 0 else None,
        )

    slot_bytes = {slot: chart1.build_slot(tc, rep.warnings) for slot, (tc, _) in timelines.items()}
    chart_bytes = chart1.assemble(slot_bytes)

    _base_db_sha256 = _sha256_of(Path(db_path)) if Path(db_path).exists() else ""
    db = MusicDB(db_path)
    if song_id is None:
        if music_db_name == "music_data.bin":
            # 原版曲库没有 ORIGIN 分组：从槽表顶部往下找（v25 槽表 26000 → 25999 起）
            top = id_top if id_top is not None else (getattr(db, "slot_total", 0) - 1)
            song_id = allocate_song_id(db, top, block=None)
        else:
            # 号段跟着「放入 IIDX 文件夹」走：1ST/SUBSTREAM 共用 1xxx，第 n 代用 nxxx
            # （实测官方 2677 首里 2644 首的 ID 千位 == ORIGIN）
            block = origin_version if origin_version >= 2 else 1
            top = id_top if id_top is not None else block * 1000 + 999
            song_id = allocate_song_id(db, top, block=block)
    rep.song_id = song_id
    if song_id in db.ids:
        # 迭代重转同一首时把旧记录替换掉（ID 是显式给的，视为覆盖）
        db.remove_song(song_id)
        rep.warnings.append(f"song id {song_id} replaced")

    rep.title = title or src_chart.title or folder.name
    rep.artist = artist or src_chart.artist or "Unknown"
    rep.genre = genre or src_chart.genre or "BMS"
    try:
        rep.title.encode("cp932")
        ascii_title = rep.title
    except UnicodeEncodeError:
        ascii_title = re.sub(r"[^\x20-\x7E]", "", rep.title).strip() or f"BMS {song_id}"

    preview_bytes = None
    if with_preview and reuse_audio is None:
        progress("building 10s preview")
        if mixed_pcm is not None:
            # 首选：直接从混好的整曲 PCM 截前 10 秒 —— 与真正播出来的完全一致
            preview_bytes = audio.preview_from_pcm(mixed_pcm)
        else:
            preview_bytes = audio.build_preview(preview_events,
                                                lambda s: sample_files.get(s))
        if len(preview_bytes) < 1000:
            rep.warnings.append("preview too short, skipped")
            preview_bytes = None

    mod_root = out_dir / "data_mods" / mod_dir
    SID = f"{song_id:05d}"          # IIDX 里目录与文件名统一 5 位补零（01999/01999.1）
    song_dir = mod_root / "sound" / SID
    info_dir = mod_root / "info/0"
    song_dir.mkdir(parents=True, exist_ok=True)
    info_dir.mkdir(parents=True, exist_ok=True)
    _pct(64, "曲库与谱面写入完成")
    _mark("曲库与谱面准备完成")
    (song_dir / f"{SID}.1").write_bytes(chart_bytes)

    if reuse_audio is not None:
        src_dir = mod_root / "sound" / f"{reuse_audio:05d}"
        for name in (f"{reuse_audio:05d}.2dx", f"{reuse_audio:05d}_pre.2dx"):
            src = src_dir / name
            if not src.exists():
                rep.warnings.append(f"复用音频缺失: {src}")
                continue
            dst = song_dir / name.replace(f"{reuse_audio:05d}", f"{song_id:05d}")
            _link_or_copy(src, dst, copy_mode)
    else:
        (song_dir / f"{SID}.2dx").write_bytes(keys_bytes)
        if preview_bytes:
            (song_dir / f"{SID}_pre.2dx").write_bytes(preview_bytes)

    levels = {p.slot: p.level for p in picks}

    # ---- BGA：默认自动烘焙（视频底直转 / 图片底翻转 / 叠加层合成）----
    from . import bga as bga_mod
    movie_dir = mod_root / "movie"
    movie_dir.mkdir(parents=True, exist_ok=True)
    movie_path: Path | None = None
    if not bake_bga:
        # 不生成影片：把 bga 指向自己（没有对应 mp4）→ 游戏里该区域黑屏。
        # 绝不指向别的歌，避免复用他人画面。
        bga = f"{song_id:05d}"
        movie_path = None
        rep.warnings.append("BGA: 按设置跳过（游戏内显示黑屏）")
        main_pick = None
    else:
        main_pick = max(picks, key=lambda p: len(timelines[p.slot][0].notes))
    if bake_bga and main_pick is not None and main_pick.path is not None:
        main_tc = timelines[main_pick.slot][0]
        _contents = (Path(db_path).parents[3] if len(Path(db_path).parents) > 3 else None)
        _fmt = assets.video_format(db.version, _contents)
        out_movie = movie_dir / f"{song_id:05d}{assets.video_ext(_fmt)}"
        try:
            src_chart_for_bga = bms.parse(Path(main_pick.path))
            info = bga_mod.bake(src_chart_for_bga, main_tc, out_movie,
                        on_progress=lambda f: _pct(64 + 32 * f, "BGA 合成"),
                                progress=lambda m: progress(f"  {m.strip()}"),
                                video_fmt=_fmt)
            movie_path = out_movie
            rep.warnings.append(f"BGA: {info.get('mode')} → {out_movie.name} "
                                f"（{_fmt}，{out_movie.stat().st_size/1e6:.1f} MB）")
            bga = f"{song_id:05d}"
        except Exception as e:                                 # noqa: BLE001
            rep.warnings.append(f"BGA 烘焙失败（游戏内将黑屏）：{str(e)[:120]}")
            bga = f"{song_id:05d}"
    elif bake_bga:
        rep.warnings.append("BGA: 拿不到主谱面路径，跳过（游戏内将黑屏）")

    # ---- 附属数据：只写"我们自己的库"需要的文件（记录存库 / merged XML / 封面）----
    # 注意：必须在 db.save 之后调用 —— 那时才拿得到写进曲库的原始记录
    _warn_glyphs(rep, subtitle)
    db.add_song(song_id=song_id, title=rep.title, title_ascii=ascii_title,
                genre=rep.genre, artist=rep.artist, levels=levels, bga_filename=bga,
                subtitle=subtitle, version=origin_version,
                font_idx=font_index)
    _pct(97, "写入曲库")
    db_path_out = info_dir / music_db_name
    db.save(db_path_out, slot_total=slot_total_for(db, song_id))
    info_db_path = db_path_out
    _pct(100, "完成")

    try:
        _meta = _write_side_files(
            meta_root=Path(meta_root) if meta_root else Path(db_path).parent.parent.parent,
            out_dir=out_dir, song_id=song_id, title=rep.title, artist=rep.artist,
            mod_dir=mod_dir,
            subtitle=subtitle, genre=rep.genre,
            level_map={p.slot: p.level for p in picks},
            picking=[{"file": p.path.name if p.path else "", "slot": p.slot} for p in picks],
            source_folder=str(folder), bga=bake_bga,
            stagefile=(src_chart.header.get("STAGEFILE") or ""),
            chart_dir=folder, with_video=bool(movie_path and movie_path.exists()),
            db_path=db_path_out,
            thumb_source=thumb_source,
            lib_root=Path(lib_root) if lib_root else Path.cwd() / "library",
            video=movie_path,
            fallback_bmp=next((_bm for _bm in (src_chart.bmp or {}).values()
                               if str(_bm).lower().endswith((".png", ".bmp", ".jpg"))), ""),
            progress=progress)
        if _meta:
            rep.warnings.append("附属文件：" + "；".join(_meta))
    except Exception as e:                                     # noqa: BLE001
        rep.warnings.append(f"附属文件写入失败（不影响主转换）：{str(e)[:140]}")

    # ---- 坏判层（POOR 层）：借模板生成 graphic/<id>.ifs，并打开记录开关 ----
    if bake_poor:
        _build_poor_layer(rep, src_chart, folder, song_id, mod_root, info_db_path,
                          meta_root, lib_root, progress)

    rep.base_db_sha256 = _base_db_sha256
    rep.output = str(out_dir)
    rep.ok = True
    (out_dir / "转换报告.json").write_text(
        json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return rep
