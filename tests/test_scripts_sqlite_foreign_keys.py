"""``scripts/`` 里可写的 ``sqlite3.connect`` 必须显式声明 ``PRAGMA foreign_keys``。

2026-09-10 计算服务器 B 实测的故障：SQLite 的 ``foreign_keys`` 是**每连接**开关、
**默认关**。``app/db.py`` 的 ``get_conn()`` 开了它，所以走产品路径的删除会正常触发
``ON DELETE SET NULL`` / ``CASCADE``；但 ``scripts/reset_project_episodes.py`` 这类
直接删库的脚本走裸 ``sqlite3.connect``，谁都没写这一行，**级联动作一次都没触发过**。
攒下 7332 条悬挂引用后，``scripts/backup_manju_db.py`` 的 ``PRAGMA foreign_key_check``
验证一条不过就把整份备份挪进隔离区——**连续五晚没有可用备份，而没人看得见**。

判据是「有没有表态」，不是「必须为 ON」：``scripts/repair_dangling_fk_refs.py`` 修复
期间**必须**关着（开着会让 DELETE 再触发一轮级联，把修哪些行从计划里悄悄改掉），它
写了 ``PRAGMA foreign_keys=OFF`` 并在原地注明理由，同样算合规。默认值才是坑——沉默
地拿到「关」，和明知故犯地写「关」，是两件事。

范围收敛到**会执行 DELETE 的脚本里的可写连接**：悬挂引用只由删除产生，只读连接
（``?mode=ro``）与从不删除的脚本都造不出来。判据取自模块里有没有 ``DELETE FROM``
这一数据事实，不列脚本名单——名单会在下一个新脚本上失效。
"""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _string_parts(node: ast.AST) -> str:
    """把表达式里所有字符串字面量拼起来——f-string / 拼接 / 裸字面量都覆盖。"""
    return "".join(
        sub.value for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    )


def _is_sqlite_connect(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
    )


def _declares_foreign_keys(scope: ast.AST) -> bool:
    """作用域里是否出现过 ``PRAGMA foreign_keys=<ON|OFF>`` 的字符串。"""
    return any(
        "pragma foreign_keys" in sub.value.lower().replace(" =", "=")
        for sub in ast.walk(scope)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    )


def _deletes_rows(module: ast.Module) -> bool:
    """整个模块里有没有 ``DELETE FROM`` ——只有删除会制造悬挂引用。

    判断放在**模块**粒度而不是连接所在的函数：``reset_project_episodes.py`` 在
    ``main()`` 里建连接、把它传给 ``sweep_orchestration_residue()`` 去删——正是造出
    线上那批悬挂引用的那个脚本。按函数作用域找 DELETE 会漏掉它，而漏掉的恰好是唯一
    的真阳性。宁可多问几句：模块里没有 DELETE 的脚本本来就不会被问到。
    """
    return any(
        "delete from" in sub.value.lower()
        for sub in ast.walk(module)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    )


def _writable_connects_without_declaration(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # 每个 connect 归属到最近的函数作用域；模块级的归模块。
    owner: dict[int, ast.AST] = {}
    for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))]:
        for node in ast.walk(scope):
            if _is_sqlite_connect(node):
                owner[id(node)] = scope  # 后写的是更内层的作用域，正是想要的归属

    problems: list[str] = []
    for node, scope in ((n, owner[id(n)]) for n in ast.walk(tree) if _is_sqlite_connect(n)):
        target = _string_parts(node.args[0]) if node.args else ""
        if "mode=ro" in target:
            continue
        if not _deletes_rows(tree):
            continue
        if _declares_foreign_keys(scope):
            continue
        problems.append(
            f"{path.name}:{node.lineno} 这个连接会执行 DELETE，却没有显式声明 "
            "PRAGMA foreign_keys——默认是关的，ON DELETE SET NULL/CASCADE 不会触发"
        )
    return problems


def test_writable_script_connections_declare_foreign_keys() -> None:
    problems: list[str] = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        problems.extend(_writable_connects_without_declaration(path))
    assert problems == [], "\n".join(problems)


def test_guard_catches_a_bare_writable_connect(tmp_path) -> None:
    """守卫本身要真的会红——不然它只是一行永远为真的断言。"""
    sample = tmp_path / "bare.py"
    sample.write_text(
        "import sqlite3\n"
        "def go(p):\n"
        "    conn = sqlite3.connect(p)\n"
        '    conn.execute("DELETE FROM jobs WHERE id=?", (1,))\n'
        "    return conn\n",
        encoding="utf-8",
    )
    assert len(_writable_connects_without_declaration(sample)) == 1
    # 建连接的函数自己不删、传给别的函数去删——线上那个脚本的形状，必须照样抓到
    handed_off = tmp_path / "handed_off.py"
    handed_off.write_text(
        "import sqlite3\n"
        "def sweep(conn):\n"
        '    conn.execute("DELETE FROM jobs")\n'
        "def main(p):\n"
        "    sweep(sqlite3.connect(p))\n",
        encoding="utf-8",
    )
    assert len(_writable_connects_without_declaration(handed_off)) == 1
    # 同样是可写连接，脚本里根本没有 DELETE 就造不出悬挂引用，不在范围内
    updater = tmp_path / "update_only.py"
    updater.write_text(
        "import sqlite3\n"
        "def go(p):\n"
        "    conn = sqlite3.connect(p)\n"
        '    conn.execute("UPDATE jobs SET status=?", ("x",))\n'
        "    return conn\n",
        encoding="utf-8",
    )
    assert _writable_connects_without_declaration(updater) == []


def test_guard_accepts_readonly_uri_and_explicit_declaration(tmp_path) -> None:
    readonly = tmp_path / "ro.py"
    readonly.write_text(
        "import sqlite3\n"
        "def go(p):\n"
        '    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)\n'
        '    conn.execute("DELETE FROM jobs")\n'
        "    return conn\n",
        encoding="utf-8",
    )
    assert _writable_connects_without_declaration(readonly) == []
    declared = tmp_path / "declared.py"
    declared.write_text(
        "import sqlite3\n"
        "def go(p):\n"
        "    conn = sqlite3.connect(p)\n"
        '    conn.execute("PRAGMA foreign_keys=ON")\n'
        '    conn.execute("DELETE FROM jobs")\n'
        "    return conn\n",
        encoding="utf-8",
    )
    assert _writable_connects_without_declaration(declared) == []
    # 显式关掉也算表态（修复脚本必须关着，见模块 docstring）
    off = tmp_path / "off.py"
    off.write_text(
        "import sqlite3\n"
        "def go(p):\n"
        "    conn = sqlite3.connect(p)\n"
        '    conn.execute("PRAGMA foreign_keys=OFF")\n'
        '    conn.execute("DELETE FROM jobs")\n'
        "    return conn\n",
        encoding="utf-8",
    )
    assert _writable_connects_without_declaration(off) == []
