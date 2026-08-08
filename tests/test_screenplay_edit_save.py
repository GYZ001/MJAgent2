"""回归：剧本台「保存剧本」不得因 sqlite3.Row 无 .get 而 500。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api, db
from app.capabilities import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.policy import reset_approvals_for_tests
from app.local_session import APPROVAL_HEADER, ensure_session_secret, set_request_session_id
from app.schemas import (
    Bible,
    Character,
    EpisodeScreenplay,
    KeyDialogueChain,
    KeyDialogueTurn,
    World,
)
from tests.conftest import SessionTestClient
from tests.test_screenplay_stage import (
    _RAINY_KEY_LINES,
    _RAINY_KEY_POINTS,
    _contract,
    _scene,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-edit.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()

    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_approval_token(request: Request, call_next):
        set_request_approval_token(request.headers.get(APPROVAL_HEADER))
        # 单元测试未挂全局会话闸门：显式绑定会话，保证批准令牌可下发（T4）。
        set_request_session_id(ensure_session_secret())
        try:
            return await call_next(request)
        finally:
            set_request_approval_token(None)
            set_request_session_id(None)

    test_app.include_router(api.router)
    with TestClient(test_app) as test_client:
        yield SessionTestClient(test_client)


def _valid_script() -> EpisodeScreenplay:
    """复用剧本台已验证可通过 validate_screenplay 的雨夜样本。"""
    full_script_text = "\n\n".join([
        "【场1】夜 / 咖啡厅最里侧",
        "雨水顺着玻璃滑下，谷言独自守在最里面的位置，指尖一直压着已经凉透的纸杯，目光钉在门口。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上的风铃忽然响起，谷言抬头，看见失踪多日的旧友站在雨幕里，脸色苍白，袖口还沾着暗红的血迹。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下后没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边，声音压得极低，眼神不停瞟向门外，仿佛随时会有人闯进来。",
        "谷言（攥紧钥匙）：你到底想说什么？",
    ])
    return EpisodeScreenplay(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言在雨夜等来失踪旧友，真相逼近门槛。",
        script_format_note="场次化台本稿，含场标、动作段与对白段",
        scene_outline=[
            _scene(1, "【场1】夜 / 咖啡厅最里侧", "谷言雨夜独自守在咖啡厅，等待迟迟未到的旧友，内心愈发不安。"),
            _scene(2, "【场2】夜 / 咖啡厅门口", "失踪多日的旧友带着血迹现身门口，谷言惊起追问对方的去向。"),
            _scene(3, "【场3】夜 / 咖啡厅座位", "旧友递出储物柜钥匙并低声示警，谷言陷入信任与戒备的两难。"),
        ],
        full_script_text=full_script_text,
        emotional_curve="从压抑等待到骤然紧绷，最后落到更大的不安与悬念。",
        ending_hook="谷言刚要追问，门外第二次响起更重的敲门声。",
        source_basis="保留雨夜会面、旧友递钥匙、警告不要信任来人的核心事件，并压缩原文过渡。",
        character_state_changes=["谷言从克制等待转为警觉戒备", "旧友从强撑冷静转为急切示警"],
        key_lines=[f"谷言：{line}" for line in _RAINY_KEY_LINES],
        dialogue_chains=[
            KeyDialogueChain(
                chain_id=f"DC{index}",
                topic=f"雨夜主线对白{index}",
                turns=[KeyDialogueTurn(
                    speaker="谷言",
                    line=line,
                    function="statement",
                    source_text=line,
                )],
            )
            for index, line in enumerate(_RAINY_KEY_LINES, start=1)
        ],
        key_plot_points=_RAINY_KEY_POINTS,
        opening="雨夜等待",
        development="旧友现身并递出钥匙",
        conflict="旧友警告有人将至，谷言难辨真假",
        climax="门外再次响起敲门声，危险逼近",
        **_contract(),
    )


def _seed_episode(*, with_artifact: bool = False) -> None:
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，眉眼冷峻，腕戴银色手表",
                personality="冷静",
                speech_style="短句直接，语气克制",
            ),
            Character(
                name="旧友",
                role="配角",
                appearance_canonical="三十岁男性，湿发，灰外套，袖口有暗红血迹",
                personality="急切",
                speech_style="压低声音",
            ),
        ],
        world=World(era="现代", genre="都市", visual_style_canonical="都市漫剧厚涂风，柔和侧光，冷暖对比色"),
    )
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_status, created_at) "
        "VALUES('p1', 'demo', 'ready', ?, 'ready', 1)",
        (bible.model_dump_json(),),
    )
    script = _valid_script()
    artifact_id = "art_sp_old" if with_artifact else None
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, synopsis, cliffhanger, source_chapters,
               target_duration_s, screenplay_json, screenplay_status, screenplay_artifact_id,
               status, created_at
           ) VALUES('e1', 'p1', 1, '雨夜敲门', '梗概', '钩子', '[1]', 50, ?, 'ready', ?,
                    'planned', 1)""",
        (script.model_dump_json(), artifact_id),
    )
    conn.commit()


def test_edit_screenplay_does_not_500_on_row_get(client: TestClient) -> None:
    """复现：_episode_or_404 返回 Row 后调用 ep.get → AttributeError → SYS 500。"""
    _seed_episode(with_artifact=True)
    script = _valid_script()
    # 模拟前端轻微编辑后保存（ScriptPage PUT /episodes/{id}/screenplay）
    script.logline = "谷言在雨夜等来失踪旧友，真相逼近门槛。（已改）"
    script.full_script_text += "\n门外再次响起更重的敲门声。"
    resp = client.put(
        "/api/episodes/e1/screenplay",
        json={"screenplay": script.model_dump(mode="json")},
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("saved") is True or body.get("ok") is True
    row = db.get_conn().execute(
        "SELECT screenplay_artifact_id,screenplay_production_revision_id,"
        "screenplay_completion_certificate_id,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert row["screenplay_artifact_id"]
    assert row["screenplay_production_revision_id"]
    assert row["screenplay_completion_certificate_id"]
    assert row["active_screenplay_run_id"] is None
    published = db.get_conn().execute(
        "SELECT * FROM episodes WHERE id='e1'"
    ).fetchone()
    from app.domain.common import _screenplay_ready

    assert _screenplay_ready(dict(published)) is True
    consumed = db.get_conn().execute(
        "SELECT consumed_at FROM completion_certificates WHERE id=?",
        (row["screenplay_completion_certificate_id"],),
    ).fetchone()
    assert consumed["consumed_at"] is not None
    artifact = db.get_conn().execute(
        "SELECT type,status FROM artifacts WHERE id=?", (row["screenplay_artifact_id"],)
    ).fetchone()
    assert tuple(artifact) == ("screenplay_document", "approved")


def test_edit_screenplay_version_conflict_uses_dict_get(client: TestClient) -> None:
    """版本比对路径同样依赖 ep.get；冲突时应 409 而非 500。"""
    _seed_episode(with_artifact=True)
    script = _valid_script()
    resp = client.put(
        "/api/episodes/e1/screenplay",
        json={
            "screenplay": script.model_dump(mode="json"),
            "expected_version": "art_stale",
        },
    )
    assert resp.status_code == 409, resp.text
    assert "版本冲突" in resp.json()["detail"]
