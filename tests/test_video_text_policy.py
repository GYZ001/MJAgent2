"""画面文字默认策略（app.production.video_text_policy）：2026-09-28 推翻 2026-09-14
「画面文字由视频模型直接生成」的旧默认——生产实测该模型中文字形不稳定，手机通话界面、
结束界面、招牌与黑板字全部乱码。本文件只服务 ``app.compiler``（旧架构逐镜编译路径）；
分镜台 2.x 的对应改动在 ``app.production.storyboard_dialects``。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.video_text_policy import _TEXT_POLICY_NONE, compile_text_policy


def test_default_policy_avoids_readable_on_screen_text():
    shot = SimpleNamespace(required_text=None)
    text = compile_text_policy(shot)
    assert text == _TEXT_POLICY_NONE
    assert "不出现需要读出的文字" in text
    assert "背面/侧面/虚焦拍摄" in text
    assert "改由台词" in text


def test_old_policy_of_generating_exact_on_screen_text_is_gone():
    """红态验证：修复前的旧默认（本次替换掉的那句）允许模型按画面描述直接生成牌匾/
    书信文字并要求逐字一致——这条断言证明旧默认已经不在了，不是新旧两句并存。"""
    assert "按画面描述直接生成" not in _TEXT_POLICY_NONE
    assert "逐字一致" not in _TEXT_POLICY_NONE


def test_explicit_required_text_strategies_are_unaffected_by_the_default_retirement():
    """required_text 显式声明的四种策略（embedded_prop/deterministic_insert/audio_only/
    none）是上游对"这一镜确实需要一段指定文字"的独立决定，不受默认策略改写影响。"""
    from app.schemas import RequiredOnScreenText

    shot = SimpleNamespace(
        required_text=RequiredOnScreenText(
            exact_text="距续约 30 天", surface="手机屏幕", strategy="embedded_prop",
        )
    )
    text = compile_text_policy(shot)
    assert "距续约 30 天" in text
    assert text != _TEXT_POLICY_NONE
