#!/usr/bin/env python3
"""BMS -> IIDX32 omnimix song package (CLI).

usage:
  python bms2iidx.py inspect --db <music_omni.bin>
  python bms2iidx.py convert "<BMS song folder>" --db <music_omni.bin> --out <dir> [options]

options:
  --id N              song id (default: auto, 32999 downward)
  --title/--artist/--genre   override metadata
  --bga NAME          bga movie name (default 29cy = reuse an existing movie)
  --no-preview        skip 10s preview
  --audio-mode M      keysound | stream | hybrid  (default keysound)
  --sample-order S    long-first | bms          (default long-first)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import convert as conv                       # noqa: E402
from core import plan as planmod                        # noqa: E402
from core.musicdb import MusicDB, MusicDBError         # noqa: E402


def cmd_plan(args):
    """只读：列出文件夹里所有谱面 + 推荐等级 + 自动分配难度槽（GUI 用同一份数据）"""
    p = planmod.scan(Path(args.folder))
    if args.json:
        print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(planmod.render_table(p))
    return 0


def cmd_inspect(args):
    db = MusicDB(Path(args.db))
    s = db.summary()
    print(f"db: {s['path']}")
    print(f"  version v{s['version']}  records {s['count']}  id range {s['id_min']}-{s['id_max']}")
    t = db.template()
    print(f"  template record: {t['song_id']} {t['title']!r} (SPN={t['SPN_level']})")
    free = [i for i in range(32999, 31999, -1) if i not in db.ids]
    print(f"  free slots in 32000-32999: {len(free)}, top: {free[:5]}")
    return 0


def cmd_convert(args):
    t0 = time.time()
    try:
        MusicDB(Path(args.db))
    except MusicDBError as e:
        print(f"[refused] {e}", file=sys.stderr)
        return 2
    rep = conv.convert(
        folder=Path(args.folder),
        db_path=Path(args.db),
        out_dir=Path(args.out),
        song_id=args.id,
        title=args.title,
        artist=args.artist,
        genre=args.genre,
        subtitle=args.subtitle,
        id_top=args.id_top,
        lib_root=Path(args.lib_root) if args.lib_root else None,
        bake_bga=not args.no_bga,
        bake_poor=args.poor,
        bga=args.bga,
        with_preview=not args.no_preview,
        sample_order=args.sample_order,
        audio_mode=args.audio_mode,
        fx_track=args.fx_track,
        level_mode=args.level_mode,
        pick_spec=args.pick,
        level_spec=args.level,
        shared_audio=args.shared_audio,
        reuse_audio=args.reuse_audio,
        copy_mode=args.copy_mode,
        progress=lambda m: print(m, flush=True) if isinstance(m, str) else None,
    )
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    print(f"\nelapsed {time.time()-t0:.1f}s   result: {'OK' if rep.ok else 'FAIL'}")
    return 0 if rep.ok else 1


def main():
    ap = argparse.ArgumentParser(prog="bms2iidx")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("inspect", help="inspect a music db")
    p1.add_argument("--db", required=True)
    p1.set_defaults(func=cmd_inspect)

    p0 = sub.add_parser("plan", help="列出 BMS 文件夹里所有谱面 + 推荐等级 + 槽位分配（只读）")
    p0.add_argument("folder")
    p0.add_argument("--json", action="store_true", help="输出 JSON（GUI 用）")
    p0.set_defaults(func=cmd_plan)

    p2 = sub.add_parser("convert", help="convert one song")
    p2.add_argument("folder")
    p2.add_argument("--db", required=True, help="source music_omni.bin (read only)")
    p2.add_argument("--id-top", type=int, default=None,
                    help="自动分配上限（默认 = 本代*1000+999）")
    p2.add_argument("--subtitle", default="",
                    help="副标题（谱师署名，如 'BMS: obj:xxx'）")
    p2.add_argument("--lib-root", default=None, help="本机库目录（默认 ./library）")
    p2.add_argument("--no-bga", action="store_true", help="不生成 BGA（游戏内黑屏）")
    p2.add_argument("--poor", action="store_true", help="生成坏判层图形包")
    p2.add_argument("--out", required=True, help="output directory")
    p2.add_argument("--id", type=int, default=None)
    p2.add_argument("--title")
    p2.add_argument("--artist")
    p2.add_argument("--genre")
    p2.add_argument("--bga", default="29cy")
    p2.add_argument("--no-preview", action="store_true")
    p2.add_argument("--audio-mode", default="hybrid",
                    choices=["schedule", "keysound", "stream", "hybrid"])
    p2.add_argument("--sample-order", default="long-first", choices=["long-first", "bms"])
    p2.add_argument("--fx-track", default="oneshot", choices=["oneshot", "lane"],
                    help="键音组号：oneshot=0xFFFF 不互斥（默认，同真实 IIDX 曲目）；lane=按轨 1..8 强制互斥")
    p2.add_argument("--level-mode", default="density", choices=["density", "author"],
                    help="等级来源：density=按物量密度推荐（默认，1-12）；author=用 BMS #PLAYLEVEL 夹到 1-12")
    p2.add_argument("--pick", action="append", metavar="SLOT=FILE",
                    help="手工指定某个槽放哪张谱面，可重复，如 --pick ANOTHER=xx.bme")
    p2.add_argument("--level", action="append", metavar="SLOT=N",
                    help="手工指定某槽等级 1-12，可重复，如 --level ANOTHER=12")
    p2.add_argument("--shared-audio", action="store_true",
                    help="采样号按整个文件夹的并集编号（多首歌副本共用一份容器时才需要）")
    p2.add_argument("--reuse-audio", type=int, default=None, metavar="SONG_ID",
                    help="复用另一首歌的音频容器（配合 --shared-audio 与 --copy-mode）")
    p2.add_argument("--copy-mode", default="copy", choices=["copy", "hardlink"],
                    help="复用音频时用复制（默认）还是硬链接（省空间）")
    p2.set_defaults(func=cmd_convert)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
