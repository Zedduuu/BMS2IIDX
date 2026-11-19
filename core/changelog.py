"""更新日志（唯一的来源）。

界面「关于 → 更新日志」直接读这里；仓库里的 docs/CHANGELOG.md 也由它生成，
所以改一处就够。最新版本放最前面。
"""
from __future__ import annotations

from .version import APP_VERSION

CHANGELOG: list[dict] = [
    {
        "version": "v0.1.3",
        "date": "2026-11-20",
        "items": [
            "难度槽上方新增「分P」：P1/P2/P3… 各自 5 个 SP + 5 个 DP 槽，"
            "点 ＋ 加一份，切页摆谱面，加队列时每个 P 变成独立的一首歌",
            "分P 里有谱面时，加入队列会按 P 拆成「曲名P1 / 曲名P2…」，"
            "不再靠自动切分（没有拖进槽的才走自动切）",
            "修：难度槽区加 P 后按钮位置错乱/出现重复按钮",
        ],
    },
    {
        "version": "v0.1.2",
        "date": "2026-11-07",
        "items": [
            "更新检查：启动时静默检查新版本，「关于」页也能手动查（可关）",
            "关于页新增「更新日志」，不用去网页就能看改了什么",
            "修：原版（无 omnimix）模式部署后误报「曲库记录不见了」——"
            "自检以前读本体 music_data.bin，现在读 layeredfs 实际生效的 "
            "data_mods/<mod>/music_data.bin",
            "右上角显示版本与更新状态（已是最新=绿、有新版本=蓝，点击打开下载页）",
            "新增「添加更多谱面到队列」：同名的第 2、3 条自动加 P2/P3，"
            "已进队列的谱面在列表里标灰",
            "修：选 1ST 会落到 34 代文件夹（game_version 写成 `version or self.version`，"
            "1ST 的 0 被当成空值 → 34）；现在 0 就是 1ST",
            "修：连续部署多首时，后部署的会把先部署的记录整库盖掉；现在逐首并入曲库"
            "（本机库缺记录时还会从暂存区曲库兜底，不会再出现“本机库里没有这首歌的记录”）",
            "5K 判定改为“有 Key6/Key7 音符才算 7K”，5K 谱面原样转换、不丢音符",
            "转换页压缩重排：难度槽变矮、曲目信息与预览图并排、游戏环境与队列按钮并排",
            "新图标：手绘藕片（保留原图透明背景，16px 用简化矢量版），窗口、对话框、exe 都换上了",
            "超过 5 个谱面自动拆成「曲名P1 / P2…」多首歌（每侧最多 5 个难度槽），"
            "队列里就是独立的多首歌，没有额外分组概念",
            "只有 1 个谱面时，曲名默认写成「歌名 - 谱名」；"
            "开关在「设置 → 转换」，第一次会弹一次说明",
            "B/L 难度槽改为「27 代起才有」（26 代含以前灰掉）",
            "修：队列只有 1 首时进度条显示 1%（范围算错）",
            "修：选「1ST」等文件夹时会分到 34xxx 号段、歌却出现在 34 代文件夹；"
            "现在严格在所选的千位号段内取号，号段满了会明确提示",
            "新增：26 代以前禁用 Beginner / LEGGENDARIA 难度槽"
            "（拖进去会被拒绝并说明），避免写出该代不认的槽位",
        ],
    },
    {
        "version": "v0.1.1",
        "date": "2026-11-07",
        "items": [
            "原版模式第一个真机样本反馈后的修复（曲库叠加层识别）",
            "队列进度条 1% 显示问题",
        ],
    },
    {
        "version": "v0.1",
        "date": "2026-11-06",
        "items": [
            "首个公开测试版：BMS → IIDX 转换、BMS 谱面库、队列、曲库与资源管理、"
            "回收区、备份恢复",
            "omnimix / 原版环境自动识别；转换只写暂存区，部署前自动备份",
        ],
    },
]


def as_text() -> str:
    """给界面用的纯文本版（最新在最前）"""
    lines = [f"当前版本：{APP_VERSION}", ""]
    for rel in CHANGELOG:
        lines.append(f"【{rel['version']}】{rel.get('date', '')}")
        lines += [f"  · {it}" for it in rel["items"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def as_markdown() -> str:
    """生成 docs/CHANGELOG.md 用"""
    out = [f"# 更新日志\n\n当前版本：**{APP_VERSION}**\n"]
    for rel in CHANGELOG:
        out.append(f"## {rel['version']}（{rel.get('date', '')}）\n")
        out += [f"- {it}" for it in rel["items"]]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
