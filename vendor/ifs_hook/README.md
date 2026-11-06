# ifs_hook.dll（随包内置）

来自开源项目 **mon/ifs_layeredfs**，用于让 spice2x 从 `data_mods/` 覆盖游戏文件（layeredfs）。

| 项 | 值 |
|---|---|
| 版本 | **v4.1**（2026-06-26 发布） |
| 来源 | https://github.com/mon/ifs_layeredfs/releases/tag/v4.1 |
| 下载的文件 | `ifs_layeredfs_4.1.zip` 里的 `64bit/ifs_hook.dll`、`32bit/ifs_hook.dll` |
| 落地位置 | 游戏 `contents/modules/ifs_hook.dll`（由本程序「安装 hook」按钮复制） |
| 校验特征 | 导出符号 `__layeredfs_version` 存在（v3.8 起才有），文件内可见 `4.1` |
| 许可 | 见 `LICENSE.txt`（Apache-2.0 变体：不改它的命令行参数名；新增参数以 `--layered-` 开头） |

## 为什么内置

纯原版（没装 omnimix）的机器上，spice2x **自己不带** ifs_hook，会在日志里提示：

```
data_mods directory found, but ifs_hook.dll is not loaded; mods will not load
```

内置一份让用户点一下就装好，不用自己去找 GitHub。

## 怎么生效

1. 文件放到 `contents/modules/ifs_hook.dll`
2. spice2x 启动时加载它：`-k ifs_hook.dll`（**放在最前面**），
   或用 SpiceCfg 的「Inject DLL Hooks」填 `ifs_hook.dll`。
   本程序的「写入 spice2x 配置」按钮会自动把它放到 `k` 的最前面。

## 更新方法

替换本目录两个 DLL 即可（保持文件名），再更新此文件的版本号。
