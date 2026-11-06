"""打两个发布包：
   1) 程序包：dist/BMS2IIDX → out/release/BMS2IIDX-<tag>-win64.zip（含 README.md）
   2) 源码包：仓库源码（排除 out/work/dist/build/library/DL/_ref/.git）→ ...-src.zip
"""
from __future__ import annotations

import datetime
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REL = ROOT / "out" / "release"
REL.mkdir(parents=True, exist_ok=True)

import os
import sys

TAG = (sys.argv[1] if len(sys.argv) > 1 else
       os.environ.get("BMS2IIDX_TAG") or
       datetime.date.today().strftime("%Y%m%d"))
EXCLUDE_DIRS = {".git", "out", "work", "dist", "build", "library", "DL", "_ref",
                "__pycache__", ".venv", "reports"}

def readme_text() -> str:
    """说明统一放 README.md（仓库根目录），打包时原样塞进 zip"""
    path = ROOT / "README.md"
    if not path.exists():
        raise SystemExit("缺少 README.md（说明统一放这里）")
    return path.read_text(encoding="utf-8")


def zip_dir(src: pathlib.Path, dst: pathlib.Path, extra_files: dict[str, str] | None = None):
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src).as_posix())
                n += 1
        for name, text in (extra_files or {}).items():
            z.writestr(name, text)
            n += 1
    return n


def zip_source(dst: pathlib.Path):
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(ROOT.rglob("*")):
            rel = f.relative_to(ROOT)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if not f.is_file():
                continue
            if f.suffix in (".pyc", ".log") or f.name.startswith("~"):
                continue
            if rel.parts[0] == "tools" and f.name.startswith("_"):
                continue
            z.write(f, rel.as_posix())
            n += 1
    return n


def main():
    dist = ROOT / "dist" / "BMS2IIDX"
    if not (dist / "BMS2IIDX.exe").exists():
        raise SystemExit("没有找到 dist/BMS2IIDX/BMS2IIDX.exe，先打包")

    app_zip = REL / f"BMS2IIDX-{TAG}-win64.zip"
    print(f"压缩程序包 → {app_zip.name} …")
    extra = {"README.md": readme_text()}
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        f = ROOT / name
        if f.exists():
            extra[name] = f.read_text(encoding="utf-8")
    n1 = zip_dir(dist, app_zip, extra)
    print(f"   {n1} 个文件, {app_zip.stat().st_size/1e6:.1f} MB")

    src_zip = REL / f"BMS2IIDX-{TAG}-src.zip"
    print(f"压缩源码包 → {src_zip.name} …")
    n2 = zip_source(src_zip)
    print(f"   {n2} 个文件, {src_zip.stat().st_size/1e6:.1f} MB")


if __name__ == "__main__":
    main()
