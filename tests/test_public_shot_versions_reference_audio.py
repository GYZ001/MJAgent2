"""``_public_shot_versions`` 的 ``image_inputs.reference_audios``/
``reference_audio_skips`` 投影（角色固定音色 U3 生成台字段，字段名是与前端
的约定）。数据只从冻结 meta 原样搬运，不重新解析；老版本/没有这两个键时
两者都是空数组。
"""
from __future__ import annotations

import json

from app import config
from app.db import get_conn, now
from app.domain.storyboard_ops.public_shot_versions import _public_shot_versions

PROJECT_ID = "proj_pub_audio"
EPISODE_ID = "ep_pub_audio"


def _insert_version(shot_id: str, version_id: str, meta: dict) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, bible_version) "
        "VALUES(?,?,?,?,?,?)",
        (PROJECT_ID, "测试项目", "created", now(), "{}", 1),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,1,'confirmed',?)",
        (EPISODE_ID, PROJECT_ID, now()),
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,1,15)",
        (shot_id, EPISODE_ID),
    )
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, image_inputs, created_at) "
        "VALUES(?,?,1,'p','k',?,?)",
        (version_id, shot_id, json.dumps(meta, ensure_ascii=False), now()),
    )
    conn.commit()


def test_reference_audios_projected_with_media_url_and_skips() -> None:
    # _media_url 只对落在 config.PROJECTS_DIR 下且真实存在的文件返回 URL
    # （app.domain.common._media_url），不能用编造的路径。
    clip_dir = config.PROJECTS_DIR / PROJECT_ID / "voices"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / "voice_1_clip.wav"
    clip_path.write_bytes(b"fake-clip")
    meta = {
        "reference_audios": [
            {"index": 1, "character_name": "张三", "anchor_key": "", "voice_id": "voice_1",
             "clip_path": str(clip_path), "clip_sha256": "sha-1", "clip_duration_s": 4.2},
        ],
        "reference_audio_skips": [{"character_name": "李四", "reason": "未配置声音"}],
    }
    _insert_version("shot_1", "ver_1", meta)

    versions = _public_shot_versions(get_conn(), "shot_1", include_inputs=True)

    assert len(versions) == 1
    audios = versions[0]["image_inputs"]["reference_audios"]
    assert audios == [{
        "index": 1, "character_name": "张三", "voice_id": "voice_1",
        "clip_url": audios[0]["clip_url"], "clip_duration_s": 4.2,
    }]
    assert "voice_1_clip.wav" in audios[0]["clip_url"]
    assert versions[0]["image_inputs"]["reference_audio_skips"] == [
        {"character_name": "李四", "reason": "未配置声音"},
    ]


def test_missing_keys_project_to_empty_lists_for_legacy_or_disabled_versions() -> None:
    _insert_version("shot_2", "ver_2", {"mode": "REFERENCE_IMAGE_MODE"})

    versions = _public_shot_versions(get_conn(), "shot_2", include_inputs=True)

    assert versions[0]["image_inputs"]["reference_audios"] == []
    assert versions[0]["image_inputs"]["reference_audio_skips"] == []


def test_lightweight_query_without_inputs_also_yields_empty_lists() -> None:
    meta = {"reference_audios": [{"index": 1, "character_name": "张三"}]}
    _insert_version("shot_3", "ver_3", meta)

    versions = _public_shot_versions(get_conn(), "shot_3", include_inputs=False)

    assert versions[0]["image_inputs"]["reference_audios"] == []
    assert versions[0]["image_inputs"]["reference_audio_skips"] == []
