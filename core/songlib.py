"""我们自己的歌曲库（程序侧，不放在游戏目录）

为什么存原始记录：
  · 重应用时零字段映射，直接把这 2040 字节粘回曲库，不怕我们解析/写回出错
  · 每首只有 2 KB
  · 另外存一份 JSON，跨代（34→35）时按新布局重建要用

目录结构：
  <root>/index.json               所有歌的索引（含 base_db_sha256）
  <root>/records/<id>.rec         2040 字节原始记录
  <root>/records/<id>.json        可读字段
  <root>/merged/<name>.merged.xml 三个 XML 的增量文件（只含我们的条目）
"""
from __future__ import annotations

import json
import pathlib

RECORD_SIZE = 2040          # 新代（32+）记录长度，仅作参考；旧代 800/832/836/1324


class SongLib:
    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        self.records_dir = self.root / "records"
        self.merged_dir = self.root / "merged"
        self.index_path = self.root / "index.json"

    # ---------------------------------------------------------------- 基础
    def _ensure(self):
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.merged_dir.mkdir(parents=True, exist_ok=True)

    def index(self) -> dict:
        if self.index_path.exists():
            try:
                return json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:                                  # noqa: BLE001
                pass
        return {"version": 1, "songs": {}}

    def save_index(self, idx: dict):
        self._ensure()
        self.index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    # ---------------------------------------------------------------- 写入
    def save_song(self, song_id: int, record: bytes, meta: dict):
        """存一首：原始记录 + 可读字段 + 索引条目"""
        self._ensure()
        if not record:
            raise ValueError("记录为空，跳过")
        (self.records_dir / f"{song_id}.rec").write_bytes(record)
        (self.records_dir / f"{song_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        idx = self.index()
        meta = dict(meta or {}, record_size=len(record))
        idx.setdefault("songs", {})[str(song_id)] = meta
        self.save_index(idx)

    def remove_song(self, song_id: int):
        idx = self.index()
        idx.get("songs", {}).pop(str(song_id), None)
        self.save_index(idx)
        for suffix in (".rec", ".json"):
            p = self.records_dir / f"{song_id}{suffix}"
            if p.exists():
                p.unlink()

    # ---------------------------------------------------------------- 读取
    def songs(self) -> dict[int, dict]:
        idx = self.index()
        out = {}
        for k, v in (idx.get("songs") or {}).items():
            try:
                out[int(k)] = v
            except ValueError:
                continue
        return out

    def record(self, song_id: int) -> bytes | None:
        p = self.records_dir / f"{song_id}.rec"
        return p.read_bytes() if p.exists() else None

    # ---------------------------------------------------------------- merged xml
