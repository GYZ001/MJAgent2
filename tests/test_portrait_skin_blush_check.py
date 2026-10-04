"""``app.portraits.portrait_skin_blush_check``：生成后肤色局部色块核验与有界
重画。覆盖：判定两次重问后失败/成功、非写实画风跳过核验、判否触发加强措辞
重画（含 operation_id 换新，避免 dedup 复用吐回刚被拒的图）、重画次数用尽后
标记 flagged、判定本身连续失败立即停止标记 unverified（不继续重画）。
"""
from __future__ import annotations

import json

from app.portraits import portrait_skin_audit_store as audit_store
from app.portraits import portrait_skin_blush_check as sbc

_PHOTOGRAPHIC_STYLE = "真人实拍电影质感，照片级写实人像，原创虚构人物，自然光影，皮肤肌理清晰，全程实拍写实渲染，不出现卡通、动画或CG质感。"
_NON_PHOTOGRAPHIC_STYLE = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"


# ---------------------------------------------------------------------------
# judge_face_local_color
# ---------------------------------------------------------------------------

async def test_judge_succeeds_on_first_valid_response(monkeypatch, tmp_path):
    image = tmp_path / "front.jpg"
    image.write_bytes(b"x")

    async def fake_chat(_messages, **kwargs):
        assert kwargs["provider"] == "vlm-provider"
        return json.dumps({"has_local_color": True, "reason": "脸颊有一块粉红色块"})

    monkeypatch.setattr(sbc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(sbc.hiagent, "active_provider", lambda kind: "vlm-provider")

    result = await sbc.judge_face_local_color(str(image), call_meta={})
    assert result == {
        "checked": True, "rule_version": sbc.PORTRAIT_SKIN_BLUSH_RULE_VERSION,
        "has_local_color": True, "reason": "脸颊有一块粉红色块",
    }


async def test_judge_retries_once_then_succeeds(monkeypatch, tmp_path):
    image = tmp_path / "front.jpg"
    image.write_bytes(b"x")
    calls = {"n": 0}

    async def fake_chat(_messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return json.dumps({"has_local_color": False, "reason": "肤色均匀"})

    monkeypatch.setattr(sbc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(sbc.hiagent, "active_provider", lambda kind: "vlm-provider")

    result = await sbc.judge_face_local_color(str(image), call_meta={})
    assert calls["n"] == 2
    assert result["checked"] is True
    assert result["has_local_color"] is False


async def test_judge_returns_unchecked_after_two_failures(monkeypatch, tmp_path, caplog):
    image = tmp_path / "front.jpg"
    image.write_bytes(b"x")
    calls = {"n": 0}

    async def fake_chat(_messages, **_kwargs):
        calls["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(sbc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(sbc.hiagent, "active_provider", lambda kind: "vlm-provider")

    result = await sbc.judge_face_local_color(str(image), call_meta={})
    assert calls["n"] == 2
    assert result["checked"] is False
    assert result["has_local_color"] is None


def test_judge_prompt_excludes_natural_feature_color_and_light_gradient():
    """判定提示词检测范围要与生成侧规则同一部位列表（脸颊/眼皮/鼻头/额头/下巴），
    并明确排除嘴唇/眉毛/头发/瞳孔固有色与无硬边界的自然光渐变色温——否则任何嘴唇
    清晰可见的写实人像都可能被误判成「有局部色块」（2026-10-04 复查发现）。"""
    prompt = sbc._JUDGE_PROMPT
    for part in ("脸颊", "眼皮", "鼻头", "额头", "下巴"):
        assert part in prompt
    for excluded in ("嘴唇", "眉毛", "头发", "瞳孔", "渐变式暖冷色调"):
        assert excluded in prompt


# ---------------------------------------------------------------------------
# generate_front_full_with_skin_check
# ---------------------------------------------------------------------------

def _recorder(path=None):
    """``path`` 非空时真实落盘非空字节——与生产行为一致（``judge_face_local_color``/
    ``_cache_verdict`` 都要真读这个文件），否则只记录调用参数不碰磁盘。"""
    calls: list[dict] = []

    async def generate_and_write(prompt: str, meta: dict) -> None:
        calls.append({"prompt": prompt, "meta": dict(meta)})
        if path is not None:
            path.write_bytes(f"image-attempt-{len(calls)}".encode())

    return calls, generate_and_write


async def test_non_photographic_generates_once_and_skips_judge(monkeypatch, tmp_path):
    calls, generate_and_write = _recorder()

    async def fail_if_called(*_a, **_k):
        raise AssertionError("非写实画风不应调用核验")

    monkeypatch.setattr(sbc, "judge_face_local_color", fail_if_called)

    result = await sbc.generate_front_full_with_skin_check(
        prompt="p0", path=str(tmp_path / "front.jpg"), visual_style=_NON_PHOTOGRAPHIC_STYLE,
        call_meta={"operation_id": "op1"}, generate_and_write=generate_and_write,
        project_id="p1", character_name="甲",
    )
    assert result["status"] == sbc.STATUS_SKIPPED
    assert len(calls) == 1


async def test_photographic_clean_on_first_attempt(monkeypatch, tmp_path):
    calls, generate_and_write = _recorder(tmp_path / "front.jpg")

    async def fake_judge(_path, *, call_meta):
        return {"checked": True, "has_local_color": False, "reason": "肤色均匀", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", fake_judge)

    result = await sbc.generate_front_full_with_skin_check(
        prompt="p0", path=str(tmp_path / "front.jpg"), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={"operation_id": "op1"}, generate_and_write=generate_and_write,
        project_id="p1", character_name="甲",
    )
    assert result["status"] == sbc.STATUS_CLEAN
    assert len(calls) == 1
    assert len(result["attempts"]) == 1


async def test_photographic_retries_with_strengthened_prompt_and_fresh_operation_id(monkeypatch, tmp_path):
    calls, generate_and_write = _recorder(tmp_path / "front.jpg")
    judge_calls = {"n": 0}

    async def fake_judge(_path, *, call_meta):
        judge_calls["n"] += 1
        has_color = judge_calls["n"] == 1
        return {"checked": True, "has_local_color": has_color, "reason": "r", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", fake_judge)

    result = await sbc.generate_front_full_with_skin_check(
        prompt="base prompt", path=str(tmp_path / "front.jpg"), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={"operation_id": "op1"}, generate_and_write=generate_and_write,
        project_id="p1", character_name="甲",
    )
    assert result["status"] == sbc.STATUS_CLEAN
    assert len(calls) == 2
    # 第二次生成必须换过加强措辞的提示词，且 operation_id 不能复用第一次的（dedup 复用会原样吐回刚被判否的图）。
    assert calls[0]["prompt"] == "base prompt"
    assert calls[1]["prompt"] != "base prompt"
    assert calls[1]["prompt"].startswith("base prompt")
    assert calls[0]["meta"]["operation_id"] == "op1"
    assert calls[1]["meta"]["operation_id"] != "op1"


async def test_photographic_flags_after_exhausting_attempts(monkeypatch, tmp_path, caplog):
    calls, generate_and_write = _recorder(tmp_path / "front.jpg")

    async def always_bad(_path, *, call_meta):
        return {"checked": True, "has_local_color": True, "reason": "还是有色块", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", always_bad)

    result = await sbc.generate_front_full_with_skin_check(
        prompt="base", path=str(tmp_path / "front.jpg"), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={}, generate_and_write=generate_and_write,
        project_id="p1", character_name="甲",
    )
    assert result["status"] == sbc.STATUS_FLAGGED
    assert len(calls) == sbc.MAX_SKIN_BLUSH_ATTEMPTS
    assert len(result["attempts"]) == sbc.MAX_SKIN_BLUSH_ATTEMPTS


async def test_photographic_stops_immediately_when_judge_unverified(monkeypatch, tmp_path):
    """判定本身连续失败（未判定）不应当被当成「判否」继续重画——按人工确认处理，
    只消耗一次生成。"""
    calls, generate_and_write = _recorder()

    async def unverified(_path, *, call_meta):
        return {"checked": False, "has_local_color": None, "reason": "", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", unverified)

    result = await sbc.generate_front_full_with_skin_check(
        prompt="base", path=str(tmp_path / "front.jpg"), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={}, generate_and_write=generate_and_write,
        project_id="p1", character_name="甲",
    )
    assert result["status"] == sbc.STATUS_UNVERIFIED
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 生成流程自带判定回填缓存——否则存量核验端点对刚生成的图第一次查询必然
# 未命中，被迫对同一张图再发一次独立的真实模型调用（2026-10-04 复查发现）。
# ---------------------------------------------------------------------------


async def test_clean_verdict_is_cached_for_the_final_image(monkeypatch, tmp_path):
    path = tmp_path / "front.jpg"
    calls, generate_and_write = _recorder()

    async def fake_judge(_path, *, call_meta):
        return {"checked": True, "has_local_color": False, "reason": "肤色均匀", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", fake_judge)

    async def write_real_bytes(_prompt, _meta):
        path.write_bytes(b"clean-image")
        calls.append({})

    await sbc.generate_front_full_with_skin_check(
        prompt="p0", path=str(path), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={}, generate_and_write=write_real_bytes, project_id="p1", character_name="甲",
    )
    cached = audit_store.get_cached_audit(audit_store.content_sha256(str(path)), "v1")
    assert cached == {"has_local_color": False, "reason": "肤色均匀", "checked_at": cached["checked_at"]}


async def test_flagged_verdict_is_cached_too(monkeypatch, tmp_path):
    path = tmp_path / "front.jpg"

    async def always_bad(_path, *, call_meta):
        return {"checked": True, "has_local_color": True, "reason": "还是有色块", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", always_bad)

    async def write_real_bytes(_prompt, _meta):
        path.write_bytes(b"flagged-image")

    await sbc.generate_front_full_with_skin_check(
        prompt="base", path=str(path), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={}, generate_and_write=write_real_bytes, project_id="p1", character_name="甲",
    )
    cached = audit_store.get_cached_audit(audit_store.content_sha256(str(path)), "v1")
    assert cached is not None
    assert cached["has_local_color"] is True


async def test_unverified_verdict_is_not_cached(monkeypatch, tmp_path):
    path = tmp_path / "front.jpg"

    async def unverified(_path, *, call_meta):
        return {"checked": False, "has_local_color": None, "reason": "", "rule_version": "v1"}

    monkeypatch.setattr(sbc, "judge_face_local_color", unverified)

    async def write_real_bytes(_prompt, _meta):
        path.write_bytes(b"unverified-image")

    await sbc.generate_front_full_with_skin_check(
        prompt="base", path=str(path), visual_style=_PHOTOGRAPHIC_STYLE,
        call_meta={}, generate_and_write=write_real_bytes, project_id="p1", character_name="甲",
    )
    assert audit_store.get_cached_audit(audit_store.content_sha256(str(path)), "v1") is None


async def test_skipped_verdict_is_not_cached(monkeypatch, tmp_path):
    path = tmp_path / "front.jpg"

    async def write_real_bytes(_prompt, _meta):
        path.write_bytes(b"skipped-image")

    await sbc.generate_front_full_with_skin_check(
        prompt="p0", path=str(path), visual_style=_NON_PHOTOGRAPHIC_STYLE,
        call_meta={}, generate_and_write=write_real_bytes, project_id="p1", character_name="甲",
    )
    assert audit_store.get_cached_audit(audit_store.content_sha256(str(path)), "v1") is None
