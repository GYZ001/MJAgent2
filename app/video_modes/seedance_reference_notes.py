"""Seedance 参考图编号引用与中文用途说明（从 app.video_modes.seedance_pack
拆出，2026-09-03）。

拆出原因：seedance_pack.py 已在 app/FILE_CONVENTIONS.toml 的 line_count 棘轮
baseline 里（523 行，零余量），本次改造（正文 @角色名 确定性替换成 @图片N、
参考图用途说明从英文改中文并放在正文之后）要新增逻辑，这块逻辑本身只依赖
packed_refs 与 prompt_text，是纯函数，不依赖 seedance_pack.py 的任何私有
状态，天然可独立成模块。

对照 Seedance 2.0 官方指南（dexhunter/seedance2-skill 与
ambienceai.com/tutorials/seedance-prompting-guide）：参考图要用编号
`@图片N` 引用、并对每张图的用途给出明确说明，不要用人名做 @ 前缀再让模型
自己去猜哪张图对应谁；附加说明的语言也要跟正文一致（中文正文配中文说明），
不要在纯中文段落后面拼一段英文注释。

与 app.minimax_h3._tagged_prompt 的耦合（2026-09-03 修复背景里未列出、
实测发现的隐藏依赖）：这份说明文本是 Seedance 与 MiniMax H3 两个方言共用的
一份——app.media_exec.input_reference 在两种方言都适用的公共阶段统一调用
本模块生成，H3 适配器随后按“图片N”这个锚点把它替换成 `<Picture N>`。把
锚点从英文 "Reference image N" 改成中文 "图片N" 后，app/minimax_h3.py 里
匹配这个锚点的正则、以及判断“<Picture N> 是否已经在正文里出现过”的去重
正则（原来只认半角冒号）必须同步改，否则 H3 路径的 `<Picture N>` 替换会
静默失效、或去重失灵导致重复插入——这正是 CLAUDE.md「配套参数必须一起
传递」要挡的那类故障，已同步修改 app/minimax_h3.py 两处正则并补测试。
"""
from __future__ import annotations

import re
from typing import Any

from app.scene_reverse import evidence as reverse_evidence

REFERENCE_PROMPT_NOTE_MARKER = "参考图说明："
REFERENCE_SINGLE_INSTANCE_NOTE = (
    "参考图只用来锁定身份与环境外观；每个具名角色在画面里只出现一次。"
)

_TYPE_PURPOSE_ZH: dict[str, str] = {
    "character": "角色{who}的人物参考，只用来锁定长相与服装",
    "character_no_name": "人物参考，只用来锁定长相与服装",
    # 中性身份定妆照专用说明（app.portraits.neutral_identity，2026-09-30）：参考图
    # 本身不再烧服装/表情，服装与表情改由本段文字正面给出；老文案（上两条）逐字
    # 不变，冻结测试锁住，只有 ref["costume_mode"]=="neutral" 才切到这两条。
    "character_neutral": "角色{who}的人物参考，只用来锁定长相、发型与体型，服装和表情以本段文字为准",
    "character_neutral_no_name": "人物参考，只用来锁定长相、发型与体型，服装和表情以本段文字为准",
    # 头像照（定妆照头部裁切）专用说明（2026-10-02，Seedance 对图生图产出的人物
    # 头像——不论单张近景还是曾经试过的 3×3 九宫格——一律真人隐私误判拒收后，
    # 改为从全身定妆照纯像素裁切，见 app.portraits.headshot_crop）：显式说明"这是
    # 定妆照的头部裁切、只锁长相发型"，服装/表情交给正文与服装道具参考。
    # ``costume_mode=="neutral"`` 且 ``view_role=="face_closeup"`` 才切到这两条；
    # 只满足 costume_mode==neutral（例如中性身份 front_full）仍用上面两条
    # character_neutral 文案，不受影响。
    # 2026-10-03：原文案「只用来锁定长相与发型」让模型按裁切图下沿锁发型——裁切在
    # 下巴附近，垂到胸前的长发被裁掉，第 1 集第 17 段温念因此整段画成齐下巴短发
    # （正文写着「长发垂到胸前」）。改为只锁长相/发色/刘海，长度与整体发型以正文为准。
    "character_headshot_crop": (
        "角色{who}的头像参考（定妆照头部裁切），只用来锁定长相、发色与刘海；头发长度与"
        "整体发型以正文为准（参考图在下巴附近裁切，下沿以下的头发被裁掉了，不代表头发"
        "只到这里）；不用于确定服装、表情或画面构图；服装以正文与服装道具参考为准"
    ),
    "character_headshot_crop_no_name": (
        "头像参考（定妆照头部裁切），只用来锁定长相、发色与刘海；头发长度与整体发型以"
        "正文为准（参考图在下巴附近裁切，下沿以下的头发被裁掉了，不代表头发只到这里）；"
        "不用于确定服装、表情或画面构图；服装以正文与服装道具参考为准"
    ),
    "scene": "场景参考，只用来锁定环境外观",
    # 场景状态图（2026-10-02，app.video_modes.scene_state_ensure）：本段场景此刻的
    # 物理状态与场景卡默认状态不一致时，装配期用这张图替代场景卡主图，见
    # app.video_modes.scene_state_assembly.resolve_scene_entry_with_state。
    "scene_state": "场景「{who}」当前状态参考：空间布局、门窗家具位置与此刻的状态（如积水、倒伏、破损）以此图为准；图中没有人物，人物按正文",
    "prop": "道具{who}参考，只用来锁定外观与材质",
    "prop_no_name": "道具参考，只用来锁定外观与材质",
    # 道具拼图（2026-10-03，app.video_modes.prop_composite_pack）：参考图张数超出
    # 上限时，本应被丢弃的道具与最后一个放得下的道具合成一张拼图占用一个槽位。
    # 必须显式说明"这是拼图、按顺序对应哪几件物件"，否则模型会把拼图本身的网格
    # 构图当成画面内容的一部分画进视频（本仓已有画面文字/构图类似闸门拦这类问题，
    # 这里从提示词源头避免制造新的违规来源）。
    "prop_composite": (
        "这是多件道具的外观参考拼图，按从左到右、从上到下依次是：{who}；"
        "画面中只出现这些物件本身，不要出现拼图、分格或白边"
    ),
    "style": "风格参考，只用来锁定画面风格",
    # 与 app.video_plan.prev_frame_reference.PREVIOUS_FRAME_PURPOSE_ZH 同一句（那边有测试锁住），
    # 这里不 import：video_modes 包不能反向依赖 video_plan.generate 所在的包初始化链。
    "previous_shot_frame": "上一段{who}画面参考，只用来锁定场景布局、家具与关键道具的位置和形态；人物的姿势与动作按本段文字描述，不沿用这张图",
}


def _related_names(ref: dict[str, Any]) -> list[str]:
    """与 seedance_pack._reference_identity_names 同一套取名逻辑：优先取
    relatedCharacterIds/related_character_ids，角色类型再补 entity_name；
    场景类型只在反打视角（entity_name 带「·反打」后缀）时补——这是把正文里
    的 @场景名·反打 替换成 @图片N 的唯一入口，主视角场景描述不用 @ 语法，
    不需要这条。"""
    related = [
        str(name).strip()
        for name in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
        if str(name).strip()
    ]
    entity_name = str(ref.get("entity_name") or "").strip()
    ref_type = ref.get("type")
    is_reverse_scene = ref_type == "scene" and ref.get("view_role") == reverse_evidence.REVERSE_ANGLE_VIEW_ROLE
    if (ref_type == "character" or is_reverse_scene) and entity_name and entity_name not in related:
        related.append(entity_name)
    return related


def _plot_key_frame_purpose_zh(ref: dict[str, Any], who: str) -> str:
    who_part = f"{who}的" if who else ""
    beat_index = ref.get("keyframe_index") or "?"
    beat_total = ref.get("keyframe_total") or "?"
    time_ratio = ref.get("keyframe_time_ratio")
    purpose = f"{who_part}关键帧参考，只用来锁定该拍点画面"
    try:
        pct = round(float(time_ratio) * 100)
        purpose += f"（进度约{pct}%，第{beat_index}/{beat_total}拍）"
    except (TypeError, ValueError):
        purpose += f"（第{beat_index}/{beat_total}拍）"
    target = str(ref.get("keyframe_target_desc") or "").strip()
    if target:
        purpose += f"，目标画面：{target}"
    return purpose


def _scene_multi_purpose_zh(ref: dict[str, Any]) -> str:
    """两张及以上场景参考图时的用途说明：写清各自是哪个场景、哪个方向——
    只有一张时维持 ``_TYPE_PURPOSE_ZH["scene"]`` 逐字不变的既有文案（冻结
    特征测试锁住），模型只有在能分清 @图片N 对应哪个场景/方向时才用得上。
    """
    name = str(ref.get("entity_name") or "").strip()
    if ref.get("view_role") == reverse_evidence.REVERSE_ANGLE_VIEW_ROLE and name.endswith(
        reverse_evidence.REVERSE_MENTION_SUFFIX
    ):
        name = name[: -len(reverse_evidence.REVERSE_MENTION_SUFFIX)]
        return f"场景「{name}」参考，只用来锁定环境外观（与主视角相对方向的反打视角）"
    return f"场景「{name}」参考，只用来锁定环境外观（主视角）"


def _character_purpose_key(has_name: bool, costume_mode: Any, view_role: Any = None) -> str:
    """选人物参考图用途说明的字典 key：``costume_mode=="neutral"``（见
    app.portraits.neutral_identity）切到中性文案，其余任何值（含老数据没有
    这个字段的 None）都是老文案，逐字不变。``view_role=="face_closeup"``
    （定妆照头部裁切，2026-10-02）在此基础上再细分一档——它与中性身份
    front_full 共用 costume_mode=="neutral"，但图片内容不同（头部裁切 vs 单张
    全身），必须有独立说明。"""
    neutral = costume_mode == "neutral"
    if neutral and view_role == "face_closeup":
        return "character_headshot_crop" if has_name else "character_headshot_crop_no_name"
    if has_name:
        return "character_neutral" if neutral else "character"
    return "character_neutral_no_name" if neutral else "character_no_name"


def _reference_purpose_zh(ref: dict[str, Any], *, scene_count: int = 1) -> tuple[str, list[str]]:
    """返回 (这张参考图的中文用途说明, 它绑定的具名人物/场景列表)。

    ``scene_count``：本次打包的场景类参考图总数，只有两张及以上（主视角 +
    被点名的反打视角）才需要写清各自是哪个场景、哪个方向。
    """
    ref_type = str(ref.get("type") or "reference")
    related = _related_names(ref)
    who = "、".join(related)
    if ref_type == "plot_key_frame":
        return _plot_key_frame_purpose_zh(ref, who), related
    if ref_type == "character":
        template = _TYPE_PURPOSE_ZH[_character_purpose_key(bool(who), ref.get("costume_mode"), ref.get("view_role"))]
    elif ref_type == "prop" and ref.get("view_role") == "prop_composite":
        template = _TYPE_PURPOSE_ZH["prop_composite"]
        who = "、".join(
            str(name).strip() for name in (ref.get("composite_member_labels") or []) if str(name).strip()
        )
    elif ref_type == "prop":
        template = _TYPE_PURPOSE_ZH["prop" if who else "prop_no_name"]
    elif ref_type == "scene" and ref.get("view_role") == "scene_state":
        # 状态图不受「只有一张场景图才用通用文案」限制：无论本次打包的场景图
        # 是一张（状态图替代主图）还是两张（+ 反打），都要点名哪张图是状态图。
        template = _TYPE_PURPOSE_ZH["scene_state"]
        who = str(ref.get("entity_name") or "").strip()
    elif ref_type == "scene" and scene_count > 1:
        return _scene_multi_purpose_zh(ref), related
    else:
        template = _TYPE_PURPOSE_ZH.get(ref_type, f"{ref_type}参考")
    return template.format(who=who), related


def _sub_at_mentions(
    body: str, ordered_names: list[str], named_indices: dict[str, int], boundary: str,
) -> str:
    """按 ``ordered_names``（已按长度降序排好）与 ``boundary`` 结尾边界，把匹配到的
    @名字 替换成 @图片N。EP1 重跑实测：模型从第 5 段起把 @李麦麦 写成了 @bible:李麦麦
    （identity_id 前缀漏进正文），精确匹配 @名字 全部落空，Seedance 拿到的是一串无绑定
    的 @bible:xxx。可选的「字母:」前缀一并吃掉，替换结果仍是 @图片N。"""
    if not ordered_names:
        return body
    pattern = re.compile("@(?:[A-Za-z_]+:)?(" + "|".join(re.escape(name) for name in ordered_names) + ")" + boundary)
    return pattern.sub(lambda m: f"@图片{named_indices[m.group(1)]}", body)


def _replace_at_mentions_with_picture_numbers(
    body: str, named_indices: dict[str, int],
) -> str:
    """把正文里完全匹配的 @名字 确定性替换成 @图片N，名字后面的空格/标点
    原样保留——只替换 "@名字" 这一段本身。按名字长度降序建正则候选：更长
    的名字先参与匹配，避免短名字先命中、把长名字截断成"短名字+残留字符"。

    结尾边界分两档、且反打名字先处理（避免裸名字那一档抢先部分命中反打名字的前缀）：
    ① 带「·反打」后缀的反打点名用只排除 ASCII 字母/数字/下划线的宽松边界——它是从
    relevant_assets 逐字取用的封闭集合，中文散文里紧跟在它后面直接续写不加分隔符是
    常态（例如"@修表铺·反打门口回望"，"打"后面紧跟"门"没有空格），用 Python re 默认按
    Unicode 匹配、汉字也算 \\w 的边界会把这类完全合法、无歧义的点名一并挡在外面，
    整个 @ 点名原样残留、一个能替换的候选都没有（2026-09-27 审查实测复现）。
    ② 其余（不带反打后缀的）裸名字仍用严格边界（汉字续写也算越界），因为裸名字后面
    紧跟的汉字可能是原文里另一个未登记的词的一部分而不是这个名字本身——
    tests/test_segment_identity_contract.py::
    test_exact_image_subject_token_does_not_match_a_name_prefix 钉住这条：
    "@孟浩同门"里的"孟浩"不能被误当成登记名"孟浩"命中，"孟浩同门"是原文另一个
    未登记的词，不是"孟浩"本人；反打点名没有这层歧义，因为它必须完整写出「场景名+
    ·反打」这个封闭组合，不存在"其实是更长的未登记反打词"的可能。
    """
    if not named_indices:
        return body
    reverse_names = [n for n in named_indices if n.endswith(reverse_evidence.REVERSE_MENTION_SUFFIX)]
    plain_names = [n for n in named_indices if n not in reverse_names]
    body = _sub_at_mentions(
        body, sorted(reverse_names, key=len, reverse=True), named_indices, r"(?![A-Za-z0-9_])",
    )
    return _sub_at_mentions(body, sorted(plain_names, key=len, reverse=True), named_indices, r"(?![\w])")


def _compose_purposes(packed_refs: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    purposes: list[str] = []
    named_indices: dict[str, int] = {}
    scene_count = sum(1 for ref in packed_refs if str(ref.get("type") or "") == "scene")
    for idx, ref in enumerate(packed_refs, 1):
        purpose, related = _reference_purpose_zh(ref, scene_count=scene_count)
        purposes.append(f"图片{idx}：{purpose}")
        for name in related:
            named_indices.setdefault(name, idx)
    return purposes, named_indices


def _demote_residual_reverse_mentions(text: str) -> str:
    """没换成 @图片N 的 ``@场景名·反打``（反打图这次没装进请求：开关关闭、证据
    失效、文件丢失、用户整段改写过提示词）降级成普通文字，不把没有对应图片的
    @ 点名发给视频供应商。"""
    suffix = reverse_evidence.REVERSE_MENTION_SUFFIX
    residual = reverse_evidence.unmatched_reverse_mentions(text, ())
    return reverse_evidence.demote_reverse_mentions(text, [m[1:-len(suffix)] for m in residual])


def build_seedance_reference_prompt_notes(
    prompt_text: str,
    packed_refs: list[dict[str, Any]],
    *,
    duration_s: float | int | None = None,
    aspect_ratio: str,
) -> str:
    """给 prompt_text 做两件事：① 正文里完全匹配的 @角色名/@场景名替换成
    @图片N；② 在正文之后追加一段中文参考图用途说明。没有任何参考图时
    （text-only 回退）原样返回，正文里的 @名字 不受影响。marker 幂等：
    已经带过说明的 prompt 不重复加。``aspect_ratio`` 必传（本次任务的版本
    meta 快照，老任务无快照按 "9:16" 兜底）——省略会让 ``_split_video_args``
    的画幅还原逻辑无值可用。"""
    from app.compiler import _split_video_args

    if REFERENCE_PROMPT_NOTE_MARKER in prompt_text:
        return prompt_text
    prompt_body, prompt_args = _split_video_args(prompt_text, duration_s, aspect_ratio=aspect_ratio)
    purposes, named_indices = _compose_purposes(packed_refs)
    if not purposes:
        return _demote_residual_reverse_mentions(prompt_text)
    prompt_body = _demote_residual_reverse_mentions(_replace_at_mentions_with_picture_numbers(prompt_body, named_indices))
    purpose_list = "；".join(purposes) + "；" + REFERENCE_SINGLE_INSTANCE_NOTE
    if prompt_body.startswith("subject_definitions:\n"):
        heading, body = prompt_body.split("\n", 1)
        return heading + "\n" + purpose_list + "\n" + body + prompt_args
    note = REFERENCE_PROMPT_NOTE_MARKER + "\n" + purpose_list
    return prompt_body + "\n" + note + prompt_args


AUDIO_REFERENCE_NOTE_MARKER = "声音参考："


def _audio_note_part(item: dict[str, Any]) -> str:
    """单条参考音频的说明句；``role="narrator"`` 时措辞对应
    ``app.production.storyboard_speech_render.rendered_utterance`` 给旁白声道
    标签写的「旁白（{name}的声音）」——两处文案共用同一个「{name}的声音」短语，
    模型才能把这条参考音频与提示词正文里的旁白台词对上。
    """
    name, idx = item.get("character_name"), item.get("index")
    if item.get("role") == "narrator":
        return f"@音频{idx} 是{name}的声音，本段标注为「旁白（{name}的声音）」的台词都用 @音频{idx} 的音色和说话方式说出"
    return f"@音频{idx} 是{name}的声音，{name}的每一句台词都用 @音频{idx} 的音色和说话方式说出"


def _compose_audio_note(reference_audios: list[dict[str, Any]]) -> str:
    parts = [_audio_note_part(item) for item in reference_audios]
    return AUDIO_REFERENCE_NOTE_MARKER + "；".join(parts) + "。参考音频只提供音色，台词内容以本段剧本为准。"


def append_audio_reference_note(
    prompt_text: str, reference_audios: list[dict[str, Any]], *, aspect_ratio: str,
) -> str:
    """给已经追加过图片参考说明的 prompt_text 再追加一段声音参考说明（U3，角色固定
    音色视频请求接入）。

    独立于 ``build_seedance_reference_prompt_notes``——那个函数的 marker 幂等检查
    一旦命中就整体短路返回，没法在"已经加过图片说明"的 prompt 上再补一段；这里
    用同一套「剥离尾部 --ratio/--dur → 追加 → 还原」手法单独处理，marker 换成
    ``AUDIO_REFERENCE_NOTE_MARKER``，两条说明互不影响彼此的幂等判定。没有音频、
    或说明已经加过时原样返回。编号按 ``reference_audios`` 已排定的顺序（content
    里音频出现的顺序），不重排。
    """
    if not reference_audios or AUDIO_REFERENCE_NOTE_MARKER in prompt_text:
        return prompt_text
    from app.compiler import _split_video_args  # 没有音频时上面已提前返回，避免白付这次导入

    body, args = _split_video_args(prompt_text, None, aspect_ratio=aspect_ratio)
    return body + "\n" + _compose_audio_note(reference_audios) + args
