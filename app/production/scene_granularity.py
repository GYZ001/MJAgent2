"""场景粒度判据：画面可共用性——同一物理地点、同一年代/时期、同一时间段是可共用的
锚点场景；一句带过或纯抒情提及的地点不单独建场景，归并为它所属锚点场景的别名。

背景（B 上 6 个项目实测，2026-09-02）：场景图从未被任何分镜使用的比例——我欲封天
5/15、橘座在上 13/13、神墓 13/18、西游记 11/15、三国白话 18/24、跑不快 14/17。根因
不是一个判据，是两个方向同时出错：
  ① 该分的没分——「世界杯赛场」一个场景同时代表 2006 柏林/2010 南非/2018 俄罗斯/
     2022 卢赛尔决赛四届完全不同的球场，scene_canonical 里连一个具体年份/届次都没有；
  ② 不该分的分了——「第三个街口」（雨中撑伞停下的路口，一句话带过）、「柏林城市
     外景」（原文其实是坐在替补席"看着柏林的夜空"的抒情句，不是要在柏林取景）、
     「罗马城外景」（欧冠决赛的抒情句）各自独立成一张定场图。
判据用两个正交维度拆开这两个方向：``location_key``（同一物理地点+同一年代/时期归一
为同一个 key，年代不同必须是不同 key——直接解决①）与 ``role``（anchor=会在画面里
作为环境持续出现，需要独立场景图；transitional=只是经过/一句带过/抒情提及，不需要
——直接解决②）。两者都要求模型给出 ``anchor_phrase``（原文逐字依据），不给依据的
判定不予采信，退回"未识别"而不是编造。

判据从原文数据推导，不使用任何预置地点词表（CLAUDE.md 禁止黑白名单）：模型输出
location_key/era_anchor 是自由文本，不做枚举校验；代码侧只做结构性核验（anchor_phrase
必须真的在给定原文依据里逐字命中、role 必须是 anchor/transitional 二值之一，缺省时
按更安全的一侧兜底——见 ``resolve_scene_granularity_verdict`` 的文档字符串）。

不加数据库迁移：既有场景已有的 ``discovery_sources``（自由文本列表，仅供审计，不参与
出图/校验）借来存一条内部粒度标签（``encode_granularity_tag``/``decode_granularity_tag``），
供未来判定复用同一物理地点+年代时做确定性去重；旧场景没有这条标签时按"不可比对"处理
（不强行推断），不影响它们的既有身份匹配路径（``app.validators.match_scene_name`` 等）。

与 ``app.scenes`` 的分工：本模块只产出「候选场景该不该建、该并进谁」的结构化提示词与
判定解析——纯函数，不发起模型调用、不碰数据库。落库/别名登记/出图仍在 app.scenes（唯一
真源）。两个模块同层（L4，app.production 与 app.scenes 均声明 = 4），app.scenes 单向
依赖本模块，不反向。
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel

ROLE_ANCHOR = "anchor"
ROLE_TRANSITIONAL = "transitional"
_VALID_ROLES = {ROLE_ANCHOR, ROLE_TRANSITIONAL}

_GRANULARITY_TAG_PREFIX = "__granularity_v1__:"
_STRIP_RE = re.compile(r"[\s，,。.：:；;/、|()（）\-—]+")


class SceneGranularityVerdict(BaseModel):
    """``assess_new_scene`` 模型输出的解析结果（新增粒度字段，向后兼容旧调用方——
    旧字段 important/existing_scene_name/reason/name/scene_canonical/location_kind
    含义不变，新字段缺省时不影响旧调用方只读旧字段的行为）。"""

    important: bool = False
    existing_scene_name: str = ""
    reason: str = ""
    name: str = ""
    scene_canonical: str = ""
    location_kind: str = "其他"
    location_key: str = ""
    role: str = ROLE_ANCHOR
    era_anchor: str = ""
    anchor_phrase: str = ""

    def as_dict(self) -> dict:
        return self.model_dump()


def _normalize_key(value: str) -> str:
    """跟 ``app.scenes._exact_known_scene_name`` 同一套归一化口径（去分隔符/空白），
    只用于比较 location_key 这类内部标识符，不用于面向用户的文案。"""
    return _STRIP_RE.sub("", (value or "").strip())


def scene_granularity_prompt(
    label: str,
    spatial_context: str,
    *,
    style: str,
    style_rule: str,
    known_scenes: list[tuple[str, str]],
    ep_label: str,
    canonical_min: int,
    canonical_max: int,
    same_location_match_rule: str,
) -> str:
    """构造粒度判定提示词（正面陈述，不用黑名单/词表）。``known_scenes`` 是
    ``[(name, scene_canonical), ...]``——带上锚点串本身，而不只是名字，模型才有
    材料判断候选与已有场景是否属于同一 location_key。"""
    known_block = "\n".join(
        f"- {name}：{canonical}" for name, canonical in known_scenes
    ) or "（无）"
    return f"""任务：判定已确认剧本场次地点「{label}」的画面粒度，决定它该不该单独建为可复用场景图。

全片画风（场景锚点必须与之一致）：{style}
已有规范场景（含各自的锚点串，用于判断「{label}」是否与其中某个属于同一物理地点+同一
年代/时期）：
{known_block}

本场景的原文依据（{ep_label}）：
{spatial_context[:1000]}

画面可共用性判据（粒度判定的唯一标准，正面陈述）：
- location_key：这个地点的物理身份归一化标签——同一物理地点、同一年代/时期用同一个
  location_key；location_key 写地点本身的稳定短语，不写成整句描述，也不含单纯的白天/
  夜晚/天气差异（那些属于同一 location_key，用光时段写进 scene_canonical 即可）。
- era_anchor：原文明确给出的年份/届次/时代等时间锚点（如"2010年""南非世界杯"）；原文
  没有给出就填空字符串，不得推测、不得用你自己的百科知识补全。
- 同一 location_key 若 era_anchor 不同（例如同一座球场在不同届次的比赛），必须视为
  不同场景，分别登记，不得合并成一个笼统场景——这是本判据最容易出错的地方，务必对照
  「本场景的原文依据」里实际写的年代/届次判断，不要因为地点名字相似就合并。
- role="anchor"：这个地点会在画面里作为环境持续出现，是镜头需要挂靠的实际场景，必须
  建独立场景图。
- role="transitional"：这段原文只是经过、一句带过或抒情提及这个地点（例如"看着某地的
  夜空"这类抒情句、路过某个路口这类一笔带过），画面不需要在此地点单独停留展开，不建
  独立场景图；此时若「已有规范场景」里确有同 location_key+era_anchor 的场景，
  existing_scene_name 填它的完整名称，没有就留空字符串——不要为了填满这个字段而胡乱
  指向一个物理地点不同的已有场景。
- anchor_phrase：从「本场景的原文依据」中逐字摘录、能证明上面 location_key/era_anchor/
  role 判定成立的一段原文（不超过约60字）；原文里确实没有可摘录的依据就填空字符串，
  绝不改写、不得虚构、不得跨句拼接。
- important=true 当且仅当 role="anchor" 且它不属于任何已有场景的同一 location_key+
  era_anchor；important=false 时的既有口径：{same_location_match_rule}
- name：稳定的场景短标签（4~10 字），不要与已有场景重名。
- scene_canonical 是"固定场景锚点串"：{canonical_min}~{canonical_max} 字（硬门禁，写完
  数一遍），须含 地点/室内外/光线时段/标志陈设/氛围色调；只写视觉可见的环境信息，不写
  人物、不写剧情动作。{style_rule}

只输出一个 JSON 对象：
{{"important": true/false, "existing_scene_name": "已有规范场景完整名称或空字符串",
  "reason": "一句话依据", "name": str, "scene_canonical": str,
  "location_kind": "室内|室外|其他", "location_key": str, "role": "anchor|transitional",
  "era_anchor": str, "anchor_phrase": str}}"""


def _verified_anchor_phrase(anchor_phrase: str, spatial_context: str) -> str:
    """anchor_phrase 必须真的逐字命中给模型看过的原文依据，否则不予采信（空着比
    编一个假依据诚实）——跟 app.production.prep_pack 里 provenance 自校验的
    同一条纪律，只是这里判据更轻量（子串命中，不需要跨段索引）。"""
    phrase = (anchor_phrase or "").strip()
    if not phrase:
        return ""
    if _normalize_key(phrase) and _normalize_key(phrase) in _normalize_key(spatial_context):
        return phrase
    return ""


def resolve_scene_granularity_verdict(
    raw: dict,
    *,
    label: str,
    spatial_context: str,
    canonical_min: int,
    canonical_max: int,
) -> SceneGranularityVerdict:
    """把模型原始 JSON 解析并核验成 ``SceneGranularityVerdict``。

    role 缺省/非法时按更安全的一侧兜底为 anchor：漏建一个真实存在的场景（用户
    需要的画面拿不到）比多建一个场景（最多是多一次候选、不强制出图，见
    ``app.scenes.generate_scene_refs`` 的按需出图口径）风险更高，两害相权取其轻，
    不是"不知道就填 anchor"的臆造——location_key/era_anchor/anchor_phrase 仍然
    只取模型这次真实给出的值，不臆造。
    """
    role = str(raw.get("role") or "").strip()
    if role not in _VALID_ROLES:
        role = ROLE_ANCHOR
    location_key = str(raw.get("location_key") or "").strip()
    name = (raw.get("name") or "").strip() or label.strip()
    if not location_key:
        location_key = name
    canonical = (raw.get("scene_canonical") or "").strip()
    if len(canonical) > canonical_max:
        canonical = canonical[:canonical_max]
    important = bool(raw.get("important")) and role == ROLE_ANCHOR
    if important and len(canonical) < canonical_min:
        important = False  # 锚点太稀薄不足以稳定定场 → 不入库，与既有口径一致
    anchor_phrase = _verified_anchor_phrase(str(raw.get("anchor_phrase") or ""), spatial_context)
    return SceneGranularityVerdict(
        important=important,
        existing_scene_name=str(raw.get("existing_scene_name") or "").strip(),
        reason=str(raw.get("reason") or "").strip(),
        name=name,
        scene_canonical=canonical,
        location_kind=str(raw.get("location_kind") or "其他").strip() or "其他",
        location_key=location_key,
        role=role,
        era_anchor=str(raw.get("era_anchor") or "").strip(),
        anchor_phrase=anchor_phrase,
    )


def encode_granularity_tag(location_key: str, era_anchor: str, role: str) -> str:
    """编码进 ``Scene.discovery_sources``（既有自由文本列表，无需迁移）的一条
    内部标签，供未来判定做确定性 location_key+era_anchor 去重。"""
    payload = {"location_key": location_key, "era_anchor": era_anchor, "role": role}
    return _GRANULARITY_TAG_PREFIX + json.dumps(payload, ensure_ascii=False)


def anchor_discovery_sources(spatial_context: str, location_key: str, era_anchor: str) -> list[str]:
    """新建锚点场景时 ``Scene.discovery_sources`` 的完整取值：原文依据摘录 +
    粒度标签，供 app.scenes 的两个 ensure_scenes_for_* 复用，不重复拼装。"""
    return [spatial_context, encode_granularity_tag(location_key, era_anchor, ROLE_ANCHOR)]


def decode_granularity_tag(discovery_sources: list[str] | None) -> dict | None:
    """从既有场景的 discovery_sources 里取回粒度标签；没有（旧场景/非本机制建的
    场景）返回 None，调用方必须把 None 当"不可比对"处理，不得臆造。"""
    for item in discovery_sources or []:
        text = str(item or "")
        if text.startswith(_GRANULARITY_TAG_PREFIX):
            try:
                data = json.loads(text[len(_GRANULARITY_TAG_PREFIX):])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if isinstance(data, dict):
                return data
    return None


def find_anchor_by_location(
    location_key: str, era_anchor: str, scenes: list,
) -> str | None:
    """在已有场景里找同一 location_key+era_anchor 的锚点场景（确定性、零模型
    判断的第二道防线——即使某次模型调用没能正确识别出这是已有场景，这里仍能按
    结构化标签拦下重复建库）。只比对带粒度标签的场景，旧场景没有标签时视为不可
    比对，不参与匹配（宁漏勿误：不替旧数据臆造归属）。"""
    target_key = _normalize_key(location_key)
    target_era = _normalize_key(era_anchor)
    if not target_key:
        return None
    for scene in scenes:
        tag = decode_granularity_tag(list(getattr(scene, "discovery_sources", None) or []))
        if not tag or tag.get("role") != ROLE_ANCHOR:
            continue
        if _normalize_key(str(tag.get("location_key") or "")) != target_key:
            continue
        if _normalize_key(str(tag.get("era_anchor") or "")) != target_era:
            continue
        return str(getattr(scene, "name", "") or "") or None
    return None


def resolve_existing_anchor_name(
    *, location_key: str, era_anchor: str, existing_scene_name: str, scenes: list,
) -> str | None:
    """这条判定该并入哪个已有锚点场景（不建新场景/不单独出图时用）：结构化
    location_key+era_anchor 命中优先于模型自报的 existing_scene_name——前者是
    确定性比对，后者是模型这次调用的自由判断，两者都找不到才返回 None（调用方
    应据此区分"没有可挂靠的场景，本来就不需要独立画面"与"AI 未能给出可信解析"，
    不得混为一谈，见 app.scenes.ensure_scenes_for_storyboard 的用法）。"""
    by_location = find_anchor_by_location(location_key, era_anchor, scenes)
    if by_location:
        return by_location
    known_names = {str(getattr(scene, "name", "") or "") for scene in scenes}
    name = (existing_scene_name or "").strip()
    return name if name and name in known_names else None
