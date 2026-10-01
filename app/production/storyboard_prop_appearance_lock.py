"""P0-F：道具外观全集锁定（``prop_appearance_locks``）——与
``storyboard_prop_entrance``/``storyboard_wardrobe_plan`` 同一次真实回归驱动
（proj_ca86b15ab7d7 EP1 逐帧复查，2026-09-30）。

根因（只读核实，见派单）：温念的手机第 1-3 段写「黑色手机」、第 6 段起写「白色
手机壳的智能手机」——这件道具素材库压根没有卡（映射台从未把它提名成道具提及）；
行李箱道具卡的 appearance 写着「24寸竖款哑光深卡其色ABS硬壳拉杆箱……」，但
``asset_manifest.props`` 里这条记录的 ``segment_indexes`` 只有 ``[31]``（模型只在
原文第 31 段的那一句直接提及旁边打了锚点），本集分镜段 9/10/12 的
``source_segment_indexes`` 都不包含 31，``storyboard_pack._segment_relevant_
assets`` 按交集过滤后这三段的 ``relevant_assets.props`` 里根本没有这张卡，模型
只能各自现编——写成「中号深蓝色帆布面软壳拉杆箱」；唯独包含 31 的段 11 才见到
真卡片，写出了与卡片逐字一致的外观。两个根因分属「没有卡」与「有卡但映射的
原文段过滤太窄」，共同点是：道具的外观锚点只在阶段二按 ``segment_indexes`` 交集
分发，而这个交集只反映「原文哪几段直接提到了这件道具」，不反映「分镜哪几段的
画面里会拍到它」——箱子被温念一路拖着走，后面几段画面里明明在，原文不会逐句
重复描述它。

修法：不再依赖阶段二那次窄口径的交集过滤，改在阶段一（节拍表生成时，模型已经
通读全文、知道一件道具会在哪些节拍反复出现）让模型为每一件会出现在两段以上
画面里的道具锁定一次外观——有卡的逐字抄卡片（代码核验逐字一致，不信任模型的
转述，见 ``verify_and_lock_appearances``），没有卡的自己写一次；随手一起报出
这件道具全集范围内会出现在哪些节拍（``beat_ids``），阶段二按节拍号（不是原文
段号）把这份外观分发给每一个画到它的段（见 ``moments_for_segment``），从根上
绕开 ``segment_indexes`` 交集过滤这道窄口径。

已知局限（本次不实现，P2）：素材库里已经建卡、但映射台这一集完全没有把它提名
成道具提及（``asset_manifest.props`` 里压根没有这条记录）的场景，本模块的核验
覆盖不到——``known_prop_card_appearance_index`` 只能看见映射台已经提名过的道具
的卡片外观（复用 ``storyboard_scene_binding._enrich_asset_manifest_canonical_
visuals`` 已经补进 ``asset_manifest.props[].appearance`` 的那份数据，不重新查
世界书），要覆盖这个局限需要把 ``bible`` 一路穿透进阶段一调用链（
``storyboard_pack.py``/``storyboard_short_drama_review.py`` 两个文件都在行数
棘轮基线上零/近零余量），本次两个真实确认的根因都不落在这个局限范围内，留给
以后有专门预算时再评估。

评审补丁（2026-09-30，同批）：① ``verify_and_lock_appearances`` 此前『无卡』分支
对模型原样抄写占位说明文字（``_NO_CANONICAL_PROP_APPEARANCE_NOTE``）不做任何
核验，同类失败模式已在 wardrobe 字段真实发生过一次——现丢弃这类条目（见
``_is_placeholder_echo``）；『有卡』分支的 label 精确匹配增加排版归一后的兜底
（``_lookup_card_appearance``），并对『卡片一张都没被命中』记可见信号（见
``log_unmatched_prop_cards``）。② ``ensure_prop_form_matches_lock``（
``storyboard_continuity_memo``）只纠正 ``continuity_memo.props[].form`` 这个
旁路记账字段，从不核对真正发给视频模型的 ``prompt_text`` 是否真的写成了锁定
外观——新增 ``segment_advisories``（非阻断，同 ``storyboard_prop_entrance.
segment_advisories`` 的能力边界与哲学）补上这道核对，接入
``storyboard_pack._segment_content_advisories``。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.production.screenplay_markers import beat_is_shot
from app.production.storyboard_prop_assets import _NO_CANONICAL_PROP_APPEARANCE_NOTE

log = logging.getLogger(__name__)


def prop_appearance_lock_beat_sheet_rules() -> list[str]:
    """阶段一 rules[]，两档都无条件追加（不按 adaptation_mode 分支）。"""
    return [
        "本集里如果同一件道具会出现在两段或更多段落的画面中，必须在 prop_appearance_locks 里"
        "为它锁定一次外观：label 是这件道具的称呼，appearance 是它完整的可视特征（材质、颜色、"
        "形状、磨损细节等），beat_ids 列出全集里这件道具会出现在画面中的每一个节拍（不只是它"
        "第一次出场的那个节拍，后面画面里持续在场、哪怕原文没有再描述它的节拍也要计入）——一件"
        "道具在全集里只锁定一次，之后它出现在任何一段画面里都必须逐字沿用这里写的 appearance，"
        "不能因为原文没有再次描述它就重新编写、也不能换一套说法。",
        "known_assets.props 里已经带非空 appearance 字段的道具，说明素材库已经为它建立了标准"
        "外观：为它写 prop_appearance_locks 条目时，label 必须使用 known_assets.props 里给出的"
        "原始 label，appearance 必须逐字复制 known_assets.props 里的 appearance 原文，一个字都"
        "不能改写、概括、翻译或补充新细节；known_assets.props 里没有这件道具、或它的 appearance"
        "写着素材库没有标准外观的说明文字时，才由你在这里第一次为它确定一套具体的可视特征。",
    ]


def known_prop_card_appearance_index(payload: dict[str, Any]) -> dict[str, str]:
    """``label/canonical_name -> 素材库标准外观``（逐字），只收有真实卡片外观
    的道具（跳过 ``_NO_CANONICAL_PROP_APPEARANCE_NOTE`` 占位说明）。数据源与
    ``manifest_brief_for_prompt`` 喂给模型看到的 ``known_assets.props[].
    appearance`` 完全同一份（都读 ``payload["asset_manifest"]["props"]``），
    保证「模型看到的」与「代码核验用的」是同一份真相。

    2026-09-30：同时按 ``label``（映射台这次的原文写法，模型在
    ``prop_appearance_locks[].label`` 里通常会原样沿用）与 ``canonical_name``
    （映射台对照既有卡片绑定的规范卡名，见 app.production.prep_pack.
    discovery._prep_pack_build_prop_manifest）两个键收录同一段外观——模型
    偶尔会凭自己的先验知识直接写出卡片的规范名而不是 ``known_assets`` 里
    展示的原文写法，只按 ``label`` 精确匹配会让这种情形退化成"无卡"分支、
    原样采信模型自编文字（同 ``_lookup_card_appearance`` 排版归一兜底要解决
    的同一类漏判）。
    """
    manifest = payload.get("asset_manifest") or {}
    index: dict[str, str] = {}
    for prop in manifest.get("props") or []:
        appearance = str(prop.get("appearance") or "").strip()
        if not appearance or appearance == _NO_CANONICAL_PROP_APPEARANCE_NOTE:
            continue
        for key in (str(prop.get("label") or "").strip(), str(prop.get("canonical_name") or "").strip()):
            if key:
                index[key] = appearance
    return index


def _valid_beat_ids(lock: Any, known_beat_ids: set[str]) -> list[str]:
    return [beat_id for beat_id in lock.beat_ids if beat_id in known_beat_ids]


def _normalize_label(label: str) -> str:
    """去空白 + casefold，容忍模型在 label 上的大小写/全半角空格出入——不归并同义词，
    只归一排版差异，与 ``storyboard_dialogue_repeat._normalize`` 同一处理强度。"""
    return re.sub(r"\s+", "", label or "").casefold()


def _lookup_card_appearance(label: str, card_index: dict[str, str]) -> str | None:
    """精确 label 命中优先；命中不到时退化到排版归一后的匹配（模型在 label 上偶尔
    与 known_assets 给出的原始 label 有大小写/空格出入，精确匹配会静默 miss、退化成
    "无卡"分支，卡片保护不到），仍命中不到才是真的"没有卡"。"""
    exact = card_index.get(label.strip())
    if exact is not None:
        return exact
    normalized_target = _normalize_label(label)
    for card_label, appearance in card_index.items():
        if _normalize_label(card_label) == normalized_target:
            return appearance
    return None


def _is_placeholder_echo(appearance: str) -> bool:
    """appearance 是不是把喂给模型看的『素材库没有标准外观』占位说明文字原样抄了
    回来——这段说明文字（``_NO_CANONICAL_PROP_APPEARANCE_NOTE``）是喂给模型看的
    ``known_assets.props[].appearance`` 内容本身（见 ``storyboard_context_segments.
    manifest_brief_for_prompt``），不是猜测模型自由文本的措辞：判据是与这个已知系统
    常量做包含比对，同一类失败模式已在 wardrobe 字段真实发生过一次（原样把外观锚点的
    内部说明文字写进提示词，见 ``storyboard_continuity_memo`` 模块 docstring
    2026-09-30 段）。"""
    return _NO_CANONICAL_PROP_APPEARANCE_NOTE.strip() in appearance.strip()


def _repaired_appearance(card_appearance: str | None, appearance: str, label: str) -> str:
    """有卡片时强制改写成卡片原文（代码核验逐字一致，不信任模型的转述），
    没有卡片时原样保留模型自己写的这一次。"""
    if not card_appearance or appearance.strip() == card_appearance.strip():
        return card_appearance or appearance
    log.info(
        "[STORYBOARD_PROP_APPEARANCE_LOCK_REPAIR] 道具「%s」外观锁定与素材库标准外观不一致，"
        "已按素材库卡片原文强制改写", label,
    )
    return card_appearance


def log_unmatched_prop_cards(locks: list[Any], card_index: dict[str, str]) -> None:
    """可见信号（不阻断）：素材库有道具标准外观卡片，但没有任何一条
    ``prop_appearance_locks``（含排版归一后）命中同一张卡片——大概率是模型没有
    按规则使用卡片给出的原始 label，这件道具的外观核验会静默退化成"无卡"分支、
    原样采信模型自编文字，需要人工核查。"""
    if not card_index:
        return
    matched = {_normalize_label(lock.label) for lock in locks}
    for card_label in card_index:
        if _normalize_label(card_label) not in matched:
            log.warning(
                "[STORYBOARD_PROP_APPEARANCE_LOCK_CARD_UNUSED][未拦截] 素材库道具卡片「%s」"
                "有标准外观，但没有任何一条道具外观锁定命中它（可能模型漏报了这件道具，或 "
                "label 写法与卡片不一致），本集这件道具可能各段各自现编外观，请人工核查", card_label,
            )


def verify_and_lock_appearances(
    locks: list[Any], known_beat_ids: set[str], card_index: dict[str, str],
) -> list[Any]:
    """核验/纠正阶段一提名的道具外观锁定，返回清洗后的列表：

    - ``beat_ids`` 剔除引用未知 beat_id 的条目（同 ``storyboard_prop_entrance.
      valid_prop_entrances`` 的剔除哲学，记日志不静默、不回退到"猜一个节拍"）；
      一件锁定的 ``beat_ids`` 如果被剔到空，整条锁定连同它一起剔除——没有任何
      一段能认领到它，留着也用不上。
    - 没有卡片、且 appearance 是模型原样抄写"素材库没有标准外观"占位说明文字
      （见 ``_is_placeholder_echo``）的条目整条丢弃——它不是模型自己写的可视
      特征，留着会把这段说明文字发给视频模型。
    - ``label`` 命中 ``card_index``（含排版归一后的兜底匹配，见
      ``_lookup_card_appearance``）时，``appearance`` 强制改写成卡片原文，不管
      模型这次写的是什么，见 ``_repaired_appearance``。
    """
    result: list[Any] = []
    for lock in locks:
        beat_ids = _valid_beat_ids(lock, known_beat_ids)
        if not beat_ids:
            log.warning(
                "[STORYBOARD_PROP_APPEARANCE_LOCK_DROPPED][未拦截] 道具外观锁定「%s」引用的 "
                "beat_id 全部不存在于本集节拍表，已剔除，不参与任何段的外观分发——如果这是遗漏的"
                "正确节拍，需要人工核对本集生成结果", lock.label,
            )
            continue
        card_appearance = _lookup_card_appearance(lock.label, card_index)
        if card_appearance is None and _is_placeholder_echo(lock.appearance):
            log.warning(
                "[STORYBOARD_PROP_APPEARANCE_LOCK_PLACEHOLDER_ECHOED][未拦截] 道具外观锁定"
                "「%s」的 appearance 是素材库『没有标准外观』的内部说明文字原样抄写，不是模型"
                "自己新写的可视特征，已丢弃，不参与任何段的外观分发——请人工核对本集生成结果",
                lock.label,
            )
            continue
        result.append(lock.model_copy(update={
            "beat_ids": beat_ids, "appearance": _repaired_appearance(card_appearance, lock.appearance, lock.label),
        }))
    log_unmatched_prop_cards(locks, card_index)
    return result


def locks_by_label(locks: list[Any]) -> dict[str, str]:
    """``label -> 锁定外观``，供 continuity_memo 的 props.form 回填使用。"""
    return {lock.label: lock.appearance for lock in locks}


def moments_for_segment(segment_beat_ids: list[str], locks: list[Any]) -> list[Any]:
    """本段可见的道具外观锁定：``locks`` 的 ``beat_ids`` 与本段 ``beat_ids``
    有交集即命中——与 ``storyboard_prop_entrance.moments_for_segment`` 不同，
    这里不是"认领一次就从池子里划走"：同一件道具的锁定要能被它出现的每一个
    段重复取用，"claim once" 会让除第一次认领之外的段拿不到这份外观，等于
    重新制造本模块要解决的那个窄口径问题。
    """
    wanted = set(segment_beat_ids)
    return [lock for lock in locks if wanted & set(lock.beat_ids)]


def segment_rule_text(locks_here: list[Any]) -> list[str]:
    """阶段二 per-segment 正面陈述：本段画面里这件道具按三态可见性决定怎么用它锁定的外观。

    2026-10-01 第一版（第 1 集第五版真实回归，见 ``app.production.storyboard_prop_
    visibility`` 模块 docstring）：``locks_here``（按 beat_id 命中）只说明这件道具这一段
    "在场"，不说明它这一段画面里"看不看得见"——它完全可能被衣物/容器遮住。旧文案「在本段
    画面中出现」把"在场"断言成了"可见"，会让模型即使画面写明道具被遮住，也照样把这句要求
    理解成必须写出完整外观。改成条件句，可见性判据统一指向 ``storyboard_prop_visibility``
    那条无条件追加的通用规则，这里不重复定义判据，只重复提醒"这件道具已经锁定了外观、各种
    可见状态下要用哪一段文字"。

    2026-10-01 第二版（同一集第 13/17 段真实成片复查，协调方发现）：第一版「看得见就必须
    逐字沿用」是二元的，对"只露出一截"的部分可见道具/衣物同样会诱发"把被遮住部位的款式
    细节也写出来"的问题（开衫/长裙被外套盖住大半，仍被要求逐字沿用全段标准外观，模型据此
    把外套画成敞开）。改成三态分流，与 ``storyboard_prop_visibility`` 的三态判据对齐：
    完全可见才逐字沿用整段；部分可见只能摘抄外观描述里与露出部分对应的颜色/花纹/材质
    用词，不抄被遮住部位的款式细节；完全不可见维持第一版「不写、不列入」。
    """
    return [
        f"道具「{lock.label}」全集范围内的外观已锁定：{lock.appearance}——本段画面里这件道具"
        "完全可见（判据见前面的道具可见性规则，没有被其它衣物/容器/道具盖住任何部分）时，"
        "必须逐字沿用这段外观描述（颜色/材质/形状/磨损细节等一个字都不能改），不得因为这不是"
        "它第一次出场就重新编写、也不得换一套说法；只露出一部分、其余被遮住时，只能从这段"
        "外观描述里摘抄与露出部分对应的颜色/花纹/材质用词，不得抄描述被遮住部位款式细节"
        "（领口形状、袖子长短、腰身剪裁、内侧标签这类）的那几句，不得逐字整段照抄；完全不"
        "可见、收起或根本不在画面中时，按道具可见性规则处理，不写这段外观、也不列入"
        " resources.props"
        for lock in locks_here
    ]


def segment_advisories(locks_here: list[Any], prompt_text: str) -> list[str]:
    """非阻断，供 ``_segment_content_advisories`` 合并进 ``degraded_capabilities``。

    ``ensure_prop_form_matches_lock``（``storyboard_continuity_memo``）只强制纠正
    ``continuity_memo.props[].form`` 这个旁路记账字段，从不检查真正发给视频模型的
    ``prompt_text`` 是否真的写成了锁定外观——本函数补上这道对 ``prompt_text`` 本身
    的核对，写法与判据同 ``storyboard_prop_entrance.segment_advisories``：能力边界
    也相同，只能判断锁定的外观描述是否以改写措辞出现，判不出画面是否真的画对，
    因此只记日志不阻断，不参与语义重试。

    2026-10-01（``storyboard_prop_visibility`` 落地后的已知限制）：外观没出现在
    ``prompt_text`` 里现在有两种合法原因——模型漏写，或者模型正确识别出这件道具
    本段被遮住/收起，按道具可见性规则没有写出外观。本函数拿不到"模型当时判定的
    可见性"这个事实，无法区分这两种原因，提示文案据实说明这一点，不再把"没出现"
    默认暗示成"漏写"。"""
    advisories: list[str] = []
    for lock in locks_here:
        if not beat_is_shot(f"必现内容：{lock.appearance}", prompt_text):
            advisories.append(
                "[STORYBOARD_PROP_APPEARANCE_LOCK_NOT_SHOWN][未拦截] 道具"
                f"「{lock.label}」的外观已在全集范围内锁定为「{lock.appearance}」，但看起来没有"
                "被写进提示词（只能判断外观描述是否以改写措辞出现，判不出画面是否真的画对；也"
                "可能是这件道具本段被遮住/收起，按道具可见性规则正确地没有写出外观，不一定是"
                "遗漏），请人工核查——确认是遗漏的话可在分镜台编辑本段镜头稿补上"
            )
    return advisories


def log_missing_appearance_locks(entrances: list[Any], locks: list[Any]) -> None:
    """可见信号（不阻断，不兜底编造）：入场计划（``storyboard_prop_entrance``）
    提名过的道具如果没有对应的外观锁定，说明这件道具大概率会在后续段落里被
    模型各自现编外观——记一条日志供人工核查，不是把它塞进候选清单直接生成
    占位文本（CLAUDE.md「不得兜底填充」）。

    用 ``entrances``（阶段一模型独立读全文产出的"这件道具值得交代出场"判断）
    而不是 ``asset_manifest.props`` 作对照组：后者的 ``segment_indexes`` 正是
    本模块要绕开的窄口径（见模块 docstring），拿它来判定"缺不缺锁定"会把
    同一个根因绕回来。
    """
    locked_labels = {lock.label for lock in locks}
    for entry in entrances:
        if entry.label not in locked_labels:
            log.warning(
                "[STORYBOARD_PROP_APPEARANCE_LOCK_MISSING][未拦截] 道具「%s」有入场计划但没有"
                "对应的外观锁定，后续各段可能各自现编外观、彼此不一致，请人工核查", entry.label,
            )
