"""分镜台蒙太奇镜头形态校验（WS7，2026-09-02）。

背景与契约定义见 app.schemas.shot_montage 模块 docstring：``Shot.form ==
"montage"`` 标记「这一行原文是叙述者总结/回忆列举/跨年排比」，``Shot.beats``
承载段内最多 3 个独立拍点。这里只校验这个新形态自己的结构完整性，不重新做
台词在场/溯源判定（那是 storyboard_core.py 的 storyboard_pack_dialogue_errors
已经在管的事，两者正交）。

零回归判据：本模块每个函数第一行都是 ``if shot.form != "montage": return
[]``——``form`` 默认 "scene"，绝大多数既有分镜（含台词全部 offscreen_voice
的单场景内心独白）永远走这条早退，不会被本模块判错，也不会被本模块改判。
"""
from __future__ import annotations

from app.schemas import Shot, Storyboard, is_narrator_label

MONTAGE_MIN_BEATS = 1
MONTAGE_MAX_BEATS = 3


def storyboard_montage_shot_errors(
    shot: Shot,
    *,
    known_scene_names: set[str],
) -> list[str]:
    """校验一个 montage 镜头自身的结构：不校验 form!="montage" 的行。

    ``known_scene_names`` 必须由调用方显式给出（本集映射包/已用场景的规范名
    集合），不设默认值——空集合不代表「跳过检查」，而是「这一集目前没有任何
    合法场景名」，任何非空 scene_name 在这种输入下都应该被判定越界，不能因为
    集合恰好是空的就静默放行。
    """
    if shot.form != "montage":
        return []
    tag = f"shot_no={shot.shot_no}"
    errors: list[str] = []
    if not (shot.narration or "").strip():
        errors.append(
            f"[STORYBOARD_PACK_MONTAGE_NARRATION_EMPTY] {tag} form=montage 但 narration 为空"
        )
    beat_count = len(shot.beats)
    if not (MONTAGE_MIN_BEATS <= beat_count <= MONTAGE_MAX_BEATS):
        errors.append(
            f"[STORYBOARD_PACK_MONTAGE_BEAT_COUNT] {tag} beats 数={beat_count}，"
            f"必须在 {MONTAGE_MIN_BEATS}-{MONTAGE_MAX_BEATS} 之间"
        )
    for index, beat in enumerate(shot.beats):
        scene_name = (beat.scene_name or "").strip()
        if scene_name and scene_name not in known_scene_names:
            errors.append(
                f"[STORYBOARD_PACK_MONTAGE_BEAT_SCENE_UNKNOWN] {tag} beats[{index}] "
                f"scene_name「{scene_name}」不在本集映射包场景清单内"
            )
        if not (beat.visual or "").strip():
            errors.append(
                f"[STORYBOARD_PACK_MONTAGE_BEAT_VISUAL_EMPTY] {tag} beats[{index}] visual 为空"
            )
    return errors


def validate_storyboard_pack_montage(
    board: Storyboard,
    *,
    known_scene_names: set[str],
) -> list[str]:
    errors: list[str] = []
    for shot in board.shots:
        errors.extend(
            storyboard_montage_shot_errors(shot, known_scene_names=known_scene_names)
        )
    return errors


def storyboard_narrator_label_errors(shot: Shot) -> list[str]:
    """「旁白」是画外叙述声音，不是画面里的人：它绝不能出现在 characters /
    characters_visible 里（那两个字段回答「画面里/这一镜相关的是谁」）。

    与本模块其余函数不同，这条规则跟 form 无关——旁白误入这两个字段是数据
    卫生问题，"scene" 形态的行同样可能踩中（实测：跑不快的孩子 第 2 集第 1
    镜的 characters 里就有「旁白」）。
    """
    tag = f"shot_no={shot.shot_no}"
    errors: list[str] = []
    if any(is_narrator_label(name) for name in shot.characters):
        errors.append(
            f"[STORYBOARD_PACK_NARRATOR_IN_CHARACTERS] {tag} characters 中出现「旁白」——"
            "旁白是画外叙述声音，不是画面里的人，不得列入 characters"
        )
    if any(is_narrator_label(name) for name in shot.characters_visible):
        errors.append(
            f"[STORYBOARD_PACK_NARRATOR_IN_CHARACTERS_VISIBLE] {tag} characters_visible "
            "中出现「旁白」——同上，旁白不进画面在场清单"
        )
    return errors
