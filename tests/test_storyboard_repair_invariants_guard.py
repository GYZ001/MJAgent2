"""守卫：分镜台两个确定性修补器的「不删受保护内容」不变量不得被悄悄放宽。

2026-09-16 龙猫出爪连播第 3–6 集整批失败，根因是修补器删掉的正是同一轮阻断
校验要求必须在的内容，模型无论怎么写都过不了（完整案情见两个模块自己的
docstring）。两条保护都是**沉默型**的——删掉之后测试不会红成一片，只会在某次
真实连播里重新变成整集失败，所以在这里用 AST 把它们钉死：

- ``repair_preempted_dialogue``/``repaired_repeated_delivery_errors`` 的
  ``required_texts`` 必须是 keyword-only 且**没有默认值**。给了默认值就等于
  「漏传不报错」，保护会静默失效回到删本段必保台词的老行为
  （CLAUDE.md：可选参数是缺陷的温床）。
- ``strip_extra_reference_markers`` 必须真的去算 ``_protected_names``。不算
  就会把有参考图角色的 @ 当群演标记剥掉，而 @ 是这些角色绑定参考图的唯一途径。
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _function(module_path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((_ROOT / module_path).read_text(encoding="utf-8"))
    found = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == name), None,
    )
    assert found is not None, f"{module_path} 里找不到 {name}()，重命名时请同步改本守卫"
    return found


def _kwonly_names_without_default(func: ast.FunctionDef) -> set[str]:
    return {
        arg.arg for arg, default in zip(func.args.kwonlyargs, func.args.kw_defaults)
        if default is None
    }


def test_preemption_repair_requires_explicit_required_texts() -> None:
    for name in ("repair_preempted_dialogue", "repaired_repeated_delivery_errors"):
        func = _function("app/production/storyboard_dialogue_repeat_repair.py", name)
        assert "required_texts" in _kwonly_names_without_default(func), (
            f"{name}() 的 required_texts 必须是 keyword-only 且无默认值：本段必保原话"
            "一旦漏传，抢说修补会重新把它删掉，紧接着 quote_provenance_errors 报"
            "「必保引用须保留完整原话」，模型逐字照录也过不了"
        )


def test_preemption_repair_skips_lines_that_are_own_required_quotes() -> None:
    func = _function("app/production/storyboard_dialogue_repeat_repair.py", "repair_preempted_dialogue")
    source = ast.unparse(func)
    assert "required_texts" in source and "own" in source, (
        "repair_preempted_dialogue() 必须用 required_texts 组出本段必保原话集合并跳过命中的行"
    )


def test_reference_repair_computes_protected_names() -> None:
    func = _function("app/production/storyboard_reference_repair.py", "strip_extra_reference_markers")
    called = {
        node.func.id for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_protected_names" in called, (
        "strip_extra_reference_markers() 必须计算 _protected_names 并传给改写：@ 是有参考图"
        "角色绑图的唯一途径，被当群演剥掉就再也绑不回来"
    )
    assert "_known_extra_names" in called, "群演名单仍须参与判定，否则修补本身失效"


def test_reference_repair_does_not_fall_back_to_bare_replace() -> None:
    """裸 replace 会让群演标签「王婶」把「@王婶的老狗」拦腰斩断（第 6 集实测）。"""
    source = (_ROOT / "app/production/storyboard_reference_repair.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    replaces = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
    ]
    assert not replaces, (
        "不得用 str.replace 改写 @ 标记：它没有词边界概念，群演标签是某个角色名的前缀时"
        "会破坏那个合法引用；改写走 _rewrite_markers 的最长整名匹配"
    )
