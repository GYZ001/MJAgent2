"""视频提示词里的「画面文字」规则：正文一句 + 负面清单一条。

2026-09-14 用户裁定：视频生成只负责画面与声音，台词字幕由后续功能另做；牌匾、书信这类
画面本身需要的文字由视频模型直接生成，不再「留白交后期」。生产 210 镜里 0 镜带
``required_text``，所以 ``_TEXT_POLICY_NONE`` 就是线上每一个视频提示词的文字规则——它曾写成
「画面中不出现任何文字」，与分镜方言「牌匾由模型直接生成」自相矛盾。

策略（``RequiredOnScreenText.strategy``，默认 ``embedded_prop``）：
- ``embedded_prop``：指定文字由本镜直接生成（默认）；
- ``deterministic_insert``：原始视频无字，终剪确定性插字（显式选用）；
- ``audio_only``：只靠声音交付信息；
- ``none``：明确要求画面无字。
"""
from __future__ import annotations

from app.continuity import required_text_strategy
from app.schemas import Shot

# 措辞受提示词预算约束（必填段落总长上限见 compiler），改动前先跑 test_compiler_prompt_budget。
_TEXT_POLICY_NONE = "台词只出声不出字幕；画面文字（牌匾、书信）按画面描述直接生成，逐字一致。"


def compile_text_policy(shot: Shot) -> str:
    required = getattr(shot, "required_text", None)
    if required is not None and (getattr(required, "exact_text", None) or "").strip():
        exact = required.exact_text.strip()
        surface = (required.surface or "画面指定表面").strip()
        strategy = required_text_strategy(shot)
        if strategy == "audio_only":
            return (
                f"只通过对白/画外音交付「{exact}」的信息；{surface}上不出现可读文字。"
                "画面中禁止字幕、标志、水印或乱码。"
            )
        if strategy == "deterministic_insert":
            return (
                f"只生成无字、干净的「{surface}」与人物表演；不得尝试拼写「{exact}」。"
                "精确中文由服务端确定性插入镜头交付，原始视频禁止任何可读字。"
            )
        if strategy == "none":
            return "画面不出现任何文字、字幕、标志或水印。"
        style = (required.style or "清晰可读").strip()
        start = getattr(required, "appear_start_s", 0.0) or 0.0
        until = getattr(required, "stable_until_s", None)
        until_s = f"{until}s" if until is not None else "镜头结束"
        return (
            f"仅在{surface}上于 {start}s 起稳定显示指定文字「{exact}」，保持到 {until_s}；"
            f"文字样式：{style}。除此之外画面无其它文字，台词只出声不出字幕。"
        )
    return _TEXT_POLICY_NONE


def text_negative(shot: Shot, strategy: str) -> str:
    """负面清单里的文字条目：只禁字幕类叠加；画面文字按策略处理。"""
    if strategy == "embedded_prop":
        return f"除「{(shot.required_text.exact_text or '').strip()}」外不要出现字幕或其它文字"
    if strategy == "none":
        return "不要生成字幕、名条、标题条、乱码或水印"
    return "不要生成字幕、乱码、可读道具字样或水印"
