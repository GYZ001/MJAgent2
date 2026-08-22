"""剧本台工作区投影：不下发的权威字段，写回时必须由服务端补齐。

``view=script`` 不再回传由生成管线撰写、页面既不展示也不编辑的
``narrative_plan``（实测占该响应体的 85%）。这带来一条必须锁死的不变量：
**页面从未收到的字段，不能因为「提交里没有它」而被当成删除**，
否则一次普通的页面保存就会静默发布一份没有叙事权威的剧本。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import api, db
from app.domain.common import (
    SCREENPLAY_WORKSPACE_WITHHELD_FIELDS,
    merge_withheld_screenplay_fields,
    screenplay_workspace_projection,
)
from app.schemas import EpisodeScreenplay

_PLAN = {"contract_version": "x", "scope_id": "SCOPE1", "events": []}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workspace-projection.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','ready',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) "
        "VALUES('p1',1,'第一章','少年抬头看天。',8)",
    )
    script = EpisodeScreenplay(
        episode_no=1,
        title="第一集",
        full_script_text="【场1】山顶\n少年抬头看天。",
    ).model_dump(mode="json")
    script["narrative_plan"] = _PLAN
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters,
               screenplay_json, screenplay_status, screenplay_updated_at,
               status, created_at
           ) VALUES('e1','p1',1,'第一集','[1]',?, 'ready', ?, 'planned', ?)""",
        (json.dumps(script, ensure_ascii=False), db.now(), db.now()),
    )
    conn.commit()
    yield


def test_script_view_withholds_pipeline_authored_fields() -> None:
    detail = api.episode_detail("e1", view="script")

    assert detail["screenplay_withheld_fields"] == list(
        SCREENPLAY_WORKSPACE_WITHHELD_FIELDS
    )
    for field in SCREENPLAY_WORKSPACE_WITHHELD_FIELDS:
        assert field not in detail["screenplay"]
    # 页面真正要用的内容一个都不能少。
    assert detail["screenplay"]["full_script_text"].startswith("【场1】")


def _authority_plan() -> dict:
    """权威归一化后的 narrative_plan（pydantic 会补齐合同默认值）。"""
    row = db.get_conn().execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    return EpisodeScreenplay.model_validate(
        json.loads(row["screenplay_json"])
    ).model_dump(mode="json")["narrative_plan"]


def test_board_view_still_receives_the_full_document() -> None:
    board = api.episode_detail("e1", view="board")

    assert board["screenplay"]["narrative_plan"]["scope_id"] == "SCOPE1"


@pytest.mark.parametrize("field", SCREENPLAY_WORKSPACE_WITHHELD_FIELDS)
def test_absent_withheld_field_inherits_current_authority(field: str) -> None:
    detail = api.episode_detail("e1", view="script")
    draft = dict(detail["screenplay"])
    draft["title"] = "第一集（改）"

    merged = api._screenplay_payload_with_authority_fields("e1", draft)

    assert merged[field] == _authority_plan()
    assert merged["title"] == "第一集（改）"


def test_explicitly_supplied_value_is_never_overwritten() -> None:
    supplied = {"narrative_plan": {"contract_version": "explicit"}}
    authority = EpisodeScreenplay.model_validate(
        {"episode_no": 1, "narrative_plan": _PLAN}
    )

    merged = merge_withheld_screenplay_fields(supplied, authority=authority)

    assert merged["narrative_plan"] == {"contract_version": "explicit"}


def test_draft_save_round_trip_keeps_the_narrative_authority() -> None:
    detail = api.episode_detail("e1", view="script")
    draft = dict(detail["screenplay"])
    draft["title"] = "第一集（草稿）"

    api.save_screenplay_draft("e1", {"content": draft})
    stored = api.get_screenplay_draft("e1")["draft"]["content"]

    assert stored["narrative_plan"] == _authority_plan()
    assert stored["title"] == "第一集（草稿）"


def test_projection_helper_leaves_non_dict_payloads_untouched() -> None:
    assert screenplay_workspace_projection(None) is None
    assert merge_withheld_screenplay_fields(None, authority=None) is None
