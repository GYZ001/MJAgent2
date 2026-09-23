"""台词修订保存不再无条件硬删 shot_versions（用户 2026-09-23 拍板）。

背景：分镜台「修订台词」保存时，app/domain/storyboard_ops/edit_shot.py 会调用
app/artifacts.py::stage_shot_artifact_cleanup，此前对该镜 shot_versions 无条件
DELETE 全部版本并把视频/末帧写进 media_cleanup_outbox 物理删除——花过视频额度的
视频不可恢复。现改为：应保留的那一版转 stale（app/evidence/dialogue_revision_
retention.py::apply_dialogue_revision_retention），其余未采用候选照旧硬删；为
控制磁盘占用，每镜最多保留 1 个旧版本。

验收返工（2026-09-23 第二轮）修的两个缺陷：
- 缺陷 1：第一次修订后 shots.adopted_version_id 被清空，若用户没生成新视频就再改
  一次台词，旧实现会把第一次保留的版本当普通候选一并删掉。修法：
  dialogue_revision_preserved_version 在调用方事务内现查——优先本镜当前采用版本，
  没有则回退到「上一次台词修订已保留的版本」，都没有才是 None（真全删）。
- 缺陷 2：预览计数必须与保存后的实际结果一致，不能前端自己猜。修法：
  dialogue_revision_video_counts 与 dialogue_revision_preserved_version 是唯一
  判据，shot_edit_session.py 的预览与 edit_shot 的保存共用同一次解析。

场景对应用户拍板的验收点：
1. 有采用版本 + 2 个未采用候选 → 采用版本行保留（stale）、文件不删；候选硬删。
2. 有新采用版本时再修订 → 新采用的转保留、上一次保留的被清掉（每镜最多 1 个）。
3. 无采用版本且无历史保留版本 → 全部照旧删除（真正的「什么都不保留」基线）。
4. 保留版本被采纳时返回中文 409（复用 app/domain/video_ops/adopt.py 既有闸门）。
5. 红绿证据 + 「修订→不生成不采纳→再修订」不再丢失保留版本。
6. 非台词编辑（改场景/景别等）连历史保留版本一起删，行为不变。
7. 预览给出的删除/保留数与保存后的真实结果逐一相等。
8. 至少一条经真实 edit_shot 入口的 2.x 台词修订端到端用例。
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy

import pytest
from fastapi import HTTPException

from app import api, artifacts, storyboard_workspace as workspace
from app.capabilities.direct import enter_handler
from app.evidence.dialogue_revision_retention import (
    DIALOGUE_REVISION_STALE_REASON,
    dialogue_revision_preserved_version,
    dialogue_revision_video_counts,
)
from app.production.storyboard_speech_render import render_segment_speech
from tests.conftest import patch_api_everywhere
from tests.test_storyboard_workspace_prd import storyboard_db  # noqa: F401  复用同一套隔离库夹具

_PROMPT = "prompt"
_SEGMENT_SHOT_ROW = {"shot_contract_json": json.dumps({"storyboard_pack_segment": {"dummy": True}})}
_NO_SEGMENT_SHOT_ROW = {"shot_contract_json": "{}"}


def _insert_version(conn: sqlite3.Connection, *, version_id: str, video_path, version_no: int) -> None:
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES(?,'s1',?,?,?,'succeeded',?,1)""",
        (version_id, version_no, _PROMPT, f"idem-{version_id}", str(video_path)),
    )


def _seed_three_versions(storyboard_db, tmp_path):
    """采用版本 v-adopted + 两个未采用候选 v-cand1/v-cand2，各自带真实视频文件。"""
    adopted_path = tmp_path / "adopted.mp4"
    cand1_path = tmp_path / "cand1.mp4"
    cand2_path = tmp_path / "cand2.mp4"
    for path in (adopted_path, cand1_path, cand2_path):
        path.write_bytes(b"video")
    _insert_version(storyboard_db, version_id="v-adopted", video_path=adopted_path, version_no=1)
    _insert_version(storyboard_db, version_id="v-cand1", video_path=cand1_path, version_no=2)
    _insert_version(storyboard_db, version_id="v-cand2", video_path=cand2_path, version_no=3)
    storyboard_db.execute("UPDATE shots SET adopted_version_id='v-adopted' WHERE id='s1'")
    storyboard_db.commit()
    return adopted_path, cand1_path, cand2_path


def test_dialogue_revision_retains_adopted_version_deletes_other_candidates(
    storyboard_db, tmp_path, monkeypatch,
) -> None:
    from app import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module.config, "PROJECTS_DIR", tmp_path / "projects")
    adopted_path, cand1_path, cand2_path = _seed_three_versions(storyboard_db, tmp_path)

    storyboard_db.execute("BEGIN IMMEDIATE")
    staged = artifacts.stage_shot_artifact_cleanup(
        storyboard_db, "s1", preserve_version_id="v-adopted",
    )
    # 提交前：DB 已经只剩保留行，但文件删除延后到 flush（media_cleanup_outbox 语义）。
    rows = storyboard_db.execute(
        "SELECT id,status,error,video_path FROM shot_versions WHERE shot_id='s1'"
    ).fetchall()
    assert {row["id"] for row in rows} == {"v-adopted"}
    kept = rows[0]
    assert kept["status"] == "stale"
    assert kept["error"] == DIALOGUE_REVISION_STALE_REASON
    assert kept["video_path"] == str(adopted_path)
    assert staged["videos"] == 2  # 只统计将被删除的候选，不含保留项
    storyboard_db.commit()

    assert artifacts.flush_media_cleanup_outbox(staged["outbox_id"]) is True
    assert adopted_path.exists(), "修订前采用的版本文件必须原样保留"
    assert not cand1_path.exists(), "未采用候选必须照旧硬删"
    assert not cand2_path.exists(), "未采用候选必须照旧硬删"
    assert storyboard_db.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()["adopted_version_id"] is None


def test_new_adoption_before_second_revision_replaces_retained_version_via_resolver(
    storyboard_db, tmp_path, monkeypatch,
) -> None:
    """场景 2：每镜最多保留 1 个旧版本。上一次修订保留了 v-round1；用户重新生成并采用了
    v-round2 后再次修订台词——resolver（不是硬编码）要选中 v-round2，v-round1 被清出。"""
    from app import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module.config, "PROJECTS_DIR", tmp_path / "projects")
    round1_path = tmp_path / "round1-kept.mp4"
    round2_path = tmp_path / "round2-newly-adopted.mp4"
    round1_path.write_bytes(b"video")
    round2_path.write_bytes(b"video")
    # 模拟第一轮修订已经跑完：v-round1 是那次保留下来的 stale 版本。
    _insert_version(storyboard_db, version_id="v-round1", video_path=round1_path, version_no=1)
    storyboard_db.execute(
        "UPDATE shot_versions SET status='stale',error=? WHERE id='v-round1'",
        (DIALOGUE_REVISION_STALE_REASON,),
    )
    # 用户重新生成并采用了新的一版，随后发起第二次台词修订。
    _insert_version(storyboard_db, version_id="v-round2", video_path=round2_path, version_no=2)
    storyboard_db.execute("UPDATE shots SET adopted_version_id='v-round2' WHERE id='s1'")
    storyboard_db.commit()

    storyboard_db.execute("BEGIN IMMEDIATE")
    resolved = dialogue_revision_preserved_version(
        storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"dialogues"},
    )
    assert resolved == "v-round2", "有当前采用版本时必须优先它，不是继续沿用上一次的保留"
    staged = artifacts.stage_shot_artifact_cleanup(
        storyboard_db, "s1", preserve_version_id=resolved,
    )
    rows = storyboard_db.execute(
        "SELECT id,status FROM shot_versions WHERE shot_id='s1'"
    ).fetchall()
    assert {row["id"] for row in rows} == {"v-round2"}
    assert rows[0]["status"] == "stale"
    storyboard_db.commit()

    assert artifacts.flush_media_cleanup_outbox(staged["outbox_id"]) is True
    assert not round1_path.exists(), "上一次保留的版本这次必须被清出（1 镜最多保留 1 个）"
    assert round2_path.exists(), "这一次修订前采用的版本必须保留"


def test_no_adoption_and_no_previous_retained_version_deletes_everything(
    storyboard_db, tmp_path, monkeypatch,
) -> None:
    """场景 3（真正的基线，不是「无采用版本」这么宽）：既没有当前采用版本，也没有历史台词
    修订保留下来的版本——resolver 与 stage_shot_artifact_cleanup 都必须给出「全删」。"""
    from app import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module.config, "PROJECTS_DIR", tmp_path / "projects")
    cand1_path = tmp_path / "only-cand1.mp4"
    cand2_path = tmp_path / "only-cand2.mp4"
    cand1_path.write_bytes(b"video")
    cand2_path.write_bytes(b"video")
    _insert_version(storyboard_db, version_id="v-only1", video_path=cand1_path, version_no=1)
    _insert_version(storyboard_db, version_id="v-only2", video_path=cand2_path, version_no=2)
    assert storyboard_db.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()["adopted_version_id"] is None
    storyboard_db.commit()

    storyboard_db.execute("BEGIN IMMEDIATE")
    resolved = dialogue_revision_preserved_version(
        storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"dialogues"},
    )
    assert resolved is None, "既无采用版本也无历史保留版本时，resolver 必须给 None（全删）"
    staged = artifacts.stage_shot_artifact_cleanup(storyboard_db, "s1", preserve_version_id=resolved)
    assert storyboard_db.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()["c"] == 0
    assert staged["videos"] == 2
    storyboard_db.commit()

    assert artifacts.flush_media_cleanup_outbox(staged["outbox_id"]) is True
    assert not cand1_path.exists()
    assert not cand2_path.exists()


def test_adopting_dialogue_revision_retained_version_raises_chinese_409(
    tmp_path, monkeypatch,
) -> None:
    """场景 4：保留版本（status='stale'）不可再被采纳：命中 app/domain/video_ops/adopt.py
    的 _assert_version_adoptable 第一道闸门，给出中文 409，而不是让它悄悄被当成正常候选。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    from app import db

    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s','e',1,5)")
    video_path = tmp_path / "retained.mp4"
    video_path.write_bytes(b"video")
    _insert_version(conn, version_id="v-retained", video_path=video_path, version_no=1)
    conn.execute(
        "UPDATE shot_versions SET status='stale',error=? WHERE id='v-retained'",
        (DIALOGUE_REVISION_STALE_REASON,),
    )
    conn.commit()

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *_args: None)

    with pytest.raises(HTTPException) as exc:
        api._adopt_version_core(
            "s", {"version_id": "v-retained", "reason": "尝试采纳过期保留版本", "human_override": True},
        )
    assert exc.value.status_code == 409
    assert "成功" in str(exc.value.detail) or "过期" in str(exc.value.detail)


def _old_buggy_preserve_target(cached_shot_row) -> str | None:
    """修复前 stage_edit_media_cleanup 的内联逻辑的独立手写副本（缺陷 1 的根因）：
    直接信调用方早先缓存的 shot 行取 adopted_version_id，不做任何回退。第一次修订
    后这一列已被置 NULL，第二次修订会把上一次保留的版本当普通候选一并删掉。这里
    手写而不是从当前实现里抠一份出来跑——独立观察点，且不改动共享工作区的真实
    文件（CLAUDE.md「验证要有独立观察点」，本工作区同时有其他代理在改 app/db.py
    等文件，不能用 git apply -R 回退验证）。"""
    return cached_shot_row["adopted_version_id"]


def test_defect1_red_then_green_second_revision_without_new_generation(
    storyboard_db, tmp_path, monkeypatch,
) -> None:
    """红→绿：修订 → 不生成不采纳 → 再修订，第一次保留的版本行与文件必须都还在、仍是
    stale（场景 5）。红用上面手写的修复前逻辑独立复现；绿用真实的
    dialogue_revision_preserved_version，两者对同一份数据库状态得出不同结论。"""
    from app import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module.config, "PROJECTS_DIR", tmp_path / "projects")
    kept_path = tmp_path / "kept-across-two-revisions.mp4"
    kept_path.write_bytes(b"video")
    _insert_version(storyboard_db, version_id="v-a", video_path=kept_path, version_no=1)
    storyboard_db.execute("UPDATE shots SET adopted_version_id='v-a' WHERE id='s1'")
    storyboard_db.commit()

    # 第一次修订：真实实现保留 v-a。
    storyboard_db.execute("BEGIN IMMEDIATE")
    staged1 = artifacts.stage_shot_artifact_cleanup(
        storyboard_db, "s1",
        preserve_version_id=dialogue_revision_preserved_version(
            storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"dialogues"},
        ),
    )
    storyboard_db.commit()
    assert artifacts.flush_media_cleanup_outbox(staged1["outbox_id"]) is True
    assert kept_path.exists()

    # edit_shot.py 保存路径早先（BEGIN IMMEDIATE 之前）缓存的 shot 行——此刻
    # adopted_version_id 已经是 NULL，是缺陷 1 的竞态前提。
    cached_shot = dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone())
    assert cached_shot["adopted_version_id"] is None

    # 红：手写的修复前逻辑只信这份缓存，算出「什么都不保留」——如果真按这个结果去清理，
    # v-a 会被当普通候选删掉，文件与行都没了。
    buggy_result = _old_buggy_preserve_target(cached_shot)
    assert buggy_result is None, "复现缺陷 1：修复前逻辑会丢失第一次保留的版本"

    # 绿：真实 resolver 在调用方事务内现查，回退到上一次保留的版本。
    storyboard_db.execute("BEGIN IMMEDIATE")
    real_result = dialogue_revision_preserved_version(
        storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"dialogues"},
    )
    assert real_result == "v-a", "真实实现必须回退到上一次台词修订保留的版本"

    # 第二次修订按真实 resolver 的结果跑一遍 cleanup：v-a 必须原样留在库里、文件还在。
    staged2 = artifacts.stage_shot_artifact_cleanup(
        storyboard_db, "s1", preserve_version_id=real_result,
    )
    row = storyboard_db.execute(
        "SELECT status,error,video_path FROM shot_versions WHERE id='v-a'"
    ).fetchone()
    assert row is not None, "第一次保留的版本行不能被第二次修订删掉"
    assert row["status"] == "stale"
    assert row["error"] == DIALOGUE_REVISION_STALE_REASON
    storyboard_db.commit()
    assert artifacts.flush_media_cleanup_outbox(staged2["outbox_id"]) is True
    assert kept_path.exists(), "第一次保留的视频文件不能被第二次修订清掉"


def test_non_dialogue_edit_deletes_previously_retained_version_too(
    storyboard_db, tmp_path, monkeypatch,
) -> None:
    """场景 6：非台词修订（如改场景/景别）保持旧行为——changed_fields 不含 dialogues 时，
    resolver 直接给 None，历史保留版本也会被这次编辑一并硬删，不享受保留特权。"""
    from app import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module.config, "PROJECTS_DIR", tmp_path / "projects")
    retained_path = tmp_path / "was-retained.mp4"
    retained_path.write_bytes(b"video")
    _insert_version(storyboard_db, version_id="v-was-retained", video_path=retained_path, version_no=1)
    storyboard_db.execute(
        "UPDATE shot_versions SET status='stale',error=? WHERE id='v-was-retained'",
        (DIALOGUE_REVISION_STALE_REASON,),
    )
    storyboard_db.commit()

    storyboard_db.execute("BEGIN IMMEDIATE")
    resolved = dialogue_revision_preserved_version(
        storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"scene_time"},  # 非台词字段
    )
    assert resolved is None
    staged = artifacts.stage_shot_artifact_cleanup(storyboard_db, "s1", preserve_version_id=resolved)
    assert storyboard_db.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()["c"] == 0
    storyboard_db.commit()
    assert artifacts.flush_media_cleanup_outbox(staged["outbox_id"]) is True
    assert not retained_path.exists(), "非台词修订不保留历史版本，必须连它一起删"


@pytest.mark.parametrize(
    "adopted_version_id,seed_previous_stale,expected",
    [
        ("v-x", False, (2, 1)),   # 有采用版本：3 个候选里删 2 个、保留 1 个
        (None, True, (2, 1)),     # 无采用版本但有历史保留版本：回退保留，删数不变
        (None, False, (3, 0)),    # 都没有：3 个全删，不保留
    ],
)
def test_preview_video_counts_match_resolver_in_all_branches(
    storyboard_db, adopted_version_id, seed_previous_stale, expected,
) -> None:
    """场景 7（预览侧）：dialogue_revision_video_counts 三种分支都要与
    dialogue_revision_preserved_version 单独算出的保留判断一致——这是预览接口
    实际调用的同一个函数，不是另外重新实现一遍。"""
    storyboard_db.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v-x',?,1,'p','k1','succeeded',1)", ("s1",),
    )
    storyboard_db.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v-y',?,2,'p','k2','succeeded',1)", ("s1",),
    )
    storyboard_db.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v-z',?,3,'p','k3','succeeded',1)", ("s1",),
    )
    if seed_previous_stale:
        storyboard_db.execute(
            "UPDATE shot_versions SET status='stale',error=? WHERE id='v-z'",
            (DIALOGUE_REVISION_STALE_REASON,),
        )
    if adopted_version_id:
        storyboard_db.execute(
            "UPDATE shots SET adopted_version_id=? WHERE id='s1'", (adopted_version_id,),
        )
    storyboard_db.commit()

    total = storyboard_db.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()["c"]
    deleted, retained = dialogue_revision_video_counts(
        storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"dialogues"}, total,
    )
    assert (deleted, retained) == expected

    # 与 resolver 单独算出的保留目标交叉核对：retained==1 时必须真的解析出一个 id。
    preserved = dialogue_revision_preserved_version(storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"dialogues"})
    assert (preserved is not None) == bool(retained)


def test_video_counts_ignore_segment_when_not_dialogue_edit(storyboard_db) -> None:
    """非台词字段改动时，预览的保留数必须是 0——不能因为该镜有段落就误判成台词修订。"""
    storyboard_db.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v-only',?,1,'p','k1','succeeded',1)", ("s1",),
    )
    storyboard_db.execute("UPDATE shots SET adopted_version_id='v-only' WHERE id='s1'")
    storyboard_db.commit()
    deleted, retained = dialogue_revision_video_counts(
        storyboard_db, "s1", _SEGMENT_SHOT_ROW, {"camera_move"}, 1,
    )
    assert (deleted, retained) == (1, 0)


_E2E_SEGMENT = {
    "speech_dialect": "seedance_compact_director_brief",
    "speech_template": "镜头1：@少年 开口发声{{speech:U01}}",
    "prompt_text": "",
    "resources": {"characters": []},
    "dialogue": [
        {
            "utterance_id": "U01", "speaker_identity_id": "少年", "line": "我们走吧。",
            "delivery": "spoken_dialogue", "delivery_kind": "spoken_dialogue",
        },
    ],
}


def test_edit_shot_endpoint_retains_adopted_version_for_2x_segment_dialogue_revision(
    storyboard_db, tmp_path, monkeypatch,
) -> None:
    """场景 8：不直接调 stage_shot_artifact_cleanup，走真实 PUT /shots/{id}（api.edit_shot）
    三步握手，证明 stage_edit_media_cleanup 与共享判据真的在保存路径上被触发；顺带核对
    预览给出的删除/保留数与保存后的真实结果逐一相等（场景 7 的端到端版本）。"""
    from app import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module.config, "PROJECTS_DIR", tmp_path / "projects")
    segment = render_segment_speech(deepcopy(_E2E_SEGMENT), dialect="seedance_compact_director_brief")
    row = storyboard_db.execute("SELECT shot_contract_json FROM shots WHERE id='s1'").fetchone()
    contract = json.loads(row["shot_contract_json"])
    contract["storyboard_pack_segment"] = segment
    storyboard_db.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='s1'",
        (json.dumps(contract, ensure_ascii=False),),
    )
    adopted_path = tmp_path / "e2e-adopted.mp4"
    cand_path = tmp_path / "e2e-candidate.mp4"
    adopted_path.write_bytes(b"video")
    cand_path.write_bytes(b"video")
    _insert_version(storyboard_db, version_id="v-e2e-adopted", video_path=adopted_path, version_no=1)
    _insert_version(storyboard_db, version_id="v-e2e-cand", video_path=cand_path, version_no=2)
    storyboard_db.execute("UPDATE shots SET adopted_version_id='v-e2e-adopted' WHERE id='s1'")
    storyboard_db.commit()

    session = workspace.create_edit_session("s1")
    changes = {"dialogues": [
        {"speaker": "少年", "line": "我们快走吧。", "emotion": "平静", "delivery": "spoken_dialogue"},
    ]}
    preview = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })
    assert preview["by_artifact_type"]["视频版本"] == 1  # v-e2e-cand 会被删
    assert preview["by_artifact_type"]["保留视频版本"] == 1  # v-e2e-adopted 会转保留

    import asyncio

    async def _save():
        # enter_handler() 只是绕开命令总线的「预检需要二次确认」外层（本镜已有媒体，
        # preflight 判 R2_MATERIAL），不影响真实调用的是 api.edit_shot 本身——它的
        # 分镜台专属三步握手（edit-session/impact-preview）已经在上面走完，是同一套
        # 生产代码路径，其它测试（test_storyboard_workspace_prd.py 同类用例）也这样用。
        with enter_handler():
            return await api.edit_shot("s1", {
                **changes,
                "expected_version": session["baseline_artifact_id"],
                "edit_session_token": session["edit_session_token"],
                "preview_token": preview["preview_token"],
                "baseline_content_hash": session["baseline_content_hash"],
                "change_source": "test_dialogue_revision_e2e",
                "revision_reason": "测试环境台词修订",
            })

    result = asyncio.run(_save())
    assert result["ok"] is True
    cleanup_outbox_id = result["invalidated"].get("outbox_id")
    if cleanup_outbox_id:
        artifacts.flush_media_cleanup_outbox(cleanup_outbox_id)

    kept = storyboard_db.execute(
        "SELECT status,error,video_path FROM shot_versions WHERE id='v-e2e-adopted'"
    ).fetchone()
    assert kept is not None, "共享判据没有被真实保存路径触发：保留版本行不见了"
    assert kept["status"] == "stale"
    assert kept["error"] == DIALOGUE_REVISION_STALE_REASON
    assert adopted_path.exists(), "预览说保留、保存后必须真的保留文件"
    assert storyboard_db.execute(
        "SELECT id FROM shot_versions WHERE id='v-e2e-cand'"
    ).fetchone() is None
    assert not cand_path.exists(), "预览说删除、保存后必须真的删除文件"
