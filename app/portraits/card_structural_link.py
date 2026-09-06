"""建卡前归并的结构候选：同一姓氏键的既有卡（2026-09-06 「许师姐」/「许姓女子」两张卡）。

``card_merge.resolve_card_merge_target`` 的候选集原来只收「规范名/别名逐字出现在 label 卷宗里」
的卡。第 6 轮实测：第 1 集先按「许姓女子」建卡，第 5 集再遇「许师姐」，两个称呼从不逐字互含，
候选集为空、直接建了第二张卡，同一人两套定妆。汉语称谓里「姓 + 关系称谓」（许师姐）与
「姓 + 姓 + 描述」（许姓女子）都是语法形态，不是任何人物的名单：这里把它们分解成同一个
**姓氏键**，同键的既有卡进入候选，交给既有的选择题裁决 + 窗口共现机械核验——判据链一步
不少，只是候选集多了一条结构来源。方向仍 fail-open 到建新卡：两个同姓的不同人（上官修/
上官宋）过不了共现核验就照常各建各的。
"""
from __future__ import annotations

import re

from app.schemas import Bible
from app.source_excerpt import index_source_segments

from .card_owner import RELATIONAL_TITLE_SUFFIXES

_SURNAME_MARKER_RE = re.compile(r"^(\S)姓")
STRUCTURAL_DOSSIER_LIMIT = 8


def surname_key(label: str) -> str | None:
    """「许师姐」→「许」（关系称谓前只剩一个字），「许姓女子」→「许」（"X姓"标记）；其它形态 None。

    只认这两种形态：剩余 ≥2 字的（「王腾飞师兄」）是完整人名，归 ``strip_relational_title``。"""
    text = str(label or "").strip()
    marker = _SURNAME_MARKER_RE.match(text)
    if marker:
        return marker.group(1)
    for suffix in RELATIONAL_TITLE_SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) == 1:
            return text[0]
    return None


def structural_candidates(bible: Bible, label: str) -> list[str]:
    """人物谱里与 ``label`` 同姓氏键的卡名：卡名或别名自身能分解出同一姓氏键，或以该字开头
    的 ≥2 字称谓（汉语姓在前——「许青」对「许师姐」也是合法候选，选不选由裁决与共现核验定）。"""
    key = surname_key(label)
    names: list[str] = []
    for character in getattr(bible, "characters", None) or []:
        forms = [character.name, *(alias.text for alias in character.aliases)]
        if character.name in names or character.name == label:
            continue
        # 包含关系：「虎爷」⊂「自称虎爷的大汉」（第 13 轮两张卡）——一方逐字含另一方（≥2 字）就是候选
        contained = len(label) >= 2 and any(f and f != label and (label in f or (len(f) >= 2 and f in label)) for f in forms)
        if contained or (key and any(f and (surname_key(f) == key or (len(f) >= 2 and f[0] == key)) for f in forms)):
            names.append(character.name)
    return names


def with_structural_entries(
    dossier: list[dict], chapters_by_idx: dict[int, str], candidate_forms: list[str], label: str,
) -> list[dict]:
    """在 label 卷宗后追加「同章节里含候选称谓」的原文段，让模型看到候选一侧的描写
    （「穿着银袍的许姓女子」），并重新编号 entry_index。没有结构候选时原样返回。"""
    forms = [f for f in candidate_forms if f]
    if not forms:
        return dossier
    extra: list[dict] = []
    for chapter_idx in sorted(chapters_by_idx):
        for segment_index, segment in enumerate(index_source_segments(chapters_by_idx[chapter_idx]), start=1):
            if label in segment.text or not any(f in segment.text for f in forms):
                continue
            extra.append({"chapter_idx": chapter_idx, "segment_index": segment_index, "text": segment.text})
            if len(extra) >= STRUCTURAL_DOSSIER_LIMIT:
                break
        if len(extra) >= STRUCTURAL_DOSSIER_LIMIT:
            break
    merged = [*dossier, *extra]
    for entry_index, item in enumerate(merged, start=1):
        item["entry_index"] = entry_index
    return merged
