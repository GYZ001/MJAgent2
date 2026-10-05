"""scripts/storyboard_prop_continuity_dry_run.py -- 对一集只跑复核判定与核验，
输出拟重写的段与理由，不写库。真实 sqlite 文件 + 只读连接，验证「写操作在
这条连接上必须失败」这条硬约束本身，不只是信任调用方记得传 dry_run。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.harness import model_gateway
from tests.test_prop_continuity_review import _bible, fixture  # noqa: F401 -- 复用同一套夹具，不重开第二份

import scripts.storyboard_prop_continuity_dry_run as dry_run


def test_dry_run_reads_real_sqlite_file_through_read_only_connection(fixture, monkeypatch):
    conn, episode, _payload = fixture
    conn.commit()

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](violations=[{
            "kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
            "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下",
        }])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    ro_conn = dry_run._read_only_connection(str(db.DB_PATH))
    try:
        loaded_episode, bible = dry_run._load_episode_and_bible(ro_conn, "ep")
        assert loaded_episode["id"] == "ep"
        outcomes = asyncio.run(dry_run.review_existing_episode_segments(ro_conn, episode=loaded_episode, bible=bible))
    finally:
        ro_conn.close()
    assert [o.segment_no for o in outcomes] == [1, 2]
    assert outcomes[0].violations == []
    assert outcomes[1].violations[0].kind == "prop_state_regression"


def test_dry_run_connection_rejects_writes():
    """硬约束本身要能证伪：只读 URI 连接上的写操作必须报错，不是调用方自律。"""
    conn = dry_run._read_only_connection(str(db.DB_PATH))
    try:
        with pytest.raises(Exception, match="readonly|read-only"):
            conn.execute("UPDATE shots SET shot_no=shot_no")
    finally:
        conn.close()


def test_load_episode_and_bible_rejects_unknown_episode():
    conn = dry_run._read_only_connection(str(db.DB_PATH))
    try:
        with pytest.raises(ValueError, match="不存在"):
            dry_run._load_episode_and_bible(conn, "不存在的集")
    finally:
        conn.close()
