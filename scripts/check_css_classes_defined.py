#!/usr/bin/env python3
"""前端 ``className`` 里出现的类，必须在某个 CSS 文件里真的有定义。

事故（2026-08-30）：``AccountAdminDialogs.tsx`` 的四个弹窗都用
``className="dialog-backdrop"`` / ``"dialog"``，而这两个类**从未被定义过**。
没有样式的 ``div`` 就是文档流里的普通块——"弹窗"于是排在页面最底部。用户报
「删除用户时没有弹窗，是在页面下方让我选择」，就是这个。

四道既有前端闸门一个都抓不到：``check_dark_theme`` 查暗色覆盖、
``check_css_split`` 查跨页选择器、``tsc`` 查类型、``vitest`` 查渲染结果
（渲染出的 DOM 结构是对的，错的是它长什么样）。CSS 对未定义的类天然静默，
这类缺陷只能靠人眼看见——而人眼看的是自己刚写的东西，最容易漏。

判据从数据推导：扫 tsx/ts 里所有静态 ``className`` 字面量，与所有 CSS 文件里
定义过的类名求差集。动态拼接的类名（模板串、条件表达式）**跳过不判**——它们
无法静态确定，硬判会制造假阳性（"空集合不等于无需检查"的反面：判不了就别装
作判了，如实跳过并在报告里说明跳过了多少）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"

#: 静态 className："a b c" 或 className='a b'
_STATIC_CLASS_RE = re.compile(r'className=(?:"([^"{}]*)"|\'([^\'{}]*)\')')
#: CSS 里的类定义：.foo（排除伪类/伪元素本身）
_CSS_CLASS_RE = re.compile(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)")
#: 这些来自第三方或浏览器约定，不在本仓库 CSS 里定义。
_EXTERNAL = frozenset({"sr-only", "visually-hidden"})

#: 存量：本闸门加入时（2026-08-30）已经存在的「整元素无样式」写法，按位置登记。
#: 这是**棘轮，不是白名单**——只允许变少。它们多半是纯语义容器（页面外壳、
#: 折叠标记），但我没有逐个确认过，所以如实记成待查而不是假装它们没问题。
#: 修掉一个就从这里删一行；新出现的一律报红。
KNOWN_UNSTYLED: frozenset[str] = frozenset({
    "frontend/src/components/JsonViewer.tsx:json-viewer-toggle",
    "frontend/src/components/JsonViewer.tsx:json-collapsible",
    "frontend/src/components/VisualStyleDialog.tsx:query-inline",
    "frontend/src/components/harness/EvidenceDrawer.tsx:evidence-conclusion",
    "frontend/src/pages/AccountAdminPage.tsx:account-admin",
    "frontend/src/pages/BiblePage.tsx:conflict-field-list",
    "frontend/src/pages/CinemaPage.tsx:delivery-records-empty",
    "frontend/src/pages/ScenesPage.tsx:link-button",
})


def _defined_classes() -> set[str]:
    found: set[str] = set()
    for css in SRC.rglob("*.css"):
        text = css.read_text(encoding="utf-8")
        # 去掉注释，免得把注释里举例的类名当成定义
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        found.update(_CSS_CLASS_RE.findall(text))
    return found


def _unstyled_elements(defined: set[str]) -> list[tuple[str, str]]:
    """找出**整个元素一个样式都没有**的 className。

    判据不是"某个类没定义"——那会把语义/测试钩子全报出来：本仓库大量写法是
    ``className="empty query-loading"`` / ``className="card wall-summary"``，
    样式由 ``.empty``/``.card`` 提供，后一个只是给读代码和测试用的标记，没有
    对应 CSS 完全正常。抽查 17 个初版报出的类，全部属于这一类，没有一个是真缺陷。

    真正的缺陷形状是**这个元素上所有类都没定义**——它因此没有任何样式，
    静默退化成文档流里的普通块。``className="dialog-backdrop"`` 就是这样：
    孤零零一个类、还不存在，于是"弹窗"排到了页面最底部。
    """
    problems: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.tsx")):
        if path.name.endswith(".test.tsx"):
            continue
        text = path.read_text(encoding="utf-8")
        for m in _STATIC_CLASS_RE.finditer(text):
            raw = m.group(1) or m.group(2) or ""
            classes = [c for c in raw.split() if c not in _EXTERNAL]
            if not classes:
                continue
            if any(c in defined for c in classes):
                continue
            rel = path.relative_to(ROOT).as_posix()
            # 存量按「文件:类名」登记，不含行号——行号会因无关编辑漂移，
            # 那样每次改文件都要更新棘轮，等于逼着人放宽它。
            if all(f"{rel}:{c}" in KNOWN_UNSTYLED for c in classes):
                continue
            line = text.count("\n", 0, m.start()) + 1
            problems.append((" ".join(classes), f"{rel}:{line}"))
    return problems


def main() -> int:
    defined = _defined_classes()
    problems = _unstyled_elements(defined)
    if not problems:
        print(f"css-classes: 没有「全部类都未定义」的元素（CSS 里已定义 {len(defined)} 个类）")
        return 0
    print(f"css-classes: {len(problems)} 处元素的 className 一个都没有对应样式——")
    print("它们会静默退化成无样式的普通元素（弹窗排到页面底部就是这么来的）：")
    for classes, loc in problems:
        print(f"  {loc}  className=\"{classes}\"")
    return 1


if __name__ == "__main__":
    sys.exit(main())
