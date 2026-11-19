"""版本号与产品名：只在这里改，标题栏/关于页/发布包名都读它。"""
from __future__ import annotations

APP_NAME = "BMS2IIDX"
APP_VERSION = "BETA v0.1.3"
APP_TITLE = f"{APP_NAME} {APP_VERSION}"
APP_TAG = "BETA-v0.1.3"          # 用于发布包文件名

# 可比对的版本号：(0, 1, 2)；更新检查拿它和远端 tag 比
APP_VER = tuple(int(x) for x in APP_VERSION.split()[-1].lstrip("vV").split("."))

REPO = "Zedduuu/BMS2IIDX"        # 更新检查查的仓库
