import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from tests.isolation import (
    IsolationSession,
    ProviderConfigurationIsolation,
    UNROUTABLE_PROVIDER_BASE_URL,
    isolate_provider_environment,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sandbox_lifecycle import mark_sandbox_owner, purge_stale_sandboxes

# ---------------------------------------------------------------------------
# 必须在任何 ``app.*`` 导入之前执行。
#
# ``app/config.py`` 在 **import 期**决定要不要加载 ``.env``：
#     if TEST_PROFILE == "isolated": return      # 不读 .env
# 而 ``TEST_PROFILE`` 取自 ``MANJU_TEST_PROFILE``，同样在 import 期读一次。
# 这个开关原本只有 ``scripts/verify.py`` 会设；直接跑 ``pytest`` 时它是空的，
# ``.env`` 就被 ``os.environ.setdefault`` 灌进本进程，测试跑在本机部署配置上，
# 而 CI 与新克隆没有 ``.env``、跑在代码默认值上——同一份代码两种结果。
#
# 这里在 import ``app.*`` 之前把沙箱建好并设上两个变量，让 ``_load_env()`` 从
# 头就不执行。判据是「``.env`` 根本没被读过」，不是「读了之后再逐个删掉已知的
# 坏键」——后者是黑名单，必然漏：实测本机 ``.env`` 会灌进 4 个键
# （MINIMAX_H3_API_KEY / MJ_BACKEND_HOST / MJ_LEGACY_SHARED_SESSION /
# MJ_MEDIA_REQUIRE_TICKET），而那份名单只列了后两个。
#
# ``--live-integration`` 要的正是真实部署配置，那条路径保持读 ``.env``。选项在
# ``pytest_configure`` 才解析完，这里赶在 import 之前，只能看 ``sys.argv``——
# pytest 自己的早期配置也是这么做的。
# ---------------------------------------------------------------------------
_LIVE_INTEGRATION_ARGV = "--live-integration" in sys.argv

if _LIVE_INTEGRATION_ARGV:
    os.environ["MANJU_TEST_PROFILE"] = "live-integration"
    _SANDBOX_PRECREATED = None
    _SANDBOX_PRECREATED_OWNED = False
else:
    _configured = os.environ.get("MANJU_TEST_SANDBOX", "").strip()
    if _configured:
        _SANDBOX_PRECREATED = Path(_configured).expanduser().resolve()
        _SANDBOX_PRECREATED.mkdir(parents=True, exist_ok=True)
        _SANDBOX_PRECREATED_OWNED = False
    else:
        purge_stale_sandboxes("manju-pytest-")
        _SANDBOX_PRECREATED = Path(tempfile.mkdtemp(prefix="manju-pytest-")).resolve()
        _SANDBOX_PRECREATED_OWNED = True
        mark_sandbox_owner(_SANDBOX_PRECREATED)
    os.environ["MANJU_TEST_SANDBOX"] = str(_SANDBOX_PRECREATED)
    # app/config.py 的守卫要求 isolated 必须配 MANJU_TEST_SANDBOX，所以顺序是
    # 先设 sandbox 再设 profile。
    os.environ["MANJU_TEST_PROFILE"] = "isolated"

# app.db.init_db() looks up its per-table bootstrap/migration steps by name
# through app.db_schema instead of importing these business modules directly
# (P0-3 dependency inversion, see docs/coupling_review_2026-08-29.md 第2步).
# Each of these registers itself with app.db_schema at import time; app.main's
# lifespan does the equivalent import for the running service. Test files call
# db.init_db() directly and can be run individually (not just as part of the
# full `pytest tests/` collection), so this root conftest — loaded before any
# test module — has to trigger the same registration explicitly, or a lone
# test file's db.init_db() call would raise KeyError on a registry lookup.
import app.artifacts  # noqa: F401,E402
import app.completion_grant  # noqa: F401,E402
import app.delivery  # noqa: F401,E402
import app.model_migration  # noqa: F401,E402
import app.production.certificate  # noqa: F401,E402
import app.production.grant  # noqa: F401,E402
import app.production.revision  # noqa: F401,E402
import app.production.shot_uid  # noqa: F401,E402

def patch_stages_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.stages`` in every submodule that actually binds it.

    ``app/stages.py`` was one file until it was split into the ``app.stages``
    package (see ``app/stages/__init__.py``); every call site shared a single
    module namespace, so ``monkeypatch.setattr(stages, name, value)`` reached
    all of them.  After the split each submodule holds its own copy of any
    name it imported (from ``app.stages`` re-exports or from elsewhere), so
    patching only the package-level re-export silently misses whichever
    submodule the real call happens to live in -- the patch appears to apply
    (no error) but the mocked code path is never exercised. This walks every
    submodule of ``app.stages`` and patches ``name`` wherever it is bound,
    which reproduces the pre-split single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.stages as stages

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(stages, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(stages.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{stages.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_screenplay_scene_shards_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.screenplay_scene_shards`` in every submodule that
    actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/screenplay_scene_
    shards.py`` was one file until it was split into the ``app.screenplay_scene_
    shards`` package, so ``monkeypatch.setattr(screenplay_scene_shards, name,
    value)`` only reaches the package's own re-export attribute now, not the
    independent copy each submodule bound for itself at import time (including
    the defining submodule's own module-global reference to its own sibling
    names). This walks every submodule and patches ``name`` wherever it is
    bound, reproducing the pre-split single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.screenplay_scene_shards as screenplay_scene_shards

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(screenplay_scene_shards, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(screenplay_scene_shards.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{screenplay_scene_shards.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_validators_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.validators`` in every submodule that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/validators.py`` was
    one file until it was split into the ``app.validators`` package (see
    ``app/validators/__init__.py``), so ``monkeypatch.setattr(validators, name,
    value)`` only reaches the package's own re-export attribute now, not the
    independent copy each submodule bound for itself at import time (including a
    submodule that imports a sibling submodule's name at its own top level, e.g.
    ``screenplay_validate.py``'s ``from .storyboard_delivery import
    _spine_delivery_clauses``). This walks every submodule and patches ``name``
    wherever it is bound, reproducing the pre-split single-namespace patch
    semantics.
    """
    import pkgutil
    import sys

    import app.validators as validators

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(validators, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(validators.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{validators.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_screenplay_repair_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.production.screenplay_repair`` in every submodule
    that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/production/
    screenplay_repair.py`` was one file until it was split into the
    ``app.production.screenplay_repair`` package (see ``app/production/
    screenplay_repair/__init__.py``), so ``monkeypatch.setattr(screenplay_repair,
    name, value)`` only reaches the package's own re-export attribute now, not
    the independent copy each submodule bound for itself at import time --
    including a submodule that calls a *sibling* submodule's function, e.g.
    ``checkpoint_recovery.py`` and ``revalidate_resume.py`` both call
    ``run_screenplay_qa`` via their own ``from .qa import run_screenplay_qa``.
    This walks every submodule and patches ``name`` wherever it is bound,
    reproducing the pre-split single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.production.screenplay_repair as screenplay_repair

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(screenplay_repair, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(screenplay_repair.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{screenplay_repair.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_prep_pack_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.production.prep_pack`` in every submodule that
    actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/production/
    prep_pack.py`` was one file until it was split into the
    ``app.production.prep_pack`` package (see ``app/production/prep_pack/
    __init__.py``), so ``monkeypatch.setattr(prep_pack, name, value)`` only
    reaches the package's own re-export attribute now, not the independent copy
    each submodule bound for itself at import time -- including ``get_conn``
    (imported into most submodules directly from ``app.db``) and a submodule
    that calls a sibling submodule's function, e.g. ``entry.py`` calling
    ``_generate_prep_pack_once`` via its own ``from .generate_once import
    _generate_prep_pack_once``. This walks every submodule and patches ``name``
    wherever it is bound, reproducing the pre-split single-namespace patch
    semantics. Does not apply to attribute-patching a shared module object
    itself (e.g. ``prep_pack.model_gateway.chat_structured``) -- that already
    mutates the one ``app.harness.model_gateway`` object every submodule
    references, unaffected by the package split.
    """
    import pkgutil
    import sys

    import app.production.prep_pack as prep_pack

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(prep_pack, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(prep_pack.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{prep_pack.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_portraits_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.portraits`` in every submodule that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/portraits.py`` was
    one file until it was split into the ``app.portraits`` package (see
    ``app/portraits/__init__.py``), so ``monkeypatch.setattr(portraits, name,
    value)`` only reaches the package's own re-export attribute now, not the
    independent copy each submodule bound for itself at import time -- including
    a submodule that calls a sibling submodule's function, e.g. ``cards.py``
    calling ``_has_column`` via its own ``from ._db_probe import _has_column``.
    This walks every submodule and patches ``name`` wherever it is bound,
    reproducing the pre-split single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.portraits as portraits

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(portraits, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(portraits.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{portraits.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_video_supervisor_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.video_supervisor`` in every submodule that
    actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/video_supervisor.py``
    (4,487 lines) was one file until it was split into the ``app.video_supervisor``
    package (see ``app/video_supervisor/__init__.py``), so
    ``monkeypatch.setattr(video_supervisor, name, value)`` only reaches the
    package's own re-export attribute now, not the independent copy each
    submodule bound for itself at import time -- including ``now`` (imported
    into most submodules from ``app.db``) and a submodule that calls a sibling
    submodule's function, e.g. ``run_loop.py`` calling ``_dispatch_with_heartbeat_async``
    via its own ``from .dispatch import _dispatch_with_heartbeat_async``. This
    walks every submodule and patches ``name`` wherever it is bound,
    reproducing the pre-split single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.video_supervisor as video_supervisor

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(video_supervisor, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(video_supervisor.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{video_supervisor.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_video_modes_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.video_modes`` in every submodule that actually
    binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/video_modes.py``
    (4,081 lines) was one file until it was split into the ``app.video_modes``
    package (see ``app/video_modes/__init__.py``), so
    ``monkeypatch.setattr(video_modes, name, value)`` only reaches the
    package's own re-export attribute now, not the independent copy each
    submodule bound for itself at import time -- including a submodule that
    calls a sibling submodule's function, e.g. ``reference_assemble.py``
    calling ``character_reference_assets`` via its own ``from .asset_lookup
    import character_reference_assets``. This walks every submodule and
    patches ``name`` wherever it is bound, reproducing the pre-split
    single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.video_modes as video_modes

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(video_modes, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(video_modes.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{video_modes.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_screenplay_ir_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.screenplay_ir`` in every submodule that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/screenplay_ir.py`` was
    one file until it was split into the ``app.screenplay_ir`` package (see
    ``app/screenplay_ir/__init__.py``), so ``monkeypatch.setattr(screenplay_ir, name,
    value)`` only reaches the package's own re-export attribute now, not the
    independent copy each submodule bound for itself at import time (including a
    submodule that imports a sibling submodule's name at its own top level, e.g.
    ``compiler.py``'s ``from .compile_setup import _ir_prepare_compile_setup``).
    This walks every submodule and patches ``name`` wherever it is bound,
    reproducing the pre-split single-namespace patch semantics.
    """
    import pkgutil
    import sys

    import app.screenplay_ir as screenplay_ir

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(screenplay_ir, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(screenplay_ir.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{screenplay_ir.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_narrative_blueprint_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.narrative_blueprint`` in every submodule that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/narrative_blueprint.py``
    was one file until it was split into the ``app.narrative_blueprint`` package (see
    ``app/narrative_blueprint/__init__.py``), so ``monkeypatch.setattr(
    narrative_blueprint, name, value)`` only reaches the package's own re-export
    attribute now, not the independent copy each submodule bound for itself at
    import time -- including ``source_facts`` (imported from ``app.source_facts``
    into most submodules directly) and a submodule that calls a sibling
    submodule's function, e.g. ``validate.py`` calling ``blueprint_state_subject_
    issues`` via its own ``from .state_subject_issues import
    blueprint_state_subject_issues``. This walks every submodule and patches
    ``name`` wherever it is bound, reproducing the pre-split single-namespace
    patch semantics.
    """
    import pkgutil
    import sys

    import app.narrative_blueprint as narrative_blueprint

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(narrative_blueprint, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(narrative_blueprint.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{narrative_blueprint.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_narrative_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.narrative`` in every submodule that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/narrative.py`` was
    one file until it was split into the ``app.narrative`` package (see
    ``app/narrative/__init__.py``), so ``monkeypatch.setattr(narrative, name,
    value)`` only reaches the package's own re-export attribute now, not the
    independent copy each submodule bound for itself at import time -- including
    a submodule that imports a sibling submodule's name at its own top level,
    e.g. ``screenplay_validate.py``'s ``from .plan_index import
    action_participant_delivery_errors``. This walks every submodule and patches
    ``name`` wherever it is bound, reproducing the pre-split single-namespace
    patch semantics.
    """
    import pkgutil
    import sys

    import app.narrative as narrative

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(narrative, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(narrative.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{narrative.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_video_plan_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.video_plan`` in every submodule that actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/video_plan.py`` was
    one file until it was split into the ``app.video_plan`` package (see
    ``app/video_plan/__init__.py``), so ``monkeypatch.setattr(video_plan, name,
    value)`` only reaches the package's own re-export attribute now, not the
    independent copy each submodule bound for itself at import time -- including
    a submodule that calls a sibling submodule's function, e.g. ``generate.py``
    calling ``validate_episode_plan`` via its own ``from .validate import
    validate_episode_plan``. This walks every submodule and patches ``name``
    wherever it is bound, reproducing the pre-split single-namespace patch
    semantics.
    """
    import pkgutil
    import sys

    import app.video_plan as video_plan

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(video_plan, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(video_plan.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{video_plan.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_projects_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.domain.projects`` in every submodule that
    actually binds it.

    Same rationale as ``patch_stages_everywhere`` above: ``app/domain/
    projects.py`` (1,999 lines) was one file until it was split into the
    ``app.domain.projects`` package (see ``app/domain/projects/__init__.py``),
    so ``monkeypatch.setattr(projects, name, value)`` only reaches the
    package's own re-export attribute now, not the independent copy each
    submodule bound for itself at import time -- including ``get_conn``
    (``create.py``/``listing.py``/``detail.py``/``episode_delete.py``/
    ``lifecycle.py`` each do their own ``from app.db import get_conn``) and a
    submodule that calls a sibling submodule's function, e.g. ``lifecycle.py``
    calling ``_delete_project_evidence`` via its own ``from app.domain.
    projects.evidence import _delete_project_evidence``. This walks every
    submodule and patches ``name`` wherever it is bound, reproducing the
    pre-split single-namespace patch semantics.

    ``app.domain``'s own generic ``patch_api_everywhere`` (below) already
    recurses into any domain chunk that has a ``__path__`` -- including this
    one now -- so a bare ``from app.domain import projects; monkeypatch.
    setattr(projects, name, value)`` alias *is* caught by ``tests/
    test_api_monkeypatch_guard.py``'s dynamically-computed ``SPLIT_DOMAIN_
    CHUNKS``. But that guard's alias detection only recognizes a local name
    that is exactly ``"projects"`` -- ``import app.domain.projects as
    projects_mod`` / ``from app.domain import projects as projects_api``
    (both common in this test suite, e.g. ``tests/test_account_deletion.py``,
    ``tests/test_core_regressions.py``) are *not* flagged, so a raw
    monkeypatch through one of those renamed aliases would still silently
    reach only the package-level re-export. Use this helper directly for any
    ``app.domain.projects`` symbol regardless of the local alias name --
    ``tests/test_projects_monkeypatch_guard.py`` is its dedicated, alias-aware
    AST guard.
    """
    import pkgutil
    import sys

    import app.domain.projects as projects

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(projects, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(projects.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{projects.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_worker_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.worker`` / ``app.media_exec`` in every submodule
    that actually binds it.

    ``app/worker.py`` and ``app/media_exec/*.py`` used to share a single
    ``exec()``'d namespace: ``app/worker.py`` first ``exec()``'d the same
    ``media_exec/*.py`` chunk source a *second* time into its own ``globals()``
    (producing a second, unrelated copy of every fence exception --
    ``LeaseLost``/``VideoPlanStaleFence``/``ReviewDependencyFence``/etc -- and
    every mutable registry those files define), then briefly became a bare
    ``sys.modules`` alias of ``app.media_exec`` (one shared namespace, but
    ``app.media_exec`` itself was still an ``exec()`` facade). Both are now a
    real package (``app.media_exec``, split into ``common``/``enqueue``/
    ``legacy_keyframes``/``run_job``/``concat``) with ``app/worker.py`` doing
    explicit named re-exports. ``monkeypatch.setattr(worker, name, value)`` now
    only reaches ``app.worker``'s own re-export attribute, not the independent
    copy each submodule bound for itself at import time -- including a
    submodule that calls a sibling submodule's function, e.g. ``run_job.py``
    calling ``enqueue_shot`` via its own ``from .enqueue import enqueue_shot``,
    or a name reimported from an external module independently by several
    chunks, e.g. ``get_conn`` (``common.py``/``enqueue.py``/
    ``legacy_keyframes.py``/``run_job.py``/``concat.py`` each do their own
    ``from app.db import get_conn``). This walks ``app.worker``, the
    ``app.media_exec`` package itself, and every one of its submodules,
    patching ``name`` wherever it is actually bound -- reproducing the
    pre-split single-namespace patch semantics.

    Does not apply to attribute-patching a shared module object *itself* (e.g.
    ``worker.config.PROJECTS_DIR``, ``worker.subprocess.run``, or
    ``worker._queue.put_nowait`` -- ``config``/``subprocess``/the queue and
    worker-pool objects are shared singletons every submodule references by
    the same identity, unaffected by the package split; only patching a *name*
    re-exported by value from a submodule -- i.e. rebinding it to a new object
    -- is broken by it).
    """
    import pkgutil
    import sys

    import app.worker as worker
    import app.media_exec as media_exec

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(worker, name, value, **kwargs)
    monkeypatch.setattr(media_exec, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(media_exec.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{media_exec.__name__}.{mod_name}")
        if submodule is not None and hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)


def patch_quota_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol shared by ``app.quota`` / ``app.quota_tiers`` /
    ``app.quota_addon`` / ``app.quota_scope`` in every one of those modules
    that actually binds it.

    Not a package split (no ``exec()`` facade, no ``pkgutil`` subpackage) --
    these are four flat sibling modules under ``app/``, but the same trap
    applies to any name copied across a module boundary by ``from .x import
    y``. ``app/quota.py`` used to define ``TierLimits``/``TIER_TABLE``/
    ``VALID_TIERS``/``_UNLIMITED``/``_UPGRADE_PATH`` itself; they moved to
    ``app/quota_tiers.py`` (file-length ratchet -- ``app/quota.py`` was sitting
    at 600/600 with zero slack) and ``app/quota.py`` now does
    ``from app.quota_tiers import TIER_TABLE`` (among others) to keep every
    existing ``quota.TIER_TABLE`` call site working unchanged. That import
    creates a *second, independent* binding: ``app.quota.TIER_TABLE`` and
    ``app.quota_tiers.TIER_TABLE`` are two separate names that happen to point
    at the same dict object at import time. ``monkeypatch.setattr(quota,
    "TIER_TABLE", fake)`` only rebinds the first -- ``effective_limits`` is
    defined in ``app.quota`` and reads the global name in *that* module's own
    namespace, so it does see the patch, but anything reading
    ``app.quota_tiers.TIER_TABLE`` directly (or a future caller that imports
    straight from ``app.quota_tiers`` instead of via ``app.quota``) would
    still see the real table. The reverse is just as broken: patching
    ``quota_tiers.TIER_TABLE`` alone never reaches ``app.quota``'s own copy,
    so ``effective_limits``/``check_module_concurrency`` -- the actual call
    path every production gate goes through -- would silently keep using the
    real numbers while the test believes it swapped in a fake tier table. This
    walks both ``app.quota`` and ``app.quota_tiers`` (plus ``app.quota_addon``/
    ``app.quota_scope`` for completeness, in case a future name is shared
    across all four) and patches ``name`` wherever it is actually bound.
    """
    import importlib
    import sys

    kwargs.setdefault("raising", False)
    for mod_name in ("app.quota", "app.quota_tiers", "app.quota_addon", "app.quota_scope"):
        # 用 sys.modules 按全限定名解析，不要用 getattr：子模块若再导出一个与
        # 自身文件同名的符号，包属性会被那个符号覆盖掉引用，getattr 于是静默
        # 返回错的对象，hasattr 判 False、打桩打空且不报错（同一坑 2026-08-30
        # 在 app.media_exec 拆包时实测过一次：get_conn 静默连到生产库）。
        # importlib.import_module 保证四个模块都已加载进 sys.modules（不依赖
        # 调用方是否已经 import 过），且不引入本函数用不到的局部别名。
        module = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value, **kwargs)


def patch_api_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol on ``app.api`` / ``app.domain`` in every submodule that
    actually binds it.

    ``app/api.py`` and ``app/domain/*.py`` used to share a single ``exec()``'d
    namespace: ``app/api.py`` first ``exec()``'d the same ``domain/*.py`` chunk
    source a *second* time into its own ``globals()`` (producing a second,
    unrelated copy of every class, function and mutable registry those files
    define -- ``app.api.router`` was not the same ``APIRouter`` instance as
    ``app.domain.storyboard_ops.router``), then briefly became a bare
    ``sys.modules`` alias of ``app.domain`` (one shared namespace, but
    ``app.domain`` itself was still an ``exec()`` facade covering all seven
    chunks). Both are now a real package (``app.domain``, split into
    ``common``/``projects``/``bible_ops``/``screenplay_ops``/``storyboard_ops``/
    ``review_wall``/``video_ops``) with ``app/api.py`` doing explicit named
    re-exports. ``monkeypatch.setattr(api, name, value)`` now only reaches
    ``app.api``'s own re-export attribute, not the independent copy each
    submodule bound for itself at import time -- including a submodule that
    calls a sibling submodule's function, e.g. ``video_ops.py`` calling
    ``_board_from_shot_rows`` via its own ``from app.domain.storyboard_ops
    import _board_from_shot_rows``, or a name reimported from an external
    module independently by several chunks, e.g. ``get_conn`` (``common.py``/
    ``projects.py``/``bible_ops.py``/``screenplay_ops.py``/``storyboard_ops.py``/
    ``review_wall.py``/``video_ops.py`` each do their own ``from app.db import
    get_conn``). This walks ``app.api``, the ``app.domain`` package itself, and
    every one of its seven chunk submodules, patching ``name`` wherever it is
    actually bound -- reproducing the pre-split single-namespace patch
    semantics.

    Some of those seven chunks (``bible_ops``, and any other chunk split the
    same way later) are themselves further split into a real sub-package of
    concern-based files (e.g. ``app/domain/bible_ops/precheck.py``) instead of
    one flat module -- same problem one level deeper: each of *those* files
    holds its own independent copy of any name it imports (from a sibling
    file in the same sub-package or from elsewhere), so patching only
    ``app.domain.bible_ops`` itself misses whichever sub-file the real call
    site lives in. This recurses into any chunk that has a ``__path__`` (i.e.
    is a package, not a plain module) and patches ``name`` in every one of
    its own submodules too.

    Does not apply to attribute-patching a shared module object *itself* (e.g.
    ``api.worker.pause_episode_video_tasks``, ``api.task_registry.record`` --
    ``worker``/``task_registry`` are shared singleton modules every chunk
    references by the same identity, unaffected by the package split; only
    patching a *name* re-exported by value from a chunk -- i.e. rebinding it to
    a new object -- is broken by it).
    """
    import pkgutil

    import sys

    import app.api as api
    import app.domain as domain

    kwargs.setdefault("raising", False)
    monkeypatch.setattr(api, name, value, **kwargs)
    monkeypatch.setattr(domain, name, value, **kwargs)
    for _, mod_name, _ in pkgutil.iter_modules(domain.__path__):
        # 用 sys.modules 按全限定名解析叶子模块，不要用 getattr：子模块若
        # 再导出一个与自身文件同名的符号（`from .x import x as x`），包属性
        # 会被那个符号覆盖掉子模块引用，getattr 于是静默返回错的对象，
        # hasattr 判 False、打桩打空且不报错（2026-08-30 实测：曾让
        # get_conn 静默连到生产库，造成 7 个测试假绿/假红）。
        submodule = sys.modules.get(f"{domain.__name__}.{mod_name}")
        if submodule is None:
            continue
        if hasattr(submodule, name):
            monkeypatch.setattr(submodule, name, value, raising=False)
        sub_path = getattr(submodule, "__path__", None)
        if sub_path is None:
            continue
        # Resolve each leaf submodule through sys.modules by its fully
        # qualified name, not `getattr(submodule, leaf_name)`. A further-split
        # chunk's own __init__.py can (and does -- e.g.
        # ``storyboard_ops/episode_detail.py`` defines and re-exports a
        # function literally named ``episode_detail``) re-export a symbol
        # whose name collides with one of its own submodule's filenames; the
        # explicit ``from .episode_detail import episode_detail as
        # episode_detail`` re-export line rebinds the package attribute
        # ``episode_detail`` to that *function*, shadowing the submodule
        # reference Python's import machinery bound there first. `getattr`
        # then silently returns the function (which has no ``get_conn``
        # attribute, so `hasattr` is False and nothing gets patched) instead
        # of the actual submodule -- the real call site inside that submodule
        # keeps its original, unpatched ``get_conn`` and silently queries the
        # wrong database. Every leaf submodule of an already-imported package
        # is guaranteed to already be in ``sys.modules`` (the package's own
        # ``__init__.py`` imports all of them to build its re-export surface),
        # so this lookup is reliable regardless of what its ``__init__.py``
        # chose to re-export under the same name.
        for _, leaf_name, _ in pkgutil.iter_modules(sub_path):
            leaf = sys.modules.get(f"{submodule.__name__}.{leaf_name}")
            if leaf is not None and hasattr(leaf, name):
                monkeypatch.setattr(leaf, name, value, raising=False)


_LIVE_INTEGRATION = False
_ISOLATION_SESSION: IsolationSession | None = None
_PROVIDER_ISOLATION: ProviderConfigurationIsolation | None = None
_SANDBOX: Path | None = None
_SANDBOX_OWNED = False
_DATABASE_TEMPLATE: Path | None = None
_DATABASE_TEMPLATE_INITIALIZED = False

def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("manju test isolation")
    group.addoption(
        "--live-integration",
        action="store_true",
        default=False,
        help="run only tests marked live_integration with real runtime access",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _LIVE_INTEGRATION, _ISOLATION_SESSION, _PROVIDER_ISOLATION
    global _SANDBOX, _SANDBOX_OWNED
    global _DATABASE_TEMPLATE, _DATABASE_TEMPLATE_INITIALIZED

    _LIVE_INTEGRATION = bool(config.getoption("--live-integration"))
    _DATABASE_TEMPLATE = None
    _DATABASE_TEMPLATE_INITIALIZED = False
    if _LIVE_INTEGRATION:
        os.environ["MANJU_TEST_PROFILE"] = "live-integration"
        return

    # 沙箱在本文件 import 期就建好了（见文件顶部：必须早于 app.config 的 import
    # 才能阻止 .env 被读进来）。这里只接手，不再重建——重建会让 app.config 在
    # import 期算出的 RUNTIME_ROOT 指向一个已经作废的目录。
    assert _SANDBOX_PRECREATED is not None, "isolated profile 下沙箱应已在 import 期建好"
    _SANDBOX = _SANDBOX_PRECREATED
    _SANDBOX_OWNED = _SANDBOX_PRECREATED_OWNED
    config.option.basetemp = str(_SANDBOX / "pytest-tmp")

    isolate_provider_environment(os.environ)

    # Configure the process before test module collection imports application code.
    from app import config as app_config

    app_config.RUNTIME_ROOT = _SANDBOX
    app_config.PROJECTS_DIR = _SANDBOX / "projects"
    app_config.DATA_DIR = _SANDBOX / "data"
    app_config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    app_config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    template_dir = Path(tempfile.mkdtemp(prefix="pytest-db-template-", dir=app_config.DATA_DIR))
    _DATABASE_TEMPLATE = template_dir / "manju.db"
    app_config.DB_PATH = _DATABASE_TEMPLATE
    _PROVIDER_ISOLATION = ProviderConfigurationIsolation(
        settings=app_config,
        environment=os.environ,
        blocked_endpoint=UNROUTABLE_PROVIDER_BASE_URL,
    )
    _PROVIDER_ISOLATION.apply()

    from app import db

    db.DATA_DIR = app_config.DATA_DIR
    db.DB_PATH = app_config.DB_PATH
    _ISOLATION_SESSION = IsolationSession(
        sandbox=_SANDBOX,
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    if _ISOLATION_SESSION is not None:
        _ISOLATION_SESSION.install()


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    live = bool(config.getoption("--live-integration"))
    for item in items:
        marked_live = item.get_closest_marker("live_integration") is not None
        if live and not marked_live:
            item.add_marker(pytest.mark.skip(reason="live integration mode runs only explicitly marked tests"))
        elif not live and marked_live:
            item.add_marker(pytest.mark.skip(reason="requires --live-integration"))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus
    if _ISOLATION_SESSION is None:
        return
    _ISOLATION_SESSION.restore()
    if not _ISOLATION_SESSION.audit.violations:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "TEST ISOLATION VIOLATIONS", red=True)
        for violation in _ISOLATION_SESSION.audit.violations:
            reporter.write_line(violation, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    if _ISOLATION_SESSION is not None:
        _ISOLATION_SESSION.restore()
    db = sys.modules.get("app.db")
    if db is not None:
        local = getattr(db, "_local", None)
        conn = getattr(local, "conn", None)
        if conn is not None:
            conn.close()
            local.conn = None
    if _SANDBOX_OWNED and _SANDBOX is not None:
        shutil.rmtree(_SANDBOX, ignore_errors=True)


def _restore_isolated_runtime(db, *, database_path: Path | None = None) -> None:
    if _SANDBOX is None or _DATABASE_TEMPLATE is None:
        return

    from app import config as app_config

    target_database = (database_path or _DATABASE_TEMPLATE).resolve()
    os.environ["MANJU_TEST_PROFILE"] = "isolated"
    os.environ["MANJU_TEST_SANDBOX"] = str(_SANDBOX)
    app_config.TEST_PROFILE = "isolated"
    app_config._test_sandbox = str(_SANDBOX)
    app_config.RUNTIME_ROOT = _SANDBOX
    app_config.PROJECTS_DIR = _SANDBOX / "projects"
    app_config.DATA_DIR = _SANDBOX / "data"
    app_config.DB_PATH = target_database
    app_config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    app_config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    db._local.conn = None
    db.DATA_DIR = app_config.DATA_DIR
    db.DB_PATH = app_config.DB_PATH
    if _PROVIDER_ISOLATION is not None:
        _PROVIDER_ISOLATION.apply()


def _connection_database_path(connection: sqlite3.Connection) -> Path | None:
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.ProgrammingError:
        return None
    if row is None or not row[2]:
        return None
    return Path(row[2]).resolve()


def _release_local_connection(db, *, owned_database: Path) -> None:
    connection = getattr(db._local, "conn", None)
    if connection is None:
        return
    db._local.conn = None
    if _connection_database_path(connection) != owned_database.resolve():
        return
    try:
        if connection.in_transaction:
            connection.rollback()
    finally:
        connection.close()


def _initialize_database_template(db) -> None:
    global _DATABASE_TEMPLATE_INITIALIZED

    if _DATABASE_TEMPLATE_INITIALIZED:
        return
    if _DATABASE_TEMPLATE is None:
        raise RuntimeError("pytest database template is not configured")

    _restore_isolated_runtime(db, database_path=_DATABASE_TEMPLATE)
    connection = db.get_conn()
    try:
        db.init_db()
    finally:
        connection.close()
        db._local.conn = None
    _DATABASE_TEMPLATE_INITIALIZED = True


def _clone_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _reset_command_bus_runtime(capability_bus) -> None:
    capability_bus._BUS = capability_bus.CommandBus(capability_bus.get_registry())


def _reset_media_worker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not let an in-memory media backlog leak between isolated tests.

    Must reset the queue on every ``app.media_exec`` submodule that holds its
    own bound copy, not just ``app.worker``'s re-export attribute -- since the
    package split, ``app.media_exec.worker_loop``'s and
    ``app.media_exec.worker_lifecycle``'s own ``_queue``/``_video_ready_queue``/
    ``_poll_queue`` names (each imported at its own top level via ``from
    .common import _queue, ...``) are what ``_worker_loop``/``ensure_workers``/
    ``stop`` actually read and mutate (``run_job.py`` further split
    2026-08-30 -- see that file's module docstring). A plain
    ``worker._queue = asyncio.Queue()`` (the pre-split form, back when
    ``app.worker`` and ``app.media_exec`` were the same aliased module object)
    would now only rebind ``app.worker``'s own attribute, leaving each
    submodule's operational copy holding the previous test's queue --
    exactly the silent-no-op ``patch_worker_everywhere`` (see above) exists to
    prevent, so this reset reuses it instead of assigning directly.
    """

    worker = sys.modules.get("app.worker")
    if worker is None:
        return
    fresh_queue = asyncio.Queue()
    patch_worker_everywhere(monkeypatch, "_queue", fresh_queue)
    patch_worker_everywhere(monkeypatch, "_reference_queue", fresh_queue)
    patch_worker_everywhere(monkeypatch, "_video_ready_queue", asyncio.Queue())
    patch_worker_everywhere(monkeypatch, "_poll_queue", asyncio.Queue())


@pytest.fixture(autouse=True)
def _reset_capability_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """每个测试独占数据库，并重置进程内 Command Bus 与审批状态。"""

    if _LIVE_INTEGRATION:
        yield
        return

    from app.auth.principal import Principal, set_current_principal
    from app.capabilities import bus as capability_bus
    from app.capabilities.loader import ensure_catalog_loaded
    from app.capabilities.policy import reset_approvals_for_tests
    from app import db

    if _DATABASE_TEMPLATE is None:
        raise RuntimeError("pytest database template is not configured")
    _release_local_connection(db, owned_database=_DATABASE_TEMPLATE)
    _initialize_database_template(db)

    test_database = tmp_path / "manju.db"
    _clone_database(_DATABASE_TEMPLATE, test_database)
    _restore_isolated_runtime(db, database_path=test_database)
    # app.capabilities.dispatch 不再自带 ensure_catalog_loaded()（避免 dispatch <->
    # catalog 反向 import 焊环，见该模块的注释）；测试路径下由这里统一兜底，
    # 不依赖 TestClient 是否以 `with` 触发过 FastAPI lifespan，也不依赖同一 pytest
    # 会话里更早的测试是否已经加载过目录。
    ensure_catalog_loaded()
    _reset_command_bus_runtime(capability_bus)
    _reset_media_worker_runtime(monkeypatch)
    reset_approvals_for_tests()
    # 直接调用 Command Bus 的测试（不经 HTTP，也就绕过 require_local_session）
    # 兜底注入一个系统管理员身份，后续阶段 Command Bus 收紧 scope 校验时
    # 这批测试不需要逐个改造。
    set_current_principal(
        Principal(user_id="test-bus-admin", username="test-bus-admin",
                  is_system_admin=True)
    )

    try:
        yield
    finally:
        try:
            monkeypatch.undo()
        finally:
            _reset_command_bus_runtime(capability_bus)
            _reset_media_worker_runtime(monkeypatch)
            reset_approvals_for_tests()
            set_current_principal(None)
            _release_local_connection(db, owned_database=test_database)
            _restore_isolated_runtime(db, database_path=_DATABASE_TEMPLATE)


_TEST_ADMIN_USERNAME = "test-admin"


def ensure_test_admin() -> str:
    """确保当前（隔离沙盒）数据库里有一个系统管理员账号，返回其 user_id。"""
    from app.auth.passwords import hash_password
    from app.db import get_conn, new_id, now

    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM users WHERE username=?", (_TEST_ADMIN_USERNAME,)
    ).fetchone()
    if row is not None:
        return str(row["id"])
    user_id = new_id("user")
    ts = now()
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider,
               status, is_system_admin, must_change_password, created_at,
               password_changed_at
           ) VALUES(?,?,?,?,'local','active',1,0,?,?)""",
        (
            user_id,
            _TEST_ADMIN_USERNAME,
            "测试系统管理员",
            hash_password("test-admin-password-000"),
            ts,
            ts,
        ),
    )
    conn.commit()
    return user_id


def session_headers() -> dict[str, str]:
    """测试用：为隔离测试库里的系统管理员账号签发一枚真实登录会话。"""
    from app.auth.sessions import create_session

    user_id = ensure_test_admin()
    return {"X-Manju-Session": create_session(user_id)}


class SessionTestClient:
    """包装 TestClient，自动附加 X-Manju-Session（Todolist T1 回归）。"""

    def __init__(self, client):
        self._client = client
        self._headers = session_headers()

    def request(self, method: str, url: str, **kwargs):
        headers = {**self._headers, **(kwargs.pop("headers", None) or {})}
        return self._client.request(method, url, headers=headers, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._client, name)
