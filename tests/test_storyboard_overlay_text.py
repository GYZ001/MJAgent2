"""叠加式文字确定性校验（2026-09-15《龙猫出爪》第 1 集第 2 镜：「画面右下角浮现白色“距续约30天”的文字标识」）。"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_overlay_text import overlay_text_errors

CONSTRAINT = "全片贯穿：环境音为车流声；配乐为钢琴；风格为国漫3D；约束为面部一致、手指正确、人数锁定、台词只出声不出字幕、无水印、人物与家具不穿插。"


def test_floating_caption_is_rejected_with_positive_guidance() -> None:
    draft = SimpleNamespace(prompt_text="镜头3：固定远景，玻璃上贴着大片色块横幅，画面右下角浮现白色“距续约30天”的文字标识。" + CONSTRAINT)
    errors = overlay_text_errors(draft)
    assert len(errors) == 1
    assert "浮现" in errors[0] and "手机屏幕上显示" in errors[0]


def test_post_production_caption_marker_is_rejected() -> None:
    assert overlay_text_errors(SimpleNamespace(prompt_text="镜头2：【后期字幕】距续约 30 天。" + CONSTRAINT))
    assert overlay_text_errors(SimpleNamespace(prompt_text="镜头2：画面下方出现字幕「三十天」。" + CONSTRAINT))


def test_diegetic_text_and_standard_constraint_pass() -> None:
    prompt = "镜头3：周晚低头看手机，手机屏幕上显示『距续约 30 天』倒计时；门楣牌匾上『晚安宠物医院』六字。" + CONSTRAINT
    assert overlay_text_errors(SimpleNamespace(prompt_text=prompt)) == []
    assert overlay_text_errors(SimpleNamespace(prompt_text="")) == []
