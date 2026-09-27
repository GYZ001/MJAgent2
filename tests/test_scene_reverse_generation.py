"""app.scene_reverse 生成侧：起草（draft）、判定（judge）、生成策略（produce）。

反打图「做实」链路：先起草机位背后一侧的构图描述，第一次带主视角图作种子生成，
判定；判定明确不通过才补第二次纯文生图并判定；判定调用失败不重试（与
``app.media_exec.subtitle_gate``「未判定放行」同一取舍）。draft/judge 的模型
调用全部打桩，不发真实网络请求；produce 全量依赖注入，测试直接传假协作者，
不需要打桩。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app import hiagent
from app.scene_reverse import draft, judge, produce


def _write_image(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"fake-image-bytes")
    return str(path)


# ---------------------------------------------------------------------------
# draft.draft_behind_camera_note
# ---------------------------------------------------------------------------

def test_draft_returns_note_from_structured_json(tmp_path, monkeypatch) -> None:
    async def fake_chat(_messages, **kwargs):
        assert kwargs["provider"] == "vlm-provider"
        assert kwargs["response_format"] == {"type": "json_object"}
        return '{"note": "机位背后是斑驳的砖墙与半开的木门"}'

    monkeypatch.setattr(hiagent, "chat", fake_chat)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")

    note = asyncio.run(draft.draft_behind_camera_note(
        scene_canonical="老旧修表铺", visual_style="国漫电影风",
        establishing_image_path=_write_image(tmp_path, "est.jpg"), scene_reference_id="scene_1",
    ))

    assert note == "机位背后是斑驳的砖墙与半开的木门"


def test_draft_returns_empty_string_on_provider_error(tmp_path, monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise hiagent.ProviderError("网关拒收")

    monkeypatch.setattr(hiagent, "chat", boom)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")

    note = asyncio.run(draft.draft_behind_camera_note(
        scene_canonical="老旧修表铺", visual_style="国漫电影风",
        establishing_image_path=_write_image(tmp_path, "est.jpg"), scene_reference_id="scene_1",
    ))

    assert note == ""


def test_draft_returns_empty_string_on_unparseable_json(tmp_path, monkeypatch) -> None:
    async def fake_chat(*_a, **_k):
        return "这不是 JSON"

    monkeypatch.setattr(hiagent, "chat", fake_chat)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")

    note = asyncio.run(draft.draft_behind_camera_note(
        scene_canonical="老旧修表铺", visual_style="国漫电影风",
        establishing_image_path=_write_image(tmp_path, "est.jpg"), scene_reference_id="scene_1",
    ))

    assert note == ""


# ---------------------------------------------------------------------------
# judge.judge_reverse_angle
# ---------------------------------------------------------------------------

def test_judge_passes_on_structured_json(tmp_path, monkeypatch) -> None:
    async def fake_chat(*_a, **_k):
        return '{"passed": true, "reason": "机位对调、背景换了另一侧墙面"}'

    monkeypatch.setattr(hiagent, "chat", fake_chat)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")

    verdict = asyncio.run(judge.judge_reverse_angle(
        establishing_path=_write_image(tmp_path, "est.jpg"),
        candidate_path=_write_image(tmp_path, "cand.jpg"), call_meta={"scene_reference_id": "scene_1"},
    ))

    assert verdict == {
        "checked": True, "passed": True, "reason": "机位对调、背景换了另一侧墙面", "error": None,
    }


def test_judge_string_booleans_are_read_literally_and_other_types_fail_closed(tmp_path, monkeypatch) -> None:
    """bool("false") 为真：字符串 "false" 必须判不通过，非布尔值按未判定处理、不判通过。"""
    replies = iter(['{"passed": "false", "reason": "只是平移"}', '{"passed": "yes", "reason": "?"}'])

    async def fake_chat(*_a, **_k):
        return next(replies)

    monkeypatch.setattr(hiagent, "chat", fake_chat)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")
    kwargs = dict(
        establishing_path=_write_image(tmp_path, "est.jpg"),
        candidate_path=_write_image(tmp_path, "cand.jpg"), call_meta={"scene_reference_id": "scene_1"},
    )

    first = asyncio.run(judge.judge_reverse_angle(**kwargs))
    second = asyncio.run(judge.judge_reverse_angle(**kwargs))

    assert first["checked"] is True and first["passed"] is False
    assert second["checked"] is False and second["passed"] is None


def test_judge_falls_back_to_regex_when_json_is_malformed(tmp_path, monkeypatch) -> None:
    """B 上实测过同类故障（subtitle_gate 43 次里 3 次）：整体 JSON 坏了但字段完好。"""
    async def fake_chat(*_a, **_k):
        return '好的：{"passed": false, "reason": "只是同方向平移"} 完毕'

    monkeypatch.setattr(hiagent, "chat", fake_chat)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")

    verdict = asyncio.run(judge.judge_reverse_angle(
        establishing_path=_write_image(tmp_path, "est.jpg"),
        candidate_path=_write_image(tmp_path, "cand.jpg"), call_meta={},
    ))

    assert verdict["checked"] is True
    assert verdict["passed"] is False


def test_judge_reports_unchecked_on_failure_without_raising(tmp_path, monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise TimeoutError("网关超时")

    monkeypatch.setattr(hiagent, "chat", boom)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "vlm-provider")

    verdict = asyncio.run(judge.judge_reverse_angle(
        establishing_path=_write_image(tmp_path, "est.jpg"),
        candidate_path=_write_image(tmp_path, "cand.jpg"), call_meta={},
    ))

    assert verdict["checked"] is False
    assert verdict["passed"] is None
    assert "TimeoutError" in verdict["error"]


# ---------------------------------------------------------------------------
# produce.produce_reverse_angle_view（全量依赖注入，不需要打桩）
# ---------------------------------------------------------------------------

def _make_path_factory(tmp_path: Path):
    counter = {"n": 0}

    def make_path() -> str:
        counter["n"] += 1
        return str(tmp_path / f"candidate_{counter['n']}.jpg")

    return make_path


async def _noop_save(_item, path: str) -> None:
    Path(path).write_bytes(b"generated")


def test_produce_uses_seeded_generation_and_stops_when_judge_passes(tmp_path) -> None:
    calls: dict[str, list] = {"generate": [], "judge": [], "discard": []}

    async def generate_image(prompt, **kwargs):
        calls["generate"].append((prompt, kwargs.get("seed_inputs")))
        return {"b64_json": "x"}

    async def judge_fn(**kwargs):
        calls["judge"].append(kwargs)
        return {"checked": True, "passed": True, "reason": "确实相反", "error": None}

    async def draft_fn(**_kwargs):
        return "背后是砖墙"

    result = asyncio.run(produce.produce_reverse_angle_view(
        scene_canonical="老旧修表铺", visual_style="国漫电影风", base_prompt="BASE_PROMPT。", op_identity="fp_1",
        establishing_image_path=_write_image(tmp_path, "est.jpg"),
        scene_reference_id="scene_1", scene_name="修表铺", size="1024x1820",
        make_path=_make_path_factory(tmp_path), generate_image=generate_image, save_image_item=_noop_save,
        discard_path=lambda p: calls["discard"].append(p), draft_fn=draft_fn, judge_fn=judge_fn,
    ))

    assert len(calls["generate"]) == 1
    assert calls["generate"][0][1] is not None  # 第一次带种子
    assert len(calls["judge"]) == 1
    assert calls["discard"] == []
    assert result["prompt"] == "BASE_PROMPT。原机位背后一侧的真实内容：背后是砖墙。"
    assert result["qa"]["draft"] == "背后是砖墙"
    assert result["qa"]["reverse_check"]["passed"] is True
    assert result["qa"]["attempts"] == [{"seeded": True, "passed": True, "reason": "确实相反"}]


def test_produce_retries_unseeded_when_first_judge_fails(tmp_path) -> None:
    calls: dict[str, list] = {"generate": [], "judge": [], "discard": []}
    verdicts = [
        {"checked": True, "passed": False, "reason": "只是平移", "error": None},
        {"checked": True, "passed": True, "reason": "第二次朝向对了", "error": None},
    ]

    async def generate_image(_prompt, **kwargs):
        calls["generate"].append(kwargs.get("seed_inputs"))
        return {"b64_json": "x"}

    async def judge_fn(**_kwargs):
        calls["judge"].append(1)
        return verdicts[len(calls["judge"]) - 1]

    async def draft_fn(**_kwargs):
        return ""

    result = asyncio.run(produce.produce_reverse_angle_view(
        scene_canonical="老旧修表铺", visual_style="国漫电影风", base_prompt="BASE_PROMPT。", op_identity="fp_1",
        establishing_image_path=_write_image(tmp_path, "est.jpg"),
        scene_reference_id="scene_1", scene_name="修表铺", size="1024x1820",
        make_path=_make_path_factory(tmp_path), generate_image=generate_image, save_image_item=_noop_save,
        discard_path=lambda p: calls["discard"].append(p), draft_fn=draft_fn, judge_fn=judge_fn,
    ))

    assert len(calls["generate"]) == 2
    assert calls["generate"][0] is not None  # 第一次带种子
    assert calls["generate"][1] is None  # 第二次纯文生图，不带种子
    assert len(calls["discard"]) == 1  # 第一次候选被丢弃
    assert result["qa"]["reverse_check"]["passed"] is True  # 最终判定取第二次
    assert len(result["qa"]["attempts"]) == 2
    assert result["prompt"] == "BASE_PROMPT。"  # draft 为空，不拼起草文本


def test_produce_does_not_retry_when_judge_call_itself_fails(tmp_path) -> None:
    calls: dict[str, list] = {"generate": [], "judge": []}

    async def generate_image(_prompt, **_kwargs):
        calls["generate"].append(1)
        return {"b64_json": "x"}

    async def judge_fn(**_kwargs):
        calls["judge"].append(1)
        return {"checked": False, "passed": None, "reason": "", "error": "TimeoutError"}

    async def draft_fn(**_kwargs):
        return ""

    def _must_not_discard(_path: str) -> None:
        raise AssertionError("判定失败不该丢弃候选")

    result = asyncio.run(produce.produce_reverse_angle_view(
        scene_canonical="老旧修表铺", visual_style="国漫电影风", base_prompt="BASE_PROMPT。", op_identity="fp_1",
        establishing_image_path=_write_image(tmp_path, "est.jpg"),
        scene_reference_id="scene_1", scene_name="修表铺", size="1024x1820",
        make_path=_make_path_factory(tmp_path), generate_image=generate_image, save_image_item=_noop_save,
        discard_path=_must_not_discard, draft_fn=draft_fn, judge_fn=judge_fn,
    ))

    assert len(calls["generate"]) == 1  # 判定失败不重试
    assert len(calls["judge"]) == 1
    assert result["qa"]["reverse_check"]["checked"] is False
    assert len(result["qa"]["attempts"]) == 1


def test_produce_operation_id_stable_across_different_draft_text(tmp_path) -> None:
    """draft_fn 温度非零，两次独立起草文本必然不同；但去重用的 operation_id 只能
    按调用方给的 op_identity 算，不能被起草文本带偏——否则异常重试时命中不上
    hiagent 侧已有的成功记录，会对同一逻辑请求重复付费生成一张图（回归用例）。"""
    op_ids: list[str] = []

    async def generate_image(_prompt, **kwargs):
        op_ids.append(kwargs["call_meta"]["operation_id"])
        return {"b64_json": "x"}

    async def judge_fn(**_kwargs):
        return {"checked": True, "passed": True, "reason": "确实相反", "error": None}

    drafts = iter(["背后是斑驳砖墙", "背后是半开木门与窗台"])

    async def draft_fn(**_kwargs):
        return next(drafts)

    for _ in range(2):
        asyncio.run(produce.produce_reverse_angle_view(
            scene_canonical="老旧修表铺", visual_style="国漫电影风", base_prompt="BASE_PROMPT。", op_identity="fp_1",
            establishing_image_path=_write_image(tmp_path, "est.jpg"),
            scene_reference_id="scene_1", scene_name="修表铺", size="1024x1820",
            make_path=_make_path_factory(tmp_path), generate_image=generate_image, save_image_item=_noop_save,
            discard_path=lambda _p: None, draft_fn=draft_fn, judge_fn=judge_fn,
        ))

    assert len(op_ids) == 2
    assert op_ids[0] == op_ids[1]


def test_produce_operation_id_changes_with_op_identity(tmp_path) -> None:
    """主视角图换了（落库指纹随之变）或人工重做（每次新指纹）时必须真的重新生成，
    不能因为提示词相同就复用 hiagent 侧的旧成功记录。"""
    op_ids: list[str] = []

    async def generate_image(_prompt, **kwargs):
        op_ids.append(kwargs["call_meta"]["operation_id"])
        return {"b64_json": "x"}

    async def judge_fn(**_kwargs):
        return {"checked": True, "passed": True, "reason": "确实相反", "error": None}

    async def draft_fn(**_kwargs):
        return "背后是斑驳砖墙"

    for identity in ("fp_old_establishing", "fp_new_establishing"):
        asyncio.run(produce.produce_reverse_angle_view(
            scene_canonical="老旧修表铺", visual_style="国漫电影风", base_prompt="BASE_PROMPT。", op_identity=identity,
            establishing_image_path=_write_image(tmp_path, "est.jpg"),
            scene_reference_id="scene_1", scene_name="修表铺", size="1024x1820",
            make_path=_make_path_factory(tmp_path), generate_image=generate_image, save_image_item=_noop_save,
            discard_path=lambda _p: None, draft_fn=draft_fn, judge_fn=judge_fn,
        ))

    assert len(op_ids) == 2
    assert op_ids[0] != op_ids[1]


def test_judge_prompt_rejects_mirror_and_other_space_explicitly() -> None:
    """标定口径：左右镜像曾被初版判定当成反打（吕家宅院），现版必须写明镜像与另一个空间都判否。"""
    assert "左右镜像" in judge._PROMPT and "另一个空间" in judge._PROMPT
    assert "椅背" in judge._PROMPT and "椅面" in judge._PROMPT
