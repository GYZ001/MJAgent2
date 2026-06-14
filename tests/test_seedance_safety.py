from app.compiler import SOURCE_EXCERPT_MARKER, sanitize_seedance_prompt
from app.worker import _is_seedance_copyright_restricted, _is_seedance_text_sensitive


def test_sanitize_nonaggressive_only_softens_safety_terms() -> None:
    """普通（非 aggressive）模式只做题材无关的安全降级：未成年/年龄归一 + 脏话软化。
    修真/玄幻专用的场景与情绪改写（床榻→蒲团、愤怒→不甘、诡异→神秘）默认【不启用】——
    它们会篡改非修真题材的画面与情绪，是降质项，仅在平台返回敏感后的 aggressive 重提里才用。"""
    prompt = (
        "画面主体：十五岁清秀少年。镜头动作：萧炎闭目盘腿坐在床榻上，"
        "淡淡的白色气流顺着口鼻钻入体内，古朴黑戒指诡异发光将气流吸收殆尽，"
        "萧炎愤怒地死死捏紧拳头。我草！"
        "--ratio 9:16 --dur 10"
    )

    safe = sanitize_seedance_prompt(prompt)

    # 始终生效：年龄/未成年安全归一 + 脏话软化
    assert "十五岁" not in safe
    assert "少年感年轻角色" in safe
    assert "我草" not in safe
    assert "可恶" in safe
    # 默认保留：场景/情绪/修真措辞不被改写（避免对非修真题材降质）
    assert "床榻" in safe
    assert "钻入体内" in safe
    assert "吸收殆尽" in safe
    assert "诡异" in safe
    assert "愤怒" in safe
    assert "死死" in safe
    assert safe.endswith("--ratio 9:16 --dur 10")


def test_aggressive_sanitize_rewrites_genre_terms() -> None:
    """aggressive 模式（平台已判敏感后的重提）才启用题材专用措辞降级。"""
    prompt = (
        "镜头动作：萧炎闭目盘腿坐在床榻上，气流顺着口鼻钻入体内，"
        "黑戒指诡异发光将气流吸收殆尽，萧炎愤怒地死死捏紧拳头。"
        "--ratio 9:16 --dur 10"
    )

    safe = sanitize_seedance_prompt(prompt, aggressive=True)

    assert "床榻" not in safe
    assert "修炼蒲团" in safe
    assert "钻入体内" not in safe
    assert "吸收殆尽" not in safe
    assert "诡异" not in safe
    assert "愤怒" not in safe
    assert "死死" not in safe
    assert safe.endswith("--ratio 9:16 --dur 10")


def test_aggressive_sanitize_removes_source_excerpt_and_direct_dialogue() -> None:
    prompt = (
        "台词信息：萧炎说「好不容易修炼而来的斗之气，又在消失……我草！」。"
        f"{SOURCE_EXCERPT_MARKER}手指上那古朴的黑色戒指，再次诡异的微微发光。"
        "--ratio 9:16 --dur 10"
    )

    safe = sanitize_seedance_prompt(prompt, aggressive=True)

    assert SOURCE_EXCERPT_MARKER not in safe
    assert "我草" not in safe
    assert "萧炎说" not in safe
    assert "短促口型" in safe


def test_seedance_sensitive_error_detection() -> None:
    assert _is_seedance_text_sensitive("InputTextSensitiveContentDetected")
    assert _is_seedance_text_sensitive("The request failed because the input text may contain sensitive information")
    assert _is_seedance_text_sensitive("输入文本可能包含敏感信息")
    assert not _is_seedance_text_sensitive("轮询超出 15 分钟预算")


def test_seedance_copyright_error_detection() -> None:
    assert _is_seedance_copyright_restricted(
        "The request failed because the output video may be related to copyright restrictions.")
    assert _is_seedance_copyright_restricted("输出视频可能涉及版权限制")
    assert not _is_seedance_copyright_restricted("InputTextSensitiveContentDetected")
    assert not _is_seedance_copyright_restricted("轮询超出 15 分钟预算")


def test_extra_terms_genericize_copyright_names() -> None:
    """版权重提：用中性代称替换角色专名，降低输出与原 IP 的相似度。"""
    prompt = "镜头动作：萧薰儿快步追上萧炎，走到他身侧。--ratio 9:16 --dur 10"

    safe = sanitize_seedance_prompt(
        prompt, aggressive=True, extra_terms=(("萧薰儿", "角色甲"), ("萧炎", "角色乙")))

    assert "萧薰儿" not in safe
    assert "萧炎" not in safe
    assert "角色甲" in safe
    assert "角色乙" in safe
    assert safe.endswith("--ratio 9:16 --dur 10")
