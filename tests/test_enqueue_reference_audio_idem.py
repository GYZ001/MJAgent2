"""角色固定音色 U3 第 4 条「不重烧」：

1. ``build_idem_key`` 只在 ``reference_audio_fingerprint`` 非空时追加后缀，
   空串（开关关闭、清单为空的默认值）时键与本次改动之前逐字相同。
2. ``reference_audio_idem_fingerprint``（入队时算指纹，供 1 使用）在开关关闭、
   镜头不是分镜包段落、或算出来的清单为空时都返回空串。
3. 真正挡住"已采纳镜头被重新烧掉"的机制：``app.domain.video_ops.generate``
   的 ``only_incomplete`` 完成度过滤——已采纳（``adopted_version_id``）或已有
   成功产物（``status='succeeded'`` 且 ``video_path`` 非空）的镜头在进入
   ``_enqueue_shot_impl``/``build_idem_key`` 之前就被整段过滤掉，因此 1 的
   指纹后缀变化对它们完全不可见。这里独立验证该过滤 SQL 的行为，并对源码
   文件做一次字符串核对，防止两边后续改动后静默漂移。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from pathlib import Path

from app import db, video_modes
from app.media_exec import enqueue_prompt
from app.voice import segment_refs, store as voice_store

PROJECT_ID = "proj_1"
EPISODE_ID = "ep_1"


def _conn():
    return db.get_conn()


def _base_kwargs() -> dict:
    decision = video_modes.default_reference_decision()
    return dict(
        prompt_text="镜头1：固定远景。", decision=decision, chain_after_shot_id=None,
        chain_after_version_id=None, target_prompt_fingerprint="fp1", prompt_override=None,
        previous_prompt_fingerprint="", current_reference_manifest={"input_fingerprint": "m1"},
        reference_gallery=None, reroll=False, operation_idempotency_key=None,
        supervisor_run_id=None, auto_retake_count=0, critique=None, critique_sources=None,
    )


# --------------------------------- build_idem_key ---------------------------------

def test_empty_fingerprint_produces_byte_identical_key_to_before_this_feature() -> None:
    kwargs = _base_kwargs()
    with_default = enqueue_prompt.build_idem_key(**kwargs)
    with_explicit_empty = enqueue_prompt.build_idem_key(**kwargs, reference_audio_fingerprint="")
    assert with_default == with_explicit_empty


def test_nonempty_fingerprint_changes_the_key() -> None:
    kwargs = _base_kwargs()
    without_audio = enqueue_prompt.build_idem_key(**kwargs)
    with_audio = enqueue_prompt.build_idem_key(**kwargs, reference_audio_fingerprint="abc123")
    assert without_audio != with_audio


def test_same_fingerprint_produces_the_same_key() -> None:
    kwargs = _base_kwargs()
    first = enqueue_prompt.build_idem_key(**kwargs, reference_audio_fingerprint="abc123")
    second = enqueue_prompt.build_idem_key(**kwargs, reference_audio_fingerprint="abc123")
    assert first == second


# --------------------------- reference_audio_idem_fingerprint ---------------------------

def _seed_project_and_episode() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, bible_version) "
        "VALUES(?,?,?,?,?,?)",
        (PROJECT_ID, "测试项目", "created", db.now(), "{}", 1),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,1,'confirmed',?)",
        (EPISODE_ID, PROJECT_ID, db.now()),
    )
    conn.commit()


class _Shot:
    def __init__(self, segment: dict | None) -> None:
        self.storyboard_pack_segment = segment


def _real_clip(name: str) -> str:
    """参考片段必须真实存在才会被传入（文件缺失的声音按「声音文件缺失」跳过）。"""
    path = Path(tempfile.gettempdir()) / "mj_voice_test_clips" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    return str(path)


def _manifest_with_visual_reference() -> dict:
    return {"scene": {"selected_views": [{"id": "s1"}]}, "characters": []}


def test_disabled_setting_returns_empty_fingerprint(monkeypatch) -> None:
    # 默认开启；只有显式关闭才返回空指纹
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "false" if key == segment_refs.SETTING_KEY_ENABLED else "")
    _seed_project_and_episode()
    shot = _Shot({"dialogue": [{"speaker_identity_id": "bible:张三"}], "resources": {"characters": []}})

    fp = enqueue_prompt.reference_audio_idem_fingerprint(
        _conn(), PROJECT_ID, shot, _manifest_with_visual_reference(),
        target_video_provider="hiagent", target_video_model="test-model",
    )
    assert fp == ""


def test_non_storyboard_pack_shot_returns_empty_fingerprint(monkeypatch) -> None:
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "true" if key == segment_refs.SETTING_KEY_ENABLED else "")
    _seed_project_and_episode()
    shot = _Shot(None)

    fp = enqueue_prompt.reference_audio_idem_fingerprint(
        _conn(), PROJECT_ID, shot, _manifest_with_visual_reference(),
        target_video_provider="hiagent", target_video_model="test-model",
    )
    assert fp == ""


def test_enabled_with_voice_matches_fingerprint_reference_audios(monkeypatch) -> None:
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "true" if key == segment_refs.SETTING_KEY_ENABLED else "")
    _seed_project_and_episode()
    conn = _conn()
    voice_id = voice_store.insert_generating(
        conn, project_id=PROJECT_ID, character_name="张三", model_id="m1",
        voice_prompt="p", preview_text="t", created_by="tester",
    )
    conn.commit()
    voice_store.mark_finished(
        conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE,
        clip_path=_real_clip("a_clip.wav"), clip_sha256="sha-a", clip_duration_s=3.0,
    )
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "张三", voice_id, adopted_by="tester")
    conn.commit()
    shot = _Shot({
        "dialogue": [{"speaker_identity_id": "bible:张三"}],
        "resources": {"characters": [{"identity_id": "bible:张三"}]},
    })

    # Seedance capability_snapshot() 的 supports_reference_audio 只在
    # provider/model 与当前生效通道一致时为真（observed_channel），必须用
    # 真实生效值，不能随手编一个模型名——否则会被判"未观测到"而整段跳过。
    from app import hiagent

    active_provider = hiagent.active_provider("video")
    active_model = hiagent.active_model("video", active_provider)
    fp = enqueue_prompt.reference_audio_idem_fingerprint(
        _conn(), PROJECT_ID, shot, _manifest_with_visual_reference(),
        target_video_provider=active_provider, target_video_model=active_model,
    )

    expected = segment_refs.fingerprint_reference_audios([{
        "character_name": "张三", "anchor_key": "", "voice_id": voice_id, "clip_sha256": "sha-a",
    }])
    assert fp == expected
    assert fp != ""


# --------------------------- only_incomplete 完成度过滤（真正的保护机制） ---------------------------

# 逐字复制自 app/domain/video_ops/generate.py::_generate_episode_core（约
# 217-232 行，body.get("only_incomplete") 分支）——在这里独立验证同一份 SQL
# 的行为；下面的源码字符串核对确保真实文件改了这段查询时本测试会跟着炸。
_COMPLETED_IDS_SQL = """SELECT s.id FROM shots s
                   WHERE s.episode_id=? AND (
                       s.adopted_version_id IS NOT NULL OR EXISTS(
                           SELECT 1 FROM shot_versions v
                           WHERE v.shot_id=s.id AND v.status='succeeded'
                             AND v.video_path IS NOT NULL AND v.video_path!=''
                       )
                   )"""


def test_generate_py_still_contains_the_exact_completed_ids_query() -> None:
    """漂移探测：真实文件里的查询文本变了，这条断言会先炸，提醒同步更新
    上面复制的副本与下面的行为验证。"""
    source = Path("app/domain/video_ops/generate.py").read_text(encoding="utf-8")
    assert _COMPLETED_IDS_SQL in source


def _seed_shot_with_version(shot_id: str, *, adopted: bool, succeeded_video_path: str | None) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,15)",
        (shot_id, EPISODE_ID, hash(shot_id) % 10_000),
    )
    version_id = f"{shot_id}_v1"
    status = "succeeded" if succeeded_video_path is not None else "queued"
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, "
        "video_path, created_at) VALUES(?,?,1,'p','k',?,?,?)",
        (version_id, shot_id, status, succeeded_video_path, db.now()),
    )
    if adopted:
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.commit()


def test_only_incomplete_filter_excludes_adopted_and_succeeded_shots_before_idem_key() -> None:
    _seed_project_and_episode()
    _seed_shot_with_version("shot_adopted", adopted=True, succeeded_video_path=None)
    _seed_shot_with_version("shot_succeeded_video", adopted=False, succeeded_video_path="/media/a.mp4")
    _seed_shot_with_version("shot_succeeded_empty_path", adopted=False, succeeded_video_path="")
    _seed_shot_with_version("shot_pending", adopted=False, succeeded_video_path=None)

    completed = {
        row["id"] for row in _conn().execute(_COMPLETED_IDS_SQL, (EPISODE_ID,)).fetchall()
    }

    # 已采纳、或已有真实成功产物（video_path 非空）的镜头被过滤掉——
    # build_idem_key 后缀变化对它们不可见，因为它们根本不会走到那一步。
    assert completed == {"shot_adopted", "shot_succeeded_video"}
    # 空 video_path 的"成功"行与尚未产出的镜头仍算未完成，正常走入队/幂等键。
    assert "shot_succeeded_empty_path" not in completed
    assert "shot_pending" not in completed
