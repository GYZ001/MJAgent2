"""app.portraits.headshot_crop：头像照从全身定妆照裁切（2026-10-02）。

覆盖：几何核验的代码核验分支、裁切框计算（含 clothing_top_y 截断/夹边/加宽）、
JPEG APP11（C2PA/JUMBF）段的纯字节搬运、视觉模型两次不合格抛异常、以及端到端
裁切流程（打桩 model_gateway.chat，不发真实网络请求）。
"""
from __future__ import annotations

import io
import logging
import struct

import pytest
from PIL import Image

from app import hiagent
from app.portraits import headshot_crop as hc


def _jpeg_bytes(size: tuple[int, int] = (400, 600), color=(120, 90, 60)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _app11_segment(payload: bytes = b"fake-c2pa-jumbf-payload") -> bytes:
    body = b"JP" + payload
    length = 2 + len(body)
    return b"\xff\xeb" + struct.pack(">H", length) + body


# ---------------------------------------------------------------------------
# _validate_geometry
# ---------------------------------------------------------------------------

def _valid_payload() -> dict:
    return {"head_box": [0.35, 0.05, 0.65, 0.20], "clothing_top_y": 0.30}


def test_validate_geometry_accepts_well_formed_payload() -> None:
    result = hc._validate_geometry(_valid_payload(), img_w=800, img_h=1200)
    assert result["head_box"] == [0.35, 0.05, 0.65, 0.20]
    assert result["clothing_top_y"] == 0.30


def test_validate_geometry_rejects_out_of_range_coordinate() -> None:
    bad = {**_valid_payload(), "head_box": [0.35, 0.05, 1.5, 0.20]}
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


def test_validate_geometry_rejects_non_rectangle() -> None:
    bad = {**_valid_payload(), "head_box": [0.65, 0.05, 0.35, 0.20]}
    with pytest.raises(ValueError, match="合法矩形"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


def test_validate_geometry_rejects_head_too_short() -> None:
    bad = {**_valid_payload(), "head_box": [0.35, 0.18, 0.65, 0.20]}  # 高度占比 2%
    with pytest.raises(ValueError, match="3%-40%"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


def test_validate_geometry_rejects_head_too_tall() -> None:
    bad = {**_valid_payload(), "head_box": [0.35, 0.00, 0.65, 0.50]}  # 高度占比 50%
    with pytest.raises(ValueError, match="3%-40%"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


def test_validate_geometry_rejects_bad_aspect_ratio() -> None:
    # 宽 30px / 高 180px（1200*0.15），宽高比 0.167，低于 0.45 下限
    bad = {**_valid_payload(), "head_box": [0.35, 0.05, 0.3875, 0.20]}
    with pytest.raises(ValueError, match="0.45-1.4"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


def test_validate_geometry_rejects_clothing_top_above_head_box() -> None:
    bad = {**_valid_payload(), "clothing_top_y": 0.02}  # 小于 y0=0.05
    with pytest.raises(ValueError, match="clothing_top_y"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


def test_validate_geometry_rejects_missing_head_box() -> None:
    with pytest.raises(ValueError, match="head_box"):
        hc._validate_geometry({"clothing_top_y": 0.3}, img_w=800, img_h=1200)


def test_validate_geometry_rejects_non_numeric_values() -> None:
    bad = {"head_box": ["a", 0.05, 0.65, 0.20], "clothing_top_y": 0.3}
    with pytest.raises(ValueError, match="不是数字"):
        hc._validate_geometry(bad, img_w=800, img_h=1200)


# ---------------------------------------------------------------------------
# _compute_crop_box_px
# ---------------------------------------------------------------------------

def test_compute_crop_box_basic_expansion() -> None:
    box = hc._compute_crop_box_px([0.35, 0.05, 0.65, 0.20], 0.30, img_w=800, img_h=1200)
    assert box == (220, 42, 580, 285)


def test_compute_crop_box_clothing_top_y_truncates_bottom() -> None:
    """clothing_top_y 比「下巴+头框高25%」更靠上时，下边界取更靠上的那个，
    但不小于下巴。"""
    box = hc._compute_crop_box_px([0.35, 0.05, 0.65, 0.20], 0.21, img_w=800, img_h=1200)
    chin_y = 0.20 * 1200
    assert box[3] < chin_y + (0.20 - 0.05) * 1200 * 0.25
    assert box[3] >= chin_y


def test_compute_crop_box_clamped_to_image_bounds() -> None:
    box = hc._compute_crop_box_px([0.02, 0.05, 0.30, 0.20], 0.5, img_w=100, img_h=200)
    left, top, right, bottom = box
    assert left == 0
    assert 0 <= top and right <= 100 and bottom <= 200


def test_compute_crop_box_widens_symmetrically_when_too_narrow() -> None:
    box = hc._compute_crop_box_px([0.45, 0.05, 0.55, 0.35], 0.9, img_w=1000, img_h=1000)
    left, top, right, bottom = box
    height = bottom - top
    assert right - left == round(height * 0.8)
    assert left >= 0 and right <= 1000
    # 上下边界不受加宽影响
    assert top == 20 and bottom == 425


# ---------------------------------------------------------------------------
# _neck_margin_ratio：颈部余量可见信号（不参与裁切决策）
# ---------------------------------------------------------------------------

def test_neck_margin_ratio_normal_case_is_roomy() -> None:
    # 对应 test_compute_crop_box_basic_expansion：clothing_top_y 远离下巴，
    # 下边距应等于头框高的 25%（基础扩展公式第一项生效）。
    head_box = [0.35, 0.05, 0.65, 0.20]
    box = hc._compute_crop_box_px(head_box, 0.30, img_w=800, img_h=1200)
    ratio = hc._neck_margin_ratio(head_box, box, img_h=1200)
    assert ratio == pytest.approx(0.25, abs=1e-3)
    assert ratio >= hc._TIGHT_NECK_MARGIN_RATIO


def test_neck_margin_ratio_tight_collar_case_is_below_threshold() -> None:
    # 真实定妆照实测复现（见代码审查记录）：衣领贴近下巴时下边距被压到约 14.6%。
    head_box = [0.37, 0.075, 0.625, 0.195]
    box = hc._compute_crop_box_px(head_box, 0.215, img_w=1440, img_h=2560)
    ratio = hc._neck_margin_ratio(head_box, box, img_h=2560)
    assert ratio == pytest.approx(0.1466, abs=1e-3)
    assert ratio < hc._TIGHT_NECK_MARGIN_RATIO


def test_neck_margin_ratio_zero_head_height_is_safe() -> None:
    assert hc._neck_margin_ratio([0.3, 0.1, 0.5, 0.1], (100, 100, 200, 200), img_h=1000) == 0.0


# ---------------------------------------------------------------------------
# APP11 纯字节搬运
# ---------------------------------------------------------------------------

def test_extract_app11_segments_returns_empty_for_non_jpeg() -> None:
    assert hc._extract_app11_segments(b"not-a-jpeg-at-all") == []


def test_extract_app11_segments_returns_empty_without_app11() -> None:
    assert hc._extract_app11_segments(_jpeg_bytes()) == []


def test_insert_then_extract_app11_round_trips() -> None:
    segment = _app11_segment()
    sourced = hc._insert_app11_segments(_jpeg_bytes(), [segment])
    assert segment in sourced
    extracted = hc._extract_app11_segments(sourced)
    assert extracted == [segment]
    # 输出仍是 Pillow 可以打开的合法 JPEG。
    with Image.open(io.BytesIO(sourced)) as image:
        image.load()


def test_insert_app11_segments_noop_when_empty() -> None:
    raw = _jpeg_bytes()
    assert hc._insert_app11_segments(raw, []) == raw


def test_insert_app11_segments_rejects_non_jpeg() -> None:
    with pytest.raises(ValueError, match="SOI"):
        hc._insert_app11_segments(b"not-a-jpeg", [_app11_segment()])


def test_insert_app11_segments_inserts_right_after_soi_without_app0() -> None:
    # 手写一个没有 APP0、SOI 后直接是 DQT(0xDB) 的最小 JPEG 骨架。
    minimal = b"\xff\xd8" + b"\xff\xdb" + struct.pack(">H", 4) + b"\x00\x00" + b"\xff\xd9"
    segment = _app11_segment()
    out = hc._insert_app11_segments(minimal, [segment])
    assert out[:2] == b"\xff\xd8"
    assert out[2:2 + len(segment)] == segment


# ---------------------------------------------------------------------------
# _detect_head_geometry：VLM 调用 + 重问 + 失败异常
# ---------------------------------------------------------------------------

async def test_detect_head_geometry_succeeds_on_first_valid_response(monkeypatch, tmp_path) -> None:
    import json as _json

    source = tmp_path / "front.jpg"
    source.write_bytes(_jpeg_bytes())

    async def fake_chat(_messages, **kwargs):
        assert kwargs["provider"] == "vlm-provider"
        return _json.dumps(_valid_payload())

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    result = await hc._detect_head_geometry(
        str(source), img_w=800, img_h=1200, call_meta={},
    )
    assert result["head_box"] == [0.35, 0.05, 0.65, 0.20]


async def test_detect_head_geometry_retries_once_then_succeeds(monkeypatch, tmp_path) -> None:
    import json as _json

    source = tmp_path / "front.jpg"
    source.write_bytes(_jpeg_bytes())
    calls = {"n": 0}

    async def fake_chat(_messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not even json"
        return _json.dumps(_valid_payload())

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    result = await hc._detect_head_geometry(
        str(source), img_w=800, img_h=1200, call_meta={},
    )
    assert calls["n"] == 2
    assert result["clothing_top_y"] == 0.30


async def test_detect_head_geometry_raises_after_two_failures(monkeypatch, tmp_path, caplog) -> None:
    source = tmp_path / "front.jpg"
    source.write_bytes(_jpeg_bytes())
    calls = {"n": 0}

    async def fake_chat(_messages, **_kwargs):
        calls["n"] += 1
        return "still not json"

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    with caplog.at_level(logging.WARNING, logger="app.portraits.headshot_crop"):
        with pytest.raises(ValueError, match="连续两次未通过"):
            await hc._detect_head_geometry(str(source), img_w=800, img_h=1200, call_meta={})
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# crop_headshot_from_portrait：端到端（打桩 VLM，不打桩几何/裁切/字节拼接）
# ---------------------------------------------------------------------------

async def test_crop_headshot_from_portrait_preserves_app11_provenance(monkeypatch, tmp_path) -> None:
    import json as _json

    source = tmp_path / "front_full.jpg"
    segment = _app11_segment()
    source.write_bytes(hc._insert_app11_segments(_jpeg_bytes(size=(800, 1200)), [segment]))
    dest = tmp_path / "views" / "face_closeup.jpg"

    async def fake_chat(_messages, **_kwargs):
        return _json.dumps(_valid_payload())

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    qa = await hc.crop_headshot_from_portrait(
        str(source), dest_path=str(dest), call_meta={"character_name": "甲一"},
    )

    assert qa["provenance_preserved"] is True
    assert qa["head_box"] == [0.35, 0.05, 0.65, 0.20]
    assert dest.is_file()
    output_bytes = dest.read_bytes()
    assert segment in output_bytes
    with Image.open(dest) as image:
        image.load()
        assert max(image.size) >= 768


async def test_crop_headshot_from_portrait_without_app11_flags_no_provenance(
    monkeypatch, tmp_path, caplog,
) -> None:
    import json as _json

    source = tmp_path / "front_full.jpg"
    source.write_bytes(_jpeg_bytes(size=(800, 1200)))
    dest = tmp_path / "face_closeup.jpg"

    async def fake_chat(_messages, **_kwargs):
        return _json.dumps(_valid_payload())

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    with caplog.at_level(logging.WARNING, logger="app.portraits.headshot_crop"):
        qa = await hc.crop_headshot_from_portrait(
            str(source), dest_path=str(dest), call_meta={"character_name": "甲一"},
        )

    assert qa["provenance_preserved"] is False
    assert any("无 C2PA 溯源" in record.message for record in caplog.records)


async def test_crop_headshot_from_portrait_flags_tight_neck_margin(
    monkeypatch, tmp_path, caplog,
) -> None:
    """衣领贴近下巴的定妆照：不改变裁切框（硬约束优先），但要留下可见信号。"""
    import json as _json

    source = tmp_path / "front_full.jpg"
    source.write_bytes(_jpeg_bytes(size=(1440, 2560)))
    dest = tmp_path / "face_closeup.jpg"
    tight_payload = {"head_box": [0.37, 0.075, 0.625, 0.195], "clothing_top_y": 0.215}

    async def fake_chat(_messages, **_kwargs):
        return _json.dumps(tight_payload)

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    with caplog.at_level(logging.WARNING, logger="app.portraits.headshot_crop"):
        qa = await hc.crop_headshot_from_portrait(
            str(source), dest_path=str(dest), call_meta={"character_name": "甲一"},
        )

    assert qa["neck_margin_ratio"] == pytest.approx(0.1466, abs=1e-3)
    assert any("颈部余量偏紧" in record.message for record in caplog.records)


async def test_crop_headshot_from_portrait_roomy_neck_margin_no_warning(
    monkeypatch, tmp_path, caplog,
) -> None:
    import json as _json

    source = tmp_path / "front_full.jpg"
    source.write_bytes(_jpeg_bytes(size=(800, 1200)))
    dest = tmp_path / "face_closeup.jpg"

    async def fake_chat(_messages, **_kwargs):
        return _json.dumps(_valid_payload())

    monkeypatch.setattr(hc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(hc.hiagent, "active_provider", lambda kind: "vlm-provider")

    with caplog.at_level(logging.WARNING, logger="app.portraits.headshot_crop"):
        qa = await hc.crop_headshot_from_portrait(
            str(source), dest_path=str(dest), call_meta={"character_name": "甲一"},
        )

    assert qa["neck_margin_ratio"] >= hc._TIGHT_NECK_MARGIN_RATIO
    assert not any("颈部余量偏紧" in record.message for record in caplog.records)


def test_module_uses_real_model_gateway_binding_for_monkeypatch() -> None:
    """守卫：headshot_crop 的视觉调用必须走 ``from app.harness import model_gateway``
    再 ``model_gateway.chat``（app/portraits 下禁止直接 hiagent.chat，见
    scripts/check_contract_surface.py），不能 ``from ...model_gateway import chat``
    ——后者会让测试里对 ``hc.model_gateway.chat`` 的打桩静默失效（CLAUDE.md
    「拆包会静默废掉 monkeypatch」同一类教训）。"""
    import inspect

    from app.harness import model_gateway

    assert hc.model_gateway is model_gateway
    assert hc.hiagent is hiagent
    assert "hiagent.chat(" not in inspect.getsource(hc)


def test_crop_box_trusts_clothing_top_when_collar_rises_above_estimated_chin() -> None:
    """B 上顾屿实测几何：头框下沿 0.23 偏松，立领领尖在 0.22。下边界以衣领为准
    裁在下巴估计之上，不再把领尖带进头像照。"""
    head_box = [0.40, 0.08, 0.60, 0.23]
    box = hc._compute_crop_box_px(head_box, 0.22, img_w=1440, img_h=2560)
    chin_px = 0.23 * 2560
    collar_px = 0.22 * 2560
    assert box[3] < collar_px
    assert box[3] < chin_px


def test_crop_box_floor_protects_face_when_clothing_estimate_is_too_high() -> None:
    """衣领估得离谱地高（落在嘴部附近）时，最多裁到头框 80% 高度处。"""
    head_box = [0.40, 0.08, 0.60, 0.23]
    box = hc._compute_crop_box_px(head_box, 0.15, img_w=1440, img_h=2560)
    floor_px = (0.08 + 0.15 * 0.8) * 2560
    assert box[3] == round(floor_px)
