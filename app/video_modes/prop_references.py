"""道具参考图接入分镜参考图池（P2：道具形态漂移修复的消费侧，视频侧一半）。

与 ``app.production.storyboard_prop_assets``（分镜阶段二素材清单那一半）配套：
那边把道具外观/参考图写进 ``asset_manifest.props``/segment ``resources.props``；
本模块把已经落到 ``app.multiview`` reference manifest 里的道具条目
（``manifest["props"]``，见 ``app.multiview._storyboard_pack_asset_dependencies``）
展开成 ``app.video_modes`` 参考图装配管线认识的锚点/候选形状，供
``app.video_modes.reference_assemble`` 挑进最终参考图池。

``app.props``（WS-P1 并行落地的世界书物件库）没到位前，``_prop_reference_
lookup`` 惰性 import + ImportError 兜底返回 None——道具没有参考图时按"没有
可用参考图"处理，不阻断分镜/视频生成。测试直接 monkeypatch 本模块的
``_prop_reference_lookup`` 验证装配逻辑。

2026-10-01（``resources_order``，第 1 集第 19/20 段真实成片复查）：
``app.multiview.ref_pack_priority`` 超过参考图张数上限（``max_reference_
images()``，9 张）时按道具分数+id 取舍，分数对库资产道具恒为 0（没有 QA
分数），实际落到 ``ref.id``——一串与本段画面无关的随机串，哪件道具被舍弃
因此是随机的（真实故障：第 20 段 resources.props 列了 8 项，加上人物/场景
超过 9 张，行李箱参考图被随机丢弃，成片行李箱颜色与卡片不符）。修法：本段
``resources.props`` 的声明顺序就是模型给出的重要性排序（见
``app.production.storyboard_prop_visibility`` 新增的排序正面陈述），
``resolve_segment_prop_manifest_entries`` 原样保留这个顺序、按下标打上
``resources_order``，``prop_library_anchors`` 透传给锚点字典，一路经
``app.video_modes.asset_lookup._asset_from_path``/``ReferenceImageAsset.
resources_order`` 字段带到最终参考图 dict，供 ``ref_pack_priority`` 的
道具档把它当第一级排序键——超限时优先保留声明顺序靠前的道具，不再看
与排序无关的随机 id。没有这个字段的旧数据（``resources_order is None``）
排在有序号的道具之后，组内仍按原有的 ``-quality, id`` 排序，行为不变。
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TRAILING_ANNOTATION = re.compile(r"[（(][^（()）]*[）)]$")

# 续接服装文本反推道具卡（2026-10-05，《顾念长安》proj_ca86b15ab7d7 第1集真实
# 回归）：21/35 段里 continuity_memo.characters[].wardrobe 写了具体服装外观，
# 但模型没有把对应的服装道具卡 label 写进 resources.props，装配时这些服装
# 的参考图根本没进参考图池，视频模型只能自由发挥换装。wardrobe 是模型早已
# 写出的"提名"，这里只做"代码核验它是否逐字点名了一张世界书里真实存在的
# 道具卡"——不新增任何模型调用/字段，不改 schema。
#
# 歧义判据按"文字位置是否被争抢"，不按"一句话命中几张卡"：一句续接服装经常
# 同时点名好几件衣物（外套+长裙），这是正常情况要全部采纳，不是歧义；真正
# 该拦的是两张不同卡靠文本里同一段字符命中（例如"顾屿外套"与"温念厚外套"都
# 只靠同一个"外套"二字命中），这种"抢位置"才不猜，见 infer_wardrobe_prop_
# labels 与 _matched_text_positions。
MIN_WARDROBE_PROP_COVERAGE = 0.8
MIN_WARDROBE_PROP_IDENTIFIER_LEN = 4


def _strip_trailing_annotation(label: str) -> str:
    """只剥末尾括号注释（例："浅蓝色碎花长裙（裙摆）"→"浅蓝色碎花长裙"）；
    剥完变成空字符串时原样返回整条 label（没有可用的"剥干净"结果）。"""
    stripped = _TRAILING_ANNOTATION.sub("", label).strip()
    return stripped if stripped else label


def _identity_id_core(identity_id: str) -> str:
    """identity_id 去掉 ``bible:``/``entity:`` 前缀后的主体——与
    ``app.production.storyboard_continuity_memo._identity_id_core``、
    ``storyboard_reference_repair._entry_names``、``storyboard_dialects.
    reference_mention_errors`` 里 ``identity_id.split(":", 1)`` 同一种归一。
    本仓库对这一行的既有做法是各处各自复制，不跨模块 import 私有函数。"""
    return identity_id.split(":", 1)[-1].strip() if identity_id else identity_id


def _char_coverage(identifier: str, text: str) -> float:
    """identifier 在 text 里的多段连续匹配总长度 ÷ len(identifier)；只计入
    长度>=2 的匹配块（排除单字偶然命中）。``difflib.SequenceMatcher`` 保证
    同一 identifier 内的匹配块互不重叠，结果恒在 [0, 1]。"""
    if not identifier:
        return 0.0
    matcher = difflib.SequenceMatcher(a=identifier, b=text, autojunk=False)
    covered = sum(block.size for block in matcher.get_matching_blocks() if block.size >= 2)
    return covered / len(identifier)


def _matched_text_positions(identifier: str, text: str) -> frozenset[int]:
    """identifier 在 text 里命中的字符下标集合（只计入长度>=2 的匹配块，口径
    与 ``_char_coverage`` 一致），供 ``infer_wardrobe_prop_labels`` 判定两张
    卡是否在"抢同一段文字"。"""
    if not identifier:
        return frozenset()
    matcher = difflib.SequenceMatcher(a=identifier, b=text, autojunk=False)
    positions: set[int] = set()
    for block in matcher.get_matching_blocks():
        if block.size >= 2:
            positions.update(range(block.b, block.b + block.size))
    return frozenset(positions)


def _visible_wardrobe_texts(resources: dict[str, Any], continuity_memo: dict[str, Any]) -> list[str]:
    """本段出镜人物（非 voice_only、非"旁白"——与
    ``app.multiview._storyboard_pack_asset_dependencies`` 对人物可见性的既有
    判据同一口径）各自的 ``continuity_memo.wardrobe`` 原文，空文本不收。

    identity_id 比对按去前缀后的主体值：模型在 continuity_memo.characters 里
    偶尔省略 resources.characters 已解析出的 bible:/entity: 前缀（真实回归，
    见 ``app.production.storyboard_continuity_memo.
    continuity_memo_character_advisories`` docstring），精确字符串比对会把这个
    本来在场的可见人物误判成不在场，静默漏发服装卡——与本函数要修的缺陷同一
    根因，不能再犯一遍。"""
    visible_cores = {
        _identity_id_core(str(c.get("identity_id") or ""))
        for c in (resources.get("characters") or [])
        if c.get("visibility") != "voice_only" and str(c.get("identity_id") or "") != "旁白"
    }
    return [
        str(c.get("wardrobe") or "").strip()
        for c in (continuity_memo.get("characters") or [])
        if _identity_id_core(str(c.get("identity_id") or "")) in visible_cores
        and str(c.get("wardrobe") or "").strip()
    ]


def _declared_prop_names(prop_entries: list[dict[str, Any]], bible_props: list[Any]) -> set[str]:
    """``resources.props`` 已声明的 label（剥过括号的变体也纳入）如果逐字
    等于某张世界书道具卡的 name 或某个 alias，这张卡的 name 进排除集——
    避免对模型已经显式声明过的卡重复推导。"""
    declared_labels: set[str] = set()
    for entry in prop_entries or []:
        label = str(entry.get("label") or "").strip()
        if label:
            declared_labels.add(label)
            declared_labels.add(_strip_trailing_annotation(label))
    excluded: set[str] = set()
    for prop in bible_props or []:
        identifiers = {prop.name, *prop.aliases}
        if identifiers & declared_labels:
            excluded.add(prop.name)
    return excluded


def _best_match_for_prop(prop: Any, text: str) -> tuple[float, frozenset[int]]:
    """prop 的 name/alias 里取覆盖率最高的识别串，返回其覆盖率与命中位置；
    短于 ``MIN_WARDROBE_PROP_IDENTIFIER_LEN`` 的识别串不参与比较。"""
    best_ratio: float = 0.0
    best_positions: frozenset[int] = frozenset()
    for ident in [prop.name, *prop.aliases]:
        if len(ident) < MIN_WARDROBE_PROP_IDENTIFIER_LEN:
            continue
        ratio = _char_coverage(ident, text)
        if ratio > best_ratio:
            best_ratio, best_positions = ratio, _matched_text_positions(ident, text)
    return best_ratio, best_positions


def infer_wardrobe_prop_labels(
    *, bible_props: list[Any], wardrobe_texts: list[str], already_declared: set[str],
) -> list[str]:
    """对每句出镜人物续接服装文本，在世界书道具卡（跳过已声明的）里找覆盖率
    >=0.8 且识别串(name/alias)长度>=4 的命中。一句续接服装经常同时点名好几件
    衣物（外套+长裙），这不是歧义；真正的结构性歧义是两张不同卡命中了文本里
    同一段字符（例如"顾屿外套"与"温念厚外套"都靠同一个"外套"二字命中）——
    这种情况下两张都不要，只记可见日志，不猜哪张对。跨文本去重保序返回命中
    的卡 name 列表。"""
    matched_names: list[str] = []
    seen: set[str] = set()
    for text in wardrobe_texts:
        hits: dict[str, frozenset[int]] = {}
        for prop in bible_props or []:
            if prop.name in already_declared:
                continue
            ratio, positions = _best_match_for_prop(prop, text)
            if ratio >= MIN_WARDROBE_PROP_COVERAGE:
                hits[prop.name] = positions
        ambiguous = {
            name for name, positions in hits.items()
            if any(name != other and positions & other_positions for other, other_positions in hits.items())
        }
        if ambiguous:
            log.warning(
                "[STORYBOARD_WARDROBE_PROP_AMBIGUOUS][未拦截] 续接服装文本「%s」同一段文字被多张道具卡争抢 %s，均不采用，需人工核查",
                text[:60], sorted(ambiguous),
            )
        for name, positions in hits.items():
            if name in ambiguous:
                continue
            if name not in seen:
                seen.add(name)
                matched_names.append(name)
    return matched_names


def storyboard_pack_prop_entries(
    *, segment: dict[str, Any], bible: Any, conn: Any, project_id: str, episode_no: int,
) -> list[dict[str, Any]]:
    """``resources.props``（模型已声明的道具）之后追加续接服装文本反推出的
    道具卡（续接服装是模型早已写出的"提名"，这里只是代码核验它是否指向一张
    真实存在的世界书道具卡），整份列表交给未改动签名的
    ``resolve_segment_prop_manifest_entries`` 统一做 ready 判定与排序打标。
    超限截断时模型声明项排在推导项之前，优先保留（见
    ``app.video_modes.reference_assemble._apply_prop_composite_overflow``）。
    """
    resources = segment.get("resources") or {}
    continuity_memo = segment.get("continuity_memo") or {}
    declared = list(resources.get("props") or [])
    bible_props = list(getattr(bible, "props", None) or [])
    excluded = _declared_prop_names(declared, bible_props)
    wardrobe_texts = _visible_wardrobe_texts(resources, continuity_memo)
    inferred_names = infer_wardrobe_prop_labels(
        bible_props=bible_props, wardrobe_texts=wardrobe_texts, already_declared=excluded,
    )
    merged = [*declared, *({"label": name, "description": ""} for name in inferred_names)]
    return resolve_segment_prop_manifest_entries(
        merged, conn=conn, project_id=project_id, episode_no=episode_no,
    )


def _prop_reference_lookup(conn, project_id: str, name: str, episode_no: int) -> Any:
    """与 ``app.production.storyboard_prop_assets._prop_reference_lookup`` 同一
    惰性 import 手法，两处各自持有一份（都只有几行胶水代码，不值得为此新增
    跨包耦合）——见该函数 docstring 的完整理由。
    """
    try:
        from app.props import prop_reference_for_episode
    except ImportError:
        return None
    return prop_reference_for_episode(conn, project_id, name, episode_no)


def resolve_segment_prop_manifest_entries(
    prop_entries: list[dict[str, Any]], *, conn, project_id: str, episode_no: int,
) -> list[dict[str, Any]]:
    """把分镜段 ``resources.props``（``_AiResourceProp``: label/description）
    逐条接上 ``app.props`` 的 ready 参考图，供
    ``app.multiview._storyboard_pack_asset_dependencies`` 写进 reference
    manifest（``manifest["props"]``）。ready 判据同
    ``app.multiview.scene_row_for_episode`` 一路的既有用法（``status==
    "ready"`` 且文件真实存在）；查不到/未 ready 时只带 label/description，
    ``ready`` 显式为 False——下游据此判定"这个道具没有可用参考图"，不是
    留空当成有图。``resources_order``（2026-10-01）是这条在 ``prop_entries``
    里的下标（模型声明的重要性顺序，见模块 docstring），原样带出供
    ``prop_library_anchors`` 透传——不重新排序、不去重，逐字保留输入顺序。

    ``prop_revision_id``（2026-10-01，第 1 集第 22 段真实故障追加）是命中行的
    ``prop_references.id``——``app.props.store.upsert_prop_reference`` 每次都是
    先删后插（见其 docstring「覆盖式」），同一道具重新登记外观卡必然拿到新
    id，供 ``app.multiview.manifest_revisions_match`` 据此判定冻结参考图清单
    是否因为外观卡换图而过期；没查到行（``row`` 为 None）时为 None，与
    ``ready=False`` 同义。

    label 带末尾括号注释（如"浅蓝色碎花长裙（裙摆）"）按整条原文查不到卡时，
    剥掉括号重试一次——世界书卡名没有括号，``app.production.storyboard_prop_
    label_validation.prop_label_bracket_note_errors`` 只在生成期拦截新写法，
    存量已落库的带括号 label 靠这里的回退兜底查到卡（2026-10-05）。输出的
    ``label`` 键仍是原始声明文本，不改显示。
    """
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(prop_entries or []):
        label = str(entry.get("label") or "").strip()
        row = _prop_reference_lookup(conn, project_id, label, episode_no) if label else None
        if row is None and label:
            stripped = _strip_trailing_annotation(label)
            if stripped != label:
                row = _prop_reference_lookup(conn, project_id, stripped, episode_no)
        ready = False
        image_path = ""
        if row and str(row["status"] or "") == "ready":
            candidate = str(row["image_path"] or "").strip()
            if candidate and Path(candidate).is_file():
                ready, image_path = True, candidate
        out.append({
            "label": label,
            "description": str(entry.get("description") or ""),
            "ready": ready,
            "image_path": image_path,
            "resources_order": index,
            "prop_revision_id": str(row["id"]) if row else None,
        })
    return out


def manifest_props_signature(manifest: dict[str, Any] | None) -> dict[str, tuple[Any, Any, bool]]:
    """按 label 提取道具条目的选取/版本签名，供
    ``app.multiview.manifest_revisions_match`` 判定冻结参考图清单是否过期。

    ``prop_revision_id`` 换了说明外观卡被重新登记过（旧图已不是当前外观）；
    ``resources_order`` 换了说明超限裁剪/选取顺序会不同；``ready`` 换了说明
    "有没有可用参考图"这件事本身变了。三者任一不同，旧冻结清单在道具这一维
    度上就不再代表当前状态（2026-10-01，第 1 集第 22 段真实故障，见
    ``app.multiview.manifest_revisions_match`` 调用处的完整背景）。"""
    return {
        str(prop.get("label") or ""): (
            prop.get("prop_revision_id"), prop.get("resources_order"), bool(prop.get("ready")),
        )
        for prop in (manifest or {}).get("props") or []
        if isinstance(prop, dict) and prop.get("label")
    }


def prop_library_anchors(manifest_props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ``manifest["props"]``（``resolve_segment_prop_manifest_entries`` 的
    产出）展开成与 ``app.multiview.library_anchor_assets_from_manifest`` 里
    人物/场景锚点同形状的条目，只保留真 ready 且文件存在的道具——同函数对
    人物/场景的既有判据。``resources_order`` 原样透传给锚点字典，供
    ``app.video_modes.asset_lookup._asset_from_path`` 继续带进
    ``ReferenceImageAsset``（见模块 docstring）。
    """
    anchors: list[dict[str, Any]] = []
    for prop in manifest_props or []:
        path = str(prop.get("image_path") or "")
        if not prop.get("ready") or not path or not Path(path).is_file():
            continue
        label = str(prop.get("label") or "")
        anchors.append({
            "entity_type": "prop", "entity_name": label,
            "image_path": path, "purposes": ["qa_anchor", "keyframe_seed"],
            "type": "prop", "source": "asset_library",
            "resources_order": prop.get("resources_order"),
        })
    return anchors
