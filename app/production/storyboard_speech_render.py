"""从台词合同生成唯一声道标签；新分镜用占位符保留模型安排的发声时机。

2026-09-30 口型标注去重（B 机 provider_calls id=81314，第 1 集第 20 段 opus 原始
输出逐字核实）：``……先停顿一拍，再压低声音连贯快速地答：{{speech:U02}}（发声者
开口，其他可见人物不跟随口型）光影：台灯暖黄光……``——模型在占位符**紧后**自己
又写了一遍系统本该生成的口型标注，而 ``dialogue[].line`` 里 U02 的原话是干净的
「没什么，工作上的旧资料。」，不含任何标注；``render_segment_speech`` 展开占位符
时只替换 ``{{speech:U02}}`` 这四个字本身，模型紧跟着写的那份标注原样留在旁边，
``rendered_utterance`` 又在展开结果末尾追加一份，两份紧挨着，对应一条台词只该
有一个。dialect 指令要求模型只写占位符、原话只写进 ``dialogue[].line``，不要
自己写出口型标注（见 ``storyboard_dialects`` 「系统从同一合同展开中文声道标签、
原话和口型要求」），但模型不总是照办——跟 ``storyboard_cast_lock`` 的人数锁定句
同一种失败模式。标注是本函数自己定义的两个固定短语之一（开口/闭口二选一，见
``_MOUTH_ANNOTATIONS``），不是对模型自由文本的开放式语义判断，与
``storyboard_music_bed._MUSIC_CLAUSE_RE`` 对「配乐」固定关键字同一先例；改法：
展开占位符时把占位符本身与它**紧后**若已跟着的同一条系统标注（不论是展开结果
该有的那条、还是模型写错的另一条）作为同一个替换单元一起消费掉，只留
``rendered_utterance`` 按 ``delivery_kind`` 生成的那一份——见
``_TOKEN_WITH_TRAILING_MOUTH_RE``。
"""
import logging
import re
from typing import Any

from app import textmatch
from app.production.storyboard_identity_contract import effective_delivery_kind

SPEECH_TOKEN = re.compile(r"\{\{speech:([A-Za-z0-9_-]+)\}\}")

#: 本函数自己定义、会追加进提示词的两条固定口型标注——开口（spoken_dialogue）
#: 与闭口（其余声道）二选一，是封闭集合，不是对模型自由文本的关键词猜测。
_MOUTH_ANNOTATIONS = ("发声者开口，其他可见人物不跟随口型", "画面人物嘴唇闭合无张合动作")

#: 占位符 + 紧跟其后、模型自己写的同一条系统标注（开口或闭口任一种，不论是否与
#: 这条台词实际的 delivery_kind 匹配）——两者作为一个替换单元一起被
#: ``render_segment_speech`` 消费掉，换成 ``rendered_utterance`` 按
#: delivery_kind 生成的唯一一份；模型没有紧跟着写标注时，可选组匹配空串，行为
#: 与旧版逐字相同。用来识别「模型自写标注」的字符串就是系统自己会生成的那两条
#: 固定短语，不是另立词表（见模块 docstring 2026-09-30 条）。
_TOKEN_WITH_TRAILING_MOUTH_RE = re.compile(
    SPEECH_TOKEN.pattern + "(?:（(?:" + "|".join(re.escape(a) for a in _MOUTH_ANNOTATIONS) + ")）)?"
)

log = logging.getLogger(__name__)


def remove_draft_utterance(draft: Any, line: Any) -> None:
    """删除一条发声时同步清理其模板位置，保留其它声音和镜头动作。"""
    token = "{{speech:" + str(getattr(line, "utterance_id", "")) + "}}"
    for field in ("prompt_text", "speech_template"):
        prompt = getattr(draft, field, "")
        if not prompt:
            continue
        if token in prompt:
            setattr(draft, field, prompt.replace(token, ""))
        elif not SPEECH_TOKEN.search(prompt) and line.line:
            quoted = r'[「“『"]' + re.escape(line.line) + r'[」”』"]'
            prompt = re.sub(r"(?:画外音（[^）]*）|旁白)\s*[：:]?\s*" + quoted + r"[。；;，,]?", "", prompt)
            prompt = re.sub(quoted + r"[。；;，,]?", "", prompt).replace(line.line, "")
            setattr(draft, field, re.sub(r"(?:[；;]\s*){2,}", "；", prompt))


def speaker_names(segment: dict) -> dict[str, str]:
    names = {"旁白": "旁白"}
    for entry in (segment.get("resources") or {}).get("characters") or []:
        identity = str(entry.get("identity_id") or "")
        names[identity] = str(entry.get("display_name") or identity.split(":", 1)[-1])
    return names


def rendered_utterance(
    line: dict, names: dict[str, str], *, dialect: str, narrator_voice_character: str = "",
) -> str:
    """``narrator_voice_character`` 非空且本句是旁白（``narration``）时，声道标签
    括号里写成「{角色}的声音」而不是字面量「旁白」（例如「旁白（温念的声音）」）——
    项目设置了旁白固定音色角色时，让分镜提示词与视频请求里挂的参考音频对得上（见
    app.voice.segment_refs._with_narrator_entry 与
    app.video_modes.seedance_reference_notes._audio_note_part，三处共用同一个
    「{name}的声音」短语）。空串（默认，未设置）时逐字不变。这个覆盖只影响本函数
    渲染出的自由文本，不改变 ``names``/``speaker_names()`` 本身的身份映射——
    字幕（app.subtitles.episode）与发声者校验都直接读 ``speaker_names()``，不受
    影响。
    """
    speaker = names.get(str(line.get("speaker_identity_id") or ""), str(line.get("speaker_identity_id") or ""))
    kind = effective_delivery_kind(line)
    if kind == "narration" and narrator_voice_character:
        speaker = f"{narrator_voice_character}的声音"
    label = {"spoken_dialogue": "画内对白", "offscreen_dialogue": "人物画外对白", "inner_monologue": "内心独白", "narration": "旁白"}[kind]
    if dialect == "minimax_h3_native_fields":
        return _h3_utterance(line, names, speaker=speaker, kind=kind)
    mouth = _MOUTH_ANNOTATIONS[0] if kind == "spoken_dialogue" else _MOUTH_ANNOTATIONS[1]
    sentences = re.findall(r".*?(?:[。！？!?]+|[.]+(?=\s|$)|$)", str(line.get("line") or ""), re.S)
    quoted = "".join(f"“{sentence}”" for sentence in sentences if sentence)
    return f"{label}（{speaker}）：{quoted}（{mouth}）"


def _h3_utterance(line: dict, names: dict, *, speaker: str, kind: str) -> str:
    """保留 H3 既有稳定 S 编号、<d> 原话块和英语描述合同。"""
    number = sorted(names).index(str(line["speaker_identity_id"])) + 1
    delivery = {"spoken_dialogue": "says", "offscreen_dialogue": "says in an off-screen voiceover",
                "inner_monologue": "says in an inner-monologue voiceover", "narration": "narrates in an off-screen voiceover"}[kind]
    mouth = "Only the speaker's lips move" if kind == "spoken_dialogue" else "All on-screen lips remain fully closed with no movement"
    return f"(S{number}, {speaker}) {delivery}: <d>[Chinese] {line.get('line') or ''}</d>. {mouth}."


def speech_template_errors(segment: dict, *, require_tokens: bool) -> list[str]:
    prompt = str(segment.get("prompt_text") or "")
    lines = segment.get("dialogue") or []
    tokens = SPEECH_TOKEN.findall(prompt)
    if not require_tokens and not tokens:
        return explicit_prompt_speaker_errors(segment)
    ids = [str(line.get("utterance_id") or "") for line in lines]
    errors = []
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", identity) for identity in ids) or len(set(ids)) != len(ids):
        errors.append("每条台词须有唯一 utterance_id（如 U01），包含字母、数字、下划线或连字符")
    if sorted(tokens) != sorted(ids):
        errors.append("prompt_text 须在每句发声时机写一次 {{speech:utterance_id}}，与 dialogue[] 一一对应")
    if any(str(line.get("line") or "") in prompt for line in lines if line.get("line")):
        errors.append("台词原话保存在 dialogue[].line；prompt_text 对应位置使用 speech 占位符，防止同一句发声两次")
    return errors


def render_segment_speech(segment: dict, *, dialect: str, narrator_voice_character: str = "") -> dict:
    """占位符按合同一次性展开，并保留模板供以后修订与审计。``narrator_voice_character``
    见 ``rendered_utterance`` 文档；默认空串＝改动前行为逐字不变。展开时把它与
    ``speech_dialect`` 一起写回 ``segment``（``_AiStoryboardSegmentDraft.
    narrator_voice_character`` 字段），这样任何只拿到 ``segment`` 本身、没有 conn/
    project_id 的重渲染再比对（``explicit_prompt_speaker_errors``、台词人工修订）
    都能复现当次生成实际用的旁白音色，不必外部传参、也不会读到项目设置之后被
    改动的新值。2026-09-30 用 ``_TOKEN_WITH_TRAILING_MOUTH_RE`` 代替裸
    ``SPEECH_TOKEN`` 做替换：模型偶尔在占位符后面紧跟着自己把口型标注也写一遍
    （见模块 docstring），只替换占位符本身会把这份模型自写的标注原样留在旁边，
    与紧接着追加的 canonical 标注重复；连同紧邻的同款标注一起替换掉，保证展开
    结果里每条台词只有一份标注。"""
    template = str(segment.get("speech_template") or segment.get("prompt_text") or "")
    if not SPEECH_TOKEN.search(template):
        return segment
    names = speaker_names(segment)
    by_id = {
        line["utterance_id"]: rendered_utterance(line, names, dialect=dialect, narrator_voice_character=narrator_voice_character)
        for line in segment.get("dialogue") or []
    }
    segment["speech_template"] = template
    segment["speech_dialect"] = dialect
    segment["narrator_voice_character"] = narrator_voice_character
    segment["prompt_text"] = _TOKEN_WITH_TRAILING_MOUTH_RE.sub(lambda m: by_id[m.group(1)], template)
    return segment


def _pair_label(line_text: str, labels: list[tuple[str, str]], all_lines: list[dict]) -> str | None:
    """给一条台词找出它在提示词里唯一对应的声道标签；配不唯一就返回 None。

    2026-09-19 真实误报（ERR-20260919-5a4c99，龙猫出爪 EP6 段 9）：原实现直接拿
    ``condense`` 后的文本两两相等来配对，而 ``condense`` 会把标点一起去掉——
    一问一答复述同一个词时，「……八折的？」（周晚）与「八折的。」（小李）归一后都是
    「八折的」，于是两句交叉配对、双双报冲突，而数据本身完全正确。这类台词在对话戏里
    很常见，全库当时有 3 段中招（另两段是「善」关羽/刘备、「签了」豪尔赫/里奥）。

    误报比漏报贵得多：漏掉一个发声者写错，人在成片里听得出来；误报直接阻断正确产出，
    而且按提示改不动——数据本来就是对的。所以配不唯一时不判。

    顺序：① 原文精确匹配（保留标点，一步分开问句与答句）；② 匹配不上再用 condense
    兜住真排版差异（全角半角、多余空格）；③ 归一匹配必须**双向唯一**——这条台词只命中
    一个标签，且那个标签也只对应这一条台词，否则算歧义，不判。
    """
    exact = [actual for actual, spoken in labels if spoken == line_text]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # 同一句在提示词里出现多次，无从判断说的是哪一次
    target = textmatch.condense(line_text)
    if not target:
        return None
    loose = [(actual, spoken) for actual, spoken in labels if textmatch.condense(spoken) == target]
    if len(loose) != 1:
        if loose:
            log.info("发声者校验跳过：台词 %r 归一后命中 %d 个声道标签，无法唯一配对", line_text[:20], len(loose))
        return None
    back = [one for one in all_lines if textmatch.condense(str(one.get("line") or "")) == target]
    if len(back) != 1:
        log.info("发声者校验跳过：归一文本 %r 对应 %d 条台词，无法唯一配对", target[:20], len(back))
        return None
    return loose[0][0]


def explicit_prompt_speaker_errors(segment: dict) -> list[str]:
    """校验提示词里的发声者与台词合同是否一致。

    **有 speech_template 时只走模板判据，不做文本配对**（2026-09-19）：模板判据是
    「按合同重新展开一次、与成品逐字比对」，任何发声者错位都会让展开结果对不上，
    它对这件事是完备的。再叠一层文本配对不提供额外保证，只贡献歧义误报——
    ERR-20260919-5a4c99 就是这么来的：两个判据同时跑，弱的那个先报错，而数据本身
    完全正确。这是「有的门禁多余」的具体一处，不是判据不够。

    没有模板的旧产物才退到显式声道标签的文本配对（见 _pair_label 的双向唯一规则）。
    """
    prompt = str(segment.get("prompt_text") or "")
    if segment.get("speech_template"):
        template_errors = speech_template_errors(
            dict(segment, prompt_text=segment["speech_template"]), require_tokens=True)
        if template_errors:
            return template_errors
        candidate: dict[str, Any] = dict(segment)
        render_segment_speech(
            candidate, dialect=str(segment.get("speech_dialect") or ""),
            narrator_voice_character=str(segment.get("narrator_voice_character") or ""),
        )
        if candidate["prompt_text"] != prompt:
            return ["提示词与已保存的发声模板/台词合同不同，请重新生成该片段的提示词"]
        return []
    labels = re.findall(r'(?:画外音|画内对白|人物画外对白|内心独白|旁白|on-screen dialogue|off-screen voiceover|inner monologue|narration)[（(]([^）)]+)[）)]\s*[：:]\s*[「“『"]([^」”』"]+)[」”』"]', prompt)
    names = speaker_names(segment)
    errors = []
    all_lines = list(segment.get("dialogue") or [])
    for line in all_lines:
        expected = names.get(str(line.get("speaker_identity_id") or ""), str(line.get("speaker_identity_id") or ""))
        line_text = str(line.get("line") or "")
        actual = _pair_label(line_text, labels, all_lines)
        if actual is not None and actual != expected:
            errors.append(f"台词『{line_text[:20]}』的提示词发声者「{actual}」与台词合同「{expected}」不同，请修订该片段")
    return errors


def attach_quote_provenance(segment: dict) -> None:
    """以来源段号和唯一原话匹配补充引用偏移；重复原话有歧义时要求模型给 quote_id。"""
    for line in segment.get("dialogue") or []:
        candidates = [q for q in segment.get("required_dialogue") or [] if q.get("source_segment_index") == line.get("source_segment_index") and textmatch.condense(str(q.get("text") or "")) == textmatch.condense(str(line.get("line") or ""))]
        if line.get("source_quote_id"):
            candidates = [q for q in candidates if q.get("quote_id") == line["source_quote_id"]]
        if len(candidates) == 1:
            quote = candidates[0]
            line.update(source_quote_id=quote["quote_id"], source_start=quote.get("source_start", -1), source_end=quote.get("source_end", -1))
            line["attribution_evidence"] = str(quote.get("note") or quote.get("speaker") or "原文未点名说话人")
