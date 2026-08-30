"""映射台读到的权威状态必须只有一个答案。

生产缺陷（R4）：``episode_detail`` 先把 ``screenplay_json`` 从返回字典里 pop 掉，
再用**同一个已被裁剪的字典**去算 ``screenplay_state``；``_screenplay_ready`` 的第一道
判定就是「没有页面投影 ⇒ False」，于是同一集在同一时刻：

    GET /episodes/{id}/screenplay/status  -> ready_storyboard_empty
    GET /episodes/{id}?view=script        -> qa_certificate_invalid

映射台把两者合并展示，一旦轻量状态落后或失败就会出现错误提示与错误主操作。
这一类问题的通用形态是「先裁剪返回体、后基于返回体做判定」，所以用例既锁死
两个端点的一致性，也锁死「响应体不回传投影」这个原始意图。
"""
from __future__ import annotations

import json

import pytest

from app import api, db
from app.schemas import EpisodeScreenplay


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state-agreement.db")
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


def test_script_view_and_light_status_agree_on_authority_state() -> None:
    detail = api.episode_detail("e1", view="script")
    status = api.screenplay_lightweight_status("e1")

    assert detail["screenplay_state"] == status["screenplay_state"]
    # 交付完成的集不得被报成「凭证失效」。
    assert detail["screenplay_state"]["code"] != "qa_certificate_invalid"
    assert detail["screenplay_state"]["screenplay_status"] == "ready"


def test_script_view_still_withholds_the_full_projection_column() -> None:
    detail = api.episode_detail("e1", view="script")

    # 判定看得见投影，响应体依旧不回传它。
    assert "screenplay_json" not in detail
    assert detail["screenplay"]["full_script_text"].startswith("【场1】")


def test_episode_detail_and_light_status_agree_on_prep_pack_stages() -> None:
    """红灯（第32轮，用户报告：首屏闪现旧十步阶段带后消失，根因见
    app.production.revision.screenplay_production_state 模块级 docstring
    的 E 类教训——同一语义两个端点两套目录）：集详情投影
    （episode_detail，首屏来源）与轻量状态端点
    （screenplay_lightweight_status，轮询来源）对同一集必须给出完全一致的
    prep_pack_stages——单一真源落地后，两个端点不该再各自算出一份不同的
    阶段快照，不允许任何"先渲染集详情、轮询才纠正"的窗口存在。"""
    detail = api.episode_detail("e1", view="script")
    status = api.screenplay_lightweight_status("e1")

    assert "prep_pack_stages" in detail["screenplay_production"]
    assert (
        detail["screenplay_production"]["prep_pack_stages"]
        == status["prep_pack_stages"]
    )
    assert (
        detail["screenplay_production"]["prep_pack_stages"]
        == status["screenplay_production"]["prep_pack_stages"]
    )
    # 旧十步目录已经从 rev is None（当前架构常态）分支清理，集详情投影不
    # 应该再带着它——这正是曾经首屏闪现的那份数据源。
    assert "stages" not in detail["screenplay_production"]
