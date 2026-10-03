"""分镜阶段补卡的别名收紧：模型申报的别名只有在本集分镜里真被用作某个 label
才登记（2026-10-03 派单，真实缺陷：《顾念长安》proj_ca86b15ab7d7 EP1 补卡给
"对面木椅"登记了 ['咖啡馆木质椅子','木质椅子','椅子']，其中只剩品类名的
"椅子"会让以后任何一集写"椅子"都错误复用这张咖啡馆木椅的参考图）。

单独开一个文件而不是塞进 ``test_prop_storyboard_card_pending.py``：那个文件
已经 494 行，紧贴 CLAUDE.md 文件规范「测试 ≤500 行」的基线，塞进这一块会把
它推过线（新增文件/函数严格达标，不许靠 ``[baseline.*]`` 棘轮抵账）。种子
函数复用那个文件里已有的几个最小 fixture，不重复定义。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.db import get_conn
from app.props import card_pending_ensure as ensure_mod
from tests.test_prop_storyboard_card_pending import (
    _fake_generate_image,
    _seed_episode,
    _seed_project,
    _seed_shot,
)


async def _fake_chat_structured_with_generic_aliases(_messages, **_kwargs):
    return SimpleNamespace(
        appearance_canonical="深色木质，靠背雕花，扶手处有裂纹",
        aliases=["咖啡馆木质椅子", "木质椅子", "椅子"],
    )


def test_ensure_builds_new_card_drops_aliases_never_used_as_label(monkeypatch, caplog) -> None:
    from app.props import judge, service

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured_with_generic_aliases)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    _seed_project("proj_alias_drop")
    _seed_episode("proj_alias_drop", "ep_alias_drop", 1)
    _seed_shot("ep_alias_drop", 1, [{"label": "对面木椅", "description": "深色木质，靠背雕花"}])
    _seed_shot("ep_alias_drop", 2, [{"label": "对面木椅", "description": "扶手处有裂纹"}])
    # 本集另一段把"木质椅子"本身用作了某条道具的 label（哪怕只出现一次、不满足
    # 它自己的补卡判据）——这让该词在数据里"确实被当作过 label"，按最小判据可以
    # 作为别名登记；"咖啡馆木质椅子"与"椅子"在本集分镜里从未被用作任何 label。
    _seed_shot("ep_alias_drop", 3, [{"label": "木质椅子", "description": "角落里另一张椅子"}])

    with caplog.at_level("INFO"):
        result = asyncio.run(ensure_mod.ensure_storyboard_prop_cards(
            project_id="proj_alias_drop", episode_id="ep_alias_drop",
        ))

    assert "对面木椅" in result["attempted"]
    conn = get_conn()
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_alias_drop'",
    ).fetchone()[0])
    prop = next(p for p in bible["props"] if p["name"] == "对面木椅")
    assert prop["aliases"] == ["木质椅子"]
    assert "椅子" not in prop["aliases"]
    assert "咖啡馆木质椅子" not in prop["aliases"]
    dropped_logs = [r.message for r in caplog.records if "[PROP_STORYBOARD_CARD_ALIAS_DROPPED]" in r.message]
    assert len(dropped_logs) == 1
    # 用带引号的列表项断言，而不是裸子串——"椅子"是"咖啡馆木质椅子"的子串，
    # 裸子串断言在"椅子"根本没被单独丢弃时也会假通过。
    assert "'咖啡馆木质椅子'" in dropped_logs[0]
    assert "'椅子'" in dropped_logs[0]
    assert "'木质椅子'" not in dropped_logs[0]
