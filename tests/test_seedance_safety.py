from app.compiler import sanitize_seedance_prompt
from app.harness.hiagent_input_image_privacy import INPUT_IMAGE_PRIVACY_REJECTED_KIND
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
    prompt = "镜头动作：甲二儿追上甲一。 --ratio 9:16 --dur 8"

    normalized = sanitize_seedance_prompt(
        prompt,
        aggressive=True,
        extra_terms=(("甲二儿", "角色甲"), ("甲一", "角色乙")),
    )

    assert "甲二儿追上甲一" in normalized
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


REAL_PRIVACY_RAW_BODY = (
    '{"error":{"code":"InputImageSensitiveContentDetected.PrivacyInformation",'
    '"message":"The request failed because the input image \'content[2]\' may '
    'contain real person"}}'
)


def test_input_image_privacy_rejection_points_to_switching_visual_style() -> None:
    """真实案例（2026-08-31，《我欲封天》EP3-EP10 视频阶段 8/10 集被拒）：
    视频供应商按隐私政策拒收摄影类画风的输入图，这是确定性终态（同一画风
    重试必然复现），文案不得邀请用户"重试"，必须指向真出路（换画风），
    且不能把供应商英文原文直接甩给用户了事。"""
    exc = ProviderError(
        "上游请求失败（HTTP 400）",
        raw=REAL_PRIVACY_RAW_BODY,
        failure=ProviderFailure.model_rejection(INPUT_IMAGE_PRIVACY_REJECTED_KIND),
    )
    guidance = _video_model_rejection_guidance({}, exc)

    assert guidance is not None
    code, message = guidance
    assert code == "VIDEO_INPUT_IMAGE_PRIVACY_REJECTED"
    # 确定性终态：不建议原样重试，明确说明重试大概率复现同样的拒绝。
    assert "可稍后重试" not in message
    assert "同一画风原样重试大概率复现同样的拒绝" in message
    # 指向真出路：具体的非真人画风名字（从 VISUAL_STYLE_PRESETS 派生），不是空话。
    assert "国漫电影风" in message and "古典水墨风" in message
    assert "已停止对本镜的自动付费重试" in message
    # 不把供应商英文原文直接甩给用户——原始英文措辞不出现在落地文案里。
    assert "may contain real person" not in message


def test_input_image_privacy_rejection_is_externally_terminal_and_not_retryable() -> None:
    """与 ``ProviderFailure.model_rejection`` 的既有契约保持一致：分类结果本身
    决定了 ``app.media_exec.retry_scheduling._schedule_job_retry`` 不会自动
    重排——这里只核对分类结果的形状，行为侧已由
    tests/test_provider_call_lifecycle.py 覆盖。"""
    failure = ProviderFailure.model_rejection(INPUT_IMAGE_PRIVACY_REJECTED_KIND)

    assert failure.retryable is False
    assert failure.disposition.value == "external_terminal"
    assert failure.category.value == "model_rejection"
