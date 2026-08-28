import asyncio

from app import hiagent


def test_zhipu_chat_uses_official_route(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        calls.append((url, model, key_name))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.2")
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}]))

    assert content == "ok"
    assert calls == [("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-5.2", "ZHIPU_API_KEY")]


def test_zhipu_disable_thinking_sends_thinking_disabled(monkeypatch) -> None:
    payloads: list[dict] = []

    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.3-flash")
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 131072,
            "max_output_tokens": 32768,
            "token_limits_source": "test",
        },
    )
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    content = asyncio.run(hiagent.chat(
        [{"role": "user", "content": "hi"}],
        call_meta={"disable_thinking": True, "stage_key": "character_roll_call"},
        max_tokens=512,
    ))

    assert content == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["max_tokens"] == 512


def _zhipu_harness(monkeypatch, payloads: list[dict]) -> None:
    async def fake_post_json(client, url, payload, *, kind, model, retries=2, headers=None, key_name="", meta=None):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "zhipu")
    monkeypatch.setattr(hiagent, "active_model", lambda kind, provider=None: "glm-5.3-flash")
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 1048576,
            "max_output_tokens": 131072,
            "token_limits_source": "configured",
        },
    )
    monkeypatch.setattr(hiagent.config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(hiagent, "_zhipu_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")


def test_call_site_can_declare_reasoning_effort(monkeypatch) -> None:
    """GLM-5.3 关不掉思考，reasoning_effort 是唯一能压住思考开销的旋钮。

    分镜台阶段二在 max 档稳定思考 30417~30839 token，把整份 completion 预算
    吃光；「这一步要想多深」是任务的性质，所以必须能按调用点声明。
    """
    payloads: list[dict] = []
    _zhipu_harness(monkeypatch, payloads)

    asyncio.run(hiagent.chat(
        [{"role": "user", "content": "hi"}],
        call_meta={"reasoning_effort": "low", "stage_key": "storyboard_pack_segment"},
        max_tokens=512,
    ))

    assert payloads[0]["reasoning_effort"] == "low"


def test_no_declaration_keeps_the_request_unchanged(monkeypatch) -> None:
    """不表态就不发这个字段，沿用模型自己的默认档——不改既有行为。"""
    payloads: list[dict] = []
    _zhipu_harness(monkeypatch, payloads)
    monkeypatch.setattr(hiagent.config, "TEXT_REASONING_EFFORT", "")

    asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}], max_tokens=512))

    assert "reasoning_effort" not in payloads[0]


def test_operator_default_applies_when_call_site_is_silent(monkeypatch) -> None:
    payloads: list[dict] = []
    _zhipu_harness(monkeypatch, payloads)
    monkeypatch.setattr(
        hiagent, "get_setting",
        lambda key: "high" if key == "text_reasoning_effort" else "",
    )

    asyncio.run(hiagent.chat([{"role": "user", "content": "hi"}], max_tokens=512))

    assert payloads[0]["reasoning_effort"] == "high"


def test_unknown_effort_value_is_passed_through_not_rejected(monkeypatch) -> None:
    """档位是供应商定义的开放集合（5.2 有 7 档、5.3 收成 3 档）。

    本地穷举一份白名单只会在供应商加档位时误伤合法值；真填错了由供应商拒绝。
    """
    payloads: list[dict] = []
    _zhipu_harness(monkeypatch, payloads)

    asyncio.run(hiagent.chat(
        [{"role": "user", "content": "hi"}],
        call_meta={"reasoning_effort": "ultra"},
        max_tokens=512,
    ))

    assert payloads[0]["reasoning_effort"] == "ultra"
