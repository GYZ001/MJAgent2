import asyncio

from app import hiagent
from app.errors import ContentGenerationError, classify
from app.stages import _parse_qa_result, review_portrait_image, review_scene_image


def test_parse_qa_result_recovers_scores_from_truncated_json() -> None:
    raw = '''```json
{
  "expectation_match": 0.95,
  "continuity": 1.0,
  "clean_frame": 0.8,
'''

    qa = _parse_qa_result(raw, ["expectation_match", "continuity", "clean_frame"])

    assert qa["expectation_match"] == 0.95
    assert qa["continuity"] == 1.0
    assert qa["clean_frame"] == 0.8
    assert qa["overall"] == 0.917
    assert qa["issues"]
    assert qa["qa_recovered"] is True


def test_parse_qa_result_recovers_scores_from_markdown_explanation() -> None:
    raw = """
No reference image provided. *Score:* 1.0.
**clean_frame**: There is a small AI watermark. Score: 0.75.
"expectation_match": 0.9
"""

    qa = _parse_qa_result(
        raw,
        ["expectation_match", "continuity", "clean_frame"],
        defaults={"continuity": 1.0},
    )

    assert qa["expectation_match"] == 0.9
    assert qa["continuity"] == 1.0
    assert qa["clean_frame"] == 0.75
    assert qa["overall"] == 0.883
    assert qa["issues"] == ["VLM返回非标准结构，未获得可验证的结构化诊断"]
    assert qa["qa_recovered"] is True
    assert qa["qa_recovered"] is True


def test_parse_qa_result_normalizes_complete_json() -> None:
    raw = '{"character_match": 95, "action_match": 0.8, "clean_frame": 1, "issues": "ok"}'

    qa = _parse_qa_result(raw, ["character_match", "action_match", "clean_frame"])

    assert qa["character_match"] == 0.95
    assert qa["action_match"] == 0.8
    assert qa["clean_frame"] == 1.0
    assert qa["overall"] == 0.917
    assert qa["issues"] == ["ok"]
    assert qa["qa_recovered"] is False


def test_parse_qa_result_preserves_machine_observation_fields() -> None:
    raw = (
        '{"expectation_match":0.9,"continuity":1,"clean_frame":1,"overall":0.9,'
        '"person_count":0,"watermark_detected":true,'
        '"forbidden_text_detected":false,"space_type_matches":true,'
        '"uncertainties":[]}'
    )

    qa = _parse_qa_result(raw, ["expectation_match", "continuity", "clean_frame"])

    assert qa["person_count"] == 0
    assert qa["watermark_detected"] is True
    assert qa["forbidden_text_detected"] is False
    assert qa["space_type_matches"] is True
    assert qa["uncertainties"] == []


def test_parse_qa_result_marks_missing_required_score_untrusted() -> None:
    qa = _parse_qa_result(
        '{"character_match": 0.9, "clean_frame": 0.9, "issues": []}',
        ["character_match", "action_match", "clean_frame"],
    )

    assert qa["action_match"] == 0.0
    assert qa["qa_recovered"] is True


def test_portrait_qa_uses_anchor_specific_nonhuman_rules(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "非实体形态" in expectation
        assert "属于硬门禁" in expectation
        assert call_meta["asset_kind"] == "portrait"
        return (
            '{"identity_match": 0.35, "presentation_match": 0.9, "clean_frame": 1, '
            '"overall": 0.9, "stable_identity_matches": false, '
            '"hard_failures": ["未呈现透明悬浮形态"], "issues": []}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_portrait_image("frame", "透明苍老人影，悬浮于戒指上方，神态戏谑"))

    assert qa["overall"] == 0.35
    assert qa["hard_gate_passed"] is False
    assert "结构化身份观察确认角色稳定特征不一致" in qa["hard_failures"]
    assert "未呈现透明悬浮形态" in qa["issues"]


def test_portrait_qa_keeps_expression_mismatch_as_soft_warning(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "表情、眼神、笑容" in expectation
        assert "绝不能写入 hard_failures" in expectation
        return (
            '{"identity_match": 0.92, "presentation_match": 0.3, "clean_frame": 1, '
            '"overall": 0.3, "hard_failures": ["眼神未体现炽热爱慕"], '
            '"soft_warnings": ["微笑不够轻浮"], "issues": []}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_portrait_image(
        "frame", "二十岁青年，华贵衣衫，目光炽热爱慕，嘴角虚伪轻浮微笑",
    ))

    assert qa["overall"] == 0.92
    assert qa["hard_gate_passed"] is True
    assert qa["hard_failures"] == []
    assert qa["status"] == "warning"
    assert "眼神未体现炽热爱慕" in qa["issues"]
    assert "微笑不够轻浮" in qa["issues"]


def test_portrait_qa_allows_non_occluding_corner_watermark_with_warning(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"identity_match": 0.95, "presentation_match": 1, "clean_frame": 0.9, '
            '"overall": 0.95, "watermark_detected": null, "hard_failures": [], '
            '"issues": ["画面右下角存在水印"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_portrait_image("frame", "黑发少年，玄色劲装"))

    assert qa["hard_gate_passed"] is True
    assert qa["hard_failures"] == []
    assert "画面右下角存在水印" in qa["issues"]


def test_portrait_qa_keeps_occluding_watermark_as_hard_failure(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"identity_match": 0.95, "presentation_match": 1, "clean_frame": 0.9, '
            '"overall": 0.9, "watermark_detected": true, "watermark_occluding": true, '
            '"hard_failures": ["水印遮挡人物脸部"], "issues": []}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_portrait_image("frame", "黑发少年，玄色劲装"))

    assert qa["hard_gate_passed"] is False
    assert any("遮挡" in item for item in qa["hard_failures"])


def test_portrait_qa_rejects_non_occluding_watermark_under_default_mode(monkeypatch) -> None:
    """默认 watermark_qa_mode=='reject'：定妆照路径必须与场景图/关键帧一致，
    非遮挡水印同样硬失败，不能因为是定妆照就单独放行。"""
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"identity_match": 0.95, "presentation_match": 1, "clean_frame": 0.9, '
            '"overall": 0.95, "watermark_detected": true, "watermark_occluding": false, '
            '"hard_failures": [], "issues": ["画面右下角存在AI生成水印"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_portrait_image("frame", "黑发少年，玄色劲装"))

    assert qa["hard_gate_passed"] is False
    assert qa["status"] == "failed"
    assert "检测到水印或 Logo" in qa["hard_failures"]


def test_portrait_qa_ignores_non_occluding_watermark_in_practical_quality_mode(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"identity_match": 0.95, "presentation_match": 1, "clean_frame": 0.9, '
            '"overall": 0.95, "watermark_detected": true, "watermark_occluding": false, '
            '"hard_failures": [], "issues": ["画面右下角存在AI生成水印"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_portrait_image("frame", "黑发少年，玄色劲装"))

    assert qa["hard_gate_passed"] is True
    assert qa["status"] == "warning"
    assert qa["hard_failures"] == []
    assert qa["non_occluding_provider_watermark"] is True


def test_scene_reference_qa_treats_empty_environment_as_required(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "画面必须无人" in expectation
        assert "画面无人是合格要求" in expectation
        assert "不要要求角色、人物动作、姿态、表情或互动" in expectation
        assert "space_type_matches 只判断室内外和地点大类" in expectation
        assert "layout_detail_matches 单独判断布局、陈设与结构细节" in expectation
        assert "material_contract_matches 单独判断画面材质" in expectation
        assert call_meta["initiator_label"] == "场景资产主图QA"
        return (
            '{"expectation_match": 0.9, "continuity": 1, "clean_frame": 1, '
            '"overall": 0.9, "issues": []}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "古风斗技堂，中央石质擂台", "甲家斗技堂", [], kind="head",
        initiator_label="场景资产主图QA", environment_only=True,
    ))

    assert qa["overall"] == 0.9
    assert qa["issues"] == []


def test_scene_reference_qa_ignores_non_occluding_provider_watermark(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "位于角落、不遮挡场景主体的水印/Logo 不扣分" in expectation
        return (
            '{"expectation_match": 0.92, "continuity": 1, "clean_frame": 0.3, '
            '"overall": 0.3, "person_count": 0, "watermark_detected": true, '
            '"watermark_occluding": false, "forbidden_text_detected": true, '
            '"forbidden_text_is_provider_mark": true, "space_type_matches": true, '
            '"issues": ["画面右下角带有AI生成水印"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "古风斗技堂，中央石质擂台", "甲家斗技堂", [],
        environment_only=True,
    ))

    assert qa["clean_frame"] == 0.3
    assert qa["overall"] == 0.3
    assert qa["issues"] == ["画面右下角带有AI生成水印"]
    assert qa["hard_gate_passed"] is True
    assert qa["status"] == "warning"
    assert qa["watermark_detected"] is True
    assert qa["forbidden_text_detected"] is True
    assert "实用质量模式" in qa["warnings"][0]


def test_scene_reference_qa_never_overrides_structured_facts_from_issue_prose(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"expectation_match":0.72,"continuity":1,"clean_frame":0.7,"overall":0.7,'
            '"person_count":0,"watermark_detected":false,'
            '"forbidden_text_detected":true,"space_type_matches":true,'
            '"issues":["画面右下角存在多余的AI生成文字标注，违反无文字要求",'
            '"画风偏写实，与二维厚涂要求有差异"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "二维厚涂电影院门厅", "电影院门厅", [],
        environment_only=True,
    ))

    assert qa["watermark_detected"] is False
    assert qa["forbidden_text_is_provider_mark"] is False
    assert qa["hard_gate_passed"] is False
    assert qa["status"] == "failed"


def test_scene_reference_qa_allows_corner_mark_over_partial_edge_environment(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"expectation_match":0.9,"continuity":1,"clean_frame":0.8,"overall":0.9,'
            '"person_count":0,"watermark_detected":true,'
            '"watermark_occluding":false,"forbidden_text_detected":true,'
            '"forbidden_text_is_provider_mark":true,"space_type_matches":true,'
            '"issues":["右下角存在AI生成文字水印，遮挡了部分台阶区域"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "二维厚涂钟楼旋转楼梯", "钟楼旋转楼梯", [],
        environment_only=True,
    ))

    assert qa["hard_gate_passed"] is True
    assert qa["status"] == "warning"
    assert qa["issues"] == ["右下角存在AI生成文字水印，遮挡了部分台阶区域"]
    assert qa["non_occluding_provider_watermark"] is True


def test_scene_reference_qa_rejects_watermark_over_critical_structure(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"expectation_match":0.9,"continuity":1,"clean_frame":0.4,"overall":0.4,'
            '"person_count":0,"watermark_detected":true,'
            '"watermark_occluding":true,"forbidden_text_detected":true,'
            '"forbidden_text_is_provider_mark":true,"space_type_matches":true,'
            '"issues":["右下角水印大面积遮挡场景主体和关键楼梯结构"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "二维厚涂钟楼旋转楼梯", "钟楼旋转楼梯", [],
        environment_only=True,
    ))

    assert qa["hard_gate_passed"] is False
    assert "检测到水印或 Logo" in qa["hard_failures"]


def test_scene_reference_qa_keeps_real_overlay_text_as_hard_failure(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"expectation_match":0.9,"continuity":1,"clean_frame":0.7,"overall":0.7,'
            '"person_count":0,"watermark_detected":true,'
            '"watermark_occluding":false,"forbidden_text_detected":true,'
            '"forbidden_text_is_provider_mark":false,"space_type_matches":true,'
            '"issues":["右下角AI生成水印","画面中央有剧情字幕叠字"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "古风斗技堂，中央石质擂台", "甲家斗技堂", [],
        environment_only=True,
    ))

    assert qa["hard_gate_passed"] is False
    assert "检测到禁止的多余文字" in qa["hard_failures"]


def test_scene_reference_review_uses_effective_override(monkeypatch) -> None:
    from app.scenes import _review_scene_ref

    captured = {}

    async def fake_review(image, frame_desc, scene_setting, anchors, **kwargs):
        captured.update(
            frame_desc=frame_desc,
            scene_setting=scene_setting,
            anchors=anchors,
            environment_only=kwargs.get("environment_only"),
        )
        return {"overall": 0.9, "issues": []}

    monkeypatch.setattr(hiagent, "encode_image_file", lambda _path: "frame")
    monkeypatch.setattr("app.stages.review_scene_image", fake_review)
    qa = asyncio.run(_review_scene_ref(
        "unused.jpg",
        {"name": "甲家斗技堂", "scene_canonical": "错误的藏书阁描述"},
        expected_description="古风斗技堂，中央石质擂台，四周观战席",
    ))

    assert qa["overall"] == 0.9
    assert captured == {
        "frame_desc": "古风斗技堂，中央石质擂台，四周观战席",
        "scene_setting": "甲家斗技堂",
        "anchors": [],
        "environment_only": True,
    }


def test_scene_quality_gate_is_not_misclassified_as_provider_error() -> None:
    assert classify(ContentGenerationError("场景图一致性检查未通过")) == ("quality_gate", "QA")
