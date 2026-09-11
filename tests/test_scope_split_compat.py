"""EP-01 §7 scope 细分兼容性：manju:generation-media 拆分为 media-generate ∪
media-decide 后，旧 MCP token 能调用的命令集合必须一条不多一条不少。

断言方式：真的经 ``app.mcp.tools.call_tool()``（MCP 服务器对外唯一的调用
入口，真实校验 scope 的地方）发起调用，看会不会被 ``ForbiddenError`` 挡住
——不是检查 token 的 scopes 字段本身，也不是重新实现一遍集合运算再跟自己
比对。
"""
from __future__ import annotations

import asyncio

import pytest

from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.registry import get_registry
from app.mcp import auth as mcp_auth
from app.mcp import tools as mcp_tools
from app.mcp.errors import ForbiddenError


def _claims(scopes: frozenset[str], *, token_id: str = "tok_test") -> mcp_auth.TokenClaims:
    return mcp_auth.TokenClaims(token_id=token_id, scopes=frozenset(scopes))


async def _call_without_forbidden(name: str, args: dict) -> None:
    """发起一次真实调用；只关心有没有被 scope 挡在门外（ForbiddenError）。

    参数大多指向不存在的对象，会在 scope 校验之后的域逻辑里报错（找不到
    shot/episode 等）——那是预期的、与本测试无关的失败，唯独 ForbiddenError
    不能出现。
    """
    claims = _claims({mcp_auth.LEGACY_GENERATION_MEDIA_SCOPE})
    try:
        await mcp_tools.call_tool(name, args, claims=claims)
    except ForbiddenError as exc:
        pytest.fail(f"legacy manju:generation-media token unexpectedly forbidden from {name}: {exc}")
    except Exception:
        pass  # 域逻辑失败（对象不存在等）是预期的，不是本测试要守的东西


def test_legacy_token_still_reaches_a_generate_command() -> None:
    ensure_catalog_loaded()
    asyncio.run(_call_without_forbidden("video.generate_shot", {"shot_id": "shot_does_not_exist"}))


def test_legacy_token_still_reaches_a_decide_command() -> None:
    ensure_catalog_loaded()
    asyncio.run(_call_without_forbidden("video.stop_shot", {"shot_id": "shot_does_not_exist"}))
    asyncio.run(
        _call_without_forbidden(
            "storyboard.confirm", {"episode_id": "episode_does_not_exist"}
        )
    )


def test_legacy_token_still_blocked_from_unrelated_scope() -> None:
    """legacy 展开只加 media-generate/media-decide，不是万能钥匙——需要
    manju:delivery 的命令必须仍然被挡住，证明展开没有过度放宽。
    """
    ensure_catalog_loaded()
    claims = _claims({mcp_auth.LEGACY_GENERATION_MEDIA_SCOPE})
    with pytest.raises(ForbiddenError):
        asyncio.run(
            mcp_tools.call_tool(
                "delivery.review",
                {"episode_id": "episode_does_not_exist", "decision": "approve"},
                claims=claims,
            )
        )


def test_expand_legacy_scopes_is_pure_union() -> None:
    expanded = mcp_auth.expand_legacy_scopes(frozenset({mcp_auth.LEGACY_GENERATION_MEDIA_SCOPE}))
    assert expanded == frozenset(
        {mcp_auth.LEGACY_GENERATION_MEDIA_SCOPE, "manju:media-generate", "manju:media-decide"}
    )
    # 不含旧 scope 时原样返回，不无中生有加新 scope。
    untouched = frozenset({"manju:read"})
    assert mcp_auth.expand_legacy_scopes(untouched) == untouched


def test_callable_command_set_is_identical_before_and_after_split() -> None:
    """全量核对，逐条不抽样：对每一条现在要求 media-generate 或 media-decide
    的命令，拿一枚"拆分前就能调用它"的 token（原 scopes 集合，把
    media-generate/media-decide 换回 manju:generation-media）展开
    manju:generation-media 之后，必须仍然覆盖该命令当前的完整 scopes 要求。

    不能笼统地说"只持 manju:generation-media 一个 scope 的 token"就该覆盖
    全部受影响命令——``bible.generate``/``scene.generate_bible`` 在拆分前就
    已经要求 ``{generation-text, generation-media}`` 两个 scope 同时具备，
    这不是本次拆分改变的行为，拆分前后都需要 token 另外持有
    manju:generation-text。真正要核对的是"拆分没有让任何一条命令额外变难
    调用"，所以拿的是"这条命令拆分前需要的 scope 集合"（把两个新 scope 换
    回旧名字）而不是一个统一的最小 token。
    """
    registry = ensure_catalog_loaded()
    split_scopes = {"manju:media-generate", "manju:media-decide"}
    affected = [spec for spec in registry.commands.values() if spec.scopes & split_scopes]
    assert affected, "expected at least one command reclassified onto the new split scopes"

    mismatches = []
    for spec in affected:
        pre_split_scopes = frozenset(
            {mcp_auth.LEGACY_GENERATION_MEDIA_SCOPE if s in split_scopes else s for s in spec.scopes}
        )
        legacy_token_scopes = mcp_auth.expand_legacy_scopes(pre_split_scopes)
        if spec.scopes - legacy_token_scopes:
            mismatches.append(spec.name)
    assert mismatches == [], f"legacy-equivalent token no longer covers: {mismatches}"
