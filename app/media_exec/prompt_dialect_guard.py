"""分镜台 2.x 段提示词方言守卫：入队前核对「段落持久化的方言」与「本次要投的
供应商方言」是否一致。

背景：分镜台 2.x 段的 ``prompt_text`` 由分镜模型按「本集绑定视频模型」的方言
写成（Seedance 自由中文散文 / MiniMax H3 结构化英文字段，见
``app.production.storyboard_dialects``），两种语法互不兼容。旧的
``[VIDEO_MODEL_BINDING_MISMATCH]`` 守卫（``app.media_exec.enqueue_context``）
只比较 ``episodes.target_video_model`` 与当前生效供应商是否同族——这是**状态
字段**，不是**产物信号**：本集切换过供应商但分镜台没有重新生成过分镜时，两个
状态字段可以完全一致，而段落里持久化的 ``prompt_text`` 仍是旧方言写的。旧方言
文本原样发给新供应商不会报错，新供应商会静默按自由文本理解，镜头数/三字段
结构/口型合同全部失效，还要白白花掉视频额度。

这里改成直接比较「段落持久化的方言字面值」（``storyboard_pack_segment.
target_model``，与 ``StoryboardPack.target_model`` 同一个值，见
``app.production.storyboard_pack._generate_all_segment_prompts``/
``generate_storyboard_pack``）与「本次目标供应商解析出的方言字面值」，判据落在
产物本身（CLAUDE.md「Gates and Criteria」：挂产物信号，不挂状态字段）。

老数据缺 ``target_model`` 字段时按什么处理：不能默认按 Seedance 处理。commit
18a0416f（分镜台 2.0.0）在同一个提交里first引入了
``segment_record["target_model"] = pack.target_model`` 这行赋值**与**
``MINIMAX_H3_DIALECT_INSTRUCTIONS``（MiniMax H3 方言指令本身）——也就是说，
从分镜台 2.x 段第一次存在的那一刻起，target_model 字段的记录与 H3 方言选项
就同时可用，不存在「记录方言之前只可能产出 Seedance」的历史窗口可以用来推定
缺字段=Seedance。因此缺字段一律 fail closed，与写法不兼容同样处理：拦下并
指路「重新生成本集分镜」，不猜一个可能错的默认值放行。
"""
from __future__ import annotations

import json
from typing import Any

_REGENERATE_HINT = "请在分镜台重新生成本集分镜"


def storyboard_pack_segment_from_row(shot_row: Any) -> dict[str, Any] | None:
    """从镜头行的 ``shot_contract_json`` 里取分镜台 2.x 段；非 2.x 镜头返回 None。"""
    raw = shot_row["shot_contract_json"] if "shot_contract_json" in shot_row.keys() else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    segment = data.get("storyboard_pack_segment") if isinstance(data, dict) else None
    return dict(segment) if segment else None


def dialect_mismatch_error(shot_row: Any, target_video_provider: str) -> str | None:
    """分镜台 2.x 段的持久化方言与目标供应商方言不一致（或方言字段缺失）时，
    返回中文错误文案；一致、或本就不是 2.x 段（走 compile_prompt 现编）时返回
    None。"""
    from app.production.storyboard_dialects import dialect_literal_for_target_video_model

    segment = storyboard_pack_segment_from_row(shot_row)
    if segment is None:
        return None
    stored = segment.get("target_model") or None
    expected = dialect_literal_for_target_video_model(target_video_provider)
    if stored == expected:
        return None
    if stored is None:
        return (
            "[VIDEO_PROMPT_DIALECT_MISMATCH] 本段没有记录提示词写作方言（老数据或异常"
            f"写入），无法确认是否与当前要投给的 {expected} 方言兼容；{_REGENERATE_HINT}后再试"
        )
    return (
        f"[VIDEO_PROMPT_DIALECT_MISMATCH] 本段提示词是按 {stored} 方言写的，当前要投给 "
        f"{expected}，两者提示词语法不兼容；{_REGENERATE_HINT}（改用 {expected} 方言），"
        f"或把生效视频模型切回 {stored} 对应的供应商"
    )
