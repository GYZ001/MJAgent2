"""Bible alias-ownership lookups: which character owns a given alias, whether
two characters conflict over the same alias across episodes, and canonical-
name lookup by alias (plus the legacy full-scan fallbacks).

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import json
from app.schemas import Bible


def _prep_pack_bible_alias_owner(bible: Bible | None, alias: str) -> str | None:
    """人物谱里第一个（``bible.characters`` 列表顺序）在 ``aliases`` 登记了
    这个别名字符串的角色名；没有任何角色登记则返回 ``None``。只回答"有没有
    人认领"，不判断唯一性——唯一性/冲突判定是
    ``_prep_pack_bible_alias_conflicting_owner`` 的职责，两者分工跟历史的
    "读侧函数"/"冲突检查函数"两分是同构的，不合并成一个函数。"""
    if not alias or bible is None:
        return None
    for character in bible.characters:
        if any(a.text == alias for a in (character.aliases or [])):
            return character.name
    return None


def _prep_pack_bible_alias_conflicting_owner(
    bible: Bible | None, alias: str, canonical_name: str,
) -> str | None:
    """人物谱里除 ``canonical_name`` 本人之外，是否还有别的角色也在
    ``aliases`` 里登记了同一个别名字符串？命中即返回那个冲突角色名。"""
    if not alias or bible is None:
        return None
    for character in bible.characters:
        if character.name == canonical_name:
            continue
        if any(a.text == alias for a in (character.aliases or [])):
            return character.name
    return None


def _prep_pack_cross_episode_alias_conflict(
    conn, project_id: str, episode_id: str, *, alias: str, canonical_name: str,
    bible: Bible | None = None,
) -> str | None:
    """这同一个 alias 字符串是否已经被记在了一个不同的 canonical_name 名下？
    命中就返回那个冲突的 canonical_name（供调用方拒绝这次改绑、留痕），没有
    冲突返回 None。主读源是人物谱（``bible.characters[].aliases``，见本函数
    上方 1.7.0 说明）；只有人物谱对这个别名毫无记录时，才补充查旧路径——
    项目内其它已发布分集的 asset_manifest（P2 §16 双重校验期，未退役）。
    """
    if not alias or not canonical_name:
        return None
    bible_conflict = _prep_pack_bible_alias_conflicting_owner(
        bible, alias, canonical_name,
    )
    if bible_conflict:
        return bible_conflict
    if bible is not None and _prep_pack_bible_alias_owner(bible, alias) == canonical_name:
        # 人物谱明确认领这个别名归属 canonical_name 本人、且没有第二个认领
        # 者——这是主源给出的明确"无冲突"结论，旧路径的信号不得推翻它。
        return None
    return _prep_pack_cross_episode_alias_conflict_legacy_scan(
        conn, project_id, episode_id, alias=alias, canonical_name=canonical_name,
    )


def _prep_pack_cross_episode_alias_conflict_legacy_scan(
    conn, project_id: str, episode_id: str, *, alias: str, canonical_name: str,
) -> str | None:
    """旧路径（P2 §16，双重校验期保留，仅在人物谱对该别名毫无记录时补充
    生效，见 ``_prep_pack_cross_episode_alias_conflict`` 调用点）：项目内其它
    已发布分集是否已经把这同一个 alias 字符串记在了一个不同的 canonical_name
    名下？纯粹按"同一别名字符串在项目内是否已经指向不同的人"这个结构性事实
    判断，不需要认识 alias 具体是什么词。"""
    if not alias or not canonical_name:
        return None
    rows = conn.execute(
        "SELECT screenplay_json FROM episodes WHERE project_id=? AND id!=? "
        "AND screenplay_json IS NOT NULL",
        (project_id, episode_id),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["screenplay_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        manifest = payload.get("asset_manifest") or {}
        for character in manifest.get("characters") or []:
            if alias not in (character.get("aliases") or []):
                continue
            other_name = str(character.get("display_name") or "").strip()
            if other_name and other_name != canonical_name:
                return other_name
    return None


# 角色别名注册表读侧（1.5.x task①，真实第24轮 EP3 回归 ERR-20260824-d0830a）：
# 跟场景轴 1.5.1（_prep_pack_resolve_scene_reference_with_alias）完全对称的
# 缺陷——场景侧写别名会被后续读，角色侧却只写不读。1.7.0 起主读源改为人物谱
# （见 _prep_pack_cross_episode_alias_conflict 上方 1.7.0 说明，同一次切换、
# 同一套双重校验纪律）：EP2 一次消歧确立"小胖子"→李富贵后，不必等它作为
# "已发布分集"被扫描到——只要人物谱里已经登记了这条别名（全书分析阶段申报，
# 不依赖任何一集先发布），EP1 起就能直接复用，不必每集重新赌一次消歧模型
# 调用。只返回第一个命中的候选 canonical_name——是否唯一由调用方另外过一遍
# _prep_pack_cross_episode_alias_conflict 确认（复用同一套冲突拒绝逻辑守多
# 目标，不在这里重复实现一份等价的唯一性判断）。
def _prep_pack_lookup_character_alias_canonical_name(
    conn, project_id: str, episode_id: str, name: str,
    bible: Bible | None = None,
) -> str | None:
    """是否已有数据源把 ``name`` 登记为某个角色的别名？命中返回该
    canonical_name，没有命中返回 None。主读源是人物谱，见本节顶部 1.7.0
    说明；人物谱毫无记录时才补充查旧路径（项目内其它已发布分集）。"""
    if not name:
        return None
    bible_owner = _prep_pack_bible_alias_owner(bible, name)
    if bible_owner:
        return bible_owner
    return _prep_pack_lookup_character_alias_canonical_name_legacy_scan(
        conn, project_id, episode_id, name,
    )


def _prep_pack_lookup_character_alias_canonical_name_legacy_scan(
    conn, project_id: str, episode_id: str, name: str,
) -> str | None:
    """旧路径（P2 §16，双重校验期保留，仅在人物谱对该别名毫无记录时补充
    生效）：项目内是否已有其它已发布分集把 ``name`` 登记为某个角色的别名？
    命中返回该 canonical_name，没有命中返回 None。"""
    if not name:
        return None
    rows = conn.execute(
        "SELECT screenplay_json FROM episodes WHERE project_id=? AND id!=? "
        "AND screenplay_json IS NOT NULL",
        (project_id, episode_id),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["screenplay_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        manifest = payload.get("asset_manifest") or {}
        for character in manifest.get("characters") or []:
            if name in (character.get("aliases") or []):
                canonical = str(character.get("display_name") or "").strip()
                if canonical:
                    return canonical
    return None


# 先验知识申报通道（1.5.0，用户修正令：outright 禁止会扔掉真正猜对的真名，
# "丹鬼"这类猜对了本该是加分项）。模型可能在训练语料里读过这部小说，与其
# 假装它不知道（禁止），不如让它把这份先验知识当一个可核验的候选申报出来
# （_ModelCharacterMention/_ModelSceneMention.suspected_true_name），申报本身
# 从不被直接采信——必须先通过下面这道确定性核验，核验不过就丢弃，回退到
# display_name 本身的常规解析路线（消歧/群演/发现），不静默相信任何猜测。
#
# 身份绑定审判程序（真实回归：用户抓到 EP2/EP3 把"小胖子"误绑成"王有材"，
# 又抓到 EP6"上官修身边的男子"被绑成上官修本人——旧版核验只检查候选真名
# 这个字符串在前瞻窗口里出现过，"名字存在"被当成了"身份链接"的充分条件。
# 中途曾尝试过两版结构规则："列举反证"、"包含关系方向性规则"，均被用户
# 否决——那些规则本质是穿着语法外衣的黑白名单，靠人工穷举的分隔符/方位词
# 判断语义，覆盖不了语言的全部表达方式。最终架构改为完全不猜测语义规则，
# 把"是否同一人"这个语义判断彻底交给模型，代码只负责三件事：把全部相关
# 原文老老实实检索出来、把模型的结论钉在真实存在的原文引句上、以及记账）：
#   1) 卷宗检索（代码，零语义，_prep_pack_true_name_dossier）：检索项目
#      全书（chapters 全表，不只是本集或某个前瞻窗口）里所有含 alias 的
#      自然段 ∪ 所有含 suspected_true_name 的自然段，段落原文 + 章节号
#      组成"卷宗"。反证证据（比如"小胖子、王有材"这种并列举出两个不同人
#      的段落）因为同时含两个词，天然会被检索到卷宗里，不需要另外写一条
#      "反证"规则去猜它长什么样。卷宗超过字符预算时用
#      _prep_pack_sample_dossier_entries_within_budget 做确定性（非随机）
#      采样：同时含两词的段落全部保留，只含一个词的段落按下标等距抽样。
#   2) 裁决（模型，唯一一次调用，_prep_pack_true_name_verdict，1.10.0 改为
#      候选判别，不再是同一人是非题——见 PREP_PACK_VERSION 上方 1.10.0 大
#      注释的完整根因与数据）：给模型卷宗原文 + 一份候选真名/候选场景名单
#      （suspected_true_name 本身 ∪ 人物谱/场景谱里在卷宗文本中有字面命中
#      的其它候选）+ 显式"都不是/无法确定"出口，问"称谓 alias 最可能指候选
#      中的哪一个"，不是"是不是 Y"——避免旧版是非题诱发确认偏误。
#   3) 钉证（代码，_prep_pack_true_name_pin_dossier_entry，1.10.0 改为段号
#      钉证）：模型只需引用卷宗目录里的候选编号（entry_index），不比对
#      模型转录的逐字引句（真实生产数据证明旧版逐字比对会被模型的跨段
#      拼接/摘要噪音误杀，见 PREP_PACK_VERSION 上方大注释）；钉中的卷宗
#      条目还必须逐字包含 alias 本身（待判标签，此前零要求，是"钉证在近半
#      数真实 same 判决里形同虚设"的主因）；若全卷宗存在同时含 alias 与
#      true_name 的双锚定条目，钉中的条目必须就是双锚定条目之一，否则必须
#      钉在本集自己的（alias 逐字命中的）段落上——两种情形都不满足则拒绝。
#      selected_candidate 必须精确等于 suspected_true_name 且钉证通过，才
#      算核验通过；其它任何结果（选了别的候选/选了"都不是"/钉证失败）一律
#      不采信——默认安全侧，不确定就不绑，回退到 alias 自身的常规解析路线
#      （未被发现进一步归类时自然落为群演，见 _pass 里 unresolved_
#      characters 的处理，不需要这里单独再写一条"走群演"分支）。
#   4) 记账（代码）：核验通过的判决连同钉住的原文引句进 provenance
#      （anchor_phrase 就是这句被钉住的支撑句），alias 才会被写进
#      entry["aliases"]（见调用点），写入注册表的东西天然带着完整证据链；
#      读侧（_prep_pack_lookup_character_alias_canonical_name，task①）
#      逻辑不变——源头干净，继承出去的自然干净。同一 (subject_kind, alias,
#      true_name) 组合在同一次生成里只会真正发一次模型调用：_resolve_assets
#      级别的 true_name_verdict_cache 字典按 (subject_kind, alias, true_name)
#      缓存判决结果（subject_kind 隔离角色/场景两个共用同一字典的域），
#      重复出现的提及直接复用（"注册表即缓存"里"注册表"指的是跨集持久化
#      那一层，这里的进程内字典是同一次生成内的短期缓存，两者不冲突：
#      前者防跨集重复裁决，后者防同一集内同一对提及反复调用）。
# 跨集矛盾绑定（同一 alias 在不同已发布分集里被判给不同的 true_name）：
# 复用既有的 _prep_pack_cross_episode_alias_conflict（task②）继续按拒绝
# 处理——发现冲突就不接受这次改名，回退到 alias 自身的解析路线，冲突记入
# rejected_alias_conflicts（观测）。协调方设想的"合并双方卷宗重审一次，
# 仍矛盾则两边都降级为群演"是更精细的处理，本轮未实现（没有红灯明确要求
# 这一步，且现状——拒绝新的改名、绝不静默接受任何一边——已经是安全默认
# 值，不会把错误绑定放出去），留待后续有真实回归再做。
