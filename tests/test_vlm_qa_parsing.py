import asyncio

from app import hiagent
from app.errors import ContentGenerationError, classify
from app.stages import _parse_qa_result, qa_shot, review_portrait_image, review_scene_image


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
    assert any("文字" in issue or "水印" in issue for issue in qa["issues"])
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


def test_video_qa_caps_overall_at_character_and_action_main_scores(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "overall 不得高于" in expectation
        assert "明确标为画外的叙事关系人物不得按角色缺失" in expectation
        return (
            '{"character_match": 0.9, "action_match": 0.35, '
            '"clean_frame": 1.0, "overall": 0.95, "issues": ["核心动作未出现"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(qa_shot(["frame"], "角色拿起钥匙", "夜，咖啡厅", ["黑发灰衣"]))

    assert qa["overall"] == 0.35
    assert qa["qa_recovered"] is False


def test_portrait_qa_uses_anchor_specific_nonhuman_rules(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "非实体形态" in expectation
        assert "属于硬门禁" in expectation
        assert call_meta["asset_kind"] == "portrait"
        return (
            '{"identity_match": 0.35, "presentation_match": 0.9, "clean_frame": 1, '
            '"overall": 0.9, "hard_failures": ["未呈现透明悬浮形态"], "issues": []}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_portrait_image("frame", "透明苍老人影，悬浮于戒指上方，神态戏谑"))

    assert qa["overall"] == 0.35
    assert qa["hard_gate_passed"] is False
    assert "未呈现透明悬浮形态" in qa["hard_failures"]


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


def test_scene_reference_qa_treats_empty_environment_as_required(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "画面必须无人" in expectation
        assert "画面无人是合格要求" in expectation
        assert "不要要求角色、人物动作、姿态、表情或互动" in expectation
        assert "space_type_matches 只判断室内外和地点大类" in expectation
        assert "售票口大小、柜台形式、内部通道是否完全封闭" in expectation
        assert "不得因此把 space_type_matches 设为 false" in expectation
        assert "输出必须自洽" in expectation
        assert "不得一边声明错地点，一边返回 true" in expectation
        assert call_meta["initiator_label"] == "场景资产主图QA"
        return (
            '{"expectation_match": 0.9, "continuity": 1, "clean_frame": 1, '
            '"overall": 0.9, "issues": []}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "古风斗技堂，中央石质擂台", "萧家斗技堂", [], kind="head",
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
            '"forbidden_text_detected": true, "space_type_matches": true, '
            '"issues": ["画面右下角带有AI生成水印"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "古风斗技堂，中央石质擂台", "萧家斗技堂", [],
        environment_only=True,
    ))

    assert qa["clean_frame"] == 1.0
    assert qa["overall"] == 0.92
    assert qa["issues"] == []
    assert qa["hard_gate_passed"] is True
    assert qa["status"] == "warning"
    assert qa["watermark_detected"] is True
    assert qa["forbidden_text_detected"] is True
    assert "实用质量模式" in qa["warnings"][0]


def test_scene_reference_qa_uses_provider_mark_issue_when_boolean_conflicts(monkeypatch) -> None:
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

    assert qa["watermark_detected"] is True
    assert qa["forbidden_text_is_provider_mark"] is True
    assert qa["hard_gate_passed"] is True
    assert qa["status"] == "warning"
    assert qa["issues"] == ["画风偏写实，与二维厚涂要求有差异"]


def test_scene_reference_qa_allows_corner_mark_over_partial_edge_environment(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"expectation_match":0.9,"continuity":1,"clean_frame":0.8,"overall":0.9,'
            '"person_count":0,"watermark_detected":true,'
            '"forbidden_text_detected":true,"space_type_matches":true,'
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
    assert qa["issues"] == []
    assert qa["non_occluding_provider_watermark"] is True


def test_scene_reference_qa_rejects_watermark_over_critical_structure(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        return (
            '{"expectation_match":0.9,"continuity":1,"clean_frame":0.4,"overall":0.4,'
            '"person_count":0,"watermark_detected":true,'
            '"forbidden_text_detected":true,"space_type_matches":true,'
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
            '"forbidden_text_detected":true,"space_type_matches":true,'
            '"issues":["右下角AI生成水印","画面中央有剧情字幕叠字"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    monkeypatch.setattr("app.multiview.watermark_qa_mode", lambda: "ignore_unless_occluding")
    qa = asyncio.run(review_scene_image(
        "frame", "古风斗技堂，中央石质擂台", "萧家斗技堂", [],
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
        {"name": "萧家斗技堂", "scene_canonical": "错误的藏书阁描述"},
        expected_description="古风斗技堂，中央石质擂台，四周观战席",
    ))

    assert qa["overall"] == 0.9
    assert captured == {
        "frame_desc": "古风斗技堂，中央石质擂台，四周观战席",
        "scene_setting": "萧家斗技堂",
        "anchors": [],
        "environment_only": True,
    }


def test_scene_quality_gate_is_not_misclassified_as_provider_error() -> None:
    assert classify(ContentGenerationError("场景图一致性检查未通过")) == ("quality_gate", "QA")
