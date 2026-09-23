"""按模块限定名解析泄漏写者的精度回归。

``scripts/check_write_across_await.py`` 第一遍判定「哪些辅助函数写了不提交」曾经
只按裸函数名做传递闭包：全仓 8 个同名 ``ensure_tables_on_connection``（每个包一份
建表兜底）里只有 1 个真的写了不提交，25 个同名 ``operation``（写事务回调闭包的
通用命名）里也只有少数真泄漏，但裸名匹配会把所有同名函数、以及调用它们的全部
上游函数一并牵连——2026-09-23 一次排查实测：48 条新违规里 44 条可追溯到唯一
真正泄漏的 ``ensure_tables_on_connection``，另 3 条追溯到把参数名撞了 ``operation``
的两个不相干的重试/并发包装函数。修复后调用点按 import 关系解析出模块限定名，
解析不了的调用（``self.foo()`` 这类接收者类型未知的调用等）保持裸名匹配兜底。

本文件只测「解析」这一件事本身，不依赖真仓库里具体是谁泄漏——构造最小的两模块
同名函数场景，避免真仓库后续修复某个函数后这份测试跟着失真。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_write_across_await_precision", ROOT / "scripts" / "check_write_across_await.py",
)
mod = importlib.util.module_from_spec(spec)
sys.modules["check_write_across_await_precision"] = mod
spec.loader.exec_module(mod)  # type: ignore[union-attr]

# ``mod`` 加载时会通过它自己的 sys.path 补丁 + 一条普通 ``from write_await_resolution
# import ...`` 语句真正 import 这个兄弟模块，注册在 ``sys.modules["write_await_
# resolution"]`` 下——必须拿这一个引用，不能再用 spec_from_file_location 另开一份：
# 那样会是另一个独立的模块对象，patch 它对 `_resolve_bare_name`/`_resolve_attr_call`
# 内部按裸名解析出的 `_follow_reexport` 毫无影响（CLAUDE.md「拆包会静默废掉
# monkeypatch」同一类地雷——这里踩的是「两份模块对象」而不是「两份包绑定」）。
resolution_mod = sys.modules["write_await_resolution"]


def _scan(tmp_path: Path, files: dict[str, str]) -> list[str]:
    """``files``：``{相对 app/ 的路径: 源码}``。返回命中的函数名列表（不带模块前缀，
    与 ``tests/test_check_write_across_await.py::_scan`` 的既有约定一致）。"""
    pkg = tmp_path / "app"
    pkg.mkdir(parents=True)
    for rel, source in files.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    old_root = mod.ROOT
    mod.ROOT = tmp_path
    try:
        return [item.split("::")[1] for item in mod.scan_tree(pkg)]
    finally:
        mod.ROOT = old_root


_LEAKY_HELPER = """
def ensure_tables_on_connection(conn):
    conn.execute("UPDATE t SET a=1")
"""

_CLEAN_HELPER = """
def ensure_tables_on_connection(conn):
    for stmt in ("CREATE TABLE IF NOT EXISTS t(a)",):
        conn.execute(stmt)
"""


def test_call_to_clean_same_named_function_not_flagged(tmp_path: Path) -> None:
    """caller_clean 只 import 了干净的那个 ``ensure_tables_on_connection``——尽管
    全仓另一处同名函数真的泄漏，按模块限定名解析必须认出这里调用的不是那一个。
    旧的裸名算法在这条用例上会误报（见本文件底部 test_regression_guard_against_old_algorithm）。
    """
    files = {
        "leaky_module.py": _LEAKY_HELPER,
        "clean_module.py": _CLEAN_HELPER,
        "caller_clean.py": """
from app.clean_module import ensure_tables_on_connection

async def uses_clean_helper(conn):
    ensure_tables_on_connection(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == []


def test_call_to_leaky_same_named_function_still_flagged(tmp_path: Path) -> None:
    """caller_leaky import 的是真正泄漏的那个，必须照报——同一个裸名字，解析到
    不同模块，结论必须不同（证明修复不是简单地把整个判据关掉）。"""
    files = {
        "leaky_module.py": _LEAKY_HELPER,
        "clean_module.py": _CLEAN_HELPER,
        "caller_leaky.py": """
from app.leaky_module import ensure_tables_on_connection

async def uses_leaky_helper(conn):
    ensure_tables_on_connection(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == ["uses_leaky_helper"]


def test_attribute_call_via_submodule_import_resolves_correctly(tmp_path: Path) -> None:
    """``from pkg import submodule`` + ``submodule.f(...)`` 是本仓真实场景的主力
    写法（如 ``app/orgs/store.py`` 调 ``schema.ensure_tables_on_connection``，
    44 条误报里的绝大多数都是这个形状）；子模块导入同样要按目标模块精确解析，
    不能退化成裸 attr 名字匹配。"""
    files = {
        "pkg/__init__.py": "",
        "pkg/leaky_schema.py": _LEAKY_HELPER,
        "pkg/clean_schema.py": _CLEAN_HELPER,
        "caller_attr.py": """
from app.pkg import clean_schema

async def uses_clean_submodule(conn):
    clean_schema.ensure_tables_on_connection(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == []


def test_parameter_named_like_leaky_function_not_flagged(tmp_path: Path) -> None:
    """``app/harness/undelivered_replay.py::replay_undelivered``、
    ``app/generation_concurrency.py::run_with_provider_call_slot`` 实测踩过的真实
    案例：全仓某处存在一个真泄漏、写了不提交的模块级函数恰好也叫 ``operation``
    （常见异步回调参数名撞车），但这里的 ``operation`` 是形参——按 Python 作用域
    规则，函数体内引用 ``operation`` 一律指形参本身，不可能是那个不相关的全局
    函数。两次 ``await operation()`` 复现真实源码的形状：旧算法会在第二次 await
    上误报（第一次调用触发裸名匹配时，判定已经晚于它自己那次 await）。"""
    files = {
        "leaky_module.py": """
def operation(conn):
    conn.execute("UPDATE t SET a=1")
""",
        "caller_param.py": """
async def run_with_slot(operation):
    first = await operation()
    second = await operation()
    return first, second
""",
    }
    assert _scan(tmp_path, files) == []


def test_unresolvable_receiver_still_falls_back_conservatively(tmp_path: Path) -> None:
    """``self.foo()`` 这类接收者类型未知的调用解析不了，必须退回裸名匹配旧逻辑
    ——不能因为「解析不了」就放过，哪怕实际调用的是类自己定义的同名干净方法。
    这是设计里明确接受的过度保守（宁可漏收窄，不放过任何真隐患）。"""
    files = {
        "leaky_module.py": _LEAKY_HELPER,
        "caller_unresolved.py": """
class Worker:
    async def run(self, conn):
        self.ensure_tables_on_connection(conn)
        await something()

    def ensure_tables_on_connection(self, conn):
        pass
""",
    }
    assert _scan(tmp_path, files) == ["run"]


def test_literal_write_then_await_still_flagged_directly(tmp_path: Path) -> None:
    """反向断言（真隐患必须照报）：字面量写入本身不经过任何名字解析，模块限定名
    改造不能动到这条最基础的判据。"""
    files = {
        "caller_literal.py": """
async def bad(conn):
    conn.execute("UPDATE t SET a=1")
    await something()
    conn.commit()
""",
    }
    assert _scan(tmp_path, files) == ["bad"]


def test_regression_guard_against_old_algorithm(tmp_path: Path) -> None:
    """把「裸名匹配」的旧算法接回来跑同一批场景必须变红——证明上面几条新增用例
    确实在验证一次真实修复，不是从一开始就通不过、形同虚设的断言。用
    ``_leaky_writers``/``_FunctionScan`` 的裸名参数直接复刻旧逻辑的判据（不
    另外维护一份旧文件），比对新旧两种算法在同一份源码上的结论差异。"""
    files = {
        "clean_module.py": _CLEAN_HELPER,
        "caller_clean.py": """
from app.clean_module import ensure_tables_on_connection

async def uses_clean_helper(conn):
    ensure_tables_on_connection(conn)
    await something()
""",
        "leaky_module.py": _LEAKY_HELPER,
    }
    new_result = _scan(tmp_path / "new", files)
    old_result = _scan_with_bare_name_only(tmp_path / "old", files)
    assert new_result == []
    assert old_result == ["uses_clean_helper"]


def _scan_with_bare_name_only(tmp_path: Path, files: dict[str, str]) -> list[str]:
    """复刻修复前的裸名字匹配判据：``_resolve_call`` 恒不可解析，逼
    ``_FunctionScan`` 全程走 ``flat_leaky`` 兜底分支——等价于旧版
    ``name in self.leaky`` 逻辑，用于证明新增用例相对旧算法确实翻了红转绿。"""
    old_resolve_call = mod._resolve_call
    mod._resolve_call = lambda node, scope: None
    try:
        return _scan(tmp_path, files)
    finally:
        mod._resolve_call = old_resolve_call


# ---------------------------------------------------------------------------
# 再导出门面：解析到的目标模块顶层没有这个名字时必须继续追，不能当「不泄漏」
# ---------------------------------------------------------------------------
#
# 主会话复核指出的第二个坑：第一版模块限定名解析对 `from pkg import f` 直接返回
# (path_by_dotted[pkg], f)，假设 f 就定义在 pkg 自己文件里。本仓大量符号经
# `app/domain/__init__.py`/`app/worker.py` 这类门面用 `from .sub import f as f`
# 转手再导出，f 其实定义在子模块——按第一版逻辑，(pkg/__init__.py, f) 永远不在
# qualified_leaky（那里登记的是真正定义处），于是经门面调用真泄漏写者的 async
# 函数被判"不泄漏"，漏放真隐患。下面三条用真实的 __init__.py 转手 / 多跳转手 /
# app.worker 式 `from app import worker; worker.f()` attribute 调用形状复现。

_LEAKY_NAMED = """
def leaky(conn):
    conn.execute("UPDATE t SET a=1")
"""


def test_reexport_through_package_init_still_flagged(tmp_path: Path) -> None:
    """``pkg/__init__.py`` 用 ``from .impl import leaky as leaky`` 转手再导出，
    真正的 def 在 ``pkg/impl.py``——``from app.pkg import leaky`` 必须追进去。"""
    files = {
        "pkg/__init__.py": "from .impl import leaky as leaky\n",
        "pkg/impl.py": _LEAKY_NAMED,
        "caller_facade.py": """
from app.pkg import leaky

async def uses_facade(conn):
    leaky(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == ["uses_facade"]


def test_reexport_through_two_hops_still_flagged(tmp_path: Path) -> None:
    """``__init__.py`` 转手 ``mid.py``，``mid.py`` 再转手 ``impl.py``——两跳都要追到。"""
    files = {
        "pkg/__init__.py": "from .mid import leaky as leaky\n",
        "pkg/mid.py": "from .impl import leaky as leaky\n",
        "pkg/impl.py": _LEAKY_NAMED,
        "caller_facade2.py": """
from app.pkg import leaky

async def uses_two_hop_facade(conn):
    leaky(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == ["uses_two_hop_facade"]


def test_reexport_via_worker_style_facade_attribute_call_flagged(tmp_path: Path) -> None:
    """``app/worker.py`` 式门面：调用方拿到的是整个模块对象
    （``from app import worker`` + ``worker.leaky(conn)``），不是具体函数名——
    子模块顶层同样可能只是转手，走 attribute 调用这条解析路径也要追进去。"""
    files = {
        "worker.py": "from app.impl_module import leaky as leaky\n",
        "impl_module.py": _LEAKY_NAMED,
        "caller_worker.py": """
from app import worker

async def uses_worker_facade(conn):
    worker.leaky(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == ["uses_worker_facade"]


def test_reexport_regression_guard_against_round1_algorithm(tmp_path: Path) -> None:
    """把「解析到目标模块就直接判定、不追子模块再导出」的上一版逻辑接回来，
    在同一份门面场景上必须变红——证明上面三条新增用例确实在验证一次真实修复。
    用 ``_follow_reexport`` 恒直接返回「顶层没有就此打住」复刻上一版行为。"""
    files = {
        "pkg/__init__.py": "from .impl import leaky as leaky\n",
        "pkg/impl.py": _LEAKY_NAMED,
        "caller_facade.py": """
from app.pkg import leaky

async def uses_facade(conn):
    leaky(conn)
    await something()
""",
    }
    new_result = _scan(tmp_path / "new", files)
    old_result = _scan_with_no_reexport_chasing(tmp_path / "old", files)
    assert new_result == ["uses_facade"]
    assert old_result == []


def _scan_with_no_reexport_chasing(tmp_path: Path, files: dict[str, str]) -> list[str]:
    """复刻修复前（本文件其余用例修复后的那一版）：不管目标模块顶层是否真的定义
    了这个名字，一律直接判定为 (target, name)——等价于上一版 ``_resolve_bare_name``/
    ``_resolve_attr_call`` 里 ``return (target, binding.orig_name)`` 那一行，
    不经过任何「顶层没有就继续追」的再导出链判断。这个 (target, name) 随后会去比
    对 ``qualified_leaky``（那里登记的是真正定义处 ``pkg/impl.py``），
    ``pkg/__init__.py`` 这个错误目标永远对不上，于是被判"不泄漏"——这正是
    2026-09-23 主会话复核抓到的漏报根因。"""
    # 必须 patch resolution_mod（真源），不能只 patch mod（主脚本的再导出绑定）：
    # `_resolve_bare_name`/`_resolve_attr_call` 定义在 write_await_resolution.py
    # 里，调用 `_follow_reexport(...)` 时按裸名在**自己模块**的 __globals__ 里查，
    # 与 mod 是否也绑了这个名字无关——只 patch mod 会让这条回归哨兵静默测不出东西。
    old_follow = resolution_mod._follow_reexport
    resolution_mod._follow_reexport = lambda target, name, scope, **kwargs: (target, name)
    try:
        return _scan(tmp_path, files)
    finally:
        resolution_mod._follow_reexport = old_follow


# ---------------------------------------------------------------------------
# 写完委托给「以 commit 收尾」的函数：不能被当成泄漏写者
# ---------------------------------------------------------------------------
#
# 主会话第三次复核指出：检查器只认「同一个函数体里自己调用 commit」为关闭，看不见
# `app/media_exec/job_state.py::_release_rejected_continuity_anchor` 两条 UPDATE
# 后转手调用 `release_provider_poll`（顶层最后一条语句就是 `conn.commit()`）这种
# 常见分工，把它错判成泄漏写者，牵连 `_run_job`。

_DELEGATE_WRITE_THEN_TRAILING_COMMIT = """
def release_provider_poll(conn):
    conn.execute("UPDATE jobs SET a=1")
    conn.execute("UPDATE shots SET b=2")
    conn.commit()

def delegate_write(conn):
    conn.execute("UPDATE shot_versions SET c=3")
    conn.execute("UPDATE jobs SET d=4")
    release_provider_poll(conn)
"""


def test_delegate_to_trailing_commit_function_not_flagged(tmp_path: Path) -> None:
    """``delegate_write`` 自己两条写、不自己 commit，但最后一步转手给顶层最后一条
    语句无条件 ``conn.commit()`` 的 ``release_provider_poll``——必须视为已关闭。"""
    files = {
        "helper.py": _DELEGATE_WRITE_THEN_TRAILING_COMMIT,
        "caller.py": """
from app.helper import delegate_write

async def run_job(conn):
    delegate_write(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == []


def test_delegate_to_trailing_commit_function_regression_guard(tmp_path: Path) -> None:
    """把「不识别委托提交」的上一版逻辑接回来，同一场景必须变红——证明上面这条
    新增用例确实在验证一次真实修复。用 ``qualified_committers`` 恒为空集复刻。"""
    files = {
        "helper.py": _DELEGATE_WRITE_THEN_TRAILING_COMMIT,
        "caller.py": """
from app.helper import delegate_write

async def run_job(conn):
    delegate_write(conn)
    await something()
""",
    }
    new_result = _scan(tmp_path / "new", files)
    old_ctx_init = mod._ResolutionContext.__init__

    def _init_without_committers(self, trees, root):
        old_ctx_init(self, trees, root)
        self.qualified_committers = set()

    mod._ResolutionContext.__init__ = _init_without_committers
    try:
        old_result = _scan(tmp_path / "old", files)
    finally:
        mod._ResolutionContext.__init__ = old_ctx_init
    assert new_result == []
    assert old_result == ["run_job"]


def test_delegate_to_trailing_commit_function_via_reexport(tmp_path: Path) -> None:
    """委托提交函数本身也可能经门面转手（呼应上一刀的再导出解析）：
    ``from app.pkg import release_provider_poll`` 的定义其实在 ``pkg/impl.py``。"""
    files = {
        "pkg/__init__.py": "from .impl import release_provider_poll as release_provider_poll\n",
        "pkg/impl.py": """
def release_provider_poll(conn):
    conn.execute("UPDATE jobs SET a=1")
    conn.commit()
""",
        "caller_reexport.py": """
from app.pkg import release_provider_poll

async def run_job(conn):
    conn.execute("UPDATE shot_versions SET c=3")
    release_provider_poll(conn)
    await something()
""",
    }
    assert _scan(tmp_path, files) == []


def test_branch_only_commit_not_recognized_as_trailing_commit() -> None:
    """反向：被调函数只在 if 分支里 commit，不能被当成「函数做的最后一件事就是
    提交」的委托关闭者——``_is_trailing_commit`` 只认顶层最后一条语句无条件执行
    commit，分支/循环/try 内部的 commit 不算数。

    这里直接测 ``_is_trailing_commit`` 本身，不走端到端 ``_scan``：排查中发现
    ``if ok: conn.commit()`` 这种分支内 commit，在 ``_leaky_writers`` 判定
    ``maybe_commit`` 自身是否泄漏时也会被当成"发生了"而判定"已关闭"（
    ``generic_visit`` 递归进 if 分支不分调用是否条件执行，把分支内的 commit()
    和顶层的一视同仁）——这是检查器一直就有、与本次委托提交改动无关的整体流
    不敏感限制（同样出现在原始 git HEAD 版本，见排查记录），不是本次新引入的
    问题，但意味着"被调函数只在分支提交"这个场景在端到端层面根本走不到"是否被
    误判为委托关闭者"这一步——被调函数自己先被判定"未写"，无法在 ``_scan``
    层面观察到本次改动要验证的边界，所以只能直接测判据函数本身。"""
    source = """
def maybe_commit(conn, ok):
    conn.execute("UPDATE t SET a=1")
    if ok:
        conn.commit()
"""
    fn = ast.parse(source).body[0]
    assert mod._is_trailing_commit(fn) is False
