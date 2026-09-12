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

第三颗地雷（2026-09-12 同一批修复第二轮，「新包自动接上建表自保」的结构性
缺口）：``app.db_schema.ensure_schema_respecting_caller_transaction`` 这个共
用 helper 本身修好了「调用方已持有写事务时独立连接建表会抢锁超时静默失败」
这颗根因，六个包的 ``ensure_schema()`` 也都接了它——但**上面两条守卫都查不到
「ensure_schema() 有没有真的经过 helper 分派」**，只查「有没有同连接入口」和
「同连接入口有没有用 executescript」。这个缺口的分量：同一颗雷已经复发四次
（EP-04 tier quota、``executescript`` 隐式 COMMIT 变体、``create_session``/
``resolve_session``、``models_registry``/``provisioning`` 一批遗留调用点），
每次都是「守卫守住了上一次的形态，守不住问题本身」——不把「必须经 helper 分
派」也钉成断言，第五次复发只是时间问题。判据同样从 AST 推导：``ensure_schema()``
函数体内如果出现 ``_run_write_transaction_once`` 调用，就必须能在同一个函数
体内找到 ``ensure_schema_respecting_caller_transaction(...)`` 调用、其
``run_independent=`` 关键字参数指向一个具名的嵌套函数、且 ``_run_write_
transaction_once`` 的调用点全部落在这个具名回调内部（不能在回调之外另起一
处直接调用，绕开 helper 分派）——四条判据环环相扣，任何一环缺失都判定失败，
不是简单的「文件里出现过这个名字」。

本条判据的扫描范围比前两条更宽：不按文件名是否叫 ``schema.py`` 划界，而是
按「模块级是否定义了 ``ensure_schema``」这一结构事实划界（见
``_lazy_schema_dispatch_modules``）。全仓实测 ``def ensure_schema`` 只命中 7
处——六个 ``schema.py`` 包 + ``app/audit/store.py``；后者是同一手法的原始先
例，2026-09-12 也接了同一个 helper，只是文件名不同，前两条守卫的 glob 天然
不覆盖它。这一条把它一并纳入：不是给它单独开一条按路径写死的例外，而是让
「谁该被扫到」这件事完全由「有没有 ensure_schema()+_run_write_transaction_once
这个结构特征」决定，与文件叫什么名字无关——将来任何新模块只要复用同一种 lazy
建表手法，无论放在 ``schema.py`` 还是别的文件名下，都会被这条判据自动纳入。
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


def _module_defines_ensure_schema(path: Path) -> bool:
    """结构判据：模块级是否定义了 ``ensure_schema`` 函数——不管文件名是否叫
    ``schema.py``。先做一次廉价的文本包含判断筛掉绝大多数不可能匹配的文件，
    只有命中的候选才会被 ``ast.parse``，避免对全仓 600+ 模块逐一解析。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if "def ensure_schema" not in text:
        return False
    tree = ast.parse(text, filename=str(path))
    return _defines_function(tree, "ensure_schema")


def _lazy_schema_dispatch_modules() -> list[Path]:
    """``test_ensure_schema_dispatches_independent_txn_through_shared_helper``
    专用的扫描范围：全仓任何定义了 ``ensure_schema`` 的模块，不局限于
    ``app/*/schema.py``。见本文件模块文档「第三颗地雷」一段——这是让
    ``app/audit/store.py`` 自动纳入检查、同时不必为它单独写一条按路径硬编码
    的例外的判据设计。"""
    return sorted(p for p in _APP_ROOT.rglob("*.py") if _module_defines_ensure_schema(p))


def _calls_ensure_schema_respecting_caller_transaction(tree: ast.AST) -> bool:
    """匹配 ``db_schema.ensure_schema_respecting_caller_transaction(...)`` 这
    类属性访问调用，以及（防御性地）裸名字引用——两种写法在本仓库都可能出
    现，与 ``_calls_run_write_transaction_once``/``_calls_executescript`` 同一
    判据形状。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "ensure_schema_respecting_caller_transaction":
            return True
        if isinstance(node, ast.Name) and node.id == "ensure_schema_respecting_caller_transaction":
            return True
    return False


def _run_independent_callback_name(tree: ast.AST) -> str | None:
    """从 ``ensure_schema_respecting_caller_transaction(...)`` 调用点的
    ``run_independent=`` 关键字实参里取出它引用的具名函数——必须是一个直接的
    ``ast.Name``（本仓库六个既有实现全部这么写：先定义一个 ``_run_independent``
    闭包，再把函数名传进去），拿不到具名引用就返回 ``None`` 交给调用方判定为
    「结构无法确认」。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_target = (
            (isinstance(func, ast.Attribute) and func.attr == "ensure_schema_respecting_caller_transaction")
            or (isinstance(func, ast.Name) and func.id == "ensure_schema_respecting_caller_transaction")
        )
        if not is_target:
            continue
        for kw in node.keywords:
            if kw.arg == "run_independent" and isinstance(kw.value, ast.Name):
                return kw.value.id
    return None


def _contains_node(container: ast.AST, target: ast.AST) -> bool:
    """``target`` 是否是 ``container`` 子树内的某个节点——AST 节点在同一棵树
    里是唯一对象，按身份（``is``）比较即可判定「是否落在这个函数体内」，不需
    要额外维护父指针。"""
    return any(node is target for node in ast.walk(container))


def test_at_least_one_lazy_schema_dispatch_module_is_scanned() -> None:
    """空集合不等于「无需检查」（CLAUDE.md）：确保按「定义了 ensure_schema」
    这一结构事实做的扫描真的扫到了东西，而不是因为判据写错而悄悄收集到 0 条
    用例、看起来"全绿"。"""
    modules = _lazy_schema_dispatch_modules()
    assert len(modules) > 0, (
        "全仓一个定义 ensure_schema() 的模块都没扫到，检查 "
        "_module_defines_ensure_schema 的判据是否写错"
    )


@pytest.mark.parametrize(
    "path", _lazy_schema_dispatch_modules(), ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_ensure_schema_dispatches_independent_txn_through_shared_helper(path: Path) -> None:
    """ensure_schema() 用独立连接建表这条分支必须经
    ``app.db_schema.ensure_schema_respecting_caller_transaction`` 分派，不能
    自己直接调用 ``db._run_write_transaction_once``——那样会绕开 helper 对调
    用方连接是否已在事务中的判断，重新带回「独立连接抢调用方已持有的
    ``BEGIN IMMEDIATE`` 写锁，超时后静默建表失败」这颗地雷（这颗雷在本仓库已
    经复发四次，见本文件模块文档「第三颗地雷」一段）。

    四条判据环环相扣，缺一不可：
    1. 调用了 ``_run_write_transaction_once`` 就必须也调用了
       ``ensure_schema_respecting_caller_transaction``；
    2. 后者的 ``run_independent=`` 必须指向一个具名的嵌套函数；
    3. 这个嵌套函数必须真的存在，且内部真的调用了
       ``_run_write_transaction_once``；
    4. ``_run_write_transaction_once`` 的调用点必须全部落在这个嵌套函数内
       部，不能在 ``ensure_schema()`` 里另起一处绕开 helper 直接调用。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    func = _find_function_def(tree, "ensure_schema")
    if func is None or not _calls_run_write_transaction_once(func):
        pytest.skip(
            f"{path} 的 ensure_schema() 不使用独立连接建表模式（未调用 "
            "_run_write_transaction_once），本守卫不适用"
        )

    assert _calls_ensure_schema_respecting_caller_transaction(func), (
        f"{path} 的 ensure_schema() 调用了 db._run_write_transaction_once，但没有经过 "
        "app.db_schema.ensure_schema_respecting_caller_transaction 分派——调用方已经"
        "持有写事务时会重新抢锁超时、静默建表失败。必须把独立连接建表分支包成一个"
        "具名回调，通过 run_independent= 关键字参数交给共用 helper，参见 "
        "app/quota_policy/schema.py::ensure_schema 的写法。"
    )

    callback_name = _run_independent_callback_name(func)
    assert callback_name is not None, (
        f"{path} 的 ensure_schema() 调用了 ensure_schema_respecting_caller_transaction，"
        "但没有传 run_independent=<具名函数> 关键字参数（或不是一个直接的具名引用）——"
        "helper 判定调用方未持有事务时需要它来执行独立连接建表分支。"
    )

    callback_def = _find_function_def(func, callback_name)
    assert callback_def is not None, (
        f"{path} 的 run_independent= 引用了 {callback_name!r}，但 ensure_schema() 内部"
        "找不到同名的嵌套函数定义，无法确认独立连接建表逻辑真的只在 helper 分派出的"
        "这个回调里执行。"
    )
    assert _calls_run_write_transaction_once(callback_def), (
        f"{path} 的 run_independent 回调 {callback_name!r} 内部没有调用 "
        "_run_write_transaction_once——独立连接建表逻辑可能被挪到了别处，必须确认它"
        "仍然只在 helper 判定「调用方未持有事务」时才会执行。"
    )

    stray_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "_run_write_transaction_once")
            or (isinstance(node.func, ast.Name) and node.func.id == "_run_write_transaction_once")
        )
        and not _contains_node(callback_def, node)
    ]
    assert not stray_calls, (
        f"{path} 的 ensure_schema() 在 run_independent 回调 {callback_name!r} 之外还"
        "直接调用了 _run_write_transaction_once——必须让所有独立连接建表调用都收在"
        "这一个由 helper 分派的回调里，不能绕开 helper 另起一处（这正是「自己直接调 "
        "_run_write_transaction_once 而不经 helper」的规避写法）。"
    )
