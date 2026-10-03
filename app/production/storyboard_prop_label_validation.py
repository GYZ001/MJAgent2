"""分镜段 ``resources.props[].label`` 必须逐字等于道具卡名/别名，不得带括号
说明（2026-10-02，《顾念长安》EP1 第 14/15 段真实故障，「修订本段」重生成后）。

分镜 ``resources.props`` 里出现了「浅蓝色碎花长裙（外套下摆露出的一截）」
「米白色针织开衫（外套领口露出的领口）」「深色毛衫（大衣领口露出的领边）」
这类 label——``app.production.storyboard_prop_visibility`` 的
``SEEDANCE_PROP_VISIBILITY_RULE``/``MINIMAX_H3_PROP_VISIBILITY_RULE`` 要求
"部分可见的衣物只写露出部分"，但没有说这句说明该写在哪个字段，模型把可见范围
缀进了 label 本身的括号里，而不是写进 ``description``。``app.video_modes.
prop_references.resolve_segment_prop_manifest_entries`` → ``app.props.store.
prop_reference_for_episode`` 按 label 逐字（经正名/别名归一）查道具卡，带括号
的 label 查不到卡，``ready=False``，这件衣物的参考图没有送给视频模型——第 15
段成片里温念的浅蓝碎花长裙因此被画成了另一条裤子。

修法：新增结构校验，判据是"label 是不是『已知道具名 + 紧跟括号说明』这个
形状"——不是黑白名单（不枚举衣物/道具词表），已知名字本身来自这一段真实拿到
的输入数据，见 ``_segment_known_prop_names``。命中就报错，交给调用方已有的
修复/重试机制（生成期走 ``model_gateway`` 语义重试，人工修订期走
``ValueError`` 拒绝保存），不在这里静默剥括号替模型"修好"——那会把一个本该
让模型自己纠正的错误悄悄掩盖成"看起来通过了"。

两条生产调用路径都接了本模块（读代码核实）：
- 逐段生成/「仅重新编写本段」重生成，共用
  ``storyboard_pack._generate_all_segment_prompts`` → ``storyboard_segment_
  chains._segment_validate`` → ``storyboard_identity_generation.
  generated_identity_errors``；
- 「修订本段」人工重生成，``app.domain.storyboard_ops.identity_workspace.
  prepare_identity_candidate``。

两处都已经持有同一份 ``payload``（``episode.screenplay_json``，映射时刻的
准备包快照）与本段 ``source_segment_indexes``，本模块只需要这两样，不碰
数据库、不碰世界书——与 ``app.production.storyboard_prop_appearance_lock.
known_prop_card_appearance_index`` 同一处置："模型看到的"与"代码核验用的"
是同一份数据，不是另开一条查询通道可能与模型看到的东西不同步。

不在本模块顶部 import ``app.production.storyboard_pack``：那条模块经
``storyboard_segment_chains``/``storyboard_identity_generation`` 反向依赖
到这里，会成环（见 ``app.production`` 整体 L4，互相不得倒置）。``_segment_
known_prop_names`` 因此自己按 ``segment_indexes`` 交集重新过滤一遍
``asset_manifest.props``，口径与 ``storyboard_pack._segment_relevant_
assets`` 对 props 的过滤逐字一致，字段名/数据全部单源自
``app.production.prep_pack.prop_manifest._prep_pack_build_prop_manifest``
的输出契约（``label``/``canonical_name``/``segment_indexes``），不是另造
一份可能漂移的口径。
"""
from __future__ import annotations

from typing import Any


def _segment_known_prop_names(payload: dict[str, Any], source_segment_indexes: list[int]) -> set[str]:
    """本段模型实际看到的道具清单里出现过的全部合法名字：``label``（这件道具
    这次提及时的原文写法，功能上等同于一个"这次登记的别名"）与
    ``canonical_name``（绑定到的道具卡规范名，没绑定卡时为空）——两者逐字取自
    ``payload["asset_manifest"]["props"]``，按 ``segment_indexes`` 与本段
    ``source_segment_indexes`` 是否相交筛选，口径同 ``storyboard_pack.
    _segment_relevant_assets`` 对 props 的过滤（不重复 import，见模块
    docstring）。

    ``source_segment_indexes`` 为空（旧数据/调用方未传）时不收窄，退化为全集
    已知道具——比"筛出空集合、进而把任何 label 都判成未知"更安全：空集合在
    这里的语义是"没有收窄依据"，不是"这一段合法名单本来就是空的"（CLAUDE.md
    「空集合不等于无需检查」的另一面，这里反过来是"无收窄依据不等于全部非法"）。
    """
    manifest = payload.get("asset_manifest") or {}
    wanted = set(source_segment_indexes or [])
    names: set[str] = set()
    for prop in manifest.get("props") or []:
        if wanted and not (wanted & set(prop.get("segment_indexes") or [])):
            continue
        for key in ("label", "canonical_name"):
            value = str(prop.get(key) or "").strip()
            if value:
                names.add(value)
    return names


def _label_wraps_known_name(label: str, known_names: set[str]) -> str | None:
    """label 是不是"已知道具名 + 紧跟括号说明"这个形状：返回被包住的那个已知名
    （报错文案用它指出模型该写哪个名字）；label 本身就是某个已知名（已在调用方
    精确匹配过）、或完全不以任何已知名开头，返回 None。多个已知名都是前缀时取
    最长的那个——短名恰好是长名前缀时（例如已知名同时有「手机」与「白色手机壳
    的智能手机」），应指出更精确匹配的那张卡，不是随便一个前缀命中的卡。"""
    candidates = [
        name for name in known_names
        if name and label.startswith(name) and len(label) > len(name) and label[len(name)] in "（("
    ]
    return max(candidates, key=len) if candidates else None


def prop_label_bracket_note_errors(
    props: list[dict[str, Any]], *, payload: dict[str, Any], source_segment_indexes: list[int],
) -> list[str]:
    """``resources.props[i].label`` 逐字核验：label 不是本段已知道具卡名/别名
    （精确匹配即合法，不报错——包括 label 恰好是另一张卡的完整名称、那张卡名
    本身带括号的情形），但以某个已知道具卡名/别名开头、紧接着是括号（全角
    「（」或半角「("）时判为错误——这种形状只可能是"卡名 + 可见范围说明"误写
    进了同一个字段，不可能是巧合（没有任何道具卡名天然以另一张卡名打头、还
    紧跟一个括号）。没有道具卡的通用名称、或与任何已知名都不沾边的名字一律
    放行，不臆测、不按衣物/道具词表黑白名单。
    """
    known_names = _segment_known_prop_names(payload, source_segment_indexes)
    errors: list[str] = []
    for index, prop in enumerate(props or []):
        label = str(prop.get("label") or "").strip()
        if not label or label in known_names:
            continue
        wrapped = _label_wraps_known_name(label, known_names)
        if wrapped:
            errors.append(
                f"props[{index}].label「{label}」应逐字写道具卡名「{wrapped}」，可见范围写进 description"
            )
    return errors


__all__ = ["prop_label_bracket_note_errors"]
