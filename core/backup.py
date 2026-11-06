"""备份/恢复：把我们可能改到的文件整份存档

只管「程序会改到的 IIDX 侧数据文件」，不管歌曲资源：
  data_mods/omnimix/info/0/music_omni.bin
  data_mods/omnimix/info/0/video_music_omni.xml / music_title_omni.xml / music_artist_omni.xml
  data_mods/omnimix/info/0/*.merged.xml（我们写的增量）
  以及"本体"位置：data/info/0/music_data.bin、video_music_list.xml、music_title_yomi.xml、
  music_artist_yomi.xml（无 omni 的原版路线会用到）

恢复 = 只把这些数据文件还原；歌曲文件（sound/movie/graphic）不在这里管，
       由「曲库与资源」页单独处理（可能会出现"目录里有、歌单里没有"的残留）。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import shutil

TRACKED = [
    # info 有 0/1 两个槽（偶数代 / 奇数代），两个都备份，缺的自动跳过
    *[f"data_mods/omnimix/info/{_slot}/{_name}"
      for _slot in (0, 1)
      for _name in ("music_omni.bin", "video_music_omni.xml",
                    "music_title_omni.xml", "music_artist_omni.xml",
                    "video_music_omni.merged.xml",
                    "music_title_omni.merged.xml",
                    "music_artist_omni.merged.xml")],
    *[f"data_mods/bms2iidx/info/{_slot}/{_name}"
      for _slot in (0, 1)
      for _name in ("music_data.bin", "video_music_list.merged.xml",
                    "music_title_yomi.merged.xml", "music_artist_yomi.merged.xml")],
    *[f"data/info/{_slot}/{_name}"
      for _slot in (0, 1)
      for _name in ("music_data.bin", "video_music_list.xml",
                    "music_title_yomi.xml", "music_artist_yomi.xml")],
]


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


class BackupManager:
    def __init__(self, root: str | pathlib.Path, contents: pathlib.Path):
        self.root = pathlib.Path(root)
        self.contents = pathlib.Path(contents)

    # ---------------------------------------------------------------- 查询
    def list(self) -> list[dict]:
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir(), reverse=True):
            man = d / "manifest.json"
            if not man.exists():
                continue
            try:
                m = json.loads(man.read_text(encoding="utf-8"))
            except Exception:                                  # noqa: BLE001
                continue
            m["dir"] = str(d)
            m["id"] = d.name
            out.append(m)
        return out

    # ---------------------------------------------------------------- 备份
    def snapshot(self, label: str = "", song_ids: list[int] | None = None,
                 progress=None) -> dict:
        bid = _now()
        dest = self.root / bid
        dest.mkdir(parents=True, exist_ok=True)
        files = []
        for rel in TRACKED:
            src = self.contents / rel
            if not src.exists():
                continue
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files.append({"path": rel, "size": src.stat().st_size, "sha256": _sha(src)})
            if progress:
                progress(f"备份 {rel}")
        man = {
            "id": bid,
            "label": label or bid,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "contents": str(self.contents),
            "files": files,
            "song_ids": sorted(song_ids or []),
        }
        (dest / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        return man

    # ---------------------------------------------------------------- 恢复
    def restore(self, backup_id: str, remove_song_ids: list[int] | None = None,
                progress=None) -> dict:
        """只还原数据文件；不动歌曲资源（remove_song_ids 参数保留但不再使用）"""
        src_dir = self.root / backup_id
        man_path = src_dir / "manifest.json"
        if not man_path.exists():
            raise ValueError(f"找不到备份 {backup_id}")
        man = json.loads(man_path.read_text(encoding="utf-8"))

        pre = self.snapshot(label=f"restore-before-{backup_id}")
        restored = []
        for item in man.get("files", []):
            rel = item["path"]
            src = src_dir / rel
            if not src.exists():
                continue
            dst = self.contents / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel)
            if progress:
                progress(f"恢复 {rel}")
        return {"restored": restored, "removed": [], "pre_snapshot": pre["id"]}

    def delete(self, backup_id: str) -> bool:
        d = self.root / backup_id
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True
