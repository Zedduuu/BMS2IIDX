"""自查：找出 `self.xxx` 被使用、但类上根本拿不到这个名字的情况。

起因（2026-11-06）：一次大块替换把相邻的方法整段删掉，`ast.parse` 只查语法——
语法是对的，一运行就
    AttributeError: 'BmsLibraryPage' object has no attribute '_reselect_folder'
白白排查了半小时。

做法：导入模块 → 拿到真实类 → 对源码里每个 `self.X` 用法，
用 hasattr(cls, X) 判断（这样 Qt 基类的方法不会被误报），
再用 AST 扫 `self.X = ...` 收集实例属性。

用法：
    python tools/check_self_calls.py            # 默认查 gui/ 下三个大文件
    python tools/check_self_calls.py gui/ui.py
返回码 0 = 干净；1 = 有问题。
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ["gui/ui.py", "gui/bmslib_view.py", "gui/services.py"]


def load_module(path: pathlib.Path):
    """按包名导入（gui.ui 之类），这样模块里的相对 import 才正常"""
    import importlib
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    rel = path.resolve().relative_to(ROOT.resolve())
    dotted = ".".join(rel.with_suffix("").parts)
    return importlib.import_module(dotted)


def self_assignments(cls: ast.ClassDef) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(cls):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for tgt in targets:
            for n in ast.walk(tgt):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                        and n.value.id == "self":
                    out.add(n.attr)
        # setattr(self, "edTitle", ...) 这种动态创建也要算上
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "setattr" and len(node.args) >= 2:
            a0, a1 = node.args[0], node.args[1]
            if isinstance(a0, ast.Name) and a0.id == "self" \
                    and isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                out.add(a1.value)
    return out


def self_uses(cls: ast.ClassDef) -> dict[str, int]:
    uses: dict[str, int] = {}
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            uses[node.attr] = uses.get(node.attr, 0) + 1
    return uses


def check(path: pathlib.Path) -> list[tuple[str, str, str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    mod = load_module(path)
    problems = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        real = getattr(mod, node.name, None)
        if real is None:                 # 条件定义的类，跳过
            continue
        assigned = self_assignments(node)
        # 动态创建属性（setattr(self, attr, ...) 且 attr 是循环变量）无法静态求值，
        # 把类里出现过的字符串常量也视作“可能是动态属性名”，避免误报
        literals = {n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for name, hits in sorted(self_uses(node).items()):
            if name in assigned or name in literals or hasattr(real, name):
                continue
            problems.append((path.name, node.name, f"{name}（引用 {hits} 次）"))
    return problems


def main() -> int:
    args = sys.argv[1:] or DEFAULT
    bad = []
    for a in args:
        p = pathlib.Path(a)
        p = p if p.is_absolute() else ROOT / p
        if not p.exists():
            print(f"跳过（不存在）：{p}")
            continue
        bad += check(p)
    if not bad:
        print("OK：没有发现“用了但拿不到”的 self.xxx")
        return 0
    print("可疑（类上没有这个名字，可能是被误删/拼错）：")
    for fn, cls, name in bad:
        print(f"  {fn} :: {cls} :: {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
