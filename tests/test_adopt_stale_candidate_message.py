"""候选证据被级联标记 stale 时，人工采纳必须给中文、可执行的出路，不能让底层
``ValueError("stale artifact cannot be committed")`` 原样透给界面（生产 2026-09-16
实测：同一用户 26 秒内 4 次点采纳都收到这句英文 toast）。

真实成因见 ``app/domain/video_ops/adopt.py::_assert_candidate_not_stale`` 的 docstring：
本镜分镜在候选生成之后被编辑保存，``evidence/repository.py`` 的 supersede/stale 级联把
引用旧分镜的视频候选一并标记 stale，``commit_artifact`` 对 stale 产物只会抛出那句英文。
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi import HTTPException

from app import api
from tests.conftest import patch_api_everywhere
from tests.test_episode_partial_concat import _database, _version

_PASSING = json.dumps({"passed": True, "issues": []})
_RAW_STALE_MESSAGE = "stale artifact cannot be committed"


def _setup(monkeypatch, tmp_path, *, candidate_artifact: dict, commit_should_raise_stale: bool):
    """照抄 tests/test_adopt_human_override.py 的夹具写法：patch_api_everywhere 打 get_conn /
    审计钩子，直接打桩 record_video_candidate 与 commit_artifact 的返回/行为，不touch真实
    evidence 存储——那是 app/evidence/* 的职责，本次改动禁止碰。
    """
    conn = _database()
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=False)
    conn.execute("UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'", (_PASSING,))
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *_args: None)
    patch_api_everywhere(monkeypatch, "_review_write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", lambda *_args: None)

    def _commit_artifact(*_args, **_kwargs):
        if commit_should_raise_stale:
            # 复现修复前底层真实抛出的那句英文 ValueError（app/evidence/repository.py
            # 619 行），验证领域层是否已经在到达这里之前拦截。
            raise ValueError(_RAW_STALE_MESSAGE)
        return {}

    monkeypatch.setattr(api.evidence_repository, "commit_artifact", _commit_artifact)
    from app.evidence import media as media_evidence
    monkeypatch.setattr(media_evidence, "record_video_candidate", lambda *_args, **_kwargs: candidate_artifact)
    return conn


def test_adopting_stale_candidate_raises_chinese_actionable_message(tmp_path, monkeypatch) -> None:
    """红→绿证据：candidate_artifact 的 status 是 'stale'，且 commit_artifact 一旦被
    调用就会抛底层英文 ValueError——如果领域层没有提前拦截，这条 ValueError 会不受
    HTTPException 保护地往外传，pytest.raises(HTTPException) 就会失败（修复前的真实红）。"""
    _setup(
        monkeypatch, tmp_path,
        candidate_artifact={"id": "art-v1", "status": "stale"},
        commit_should_raise_stale=True,
    )
    with pytest.raises(HTTPException) as exc:
        api._adopt_version_core(
            "s1", {"version_id": "v1", "reason": "生成台预览时人工选定 v1", "human_override": True},
        )
    message = str(exc.value.detail)
    assert exc.value.status_code == 409
    assert _RAW_STALE_MESSAGE not in message
    assert "重新生成" in message
    assert re.search(r"[一-鿿]", message), f"消息里没有中文：{message!r}"


def test_adopting_non_stale_candidate_still_succeeds(tmp_path, monkeypatch) -> None:
    """反向断言：非 stale 候选（status='validated'）采纳行为不变——新加的前置检查
    不能误伤正常候选。"""
    _setup(
        monkeypatch, tmp_path,
        candidate_artifact={"id": "art-v1", "status": "validated"},
        commit_should_raise_stale=False,
    )
    result = api._adopt_version_core(
        "s1", {"version_id": "v1", "reason": "生成台预览时人工选定 v1", "human_override": True},
    )
    assert result["adopted"] == "v1"
    assert result["artifact_id"] == "art-v1"
