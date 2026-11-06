# 项目结构

> 面向要改这个项目的人（包括将来的我）。功能说明看 `README.md`，踩过的坑看 `LESSONS.md`。

## 一、分层与依赖方向

```
app.py / gui/  ──►  core/  ──►  （只依赖标准库 + ffmpeg 等外部工具）
   界面、线程         纯逻辑
```

**硬规则：`core/` 不许 import `gui/`**。core 里全是"给定输入 → 产出文件/数据"的纯函数，
这样能被 `tools/` 下的脚本单独调用、单独测。

## 二、模块清单

| 模块 | 干什么 | 关键点 |
|---|---|---|
| `core/bms.py` | 解析 `.bms/.bme/.bml/.pms` | 编码探测（utf-8 / cp932）、BPM/STOP、长按解析 |
| `core/timeline.py` | 谱面 → 带毫秒的时间轴 | 用 `Fraction` 精确算 measure，避免浮点累计误差 |
| `core/level.py` | 物量密度 → IIDX 1–12 级建议 | 均值 + 4 秒窗峰值加权，来自 7459 个真实谱面回测 |
| `core/plan.py` | 扫描一个 BMS 文件夹，给出"哪些谱能转" | 转换页左栏的数据来源 |
| `core/convert.py` | **主转换**：BMS → 暂存区 | 阶段进度用 `[PROGRESS n]` 前缀；只写暂存区 |
| `core/audio.py` | 采样转 2DX（keysound / 单流 / 混合） | ffmpeg `-ar 44100 -ac 2 adpcm_ms`；默认不改音量 |
| `core/bga.py` | BGA 合成（图片层/影片层/坏判层） | 纯黑=透明；不裁切不插值 |
| `core/musicdb.py` | 读写 `music_data.bin` / `music_omni.bin` | 800/832/836/1324/2040 五种记录布局 |
| `core/chart.py` | 写 `.1` / `.2dx` 容器 | 12 槽 × (offset,len)，终止符 0x7FFFFFFF |
| `core/reapply.py` | 把本机库的记录写回游戏曲库 | 增删记录、合并 XML、删后重排 |
| `core/markers.py` | 生成 merged XML（曲名/作者读音、影片表） | 只增不覆盖 |
| `core/bmslib.py` | BMS 索引（SQLite） | 表 `charts`（一谱一行）+ `dirs` + `meta`；**界面按文件夹聚合成"歌曲"** |
| `core/stats.py` | 「统计 NPS」：整谱解析 → 时长/均值/峰值/推荐等级 | 按需触发，不进索引主流程 |
| `core/par.py` | 多进程 map | 按 300/块分块，失败自动退回串行 |
| `core/romanize.py` | 汉字/韩文 → 罗马音 | 判定 IIDX 字体缺字 |
| `core/poor.py` | 坏判层（借模板生成 `graphic/<id>.ifs`） | 从原曲 miss 图复制 |
| `gui/services.py` | 界面与 core 之间的"应用服务层" | 探测游戏、部署、备份、回收区、本机库、成绩库、模式判定 |
| `gui/ui.py` | 主窗口：8 个标签页 | 见文末"ui.py 的分区" |
| `gui/bmslib_view.py` | BMS 谱面库页 | 左歌曲列表 / 右预览（谱面清单 + 统计 NPS） |
| `gui/help_text.py` | 帮助页 / 关于页的 HTML | 用户可见的文案都在这里 |
| `tools/` | 开发脚本 | `check_self_calls.py`（结构自查）、`diag_layeredfs.py`（layeredfs 日志诊断） |

## 三、一次转换的数据流

```
BMS 文件夹
  → core/plan.py 扫描（列出谱面、难度、物量、推荐等级）
  → 用户在转换页选谱面 / 填曲名 / 选难度槽
  → core/convert.py
       ├─ core/bms.py + timeline.py   解析谱面，写 12 槽 .1
       ├─ core/audio.py               采样 → .2dx
       ├─ core/bga.py                 图片/影片 → mp4（25/26 代 → wmv）
       ├─ core/musicdb.py             往曲库追加一条记录
       └─ 写 data_mods/<mod>/…        只写暂存区 out/staging
  → 用户点「部署到游戏」
       ├─ 校验曲库哈希没变（omni 包更新过会拦下来）
       ├─ 自动备份将被改动的文件
       └─ 复制到 <contents>/data_mods/<mod>/…
```

## 四、落盘位置

| 位置 | 内容 | 谁负责 |
|---|---|---|
| `out/staging/` | 转换产物（`data_mods/...` 结构） | convert |
| `library/<安装名>_<hash>/bms_index.sqlite` | BMS 索引 | bmslib |
| `library/<安装名>_<hash>/queue.json` | 转换队列 | ui |
| `library/logs/app.log` | 运行日志（出问题先看这个） | ui |
| `<game>/bms2iidx_trash/` | 回收区 | services |
| `library/backups/` | 曲库/XML 备份 | services |

## 五、要改哪里（对照表）

| 想改的东西 | 改哪个文件 |
|---|---|
| 转换出来的文件名、扩展名 | `core/convert.py` 末尾的写盘段 |
| 音源编码参数 | `core/audio.py` 顶部常量 |
| BGA 合成规则（黑边/叠加/坏判） | `core/bga.py` |
| 曲库字段含义 | `core/musicdb.py` 的布局表 |
| 部署/备份/回收区/模式判定 | `gui/services.py` |
| 界面文案 / 按钮 / 布局 | `gui/ui.py`（帮助、关于在 `help_text.py`） |
| BMS 列表的列 / 筛选 / 预览 | `gui/bmslib_view.py` |
| 索引表结构 | `core/bmslib.py`（**必须写迁移**） |

## 六、硬规则（违反会出事）

1. **转换只写暂存区**，绝不直接写游戏目录；只有「部署」才动 `data_mods/`。
2. **ID 一律 5 位补零**（`01999`），早期不补零的部署要兼容读。
3. **部署前必须备份**；删歌默认进回收区，可还原。
4. **进度消息统一 `[PROGRESS n]` 前缀**，界面靠它算进度条。
5. **`core` 不许 import `gui`**。
6. **大块替换后跑结构自查**：`python tools\check_self_calls.py`。
7. **索引加列必须写迁移**，且不许做"全量重扫 10 万文件"式的迁移（见 LESSONS R5）。
8. **日志要落盘**：新页面的报错走 `self.info()` / `on_failed()`，才会进
   `library/logs/app-*.log`。

## 七、提交前自查（30 秒）

```powershell
python -m compileall -q core gui tools
python tools\check_self_calls.py
python -c "import gui.ui; import gui.services; import core.convert"
```

## 八、ui.py 的分区（约 3500 行，按顺序）

| 行段 | 内容 |
|---|---|
| 1–120 | 导入、常量、`StreamCatcher`、`Worker` |
| 120–470 | `Main` 初始化：设置、标签页、日志面板、日志落盘、stdout 重定向、excepthook |
| 470–700 | 日志/状态条/后台任务（`run_bg`）/错误处理（`on_failed`） |
| 700–1100 | 转换页（选文件夹、谱面表、难度槽、曲名字段、试听/封面/BGA） |
| 1100–1330 | 队列页 |
| 1330–1780 | IIDX 曲库与资源、备份与恢复 |
| 1780–2050 | 帮助、设置（两列网格） |
| 2050–2450 | 探测游戏、读谱、转换/部署入口 |
| 2450–3500 | 歌曲库管理、回收区、批量操作、杂项 |

> 这个文件太大是已知技术债。要拆必须**一次只搬一页 + 每搬完跑一次冒烟**，
> 不要整文件重写（见 LESSONS R2/R6）。
