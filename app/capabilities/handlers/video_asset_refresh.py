"""「参考资产已更新」面板两个写命令的 Handler——独立小文件，不放进
``app/capabilities/handlers/video.py``：该文件当前 499/500 行，没有余量
（CLAUDE.md「装不下时先想怎么拆，不要先想加基线」，同款先例见
``app/capabilities/inputs_asset_refresh.py`` 模块文档）。"""
from __future__ import annotations

from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.inputs_asset_refresh import (
    VideoAssetRefreshAdoptInput,
    VideoAssetRefreshRegenerateInput,
)
from app.capabilities.schemas import CommandResult


async def asset_refresh_regenerate(args: VideoAssetRefreshRegenerateInput) -> CommandResult:
    # 直接从真源导入，不借道 app.api/app.domain 门面：两个门面都已贴着各自
    # 的 FILE_CONVENTIONS 行数基线、没有余量，不能再为两个新符号转手一次
    # （CLAUDE.md「再导出门面不得再长，且必须从真源导出」）。
    from app.domain.video_ops.asset_refresh import _asset_refresh_regenerate_core

    outcome = await call_guarded(
        _asset_refresh_regenerate_core, args.episode_id,
        {
            "entity_keys": args.entity_keys, "qualification_version": args.qualification_version,
            "idempotency_key": args.idempotency_key, "request_id": args.request_id,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    # 只有「一段都没排上」才算整体失败——否则一旦某段被闸门拦住就把已经排队
    # 成功的那些段也判成 FAILED，`raise_if_failed` 在 data 没有 "code" 键时
    # 只回传一句纯文本 summary，前端据此渲染"段 X 被拦住"的逐段详情代码永远
    # 执行不到，已排队成功的段也不会在面板里刷新成最新状态（2026-10-03 复现）。
    # 全部被拦时仍返回 FAILED，但显式带上 "code" 让 queued/errors 结构进 detail。
    if outcome.get("errors") and not outcome.get("queued"):
        return failed(
            outcome.get("message") or "本组段落全部被闸门拦住",
            error_code="asset_refresh_regenerate_blocked",
            data={**outcome, "code": "asset_refresh_regenerate_blocked"},
        )
    return succeeded(
        outcome.get("message") or "已提交重生成", data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}"],
    )


async def asset_refresh_adopt(args: VideoAssetRefreshAdoptInput) -> CommandResult:
    # 直接从真源导入：同上一个 handler 的理由。
    from app.domain.video_ops.asset_refresh import _asset_refresh_adopt_core

    outcome = await call_guarded(
        _asset_refresh_adopt_core, args.episode_id,
        {
            "entity_key": args.entity_key, "versions": args.versions, "reason": args.reason,
            "qualification_version": args.qualification_version,
            "idempotency_key": args.idempotency_key, "request_id": args.request_id,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    adopted = outcome.get("adopted") or []
    return succeeded(
        f"已整组采用 {len(adopted)} 段", data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}"],
    )
