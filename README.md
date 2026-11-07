# BMS2IIDX（BETA v0.1）

把 BMS 歌曲转换成 **beatmania IIDX** 能直接读的歌曲包的小工具（Windows）。

> A converter for BMS beatmaps to IIDX, with song database management.
> 一个 BMS → IIDX 的转换器，带曲库管理。

* 支持 omnimix 环境（写 `music_omni.bin`）和纯原版环境（写 `music_data.bin`），程序自动判断
* **转换只写暂存区**，游戏目录一个字节都不动；要进游戏再点「部署」，部署前自动备份
* 画面按原样处理：不裁切、不放大、不插值；音源默认只转格式、不改音量
* 删歌进回收区，可还原；本机库存原始记录，随时能把歌写回曲库

> 本仓库**不包含**任何游戏本体、曲库或版权素材。使用前请自备合法获得的游戏与 BMS 曲目。

---

## 直接使用（普通用户）

1. 下载 Release 里的 `BMS2IIDX-BETA-v0.1-win64.zip`，解压到**可写目录**（不要放 `C:\Program Files`）
2. 双击 `BMS2IIDX.exe`
3. 左上「选择」→ 选一首 BMS 的歌曲文件夹 → 「读取」
4. 勾选要导入的谱面，拖到右边的 SP / DP 难度槽
5. 下方「目标游戏目录」→ 选游戏根目录或 `contents` 目录（会自动检测环境）
6. 「添加到队列」→「队列」页「开始转换」→ 完成后「部署到游戏」

包里已内置 `ffmpeg / ffprobe`，不需要另外安装。

程序会在自己旁边生成 `library\`（索引、队列、日志、备份）和 `out\`（暂存区）。
出问题时看 `library\logs\app.log`，反馈时附上最后 50 行即可。

---

## 从源码运行 / 构建

需要 Python 3.11+。

```powershell
pip install -r requirements.txt

# 开发运行
python run_gui.py

# 打包（需要 ffmpeg.exe / ffprobe.exe 放在 work\ffbin\ 下）
python -m PyInstaller BMS2IIDX.spec --noconfirm
# 产物：dist\BMS2IIDX\BMS2IIDX.exe
```

`run_gui.py` 会把 `work\ffbin\`（或打包后的 `_internal\ffmpeg\`）加进 PATH，
所以 ffmpeg 不需要装进系统。

---

## 功能一览

| 页面 | 干什么 |
|---|---|
| **转换** | 选歌、勾谱面、填曲名/曲风/作曲人/副标题、选难度槽、试听/封面/BGA/坏判层 |
| **BMS 谱面库** | 给 BMS 根目录建索引（一行 = 一首歌），按曲名/艺术家/难度筛选，双击送进转换页；可按需统计 NPS |
| **队列** | 批量转换 / 部署；单首失败不影响后面的 |
| **IIDX 曲库与资源** | 查看曲库里的歌（本家 / omnimix / 本程序部署 / 残留），右键删除或还原 |
| **备份与恢复** | 给会被改动的文件拍快照，随时回退 |
| **帮助 / 设置 / 关于** | 用法说明、默认项、版本信息 |

---

## 目录结构

```
core/    纯逻辑：BMS 解析、转换、曲库读写、BGA、音频、索引……（不依赖界面）
gui/     界面与服务层：主窗口、BMS 谱面库页、帮助/关于文案
vendor/  内置的 ifs_hook.dll（layeredfs，含其许可）
docs/    项目结构（STRUCTURE.md）、踩坑记录（LESSONS.md）、发布清单（RELEASE.md）
tools/   开发脚本：结构自查、layeredfs 日志诊断、打包发布
```

细节见 [`docs/STRUCTURE.md`](docs/STRUCTURE.md)；要改代码前请先看
[`docs/LESSONS.md`](docs/LESSONS.md)（里面是踩过的坑和规则）。

---

## 许可

本程序以 **GNU General Public License v3.0**（GPL-3.0）发布，全文见 [`LICENSE`](LICENSE)。

这意味着：你可以自由使用、修改、再分发本程序；但**如果你分发了修改后的版本，
必须同样以 GPL-3.0 公开源代码**（不能改成闭源发布）。

第三方组件（PySide6 / pypinyin / ffmpeg / ifs_layeredfs 等）许可见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。


---

## 交流反馈群：
1129787339 
