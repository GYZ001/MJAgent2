from app.compiler import sanitize_seedance_prompt
from app.hiagent import ProviderError, ProviderFailure
from app.worker import _video_model_rejection_guidance


def test_seedance_normalization_preserves_story_content() -> None:
    prompt = (
        "镜头动作：十五岁少年在卧室床榻上愤怒地说：我草！\n"
        "画面结果：黑戒指诡异发光。 --ratio 9:16 --dur 5"
    )

    normalized = sanitize_seedance_prompt(prompt)

    assert "十五岁少年" in normalized
    assert "卧室床榻" in normalized
    assert "愤怒地说：我草" in normalized
    assert "黑戒指诡异发光" in normalized
    assert normalized.endswith("--ratio 9:16 --dur 5")


def test_legacy_retry_parameters_do_not_mutate_content() -> None:
    prompt = "镜头动作：萧薰儿追上萧炎。 --ratio 9:16 --dur 8"

    normalized = sanitize_seedance_prompt(
        prompt,
        aggressive=True,
        extra_terms=(("萧薰儿", "角色甲"), ("萧炎", "角色乙")),
    )

    assert "萧薰儿追上萧炎" in normalized
    assert "角色甲" not in normalized
    assert "角色乙" not in normalized
    assert normalized.endswith("--ratio 9:16 --dur 8")


def test_video_rejection_guidance_uses_typed_provider_state_only() -> None:
    arbitrary_message = "任意未来供应商报文，不应由词语决定分类"
    guidance = _video_model_rejection_guidance(
        {"mode": "FIRST_LAST_FRAME_MODE"},
        ProviderError(
            arbitrary_message,
            failure=ProviderFailure.model_rejection(),
        ),
    )

    assert guidance is not None
    assert guidance[0] == "VIDEO_PROVIDER_MODEL_REJECTED"
    assert "FIRST_LAST_FRAME_MODE" in guidance[1]
    assert "没有改写内容" in guidance[1]


def test_untyped_provider_failure_does_not_become_model_rejection() -> None:
    guidance = _video_model_rejection_guidance(
        {"mode": "REFERENCE_IMAGE_MODE"},
        ProviderError("文本看起来像拒绝，但没有结构化状态"),
    )

    assert guidance is None
