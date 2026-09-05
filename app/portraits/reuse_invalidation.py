"""复用结果失效：模型产出下载失败时把该幂等 operation 的账本成功结果标为不可复用。"""
from __future__ import annotations

async def download_or_invalidate_reuse(conn, url: str, dest: str, call_meta: dict | None) -> None:
    """下载模型产出；失败即把该幂等 operation 在账本里的成功结果标成不可复用后再抛。
    复用来的图片 URL 在供应商那边可能已经 500（2026-09-05：3 个角色的旧 URL 让定妆任务
    连败 5 次，每次都复用同一条坏结果）；标记后下一次生成会真正重新出图。"""
    from app import hiagent
    try:
        await hiagent.download(url, dest)
    except hiagent.ProviderError:
        operation_id = str((call_meta or {}).get("operation_id") or "")
        if operation_id:
            conn.execute(
                "UPDATE provider_calls SET recovery_disposition='OUTPUT_UNREACHABLE' "
                "WHERE operation_id=? AND status IN ('OK','SUCCESS','SUCCEEDED')",
                (operation_id,),
            )
            conn.commit()
        raise
