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


def test_seed_qa_allows_summary_that_explicitly_says_minor_crop() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.92,
        "presentation_match": 0.7,
        "clean_frame": 0.9,
        "person_count": 1,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "full_body_visible": False,
        "crop_severity": "minor",
        "anatomy_valid": True,
        "soft_warnings": ["画面轻微裁切至小腿下部，未显示完整鞋底与脚部"],
        "hard_failures": [],
        "issues": ["核心角色设定匹配度较高，仅存在少量细节差异与轻微裁切问题，无硬违规内容"],
    })

    assert result["status"] == "warning"
    assert result["hard_gate_passed"] is True
    assert result["hard_failures"] == []
    assert any("轻微裁切" in item for item in result["issues"])


def test_seed_qa_does_not_invert_positive_no_defect_summary() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.95,
        "presentation_match": 0.95,
        "clean_frame": 0.98,
        "person_count": 1,
        "watermark_detected": False,
        "watermark_occluding": False,
        "forbidden_text_detected": False,
        "full_body_visible": True,
        "crop_severity": "none",
        "anatomy_valid": True,
        "soft_warnings": [],
        "hard_failures": [],
        "issues": [
            "该单角色立绘符合角色锚点的核心要求，带有雨夜雨滴特效匹配氛围，"
            "背景为浅米色，全身完整无水印文字遮挡，解剖结构正常"
        ],
    })

    assert result["hard_gate_passed"] is True
    assert result["status"] == "warning"
    assert result["hard_failures"] == []


def test_seed_qa_does_not_invert_compound_no_watermark_or_text_summary() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.9,
        "presentation_match": 0.85,
        "clean_frame": 0.95,
        "person_count": 1,
        "watermark_detected": False,
        "watermark_occluding": None,
        "forbidden_text_detected": False,
        "full_body_visible": True,
        "crop_severity": "minor",
        "anatomy_valid": True,
        "soft_warnings": ["鞋尖轻微贴近画面底部，属于轻微裁切"],
        "hard_failures": [],
        "issues": [
            "无水印、无遮挡文字/Logo",
            "人物肢体无畸形，五官正常",
        ],
    })

    assert result["hard_gate_passed"] is True
    assert result["status"] == "warning"
    assert result["hard_failures"] == []


def test_seed_qa_structured_minor_crop_overrides_bottom_crop_wording() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.85,
        "presentation_match": 0.9,
        "clean_frame": 0.97,
        "person_count": 1,
        "watermark_detected": False,
        "watermark_occluding": False,
        "forbidden_text_detected": False,
        "full_body_visible": True,
        "crop_severity": "minor",
        "anatomy_valid": True,
        "soft_warnings": ["鞋尖接近画面底部边缘"],
        "hard_failures": [],
        "issues": [
            "发型与锚点描述不符",
            "存在轻微底部裁切",
        ],
    })

    assert result["hard_gate_passed"] is True
    assert result["status"] == "warning"
    assert result["hard_failures"] == []
    assert "存在轻微底部裁切" in result["issues"]


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


def test_seed_qa_allows_subjective_anchor_differences_with_warning() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.1,
        "presentation_match": 0.9,
        "clean_frame": 0.95,
        "person_count": 1,
        "full_body_visible": True,
        "anatomy_valid": True,
        "hard_failures": [
            "视觉年龄为成年女性，不符合锚点要求的14岁少女",
            "服装不符合锚点要求的淡绿色上衣长裤，实际为紫色绣花长裙",
            "存在锚点未要求的莲花及漂浮花瓣装饰",
            "发型带有锚点未提及的额外发箍装饰",
        ],
    })

    assert result["status"] == "warning"
    assert result["hard_gate_passed"] is True
    assert result["overall"] == 0.6
    assert result["hard_failures"] == []
    assert any("设定贴合度" in item for item in result["issues"])


def test_seed_qa_respects_explicit_non_occluding_watermark_result() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.9,
        "presentation_match": 0.45,
        "clean_frame": 0.95,
        "person_count": 1,
        "watermark_detected": True,
        "watermark_occluding": False,
        "forbidden_text_detected": False,
        "full_body_visible": True,
        "crop_severity": "none",
        "anatomy_valid": True,
        "issues": ["画面存在轻微角落水印，未遮挡人物主体"],
        "hard_failures": [],
    })

    assert result["status"] == "warning"
    assert result["hard_gate_passed"] is True
    assert result["hard_failures"] == []
    assert any("角落水印" in item for item in result["issues"])


def test_seed_qa_structured_non_occlusion_overrides_summary_wording() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.95,
        "presentation_match": 0.85,
        "clean_frame": 0.92,
        "person_count": 1,
        "watermark_detected": True,
        "watermark_occluding": False,
        "forbidden_text_detected": False,
        "full_body_visible": True,
        "crop_severity": "none",
        "anatomy_valid": True,
        "issues": [
            "整体符合青年女性黑短发、米白风衣、手持电影票的核心角色设定，"
            "仅存在少量细节偏差与轻微非遮挡水印"
        ],
        "soft_warnings": ["画面右下角存在轻微未遮挡主体的水印"],
        "hard_failures": [],
    })

    assert result["status"] == "warning"
    assert result["hard_gate_passed"] is True
    assert result["hard_failures"] == []
    assert any("轻微非遮挡水印" in item for item in result["issues"])


def test_seed_qa_does_not_double_count_corner_watermark_as_forbidden_text() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.93,
        "presentation_match": 0.91,
        "clean_frame": 0.97,
        "person_count": 1,
        "watermark_detected": True,
        "watermark_occluding": False,
        "forbidden_text_detected": True,
        "full_body_visible": True,
        "crop_severity": "minor",
        "anatomy_valid": True,
        "issues": ["该立绘整体符合角色设定，仅存在轻微瑕疵"],
        "soft_warnings": ["画面角落存在轻微文字水印"],
        "hard_failures": [],
    })

    assert result["status"] == "warning"
    assert result["hard_gate_passed"] is True
    assert result["hard_failures"] == []
    assert any("水印含文字" in item for item in result["issues"])


def test_seed_qa_still_blocks_separate_forbidden_body_text() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.95,
        "presentation_match": 0.9,
        "clean_frame": 0.9,
        "person_count": 1,
        "watermark_detected": True,
        "watermark_occluding": False,
        "forbidden_text_detected": True,
        "full_body_visible": True,
        "anatomy_valid": True,
        "issues": ["人物胸前出现大段宣传文字，影响角色识别"],
        "hard_failures": [],
    })

    assert result["status"] == "failed"
    assert result["hard_gate_passed"] is False
    assert "画面检测到不允许的文字" in result["hard_failures"]


def test_seed_qa_still_blocks_an_explicitly_wrong_character() -> None:
    result = normalize_portrait_seed_qa({
        "identity_match": 0.1,
        "presentation_match": 1.0,
        "clean_frame": 1.0,
        "person_count": 1,
        "full_body_visible": True,
        "anatomy_valid": True,
        "hard_failures": ["明显生成成其他人物，属于错误角色"],
    })

    assert result["status"] == "failed"
    assert result["hard_gate_passed"] is False
    assert result["overall"] == 0.1
    assert "明显生成成其他人物，属于错误角色" in result["hard_failures"]


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
