"""FULL_REGEN_AFTER_QA_DENIED 等生产策略。"""
from __future__ import annotations

from typing import Any, Iterable

from app.production.metrics import record_full_regen_denied
from app.production.revision import ProductionRevision, get_production_revision


class FullRegenDenied(PermissionError):
    """首轮 QA 之后禁止完整生成 / 根对象替换。"""

    code = "FULL_REGEN_AFTER_QA_DENIED"

    def __init__(self, message: str, *, revision_id: str | None = None):
        self.revision_id = revision_id
        super().__init__(message)


# Patch 允许的 op；根替换与整组覆盖显式拒绝
ALLOWED_PATCH_OPS = frozenset({
    "replace_field",
    "add_field",
    "create_node",
    "delete_node",
    "move_node",
    "split_node",
    "insert_node",
    "rederive",
    "normalize_overdetail",
    "split_dialogue_chain_by_scene",
})

FORBIDDEN_PATCH_OPS = frozenset({
    "replace",
    "remove",
    "replace_root",
    "replace_array",
    "delete_all",
    "insert_all",
    "full_replace",
})

FORBIDDEN_ROOT_PATHS = frozenset({
    "",
    "/",
    "$",
    "scene_blocks",
    "shots",
    "outline.shots",
    "scene_outline",
    "full_script_text",  # 必须由 rederive 重建，禁止直接整段替换
})


def deny_full_regen_after_qa(
    revision: ProductionRevision | str,
    *,
    command: str,
    episode_id: str | None = None,
) -> None:
    """若已做过首轮 Evaluation，拒绝任何完整生成命令。"""
    rev = get_production_revision(revision) if isinstance(revision, str) else revision
    if rev is None:
        return
    if not rev.first_evaluation_done:
        return
    record_full_regen_denied(
        kind=rev.kind,
        episode_id=episode_id or rev.episode_id,
        revision_id=rev.id,
        reason=f"command={command}",
    )
    raise FullRegenDenied(
        f"FULL_REGEN_AFTER_QA_DENIED: revision {rev.id} 已完成首轮 QA，"
        f"禁止再次调用完整生成命令 {command}",
        revision_id=rev.id,
    )


def assert_baseline_allowed(
    revision: ProductionRevision | str,
    *,
    command: str,
    episode_id: str | None = None,
) -> None:
    """Baseline 只能发生一次；已有 baseline 或已 QA 则拒绝。"""
    rev = get_production_revision(revision) if isinstance(revision, str) else revision
    if rev is None:
        return
    if rev.baseline_done or rev.first_evaluation_done:
        record_full_regen_denied(
            kind=rev.kind,
            episode_id=episode_id or rev.episode_id,
            revision_id=rev.id,
            reason=f"baseline_already_done command={command}",
        )
        raise FullRegenDenied(
            f"FULL_REGEN_AFTER_QA_DENIED: revision {rev.id} 已完成 Baseline 生成"
            f"（count={rev.baseline_generation_count}），禁止再次完整生成",
            revision_id=rev.id,
        )


def assert_patch_ops_allowed(operations: Iterable[dict[str, Any]]) -> None:
    """拒绝根替换、整组覆盖、先删后建等非法 Patch。"""
    ops = list(operations or [])
    if not ops:
        raise ValueError("patch operations 不能为空")
    for op in ops:
        name = str(op.get("op") or "").strip()
        if name in FORBIDDEN_PATCH_OPS:
            raise FullRegenDenied(
                f"FULL_REGEN_AFTER_QA_DENIED: 禁止 Patch 操作 {name}"
            )
        if name not in ALLOWED_PATCH_OPS:
            raise FullRegenDenied(
                f"FULL_REGEN_AFTER_QA_DENIED: 未知或不允许的 Patch 操作 {name}"
            )
        path = str(op.get("path") or "").strip().lstrip("/")
        target = op.get("target") or {}
        # 整数组替换：path 指向根集合且 op 试图塞入完整 list
        if path in FORBIDDEN_ROOT_PATHS and name in {"replace_field", "add_field"}:
            value = op.get("value")
            if isinstance(value, list) or path in {"", "/", "$"}:
                raise FullRegenDenied(
                    f"FULL_REGEN_AFTER_QA_DENIED: 禁止替换根路径 {path or '/'}"
                )
        # 显式 delete-all + insert-all 模式
        if name == "delete_node" and (target.get("id") in {"*", "ALL", "all"}):
            raise FullRegenDenied(
                "FULL_REGEN_AFTER_QA_DENIED: 禁止 delete-all"
            )
