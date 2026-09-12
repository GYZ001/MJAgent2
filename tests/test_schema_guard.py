"""守卫：lazy 建表模块必须同时提供同连接入口，且该入口绝不能用 executescript。

背景（2026-09-12 系统性隐患修复）：``app/orgs``/``app/models_registry``/
``app/provisioning``/``app/sso``/``app/quota_policy`` 五个包都用同一套 lazy
建表模式——``ensure_schema()`` 走 ``db._run_write_transaction_once``（独立
连接 + ``BEGIN IMMEDIATE``），异常被吞掉留到下次调用重试。这在调用方已经
持有写事务的连接上调用时会去抢同一把写锁，2 秒 ``WRITE_TXN_BUSY_TIMEOUT_S``
超时后静默失败——表没建成，``_ensured_paths`` 不置位，紧接着的查询报
``no such table``。修法是额外提供 ``ensure_tables_on_connection(conn)``：
复用调用方传入的同一个连接，只做 ``CREATE TABLE/INDEX IF NOT EXISTS``，不开
新连接、不申请新锁（先例：``app/quota_policy/schema.py``，EP-04 代理在真实
故障后补上）。当时另外四个包原样带着这颗地雷，本测试就是为了让"漏补"从
静默埋雷变成 CI 立刻报红。

判据从代码事实推导（AST 扫描每一个 ``app/*/schema.py`` 是否定义了
``ensure_schema()`` 且用了 ``_run_write_transaction_once``），不维护包名
白名单——将来任何新包只要照抄同一种 lazy 建表手法，会被这条 glob 自动纳入
检查，漏补 ``ensure_tables_on_connection()`` 立刻跑红，不需要有人记得回来
更新一份名单。``app/audit/store.py`` 是同一模式的原始先例，但文件名不叫
``schema.py``（是 ``store.py``），glob 天然不覆盖它——这不是白名单排除，是
本守卫的扫描范围本就以「文件名是否叫 schema.py」这一结构事实为界；它是否
同样有风险是另一个问题，见交付报告，不在本测试判定范围内。

第二颗地雷（2026-09-12 同一批修复）：``conn.executescript()`` 在执行前会对
当前连接做一次隐式 COMMIT（CPython sqlite3 文档行为）——``ensure_tables_on_
connection(conn)`` 存在的全部意义就是跑在调用方给的、可能正处于
``BEGIN IMMEDIATE`` 里的连接上，一旦内部用 executescript 就会把调用方的事务
偷偷提交掉。models_registry/orgs/provisioning/sso 四个包的
``ensure_tables_on_connection`` 曾经原样这么写，直到配额并发闸门链路真实炸
出一次间歇性数据竞争（``tests/test_quota_concurrency_atomicity.py``）。判据
同样从 AST 推导、glob 驱动，不维护包名白名单：扫描每一个 ``app/*/schema.py``
里名为 ``ensure_tables_on_connection`` 的函数定义，函数体内任何形态的
``executescript`` 调用（属性访问或裸名字引用）都判定失败。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_ROOT = _REPO_ROOT / "app"


def _schema_modules() -> list[Path]:
    return sorted(_APP_ROOT.glob("*/schema.py"))


def _defines_function(tree: ast.Module, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


def _calls_run_write_transaction_once(tree: ast.Module) -> bool:
    """匹配 ``db._run_write_transaction_once(...)`` 这类属性访问调用，以及
    （防御性地）裸名字引用——两种写法在本仓库都可能出现。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_run_write_transaction_once":
            return True
        if isinstance(node, ast.Name) and node.id == "_run_write_transaction_once":
            return True
    return False


def _find_function_def(
    tree: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_executescript(node: ast.AST) -> bool:
    """匹配 ``conn.executescript(...)`` 这类属性访问调用，以及（防御性地）
    裸名字引用——两种写法在本仓库都可能出现。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "executescript":
            return True
        if isinstance(sub, ast.Name) and sub.id == "executescript":
            return True
    return False


def test_at_least_one_schema_module_is_scanned() -> None:
    """空集合不等于「无需检查」（CLAUDE.md）：glob 写错、目录搬空都必须先在
    这里暴露，而不是让下面的 parametrize 悄悄收集到 0 条用例、看起来"全绿"。
    """
    modules = _schema_modules()
    assert len(modules) > 0, "app/*/schema.py 一个都没扫到，检查 glob 是否写错"


@pytest.mark.parametrize(
    "path", _schema_modules(), ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_lazy_schema_module_exposes_same_connection_entry_point(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    has_ensure_schema = _defines_function(tree, "ensure_schema")
    uses_independent_txn = _calls_run_write_transaction_once(tree)
    if not (has_ensure_schema and uses_independent_txn):
        pytest.skip(
            f"{path} 不是「ensure_schema() 用独立连接建表」这种 lazy 建表模式，"
            "本守卫不适用"
        )
    assert _defines_function(tree, "ensure_tables_on_connection"), (
        f"{path} 的 ensure_schema() 用独立连接建表（db._run_write_transaction_once），"
        "但没有定义 ensure_tables_on_connection(conn) 供已经持有写事务的调用方安全"
        "建表——独立连接会跟调用方抢 BEGIN IMMEDIATE 写锁，超时后静默建表失败。"
        "参见 app/quota_policy/schema.py 的先例。"
    )


def test_at_least_one_ensure_tables_on_connection_is_scanned() -> None:
    """空集合不等于「无需检查」（CLAUDE.md）：确保下面的守卫真的扫到了至少
    一处 ``ensure_tables_on_connection`` 定义，而不是因为函数名/glob 写错而
    悄悄收集到 0 条用例、看起来"全绿"。"""
    found = [
        path
        for path in _schema_modules()
        if _find_function_def(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            "ensure_tables_on_connection",
        )
        is not None
    ]
    assert len(found) > 0, (
        "app/*/schema.py 里一个 ensure_tables_on_connection 定义都没扫到，"
        "检查函数名/glob 是否写错"
    )


@pytest.mark.parametrize(
    "path", _schema_modules(), ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_ensure_tables_on_connection_never_uses_executescript(path: Path) -> None:
    """``ensure_tables_on_connection(conn)`` 跑在调用方传入的连接上，可能正
    处于调用方的 ``BEGIN IMMEDIATE`` 事务里；``executescript`` 执行前会对
    当前连接做一次隐式 COMMIT（CPython sqlite3 文档行为），会把调用方尚未
    提交的事务连同它持有的写锁一起偷偷放掉——CLAUDE.md 记录的那三次真实
    事故同一类地雷，且已经在配额并发闸门链路上真实炸过一次
    （``tests/test_quota_concurrency_atomicity.py``）。必须逐条
    ``conn.execute()``，参见 ``app/quota_policy/schema.py`` 的先例。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    func = _find_function_def(tree, "ensure_tables_on_connection")
    if func is None:
        pytest.skip(f"{path} 没有定义 ensure_tables_on_connection，本守卫不适用")
    assert not _calls_executescript(func), (
        f"{path} 的 ensure_tables_on_connection() 内部调用了 executescript——"
        "该函数跑在调用方传入的连接上，可能正处于调用方的 BEGIN IMMEDIATE 事务"
        "里，executescript 执行前会对当前连接做一次隐式 COMMIT，会把调用方的"
        "事务偷偷提交掉。必须逐条 conn.execute()，参见 "
        "app/quota_policy/schema.py 的先例。"
    )
