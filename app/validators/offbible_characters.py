"""分镜台角色圣经外人物归一化：规范名匹配、镜头内人物提及改写、功能身份剥离。
"""
from __future__ import annotations

import re

from app.character_policy import (
    is_collective_role,
    is_functional_extra,
)
from app.schemas import (
    Bible,
    Shot,
    Storyboard,
)
from app.spoken_contract import synchronize_spoken_contract

def _canonical_bible_name(name: str, bible_names: set[str]) -> str | None:
    """把疑似别名/简称/错字的角色名【唯一】对应到圣经正名；无唯一命中返回 None（按路人剥离）。

    只认包含关系：圣经名是该名子串（"甲一少爷"→"甲一"）或该名（≥2字）是圣经名子串（"甲二"→"甲二儿"）。
    命中多于一个圣经名（如"甲"同时命中甲一/甲三）视为不可判定，返回 None——宁可剥离也不错配。"""
    name = (name or "").strip()
    if not name:
        return None
    hits = {b for b in bible_names if b in name or (len(name) >= 2 and name in b)}
    return next(iter(hits)) if len(hits) == 1 else None


_CHARACTER_TEXT_FIELDS = (
    "action_desc", "first_frame_desc", "last_frame_desc", "narration",
    "state_in", "primary_action", "state_out", "observed_state_out",
    "purpose", "emotion_beat", "spatial_anchor",
)
_CHARACTER_REFERENCE_PREFIXES = frozenset({"character_identity", "collective_group"})


def _dedupe_names(values: list[str]) -> list[str]:
    return list(dict.fromkeys(name for name in values if name))


def _replace_character_mention(text: str, old: str, new: str) -> str:
    """在不把正名撑长的前提下替换别名。

    例如「甲二」→「甲二儿」时，已有「甲二儿」不能变成「甲二儿儿」，
    但单独的「甲二」仍应同步，否则会留下新的合同分叉。
    """
    if not text or old == new:
        return text
    if new.startswith(old):
        suffix = new[len(old):]
        if suffix:
            return re.sub(re.escape(old) + rf"(?!{re.escape(suffix)})", new, text)
    return text.replace(old, new)


def _rename_shot_character(shot: Shot, old: str, new: str) -> None:
    """把别名在整个镜头合同中原子性改为圣经正名。"""
    for field in _CHARACTER_TEXT_FIELDS:
        value = getattr(shot, field, None)
        if value:
            setattr(shot, field, _replace_character_mention(value, old, new))
    for field in ("characters", "characters_visible", "audio_cast"):
        values = list(getattr(shot, field, None) or [])
        setattr(shot, field, _dedupe_names([new if name == old else name for name in values]))
    shot.do_not_repeat = [
        _replace_character_mention(value, old, new)
        for value in (shot.do_not_repeat or [])
    ]
    shot.reference_roles = [
        f"{prefix}:{new}" if separator and prefix in _CHARACTER_REFERENCE_PREFIXES and name == old else role
        for role in (shot.reference_roles or [])
        for prefix, separator, name in [str(role or "").partition(":")]
    ]
    for dialogue in shot.dialogues:
        if dialogue.speaker == old:
            dialogue.speaker = new
    for item in shot.audio_timeline:
        if (item.speaker_id or "").strip() == old:
            item.speaker_id = new


def _strip_shot_character_contract(shot: Shot, name: str) -> list[str]:
    """原子性剥离非法角色，并保留其台词文本作为修复证据。

    不能只删 dialogues：audio_timeline 优先级更高，会把旧说话人再派生回
    characters_visible；reference_roles 则会使下游继续查找不存在的角色参考图。
    """
    moved: list[str] = []
    for dialogue in shot.dialogues:
        if dialogue.speaker == name and (dialogue.line or "").strip():
            moved.append(dialogue.line.strip())
    for item in shot.audio_timeline:
        if (item.speaker_id or "").strip() == name and (item.text or "").strip():
            moved.append(item.text.strip())
    shot.characters = [value for value in shot.characters if value != name]
    shot.characters_visible = [value for value in shot.characters_visible if value != name]
    shot.audio_cast = [value for value in shot.audio_cast if value != name]
    shot.dialogues = [dialogue for dialogue in shot.dialogues if dialogue.speaker != name]
    shot.audio_timeline = [
        item for item in shot.audio_timeline if (item.speaker_id or "").strip() != name
    ]
    shot.reference_roles = [
        role
        for role in (shot.reference_roles or [])
        if not (
            (parts := str(role or "").partition(":"))[1]
            and parts[0] in _CHARACTER_REFERENCE_PREFIXES
            and parts[2] == name
        )
    ]
    # dialogues 与 timeline 往往是同一条台词，去重后只留一份。
    moved = list(dict.fromkeys(moved))
    additions = [line for line in moved if line not in (shot.action_desc or "")]
    if additions:
        evidence = "；".join(f"待修复台词信息「{line}」" for line in additions)
        merged = (shot.action_desc or "").rstrip("。； ")
        shot.action_desc = f"{merged}；{evidence}。" if merged else f"{evidence}。"
    return moved


def normalize_offbible_characters(board: Storyboard, bible: Bible | None) -> list[dict]:
    """按角色圣经与功能性路人合同确定性规范镜头角色。

    根因：原文里的测验员/围观者甲等次要在场人物会被模型写进 characters / dialogues.speaker，但它们不在
    角色圣经里 → validate_storyboard 报「角色圣经中不存在」→ 触发整轮修复（实测会与 covers 落实相互
    拉扯成多轮重试）。真正重要的新角色仍必须进入角色圣经；无姓名、无需跨集定妆的功能性路人可以按
    通用身份标签留在镜头中：
    - 能唯一对应到某圣经角色（别名/简称/错字）→ 规范成圣经正名（characters、speaker、画面文本一并替换）；
    - 功能性路人（测验员、路人甲等）→ 保留在 characters；若只作为 dialogue speaker 出现则补入 characters；
    - 其它圣经外名字 → 从可见、声轨、参考图等整个镜头合同原子性剥离，
      其台词文本暂存为 action_desc 修复证据；
    - characters_visible/audio_cast/audio_timeline/reference_roles 中的历史残留也按同一规则处理。
    就地修改 board，返回带分类依据的调整记录供监控与 Harness 留痕。"""
    bible_names = {c.name for c in bible.characters} if bible else set()
    changes: list[dict] = []
    for shot in board.shots:
        if not bible_names:
            continue
        stripped_names: set[str] = set()

        def _normalize_name(name: str) -> tuple[str | None, str]:
            value = (name or "").strip()
            if not value:
                return None, "empty"
            if value in bible_names:
                return value, "bible"
            canonical = _canonical_bible_name(value, bible_names)
            if canonical:
                return canonical, "alias"
            if is_functional_extra(value):
                return value, "functional_extra"
            if is_collective_role(value):
                return value, "collective"
            return None, "offbible"

        def _strip(name: str, source: str) -> None:
            if name in stripped_names:
                return
            moved = _strip_shot_character_contract(shot, name)
            stripped_names.add(name)
            changes.append({
                "shot_no": shot.shot_no,
                "stripped": name,
                "source": source,
                "moved_voice_lines": len(moved),
                "mutated": True,
            })

        kept: list[str] = []
        for name in list(shot.characters):
            normalized, kind = _normalize_name(name)
            if normalized is None:
                _strip(name, "characters")
            elif kind == "alias":
                _rename_shot_character(shot, name, normalized)
                kept.append(normalized)
                changes.append({
                    "shot_no": shot.shot_no,
                    "renamed": f"{name}→{normalized}",
                    "source": "characters",
                    "mutated": True,
                })
            else:
                kept.append(normalized)
            if kind == "functional_extra":
                changes.append({
                    "shot_no": shot.shot_no,
                    "allowed_functional_extra": normalized,
                    "source": "characters",
                    "mutated": False,
                })
            elif kind == "collective":
                changes.append({
                    "shot_no": shot.shot_no,
                    "allowed_collective": normalized,
                    "source": "characters",
                    "mutated": False,
                })
        shot.characters = _dedupe_names(kept)

        # 修复可见名单：它可以是 characters 的子集（例如单人对白特写），
        # 但绝不得引入 characters 之外的新身份。
        visible: list[str] = []
        for name in list(shot.characters_visible):
            normalized, kind = _normalize_name(name)
            if normalized is None:
                _strip(name, "characters_visible")
                continue
            if kind == "alias":
                _rename_shot_character(shot, name, normalized)
                changes.append({
                    "shot_no": shot.shot_no,
                    "renamed": f"{name}→{normalized}",
                    "source": "characters_visible",
                    "mutated": True,
                })
            if normalized not in shot.characters:
                shot.characters.append(normalized)
                changes.append({
                    "shot_no": shot.shot_no,
                    "added_from_visible": normalized,
                    "mutated": True,
                })
            visible.append(normalized)
        shot.characters = _dedupe_names(shot.characters)
        shot.characters_visible = _dedupe_names(visible)

        # 说话人可能只存在于 dialogues/timeline/audio_cast；最后统一扫一次，
        # 防止部分修复数据把旧角色从声轨反向注入可见名单。
        speaker_names = [
            *((dialogue.speaker or "").strip() for dialogue in shot.dialogues),
            *((item.speaker_id or "").strip() for item in shot.audio_timeline),
            *((name or "").strip() for name in shot.audio_cast),
        ]
        for name in dict.fromkeys(value for value in speaker_names if value):
            normalized, kind = _normalize_name(name)
            if normalized is None:
                _strip(name, "spoken_contract")
                continue
            if kind == "alias":
                _rename_shot_character(shot, name, normalized)
                changes.append({
                    "shot_no": shot.shot_no,
                    "renamed": f"{name}→{normalized}",
                    "source": "spoken_contract",
                    "mutated": True,
                })

        # 画内开口者必须同时进入 characters / characters_visible / audio_cast。
        # 旧链路只补 characters，使人工新增的台词在保存时被派生成
        # offscreen_voice，确认时又因可见合同不一致而被删除。
        visible_speakers: list[str] = []
        audible_speakers: list[str] = []
        for dialogue in list(shot.dialogues):
            speaker = (dialogue.speaker or "").strip()
            normalized, _kind = _normalize_name(speaker)
            if normalized is None:
                continue
            audible_speakers.append(normalized)
            if (getattr(dialogue, "delivery", "spoken_dialogue") or "spoken_dialogue") == "spoken_dialogue":
                visible_speakers.append(normalized)
        for item in shot.audio_timeline:
            speaker = (item.speaker_id or "").strip()
            normalized, _kind = _normalize_name(speaker)
            if normalized is None or item.type not in {"spoken_dialogue", "offscreen_voice"}:
                continue
            audible_speakers.append(normalized)
            if item.type == "spoken_dialogue":
                visible_speakers.append(normalized)

        roster_changed_for_dialogue = False
        if visible_speakers and not shot.characters_visible:
            shot.characters_visible = list(shot.characters)
        for speaker in dict.fromkeys(visible_speakers):
            if speaker not in shot.characters:
                shot.characters.append(speaker)
                roster_changed_for_dialogue = True
                changes.append({
                    "shot_no": shot.shot_no,
                    "allowed_functional_extra": speaker,
                    "source": "dialogue_speaker",
                    "mutated": True,
                })
            if speaker not in shot.characters_visible:
                shot.characters_visible.append(speaker)
                roster_changed_for_dialogue = True
                changes.append({
                    "shot_no": shot.shot_no,
                    "added_visible_speaker": speaker,
                    "source": "spoken_contract",
                    "mutated": True,
                })
        for speaker in dict.fromkeys(audible_speakers):
            if speaker not in shot.audio_cast:
                shot.audio_cast.append(speaker)
                changes.append({
                    "shot_no": shot.shot_no,
                    "added_audio_cast": speaker,
                    "source": "spoken_contract",
                    "mutated": True,
                })
        shot.characters = _dedupe_names(shot.characters)
        shot.characters_visible = _dedupe_names(shot.characters_visible)
        shot.audio_cast = _dedupe_names(shot.audio_cast)
        if roster_changed_for_dialogue and shot.dialogues:
            sync = synchronize_spoken_contract(shot, changed_fields={"dialogues"})
            if sync.changed:
                changes.append({
                    "shot_no": shot.shot_no,
                    "synchronized_spoken_contract": True,
                    "actions": sync.actions,
                    "mutated": True,
                })

        # 参考角色可能是唯一的残留来源；它不能越过可见/声轨校验。
        rebuilt_roles: list[str] = []
        for role in shot.reference_roles or []:
            prefix, separator, name = str(role or "").partition(":")
            if not separator or prefix not in _CHARACTER_REFERENCE_PREFIXES:
                rebuilt_roles.append(role)
                continue
            normalized, kind = _normalize_name(name)
            if normalized is None:
                changes.append({
                    "shot_no": shot.shot_no,
                    "stripped_reference_role": name,
                    "mutated": True,
                })
                continue
            rebuilt_roles.append(f"{prefix}:{normalized}")
            if kind == "alias":
                changes.append({
                    "shot_no": shot.shot_no,
                    "renamed": f"{name}→{normalized}",
                    "source": "reference_roles",
                    "mutated": True,
                })
        shot.reference_roles = list(dict.fromkeys(rebuilt_roles))
    return changes
