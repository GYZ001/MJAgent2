"""分镜台阶段二方言约束（从 app.production.storyboard_pack 拆出，2026-09-01）。

拆出原因：storyboard_pack.py 已在 app/FILE_CONVENTIONS.toml 的 line_count 棘轮
baseline 里（2087 行），本次改造（对白台账/必保台词/受控画外音/镜头数放宽）要
往那个文件加新逻辑，必须先腾出等量空间——两段方言指令文本块 + 方言解析函数
本身不依赖阶段一/阶段二任何私有状态，是天然可独立成模块的一块，纯搬移+改
方言文案本身的内容（对话镜头数 3-4→2-4、补画外音写法），不改其余任何行为。

对照 docs/STORYBOARD_PROMPT_IR_DESIGN.md 的对照表与
docs/prompt-skills/{novel-to-storyboard,minimax-h3-prompts}/。H3 的字段名与
固定语法是接口约定，逐字符照抄；Seedance 是自由散文，按同一精神收窄成可执行
规则，不是逐字抄 skill 原文。
"""
from __future__ import annotations

from app import config
from app.video_prompt_profiles import VideoPromptProfile

SEEDANCE_DIALECT_INSTRUCTIONS = f"""\
目标模型：Seedance 2.0（中文自由散文，一整块可直接复制的提示词，不要拆成
JSON 字段或分点罗列）。

- 第一句必须是「电影级预告片质感，多镜头叙事，镜头之间硬切。」——这是触发
  15 秒档多镜头模式的固定锚句，照写，不得省略或改写。
- 用「镜头1（约0-X秒）」「镜头2（约X-Y秒）」……序号排列，本段 2-4 镜：对话
  交锋段（正反打、连续对切）2 镜就够，需要交代更多空间调度或信息量的叙事
  推进段用 3-4 镜；括号里的秒数只是软提示，不是精确切点，不要为了卡秒数
  牺牲镜头数。
- 每个镜头描述顺序：一个运镜（推近/拉远/横摇/固定/跟随/环绕，只选一个，不
  要复合运镜）→ 主体（用 @角色名 引用）→ 一个具体动作 → 场景 → 光影。
- 角色的外观锚点在本段里只完整写一次，写在这个角色第一次出现的那个镜头。
  relevant_assets 里每个角色/场景都带一个外观/场景字段（角色是 appearance，
  场景是 scene_canonical）：内容是一段具体描述时，那就是这个角色/场景在本集
  的标准锚点，第一次出现时必须逐字沿用这段描述本身，不得改写、精简、替换或
  按本段情境调整；内容是「没有标准外观/场景……」这类说明文字时，才由你自行
  确定至少三个可视觉验证的特征（年龄区间、体型脸型、发型头饰、服装颜色材质、
  随身物），并让同一角色/场景在本集所有出现它的镜头里沿用同一套自定特征。
- 该角色此后的每一个「镜头N」都要重新 @点名，后面只跟一到两个连续性元素
  （头巾、伤疤、随身物、衣服主色这类跨镜头必须不变的东西），不要把整段外观
  锚点再抄一遍。请求发给视频模型时会附上这个角色的人物参考图，并写明这张图
  绑定到 @名字 上，身份和长相由图负责，文字只负责这一镜他在做什么。实测把
  整段外观每镜重抄一遍的后果：同一段里同一套外观出现 22 次，段落被撑到 1600
  字，动作和对白反而被挤到模型注意力之外。
- 上面这条「@点名即可、长相交给图」只对 relevant_assets.characters 里收录的
  角色成立——有参考图的正是这些人。本段还会出现只在原文里有个称谓、
  relevant_assets.characters 查不到的人（路人、家眷、随从这类）：他们没有任何
  参考图，@名字 绑不到东西，长相就此没有来源。这类人不要用 @ 前缀，直接写
  称谓本身，并且在每一个出现他的镜头里都带上同一套三项以上可视觉验证的特征
  （年龄区间、体型脸型、发型头饰、服装颜色材质、随身物里挑），靠文字自己把
  长相钉住。判据只看 relevant_assets.characters 里查不查得到这个人，不看他
  戏份多少。
- 情绪一律写成面部肌肉动作和肢体动作（例如「眉毛拧起、嘴大张、眼睛瞪圆」），
  不写抽象情绪词（「惊恐」「释然」这类词模型没有稳定映射）。每个镜头挑一个
  核心表演加一个关键动作就够——「喉结滚动＋眉头越皱越紧＋眼眶泛红＋下颌绷紧」
  四个微表情挤进同一个 4 秒镜头，模型哪个都做不完整。
- 承担叙事功能的关键道具靠景别和主体锁住，写成能直接照着画的构图，例如
  「中近景，画面里只有 @孟浩 一人，双手捧着深褐色干葫芦」，而不是只写道具
  被作用的动作（「玉佩砸入水面」会让道具直接消失在水花里）。一个镜头锁一件
  道具就够，每镜都喊一遍「始终清晰可见」会把真正要紧的那件稀释掉。
- 群像要正向锁人数并加负向排除，例如「画面中只有两名绿袍修士，不出现其他
  人物」，两句缺一都会导致模型自己加人。
- 神通/异能等超自然效果用物理描述代替文化词（「化作长虹」→「一道细长银白
  光带以极高速度横穿画面并留下拖影」）。
- 若这是全片收尾段，最后一镜必须是大远景或缓慢升起拉远的格局镜，不能停在
  人物中近景上。
- 台词分「画内说话」与「画外音」两种，对应结构化产出 dialogue[] 每一条的
  delivery 字段：角色在画面里张嘴说出的台词填 spoken_dialogue（默认值，不用
  特意声明）；不需要本人张嘴、由角色声音外化说出的内容（内心独白外化、
  因果/动机/关键设定的旁白性交代）填 offscreen_voice。写画外音时，「全片
  贯穿」段落的音频描述里要单独标注「画外音（角色名）：『……』」；如果这一镜
  画面里同时出现这个角色，镜头描述必须写清他的嘴唇没有张合动作（口型不随
  台词变化），不能让观众以为他正在开口说话。
- 本段 required_dialogue 给出的台词是上一阶段已经按 15 秒容量分配好的必保
  台词，必须逐句全部说出、写进 dialogue[] 与 prompt_text，不得因为篇幅紧张
  自行取舍或省略；只有当你还想在这些必保台词之外再补充原文里的其它对话、
  而本段容量确实装不下全部时，才需要自己取舍——优先保留一到两句最要紧的，
  其余改用画面交代（张嘴又闭上、摇头、把东西递过去、转身就走），留给后面的
  段落。
- 结尾必须有一段「全片贯穿：音频……；风格……；约束……」，音频（环境音/对白/
  配乐）不能留空，约束里必须包含「面部一致、手指正确、人数锁定、无字幕
  水印」。dialogue[] 里的每一句台词都必须在这段音频描述里用引号带出原话、
  逐句出现，不能只写「XX说话声」这类概括；反过来，音频描述里用引号写出的
  台词原话也必须逐句同时登记进 dialogue[]——两处台词是同一份清单的两种
  呈现，不是各自独立的两份内容。
- 本段所有台词加起来不超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 个字（只数
  汉字与字母数字，不数标点和说话人名）。这是 15 秒能说完的物理容量
  （约 {config.SPOKEN_CHARS_PER_5_SECONDS / 5:.1f} 字/秒），不是风格偏好：
  超出的部分模型只能抢读、糊读或整句吞掉，而它吞哪一句你无法预测。
- 画面中任何需要出现的文字（牌匾、书信、标题）一律写「无字」/「空白」，交给
  后期合成——Seedance 对汉字字形的还原极不稳定，这是能力缺失，不是可选项。
  凡是写了「无字」的地方，必须在 degraded_capabilities 里对应记一条后期文字
  合成清单条目（写清载体是什么、原文应该是什么字）。
"""

MINIMAX_H3_DIALECT_INSTRUCTIONS = f"""\
Target model: MiniMax H3. The prompt is exactly three fields, field names
copied character-for-character, each separated by a blank line (T2VA mode --
no image-alignment instruction line, since this project uses reference-image
mode instead of first/last-frame chaining):

integrated_multimodal_description: [Shot 1] <style>, <description>... [Shot 2]
At 00:0X.000, the camera cuts to ...

overall_soundscape: <1-4 English sentences>

non_diegetic_music: <1-3 English sentences, or N/A>

Rules:
- integrated_multimodal_description opens with "[Shot 1]" (no timestamp),
  first declaring the overall style (e.g. "Live-action, cinematic" or
  "2D-animated"), then subsequent shots use "[Shot N] At 00:SS.sss, the
  camera cuts to ..." with strictly increasing timestamps. This segment is a
  fixed 15 seconds; write 2-4 Shots total -- 2 shots is enough for a tight
  dialogue exchange (shot/reverse-shot), use 3-4 shots when the segment needs
  to establish more space or narrative movement.
- Write all descriptive prose in English. Keep dialogue and any on-screen
  text verbatim in their original language -- do not translate them.
- Camera moves are one natural English sentence combining type + amplitude +
  speed (e.g. "The camera pushes in with small amplitude at slow speed
  toward her hands"), not a stack of tags at the end of the sentence. One
  dominant camera move per shot.
- Speaking characters get a stable ID: (S1), (S2)... reused across shots for
  the same character. Put age/voice/accent context outside the <d> block;
  dialogue text goes verbatim inside: (S1) says: <d>[Chinese] 原话</d>.
  Off-screen voice uses "says in an off-screen voiceover" and must state the
  on-screen character's lips remain closed. Set that dialogue line's
  ``delivery`` to ``offscreen_voice`` in the structured output
  (dialogue[].delivery); on-screen spoken lines use the default
  ``spoken_dialogue``.
- required_dialogue lists the lines already budget-allocated to this segment
  in the previous stage; every one of them must be spoken verbatim in full
  and appear in both dialogue[] and integrated_multimodal_description -- do
  not drop or trim any of them for space. Only when you want to add lines
  beyond required_dialogue, and this segment's capacity genuinely cannot fit
  everything, pick the one or two additional lines that carry the scene and
  let the picture do the rest; leave the remainder to later segments.
- All dialogue in this segment adds up to at most
  {config.MAX_SPOKEN_CHARS_PER_SHOT} characters (count CJK characters and
  alphanumerics only, not punctuation or speaker names). That is how much
  speech physically fits in 15 seconds (about
  {config.SPOKEN_CHARS_PER_5_SECONDS / 5:.1f} characters per second), not a
  style preference: anything beyond it gets rushed, slurred, or silently
  dropped, and you cannot predict which line the model drops.
- overall_soundscape must never be left empty -- H3 is audio-visual joint
  generation and an empty field means the model invents uncontrolled sound.
  Only write "N/A" if the user explicitly wants total silence.
- non_diegetic_music: instrument / tempo / rhythm / dynamics language, not
  abstract mood words ("sad music" is invalid; "a slow solo piano note with
  a swelling low string" is valid). No music -> write "N/A".
- On-screen text (signs, letters, titles) is H3's strong suit: quote it
  verbatim in double quotes inside integrated_multimodal_description, e.g.
  reading "靠山宗". Do not translate it.
- A cut must carry new information (subject/space/state/viewpoint/time).
  Reframing alone is a camera move, not a cut.
- Reference character/scene images are attached separately by the platform,
  not embedded as an alignment instruction line; refer to them inline by
  role, e.g. "the character shown in the reference image, wearing ..." --
  give each reference material exactly one stated role, never let two
  references' roles overlap.
"""


def _dialect_for_target_video_model(target_video_model: str) -> tuple[VideoPromptProfile, str, str]:
    """Return (profile, target_model_literal, dialect_instructions).

    ``target_model`` uses the frozen contract's own vocabulary
    ("seedance_2" | "minimax_h3"), derived from the resolved prompt profile
    rather than hard-coded off the raw provider key, so this stays correct
    if a provider's profile binding ever changes.
    """
    from app.video_prompt_profiles import resolve_video_prompt_profile

    profile = resolve_video_prompt_profile(provider=target_video_model)
    if profile.render_format == "minimax_h3_native_fields":
        return profile, "minimax_h3", MINIMAX_H3_DIALECT_INSTRUCTIONS
    return profile, "seedance_2", SEEDANCE_DIALECT_INSTRUCTIONS
