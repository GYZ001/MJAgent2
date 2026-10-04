"""道具卡复核「owner 归属证据」的第二层核验：owner 卡必须与本卡在同一个
分镜段里共同出现过，不是"本项目存在一张同品类的卡"就足够（2026-10-03-v3
新增，审查发现——真实案例：《顾念长安》"热牛奶"外观被以 owner="白色陶瓷杯"
删除，但"白色陶瓷杯"是另一场戏咖啡馆那只竖纹马克杯，与"热牛奶"的杯子不是
同一件东西，仅凭"同品类卡存在"就删会让"热牛奶"杯子自己的外观信息丢失）。

判据从数据推导（CLAUDE.md「禁止黑白名单修复」）：两张卡的卡名/别名是否在
本项目同一个分镜段的 ``resources.props[]`` 里同时被列出——按
``app.props.card_pending_scan`` 同一口径解析 ``shots.shot_contract_json`` 的
``storyboard_pack_segment``，但不复用那个模块的私有解析函数（本模块按
CLAUDE.md「与 app.video_modes.scene_state_views 同一类小解析器各自持有一份」
的既有先例自成一份，且 card_pending_scan.py 不在本次改动范围内）。

数据源优先级（2026-10-03 B 沙箱对 7 个真实项目实测：全部 100% 的 shots 都带
``storyboard_pack_segment`` 且 ``resources.props`` 非空结构，见派单核实记录）：
优先用分镜数据；只有当整个项目连一条分镜段数据都没有（分镜台还没跑过）时，
才退到映射台 ``episode_prep_pack`` 产物的 ``asset_manifest.props[].
segment_indexes``（原文段号，口径与分镜段号不是同一种"段"，两者不会在同一次
判定里混用——调用方按项目整体切换数据源，不是逐条回退）。

两个数据源产出同一种形状：``label -> frozenset[(episode_id, 段号)]``——"段号"
在分镜数据里是 ``storyboard_pack_segment.segment_no``，在映射台数据里是原文
``segment_index``，调用方（``app.props.card_audit.compute_prop_card_audit``）
只需要这份字典，不关心具体来自哪个数据源。

已知局限（如实记录，不回避）：分镜段的 ``resources.props`` 只列这一段里需要
出图/可见的道具，不是这段原文里叙事上"涉及"的全部物件——真实案例：《顾念长安》
"浅灰色卫衣"胸前的压痕是"旧星盘"贴身佩戴、藏在卫衣下造成的，星盘本身在那几段
从未作为可见道具被列出（它被卫衣盖住，不需要单独出图），按这份数据源判定两者
"未共现"。2026-10-03-v3 这条判定会从"自动删除"降级为"存疑"而不是继续自动
删除——这是当时为了堵住"热牛奶/白色陶瓷杯"误删而接受的代价：宁可让这一条
头部案例从删除退回存疑交人工确认，也不要让"同品类卡存在"这种过松的判据继续
误删不相关的卡（CLAUDE.md「不要给以后的生成埋雷」，宁可少删保留原状）。

owner 归属证据的第二条路径（``owner_evidence_in_clause_text``，2026-10-04-v4
新增，B 沙箱第 4 轮真实模型实测发现）：共现数据源的上述局限对"浅灰色卫衣/
旧星盘"这条头部案例一直没有真正解决——星盘结构上永远不会出现在分镜段的
可见道具清单里，存疑会一直存在、永远等不到人工之外的方式转正。这里补一条
数据推导的替代证据：模型把这条外观子句判定为"别的物件"时给出的 owner，如果
那张卡的卡名或任一别名**逐字出现在被判删的这条子句原文本身里**——子句自己
已经把痕迹的来源写出来了（"胸前正中区域有长期放置旧星盘形成的浅淡压痕"这条
子句本身就含"旧星盘"四个字）——视为归属证据成立，与分镜段共现证据二选一
满足即可。这条路径不依赖分镜/映射数据，纯粹是"这条子句的原文里是否已经
点名了那张卡"，对"被遮住、不出图但仍被原文提及"的物件天然适用。对照组
（真实案例"热牛奶"）：子句"容器为纯白色无印花直身陶瓷马克杯"本身并不包含
"白色陶瓷杯"四个字连续出现（夹着"无印花直身"），这条新路径对它不成立，
仍然正确地转存疑，不会把"同品类卡存在"这种过松判据借道文字匹配复活。
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from app.schemas import Prop

_SegmentKey = tuple[str, int]


def _storyboard_segment_props(raw_contract: Any) -> tuple[int | None, list[str]]:
    """单条 ``shots.shot_contract_json`` 解出 ``(segment_no, 本段道具 label 列表)``；
    解析失败/没有分镜段统一返回 ``(None, [])``，不中止扫描。"""
    if not raw_contract:
        return None, []
    try:
        data = json.loads(raw_contract) if isinstance(raw_contract, str) else dict(raw_contract)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, []
    segment = data.get("storyboard_pack_segment")
    if not isinstance(segment, dict):
        return None, []
    resources = segment.get("resources") or {}
    labels = [
        str(entry.get("label") or "").strip()
        for entry in (resources.get("props") or []) if isinstance(entry, dict)
    ]
    return segment.get("segment_no"), [label for label in labels if label]


def storyboard_label_segment_keys(conn: Any, project_id: str) -> dict[str, frozenset[_SegmentKey]]:
    """分镜数据源：扫描本项目全部集的全部分镜段，返回 label -> 它出现过的
    ``(episode_id, segment_no)`` 集合。"""
    rows = conn.execute(
        "SELECT s.episode_id, s.shot_contract_json FROM shots s "
        "JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?",
        (project_id,),
    ).fetchall()
    out: dict[str, set[_SegmentKey]] = {}
    for row in rows:
        segment_no, labels = _storyboard_segment_props(row["shot_contract_json"])
        if segment_no is None:
            continue
        key: _SegmentKey = (str(row["episode_id"]), int(segment_no))
        for label in labels:
            out.setdefault(label, set()).add(key)
    return {label: frozenset(keys) for label, keys in out.items()}


def asset_manifest_label_segment_keys(conn: Any, project_id: str) -> dict[str, frozenset[_SegmentKey]]:
    """映射台退路数据源：只在整项目没有任何分镜段数据时使用（见模块
    docstring「数据源优先级」）。按每集最近一轮已批准的 ``episode_prep_pack``
    产物的 ``asset_manifest.props[].segment_indexes``（原文段号）聚合。"""
    episodes = conn.execute("SELECT id FROM episodes WHERE project_id=?", (project_id,)).fetchall()
    out: dict[str, set[_SegmentKey]] = {}
    for ep in episodes:
        episode_id = str(ep["id"])
        art = conn.execute(
            "SELECT content_json FROM artifacts WHERE type='episode_prep_pack' AND scope_id=? "
            "AND status IN ('approved','validated') ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        if art is None or not art["content_json"]:
            continue
        try:
            content = json.loads(art["content_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for entry in (content.get("asset_manifest") or {}).get("props") or []:
            label = str(entry.get("label") or "").strip()
            if not label:
                continue
            for seg_index in entry.get("segment_indexes") or []:
                try:
                    out.setdefault(label, set()).add((episode_id, int(seg_index)))
                except (TypeError, ValueError):
                    continue
    return {label: frozenset(keys) for label, keys in out.items()}


def label_segment_keys_for_project(conn: Any, project_id: str) -> dict[str, frozenset[_SegmentKey]]:
    """两个数据源按「项目整体是否有分镜数据」二选一（不是逐条回退，见模块
    docstring）。"""
    storyboard = storyboard_label_segment_keys(conn, project_id)
    if storyboard:
        return storyboard
    return asset_manifest_label_segment_keys(conn, project_id)


def _find_prop_by_identifier(identifier: str, all_props: Sequence[Prop]) -> Prop | None:
    for prop in all_props:
        if prop.name == identifier or identifier in prop.aliases:
            return prop
    return None


def _segment_keys_for_identifiers(
    label_segments: dict[str, frozenset[_SegmentKey]], identifiers: Sequence[str],
) -> frozenset[_SegmentKey]:
    keys: set[_SegmentKey] = set()
    for identifier in identifiers:
        keys |= label_segments.get(identifier, frozenset())
    return frozenset(keys)


def cooccurring_owners_for_prop(
    label_segments: dict[str, frozenset[_SegmentKey]], prop: Prop,
    other_identifiers: frozenset[str], all_props: Sequence[Prop],
) -> frozenset[str]:
    """``other_identifiers``（本项目除本卡之外全部卡名+别名，供模型 owner
    字段逐字取用的同一个集合）里，哪些确实在至少一个分镜段/原文段里与本卡
    同时出现过——数据源为空（项目还没有任何分镜/映射数据）时对全部候选都
    返回「未共现」（``own_keys`` 为空集合，交集必然为空），这是故意的 fail
    closed：没有数据不等于不用检查，见模块 docstring 的「已知局限」一节同一
    精神（CLAUDE.md「空集合不等于无需检查」）。"""
    own_keys = _segment_keys_for_identifiers(label_segments, (prop.name, *prop.aliases))
    if not own_keys:
        return frozenset()
    out: set[str] = set()
    for owner in other_identifiers:
        owner_prop = _find_prop_by_identifier(owner, all_props)
        candidates = (owner_prop.name, *owner_prop.aliases) if owner_prop else (owner,)
        if own_keys & _segment_keys_for_identifiers(label_segments, candidates):
            out.add(owner)
    return frozenset(out)


def owner_evidence_in_clause_text(owner: str, clause_text: str, all_props: Sequence[Prop]) -> bool:
    """owner 归属证据的第二条路径（数据推导，与 ``cooccurring_owners_for_
    prop`` 二选一满足即可，见模块 docstring）：把 ``owner``（模型给出、已经
    核验过落在本项目某张卡的名字/别名）解析回那张卡，检查它的**卡名**是否
    逐字出现在 ``clause_text``（被判定为"别的物件"的那条子句原文本身）里——
    子句自己用卡名点出了来源（「……长期放置旧星盘形成的浅淡压痕」）。

    只认卡名、不认别名：别名正是本复核要清理的对象之一，泛称别名（「马克杯」）
    逐字出现在不相干的子句里是常态——2026-10-04 B 沙箱实测，「热牛奶」的
    「容器为纯白色无印花直身陶瓷马克杯」因「白色陶瓷杯」卡带着泛称别名
    「马克杯」被判成归属成立而删掉，那是顾屿家的另一只杯子。``owner`` 在
    ``all_props`` 里找不到对应卡时（理论上不会发生，调用方已核验 ``owner``
    落在 ``other_identifiers`` 里）证据不成立。"""
    owner_prop = _find_prop_by_identifier(owner, all_props)
    return bool(owner_prop and owner_prop.name and owner_prop.name in clause_text)
