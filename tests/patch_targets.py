"""``patch_<pkg>_everywhere()`` helpers for package splits, kept out of
``tests/conftest.py`` purely for its line-count baseline (``app/
FILE_CONVENTIONS.toml``, ratchet only tightens -- see that file's own
docstring on why baselines don't get bumped to fit new content).
``tests/conftest.py`` re-exports every name here so existing call sites
(``from tests.conftest import patch_orgs_everywhere`` etc.) keep working
unchanged; the AST monkeypatch guards (``tests/test_orgs_monkeypatch_guard.py``
etc.) only check *call-site* shape, not where the helper itself is defined,
so moving these here does not weaken them -- confirmed by re-running the
guard suite after the move.
"""
from __future__ import annotations


def patch_models_registry_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol across ``app.models_registry``'s submodules (``crypto``/
    ``keyprovider``/``store``/``migration``) wherever it is actually bound.

    Real package (EP-05 first phase, 2026-09-10), not an ``exec()`` facade.
    Call sites use module-qualified access (``from app.models_registry import
    store as x`` then ``x.get_credential(...)``), so this isn't the classic
    ``from .x import y`` name-copy trap today, but the split-package mandate
    is unconditional and ``app/model_registry.py``/``app/hiagent.py``/
    ``app/video_providers.py``/``app/system_api.py`` are the flagged
    high-risk call sites. Walks each submodule, patches ``name`` where bound.
    """
    import importlib
    import sys

    kwargs.setdefault("raising", False)
    for mod_name in (
        "app.models_registry.crypto", "app.models_registry.keyprovider",
        "app.models_registry.store", "app.models_registry.migration",
    ):
        # sys.modules 按全限定名解析，不用 getattr：同名再导出会让 getattr 静默
        # 拿错对象（2026-08-30 在 app.media_exec 拆包时实测过：get_conn 连到生产库）。
        module = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value, **kwargs)


def patch_orgs_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol shared across ``app.orgs``'s submodules (``schema``/
    ``store``/``service``/``bootstrap``) wherever it is actually bound.

    ``app/orgs`` is a real package from day one (EP-01 第一阶段，2026-09-10) --
    each submodule holds its own module namespace, and ``__init__.py``
    deliberately imports none of them (see its docstring: avoids
    ``app.orgs.bootstrap`` (L5) leaking onto the ``app.orgs`` package's own
    L2 layer declaration). Every production call site reaches submodules via
    module-qualified access (``from app.orgs import store`` then
    ``store.get_role(...)``), so an attribute lookup at call time sees a patch
    applied directly to that submodule regardless of local aliases -- but the
    mandate to add this helper + its AST guard is unconditional for every
    package split in this repo (13 precedents before this one, ``app.
    models_registry`` from the same day is the closest template).
    """
    import importlib
    import sys

    kwargs.setdefault("raising", False)
    for mod_name in ("app.orgs.schema", "app.orgs.store", "app.orgs.service", "app.orgs.bootstrap"):
        # 用 sys.modules 按全限定名解析，不要用 getattr：见
        # patch_models_registry_everywhere 的同一条注释（getattr 会被子模块
        # 再导出的同名符号覆盖，静默返回错对象）。
        module = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value, **kwargs)


def patch_authz_everywhere(monkeypatch, name, value, **kwargs):
    """Patch a symbol shared across ``app.authz``'s submodules (``resolve``/
    ``policy``/``catalog``) wherever it is actually bound.

    ``app/authz`` predates EP-01 with a single real submodule (``resolve.py``,
    re-exported via ``__init__.py``); EP-01 adds ``policy.py``/``catalog.py``
    as siblings, so the pre-existing "one submodule, patch it directly" habit
    now has the exact same package-split hazard as the other 13 precedents in
    this repo -- a future call site could do ``from .policy import
    command_allowed`` inside ``resolve.py`` and copy the name, and
    ``monkeypatch.setattr(policy, "command_allowed", fake)`` would silently
    miss it. Same walk-and-patch shape as ``patch_orgs_everywhere``.
    """
    import importlib
    import sys

    kwargs.setdefault("raising", False)
    for mod_name in ("app.authz.resolve", "app.authz.policy", "app.authz.catalog"):
        module = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value, **kwargs)
