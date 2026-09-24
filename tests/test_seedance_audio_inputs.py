"""``app.video_modes.seedance_audio.build_seedance_audio_inputs``：读冻结的
``meta["reference_audios"]``、核对 sha256、拼 data URL。不涉及网络。
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.hiagent import ProviderError
from app.video_modes.seedance_audio import build_seedance_audio_inputs


def _write_clip(tmp_path, name: str, content: bytes = b"fake-wav-bytes") -> tuple[str, str]:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path), hashlib.sha256(content).hexdigest()


def test_missing_key_returns_empty_list() -> None:
    assert build_seedance_audio_inputs({}) == []


def test_empty_list_returns_empty_list() -> None:
    assert build_seedance_audio_inputs({"reference_audios": []}) == []


def test_happy_path_builds_data_url_in_order(tmp_path) -> None:
    path1, sha1 = _write_clip(tmp_path, "a.wav", b"clip-a")
    path2, sha2 = _write_clip(tmp_path, "b.wav", b"clip-b")
    meta = {
        "reference_audios": [
            {"index": 1, "character_name": "甲", "clip_path": path1, "clip_sha256": sha1},
            {"index": 2, "character_name": "乙", "clip_path": path2, "clip_sha256": sha2},
        ],
    }

    result = build_seedance_audio_inputs(meta)

    assert [role for _url, role in result] == ["reference_audio", "reference_audio"]
    url1, _ = result[0]
    assert url1 == f"data:audio/wav;base64,{base64.b64encode(b'clip-a').decode('ascii')}"


def test_sha256_mismatch_raises_provider_error(tmp_path) -> None:
    path, _real_sha = _write_clip(tmp_path, "c.wav", b"real-bytes")
    meta = {"reference_audios": [
        {"index": 1, "character_name": "甲", "clip_path": path, "clip_sha256": "not-the-real-hash"},
    ]}

    with pytest.raises(ProviderError, match="哈希与冻结记录不一致"):
        build_seedance_audio_inputs(meta)


def test_missing_expected_hash_raises_provider_error(tmp_path) -> None:
    path, _sha = _write_clip(tmp_path, "d.wav")
    meta = {"reference_audios": [
        {"index": 1, "character_name": "甲", "clip_path": path, "clip_sha256": ""},
    ]}

    with pytest.raises(ProviderError, match="缺少冻结哈希记录"):
        build_seedance_audio_inputs(meta)


def test_missing_file_raises_provider_error() -> None:
    meta = {"reference_audios": [
        {"index": 1, "character_name": "甲", "clip_path": "/no/such/file.wav", "clip_sha256": "abc"},
    ]}

    with pytest.raises(ProviderError, match="文件不存在"):
        build_seedance_audio_inputs(meta)


def test_non_dict_items_are_skipped(tmp_path) -> None:
    path, sha = _write_clip(tmp_path, "e.wav")
    meta = {"reference_audios": ["not-a-dict", {"index": 1, "character_name": "甲", "clip_path": path, "clip_sha256": sha}]}

    result = build_seedance_audio_inputs(meta)

    assert len(result) == 1
