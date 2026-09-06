"""新卡写入前的并发复核：快照之后人物谱多了称谓，就先归并再追加（2026-09-06 并发映射台建重卡）。

``ensure_character_card`` 的建卡裁决（模型调用）是在一份人物谱快照上做的；30 集映射台并发时，
别的集可能在这期间刚建了同一个人的卡（换个称呼）。原来 ``_bible_lock`` 里只按名字/别名逐字
复查，同一人不同称呼照样各建各的。这里在写锁内比对「快照时已知的称谓」与「现在的称谓」：
有新称谓才拿卡名再过一次 ``resolve_card_merge_target``（候选集含同姓氏键的结构候选），判同一
人则登记别名、不追加；没有新称谓（或判不是）就照常追加。模型调用只在真的发生竞态时才发生。
"""
from __future__ import annotations

import json
from collections.abc import Callable

from app import hiagent
from app.errors import log_error
from app.schemas import Bible

from .card_merge import apply_card_merge_alias, resolve_card_merge_target
from .card_owner import bible_known_labels, resolve_card_owner
from .portrait_io import _append_character_to_bible


async def append_character_or_merge(
    conn, project_id: str, card: dict, *, snapshot_labels: set[str],
    write_guard: Callable[[], None] | None = None,
) -> bool:
    """调用方须持有 ``_bible_lock``。返回 True=已追加；False=没追加（已归并成既有卡的别名，
    或写入失败）——调用方按既有的 owner 复查出口区分这两种情况。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    bible = Bible.model_validate(json.loads(row["bible_json"]))
    name = str(card.get("name") or "").strip()
    if name and resolve_card_owner(bible, name)[0] != "none":
        return False  # 另一路已把这个称呼落成卡名/别名（真名那一路带 identity_source_labels 登记的）
    if name and (bible_known_labels(bible) - set(snapshot_labels)):
        try:
            merged = await resolve_card_merge_target(conn, project_id, name, bible)
        except hiagent.ProviderError as exc:
            # 供应商故障不是「不是同一人」的证据；归并判断的合同是 fail-open 到建卡，记账后照常追加。
            log_error(exc, action="card_commit.merge_probe", context={"project_id": project_id, "name": name})
            merged = None
        if write_guard:
            write_guard()
        if merged is not None:
            apply_card_merge_alias(conn, project_id, merged[0], merged[1])
            return False
    return _append_character_to_bible(conn, project_id, card)
