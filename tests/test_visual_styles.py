from app.visual_styles import (
    VISUAL_STYLE_PRESETS,
    is_photographic_style_prompt,
    visual_style_options,
    visual_style_prompt,
)


def test_visual_style_presets_define_complete_positive_render_contracts() -> None:
    for preset in VISUAL_STYLE_PRESETS:
        assert preset.name.strip()
        assert preset.description.strip()
        assert preset.prompt.strip()
        assert preset.sample_image.strip()
        # 摄影风预设不能再声明"非真人照片"（与"照片级"自相矛盾，2026-09-27
        # 修复，见《顾念长安》第1集134秒复盘）；非摄影预设保持原有措辞不变。
        if preset.photographic:
            assert "非真人" not in preset.prompt
        else:
            assert "非真人照片" in preset.prompt

    assert "东方仙侠" not in visual_style_prompt("国漫电影风")
    assert "虚构数字角色" in visual_style_prompt("国漫电影风")


def _resolve_preset_name_by_prompt(prompt: str) -> str | None:
    """通用反查示例：给定 prompt 文本（当前值或历史遗留值）找回预设名，模拟
    "给前端显示当前画风名"这类反查逻辑应遵循的匹配口径——当前 ``prompt`` 优先，
    ``legacy_prompts`` 兼容旧值；两者都不命中则返回 None（自由文本/已下线预设）。
    """
    for preset in VISUAL_STYLE_PRESETS:
        if preset.prompt == prompt or prompt in preset.legacy_prompts:
            return preset.name
    return None


def test_legacy_photographic_prompts_still_resolve_to_same_preset() -> None:
    """2026-09-27 文案修正前落库的旧真人摄影风/精修真人风文案：改文案后仍必须
    判定为摄影风，且任何按 prompt 反查预设名的逻辑都要能反查回同一个预设名
    ——否则存量项目（如《顾念长安》）会被判成非摄影风，下游随即拼上"必须
    CG/动画渲染"的锁定句，灾难性回退。"""
    for preset in VISUAL_STYLE_PRESETS:
        for legacy in preset.legacy_prompts:
            assert is_photographic_style_prompt(legacy) is preset.photographic
            assert _resolve_preset_name_by_prompt(legacy) == preset.name

    # 本次真正改文案的只有这两个摄影预设；防止将来清空 legacy_prompts 却没人
    # 发现覆盖率归零，导致兼容性静默失效。
    covered = {p.name for p in VISUAL_STYLE_PRESETS if p.legacy_prompts}
    assert covered == {"真人摄影风", "精修真人风"}


def test_photographic_presets_prompt_has_no_self_contradiction() -> None:
    """真人摄影风/精修真人风的新文案不能再自相矛盾：既要求"照片级"渲染，又
    声明"非真人照片"/"数字角色"。旧文案是 a402e074 从动漫预设照抄的样板；
    《顾念长安》第1集134秒女主变皮克斯式卡通脸（当段缺女主参考图）疑与此有关。"""
    for preset in VISUAL_STYLE_PRESETS:
        if not preset.photographic:
            continue
        assert "非真人" not in preset.prompt
        assert "数字角色" not in preset.prompt
        assert "不对应任何真实存在的人" in preset.prompt
        assert "卡通" in preset.prompt
        assert "动画" in preset.prompt
        assert "CG" in preset.prompt


def test_non_photographic_presets_prompt_unchanged() -> None:
    """本次只改真人摄影风/精修真人风两个摄影预设文案；非摄影预设必须逐字不变，
    保证存量动画项目落库的 visual_style_canonical 幂等指纹不受影响。"""
    assert visual_style_prompt("国漫电影风") == (
        "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"
    )
    assert visual_style_prompt("古典水墨风") == (
        "古典水墨动画意境，明确插画角色、非真人照片，山水氛围，留白雅致。"
    )
    assert is_photographic_style_prompt(visual_style_prompt("国漫电影风")) is False
    assert is_photographic_style_prompt(visual_style_prompt("古典水墨风")) is False


def test_visual_style_presets_reduced_to_four_with_two_photographic() -> None:
    """负责人拍板缩减到 4 条：真人摄影风/精修真人风/国漫电影风（默认）/古典水墨风。"""
    names = [preset.name for preset in VISUAL_STYLE_PRESETS]
    assert names == ["真人摄影风", "精修真人风", "国漫电影风", "古典水墨风"]

    photographic_names = {p.name for p in VISUAL_STYLE_PRESETS if p.photographic}
    assert photographic_names == {"真人摄影风", "精修真人风"}


def test_is_photographic_style_prompt_matches_by_resolved_prompt_text() -> None:
    """按解析后的 prompt 串逐字匹配，而不是按名称——世界书落库后只保留 prompt。"""
    assert is_photographic_style_prompt(visual_style_prompt("真人摄影风")) is True
    assert is_photographic_style_prompt(visual_style_prompt("精修真人风")) is True
    assert is_photographic_style_prompt(visual_style_prompt("国漫电影风")) is False
    assert is_photographic_style_prompt(visual_style_prompt("古典水墨风")) is False
    # 自由文本/历史遗留画风/已下线预设的旧值一律按非摄影处理（保守默认，不误判）。
    assert is_photographic_style_prompt("超写实风") is False
    assert is_photographic_style_prompt("") is False
    assert is_photographic_style_prompt(None) is False


def test_visual_style_options_carry_photographic_flag_from_presets() -> None:
    """前端导入面板用 ``photographic`` 判断要不要提示"视频阶段易被拒收"——
    这里逐条核对每个选项的 ``photographic`` 与其源预设一致，不是另建一份
    独立于 ``VISUAL_STYLE_PRESETS`` 的判断。"""
    options = {item["name"]: item for item in visual_style_options()}
    assert set(options) == {p.name for p in VISUAL_STYLE_PRESETS}
    for preset in VISUAL_STYLE_PRESETS:
        assert options[preset.name]["photographic"] is preset.photographic
