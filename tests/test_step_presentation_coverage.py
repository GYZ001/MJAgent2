"""Structural guard: every statically-named step_key must have a registered
business name in app.orchestration.engine._STEP_PRESENTATIONS.

This is a C-class regression guard (this project's own taxonomy: "new
integration point forgot to wire up an existing guard"). It has fired for
real twice on episode_prep_pack alone -- once when the 4 original steps
shipped without checking test_project_observability.py's fixed-label
assertion, and again when #27 added episode_prep_pack_character_discovery /
episode_prep_pack_scene_discovery and neither got registered, surfacing to
the user as an unnamed node in the observability trace ("业务名称待配置").
Prompt-engineering discipline and manual review both failed to catch this
twice; a structural scan that runs in CI is the only guard that generalizes
to the next integration point nobody remembers to check by hand.

Scope note: dynamic step_key values built from a variable/f-string (e.g.
``f"{self.stage_key}.iteration"`` in app/loops/base.py, or
``storyboard_scene_{n}.iteration``) are NOT static literals and this AST
scan cannot and does not enumerate them -- they are unbounded in number by
construction and are handled by a *different*, already-existing mechanism:
app.observability.api._trace_step_label's regex table, checked before
falling back to step_presentation(). That table is not this test's concern;
only step_key arguments that are plain string literals at their call site
are in scope here, because those are exactly the ones a developer could
have registered but didn't.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.orchestration.engine import _STEP_PRESENTATIONS

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# Function/method names that create a step_run row, keyed to the 0-based
# position of the step_key argument among *positional* args (run_id/self is
# already consumed for bound calls). Covers every step-creating entry point
# in the codebase as of this writing (see this file's module docstring for
# how dynamic keys are deliberately excluded, not missed):
#   - app.evidence.repository.create_step(run_id, step_key, ...)
#   - app.orchestration.engine.WorkflowRecorder.step(step_key, operation, ...)
#     (called as `recorder.step(...)` or `WorkflowRecorder(...).step(...)`)
#   - app.production.prep_pack._begin_step(run_id, step_key, ...)
#   - app.production.prep_pack._run_sync_step(run_id, step_key, fn)
#   - app.production.prep_pack._run_async_step(run_id, step_key, fn)
#   - app.production.prep_pack._call_structured(*, run_id, step_key, ...)
#     (第29轮身份绑定审判程序回归发现的盲区：这是一个更高层的包装函数，
#     自己内部再调用 _begin_step(run_id, step_key, ...) -- 但那次内部调用
#     传的是变量 step_key，不是字面量，原扫描器天然识别不了；真正的字面量
#     只出现在调用 _call_structured(step_key="...") 这一层，而 _call_
#     structured 本身从未被列入识别集，所以字面量连"看得到但过滤掉"都
#     算不上，是根本不会被访问到的调用点。凡是后续新增"包一层再转发给
#     _begin_step/_run_async_step/_run_sync_step 的 step_key"式 helper，
#     都必须把 helper 自己的名字加进这张表，不能假设内层调用会被扫描器
#     顺着传播找到——本扫描器不做数据流/调用链追踪，只认字面量出现的
#     那一个调用点。
_STEP_KEY_ARG_POSITION = {
    "create_step": 1,
    "step": 0,
    "_begin_step": 1,
    "_run_sync_step": 1,
    "_run_async_step": 1,
    "_call_structured": 1,
}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _literal_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_literal_step_keys(root: Path) -> dict[str, list[str]]:
    """Return {step_key: [".../file.py:lineno", ...]} for every statically
    literal step_key argument found across all *.py files under ``root``."""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name not in _STEP_KEY_ARG_POSITION:
                continue
            position = _STEP_KEY_ARG_POSITION[name]
            key = None
            # keyword form: step_key="..."
            for kw in node.keywords:
                if kw.arg == "step_key":
                    key = _literal_str(kw.value)
                    break
            # positional form
            if key is None and len(node.args) > position:
                key = _literal_str(node.args[position])
            if key is None:
                continue  # dynamic (f-string/variable) -- out of scope, see module docstring
            found.setdefault(key, []).append(f"{path.relative_to(root.parent)}:{node.lineno}")
    return found


def test_scanner_finds_known_step_keys():
    """Guard the scanner itself: if this goes empty/wrong, the coverage
    test below would pass vacuously and stop meaning anything."""
    found = scan_literal_step_keys(APP_DIR)
    assert "episode_prep_pack_publish" in found
    assert "screenplay_document" in found
    assert "scene_bible" in found  # recorder.step("scene_bible", ...) form


def test_every_statically_named_step_key_has_a_registered_business_name():
    found = scan_literal_step_keys(APP_DIR)
    missing = sorted(
        key for key in found
        if key not in _STEP_PRESENTATIONS or not _STEP_PRESENTATIONS[key].name.strip()
    )
    assert missing == [], (
        "以下 step_key 有静态字面量调用点，但 "
        "app.orchestration.engine._STEP_PRESENTATIONS 里没有非空业务名，"
        "用户会在观测台看到「业务名称待配置」：\n"
        + "\n".join(f"  {key}: {found[key]}" for key in missing)
    )
