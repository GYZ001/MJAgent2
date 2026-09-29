"""台词字幕/名条确定性校验（2026-09-15 收窄：只拦把台词叠在画面上，画面艺术字/倒计时不拦）。

2026-09-28：``app.production.storyboard_dialects`` 把画面文字的默认方言规则改成了「画面不
出现需要阅读的文字」，但本文件的判据范围没有变——``test_diegetic_art_text_and_standard_
constraint_pass`` 里的招牌/倒计时示例依旧只验证 overlay_text_errors 不拦这一类（拦截范围
仍只是「把台词叠在画面上」），不代表这类文字是新默认下的推荐写法。真正需要同步的是命中校验
时返回给模型重写的提示语措辞，见下面的 test_rewrite_hint_matches_the_new_on_screen_text_default。
"""
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


def test_rewrite_hint_matches_the_new_on_screen_text_default() -> None:
    """红态验证：修复前命中校验时返回的重写提示语里还写着旧默认「画面本身需要的文字……
    可以写，写清载体与逐字内容即可」，与新默认「画面不出现需要阅读的文字」正面冲突——
    这条断言证明旧措辞已经不在了，且新措辞与新默认口径一致。"""
    errors = overlay_text_errors(SimpleNamespace(prompt_text="镜头1：画面底部叠加台词文本。" + CONSTRAINT))
    assert len(errors) == 1
    assert "写清载体与逐字内容即可" not in errors[0]
    assert "可以写" not in errors[0]
    assert "不出现需要阅读的文字" in errors[0]
    assert "背面、侧面或虚焦角度拍摄" in errors[0]
    assert "改由台词或画外音说出" in errors[0]
