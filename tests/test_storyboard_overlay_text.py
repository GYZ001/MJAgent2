"""台词字幕/名条确定性校验（2026-09-15 收窄：只拦把台词叠在画面上，画面艺术字/倒计时不拦）。"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_overlay_text import overlay_text_errors

CONSTRAINT = "全片贯穿：环境音为车流声；配乐为钢琴；风格为国漫3D；约束为面部一致、手指正确、人数锁定、台词只出声不出字幕、无水印、人物与家具不穿插。"


def test_dialogue_subtitle_and_name_tag_are_rejected() -> None:
    assert overlay_text_errors(SimpleNamespace(prompt_text="镜头2：画面下方出现字幕「三十天」。" + CONSTRAINT))
    assert overlay_text_errors(SimpleNamespace(prompt_text="镜头2：人物下方显示说话人名条。" + CONSTRAINT))
    errors = overlay_text_errors(SimpleNamespace(prompt_text="镜头1：画面底部叠加台词文本。" + CONSTRAINT))
    assert len(errors) == 1 and "台词字幕" in errors[0]


def test_diegetic_art_text_and_standard_constraint_pass() -> None:
    prompt = ("镜头3：固定远景，玻璃上贴着大片色块横幅，画面右下角浮现白色“距续约30天”的文字标识；"
              "门楣牌匾上『晚安宠物医院』六字；手机屏幕上显示『距续约 30 天』倒计时。" + CONSTRAINT)
    assert overlay_text_errors(SimpleNamespace(prompt_text=prompt)) == []
    assert overlay_text_errors(SimpleNamespace(prompt_text="")) == []
