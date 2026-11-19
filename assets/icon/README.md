# 图标

- `lotus-illustration.png` —— 手绘藕片（用户提供，1254×1254 白底）
- `lotus.svg` —— 极简矢量藕片，**只给 16/24px 用**（插画缩到这么小会糊）
- `BMS2IIDX-<size>.png` —— 由 `tools/make_icons.py` 从插画生成（白底已转透明，四周留 8% 边距）
- `BMS2IIDX.ico` —— 16/24(简化矢量) + 32/48/64/128/256(插画) 多尺寸

改图标：换掉 `lotus-illustration.png` 后重新跑

```powershell
python tools/make_icons.py
python -m PyInstaller BMS2IIDX.spec --noconfirm
```
