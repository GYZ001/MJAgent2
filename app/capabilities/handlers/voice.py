"""Handler：人物卡固定声音命令的 REST 包装（``call_guarded`` 统一转译异常）。

每个 handler 以模块限定方式（``voice_routes.<函数>``）调用
``app.domain.bible_ops.voice_routes`` 里同名的真实 REST 路由函数——与 ``handlers.bible.portrait_generate`` 调用 ``api.start_refs``
同一先例：该函数内部会先探测 ``in_handler()``，从这里进来时直接跳过
``ui_route`` 重新分发，执行真正的业务逻辑。直接从 ``app.domain.bible_ops``
导入（不经 ``app.api``），理由同 ``handlers.bible.nominate_character``：
``app/api.py``/``app/domain/__init__.py`` 的整仓再导出列表卡在 line_count
棘轮基线的零余量上。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def suggest_description(args: I.VoiceDescriptionInput) -> CommandResult:
    # 延迟导入：handlers 包在 app.capabilities 顶层初始化期被 catalog 加载，
    # 此时提前 import app.domain.bible_ops 会与它函数体内的 app.capabilities.
    # dispatch 导入互相等待，同 handlers.bible 各处的既有写法。
    from app.domain.bible_ops import voice_routes

    outcome = await call_guarded(voice_routes.voice_description_suggest, args.project_id, args.character)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"角色「{args.character}」的声音描述建议已生成", data=outcome)


async def generate(args: I.VoiceGenerateInput) -> CommandResult:
    from app.domain.bible_ops import voice_routes  # 延迟导入理由同上（suggest_description）

    outcome = await call_guarded(
        voice_routes.generate_character_voice, args.project_id, args.character,
        body={
            "voice_prompt": args.voice_prompt, "preview_text": args.preview_text,
            "idempotency_key": args.idempotency_key,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"角色「{args.character}」的声音已生成", data=outcome)


async def adopt(args: I.VoiceAdoptInput) -> CommandResult:
    from app.domain.bible_ops import voice_routes  # 延迟导入理由同上（suggest_description）

    outcome = await call_guarded(voice_routes.adopt_character_voice, args.project_id, args.character, args.voice_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"角色「{args.character}」已采用新声音", data=outcome)


async def generate_missing(args: I.ProjectScopedInput) -> CommandResult:
    from app.domain.bible_ops import voice_routes  # 延迟导入理由同上（suggest_description）

    outcome = await call_guarded(voice_routes.generate_missing_voices, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已受理 {outcome.get('accepted', 0)} 个角色的声音补齐", data=outcome)
