"""补卡（``app.props.card_pending_ensure``）跨集共享 label 的异步建卡窗口。

代码评审修复：任务 A 的汇报原文断言"补卡不会把在途任务判成
``REVIEW_DEPENDENCY_STALE``"，但只验证了触发闸门的那一次派发本身，没有覆盖
"道具卡按 (project_id, label) 项目级共享、另一集更早冻结的 manifest 不会被
这次补卡回填"这条路径。``card_pending_ensure`` 模块 docstring 已纠正为"范围
有限，不是全局保证"；本文件用真实数据证实该路径确实会让另一集自己的
manifest 签名变化——这是 ``app.video_modes.prop_references.
manifest_props_signature`` 2026-10-01 就刻意设计的漂移信号（道具从无图变
有图，需要重新生成），不是本功能引入的新漏洞，判定结果是任务状态转
``failed`` 并可重试，不是卡死不动。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.db import get_conn, now
from app.props import card_pending_ensure as ensure_mod
from app.props.card_pending_store import get_pending


def _seed_project(project_id: str) -> None:
    bible = {
        "characters": [], "scenes": [], "props": [],
        "world": {"era": "", "genre": "", "visual_style_canonical": "国漫电影风"},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


def _seed_episode(project_id: str, episode_id: str, episode_no: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        (project_id, episode_no, f"第{episode_no}章", "正文占位。"),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, source_chapters, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_id, project_id, episode_no, "scripted", json.dumps([episode_no]), now()),
    )
    conn.commit()


def _seed_shot(episode_id: str, shot_no: int, props: list[dict]) -> None:
    segment = {"resources": {"props": props}}
    conn = get_conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        (f"{episode_id}_s{shot_no}", episode_id, shot_no, 10,
         json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False)),
    )
    conn.commit()


async def _fake_chat_structured(_messages, **_kwargs):
    return SimpleNamespace(appearance_canonical="白色陶瓷材质、圆口、带配套茶托", aliases=[])


def test_cross_episode_shared_label_build_drifts_unrelated_episode_manifest(monkeypatch, tmp_path) -> None:
    from app.props import judge, service
    from app.video_modes.prop_references import (
        manifest_props_signature,
        resolve_segment_prop_manifest_entries,
    )

    async def _fake_generate_image(project_id, name, _prompt):
        # ready 判据要求文件真实存在于盘上（见 resolve_segment_prop_manifest_
        # entries），光返回一个不存在的路径字符串不足以复现本场景。
        path = tmp_path / project_id / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-png-bytes")
        return str(path)

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)

    project_id = "proj_cross_ep"
    _seed_project(project_id)
    _seed_episode(project_id, "ep_a", 1)
    _seed_episode(project_id, "ep_b", 2)
    # 集 A 只用这个 label 一次——自己达不到 ≥2 段判据，不会触发它自己的补卡；
    # 集 B 用两次，满足判据，会触发补卡。
    _seed_shot("ep_a", 1, [{"label": "白色陶瓷杯", "description": "集 A 的杯子"}])
    _seed_shot("ep_b", 1, [{"label": "白色陶瓷杯", "description": "集 B 描述一"}])
    _seed_shot("ep_b", 2, [{"label": "白色陶瓷杯", "description": "集 B 描述二"}])

    conn = get_conn()
    prop_entries = [{"label": "白色陶瓷杯", "description": "集 A 的杯子"}]
    frozen = {"props": resolve_segment_prop_manifest_entries(
        prop_entries, conn=conn, project_id=project_id, episode_no=1,
    )}
    assert frozen["props"][0]["ready"] is False  # 集 A 冻结时确实无卡

    # 集 B 的分镜触发补卡（与集 A 无关的派发）。
    asyncio.run(ensure_mod.ensure_storyboard_prop_cards(project_id=project_id, episode_id="ep_b"))
    row = get_pending(conn, project_id=project_id, label="白色陶瓷杯")
    assert row["status"] == "ready"

    current = {"props": resolve_segment_prop_manifest_entries(
        prop_entries, conn=conn, project_id=project_id, episode_no=1,
    )}
    assert current["props"][0]["ready"] is True
    assert manifest_props_signature(frozen) != manifest_props_signature(current)
