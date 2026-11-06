# 第三方组件与致谢

本程序自身代码以 **GPL-3.0** 发布（见根目录 `LICENSE`）。以下是随包使用或内置的第三方组件。

## 随程序内置 / 打包

| 组件 | 用途 | 许可 | 说明 |
|---|---|---|---|
| [PySide6](https://pypi.org/project/PySide6/)（Qt for Python） | 图形界面 | LGPL-3.0（Qt 为 LGPL/商业双许可） | 以动态链接方式使用，未修改 Qt 本身 |
| [pypinyin](https://github.com/mozillazg/python-pinyin) | 中文 → 拼音 | MIT | 仅曲名含中文字形时使用 |
| [ffmpeg](https://ffmpeg.org/) / ffprobe | 视频与音频转码 | 构建的二进制通常为 GPL（含 x264 等）或 LGPL | **不随源码仓库分发**；发布包里自带一份，遵循其许可，仅作为独立可执行程序调用 |
| [ifs_layeredfs / ifs_hook.dll](https://github.com/mon/ifs_layeredfs) | layeredfs 文件重定向 | Apache-2.0 变体（保留其 `LICENSE.txt`） | `vendor/ifs_hook/` 内含原始许可文件，未修改 DLL |

## 设计上参考的项目

| 项目 | 参考了什么 |
|---|---|
| [aixxe/omnifix](https://github.com/aixxe/omnifix) | omnimix 运行时补丁层的加载方式（本程序不包含其代码） |
| [drmext/iidx-datatools](https://github.com/drmext/iidx-datatools) | 曲库（music_data.bin）记录布局的格式思路；`core/_vendor/musicdata_tool.py` 源自该项目 |
| [ifstools](https://github.com/mon/ifstools) | IFS 图形包读写思路 |

## 字体与素材

* 程序界面使用系统自带字体。
* 不含任何 beatmania IIDX / Konami 的图片、音频、影片或字体资源。
* 不含任何 BMS 歌曲的音源、BGA 或谱面；转换时需要用户自备。

## 免责

本工具只做格式转换，不提供、不包含任何游戏本体或版权素材。
使用者需自行确保其游戏与曲库的合法来源，并自行承担使用后果。
