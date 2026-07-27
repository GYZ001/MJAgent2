from __future__ import annotations

import asyncio
import json

from app import hiagent, multiview
from app.portrait_policy import normalize_portrait_seed_qa


def test_character_pack_qa_demotes_acting_direction_and_scores_stable_views(
    tmp_path, monkeypatch,
) -> None:
    front = tmp_path / "front.jpg"
    profile = tmp_path / "profile.jpg"
    front.write_bytes(b"front")
    profile.write_bytes(b"profile")

    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "定妆多视角允许使用统一的中性表情" in expectation
        assert "body_consistency" in expectation
        return json.dumps({
            "overall": 0.91,
            "face_consistency": 0.94,
            "outfit_consistency": 0.93,
            "hair_consistency": 0.95,
            "body_consistency": 0.92,
            "views": [
                {
                    "view_role": "front_full", "identity_match": 0.92,
                    "presentation_match": 0.3, "clean_frame": 1.0, "overall": 0.3,
                    "issues": [], "hard_failures": ["眼神未体现炽热爱慕"],
                },
                {
                    "view_role": "profile", "identity_match": 0.9,
                    "presentation_match": 0.4, "clean_frame": 1.0, "overall": 0.4,
                    "issues": [], "hard_failures": ["微笑不够轻浮"],
                },
            ],
            "issues": [],
            "hard_failures": ["整体表情不够妩媚"],
        }, ensure_ascii=False)

    monkeypatch.setattr(hiagent, "encode_image_file", lambda path: str(path))
    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)

    result = asyncio.run(multiview.review_character_pack_consistency([
        {"view_role": "front_full", "image_path": str(front)},
        {"view_role": "profile", "image_path": str(profile)},
    ], "华贵衣衫，目光炽热爱慕，嘴角虚伪轻浮微笑"))

    assert result["status"] == "warning"
    assert result["hard_failures"] == []
    assert result["body_consistency"] == 0.92
    assert result["views"][0]["overall"] == 0.92
    assert result["views"][1]["overall"] == 0.9
    assert all(view["status"] == "ready" for view in result["views"])
    assert "整体表情不够妩媚" in result["issues"]


def test_seed_qa_allows_minor_foot_crop_with_warning() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.95,
        "presentation_match": 0.3,
        "clean_frame": 0.7,
        "person_count": 1,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "full_body_visible": False,
        "crop_severity": "minor",
        "anatomy_valid": True,
        "issues": ["人物脚部未完全展示，全身完整性不足"],
        "hard_failures": [],
    })

    assert result["status"] == "warning"
    assert result["hard_gate_passed"] is True
    assert result["hard_failures"] == []
    assert any("轻微裁切" in item for item in result["issues"])


def test_seed_qa_keeps_major_crop_as_hard_failure() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.95,
        "presentation_match": 0.8,
        "clean_frame": 0.8,
        "person_count": 1,
        "full_body_visible": False,
        "crop_severity": "major",
        "anatomy_valid": True,
        "issues": ["画面只到腰部以上，下半身缺失"],
        "hard_failures": [],
    })

    assert result["status"] == "failed"
    assert result["hard_gate_passed"] is False
    assert "主体全身未完整可见" in result["hard_failures"]


def test_character_pack_qa_demotes_non_occluding_provider_watermark(
    tmp_path, monkeypatch,
) -> None:
    paths = []
    for role in ("front_full", "three_quarter", "profile"):
        path = tmp_path / f"{role}.jpg"
        path.write_bytes(role.encode())
        paths.append({"view_role": role, "image_path": str(path)})

    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "不遮挡人物" in expectation
        return json.dumps({
            "overall": 0.94,
            "face_consistency": 0.99,
            "outfit_consistency": 0.97,
            "hair_consistency": 0.99,
            "body_consistency": 0.99,
            "views": [
                {"view_role": "front_full", "identity_match": 1, "clean_frame": 1,
                 "overall": 1, "issues": [], "hard_failures": []},
                {"view_role": "three_quarter", "identity_match": 1, "clean_frame": 1,
                 "overall": 1, "issues": [], "hard_failures": []},
                {"view_role": "profile", "identity_match": 1, "clean_frame": 0.85,
                 "overall": 0.9, "issues": [], "hard_failures": ["存在页面文字水印"]},
            ],
            "issues": [],
            "hard_failures": ["第三个profile视角存在文字水印"],
        }, ensure_ascii=False)

    monkeypatch.setattr(hiagent, "encode_image_file", lambda path: str(path))
    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)

    result = asyncio.run(multiview.review_character_pack_consistency(paths, "华贵衣衫"))

    assert result["status"] == "warning"
    assert result["hard_failures"] == []
    assert result["views"][2]["status"] == "ready"
    assert "存在页面文字水印" in result["views"][2]["issues"]
