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

2.2.0 更新（方言规则层，2026-09-01）：专家审阅真实 EP1 十八段产出后拍板六项
规则——画外音口型写法统一、台词锚定到发生它的那个镜头、关键道具补建立镜头
与构图锁、同框人数上限与受力描写、跨空间对话的建立镜、镜头时长按信息密度
伸缩（不设硬性秒数上限）。Seedance 中文块与 H3 英文块逐条对称落地，任何一条
只改一边都视为半成品；两块的对称纪律见上一段与函数级校验思路。
"""
from __future__ import annotations

import re
from typing import Any

from app import config
from app.schemas import MontageBeat
from app.video_prompt_profiles import VideoPromptProfile

SEEDANCE_DIALECT_INSTRUCTIONS = f"""\
目标模型：Seedance 2.0（中文自由散文，一整块可直接复制的提示词，不要拆成
JSON 字段或分点罗列）。

- 第一句必须是「电影级预告片质感，多镜头叙事，镜头之间硬切。」——这是触发
  15 秒档多镜头模式的固定锚句，照写，不得省略或改写。
- 用「镜头1：」「镜头2：」……序号排列，不写秒数区间——秒数区间会被模型当成
  字面时间码执行、当成精确切点，反而牺牲镜头数或撑爆某一镜；本段固定 15
  秒，写 2-4 镜。镜头数和每镜时长由这一段的信息密度决定，不是由秒数倒推：
  对话交锋、情绪凝视这类段落可以用长镜，但长镜必须写满能看的表演内容
  （微表情递进过程、动作分解过程），不能让镜头空转；动作与信息密集处改用
  3-4 个短镜切分，对话交锋段（正反打、连续对切）2 镜就够。不要为了凑时长
  写一个长时间静止、信息量为零的镜头——实测段 16 镜 2 是一个 9 秒的袖口
  特写，画面里除了袖口没有任何变化，这种镜头会让模型自行在中途插入切点，
  导致最终成片的镜头编号和分镜稿对不上，出问题也无法定点重跑那一镜。
- 每个镜头描述顺序：一个运镜（推近/拉远/横摇/固定/跟随/环绕，只选一个，不
  要复合运镜）→ 主体（用 @正名 引用）→ 一个具体动作 → 场景 → 光影。@ 后面
  直接跟 relevant_assets.characters 里这个人的 display_name（例如 @黄总）；
  identity_id（bible:黄总、entity:… 这类带前缀的 id）只用于 dialogue[] 的
  speaker_identity_id 与 resources，不写进 prompt_text——正文里出现 @bible:黄总
  会让人物参考图绑不上，这个人的长相就没有来源了。
- 构图按 aspect_ratio（本项目画幅）来写：16:9 横屏时，两人对话优先同框左右
  站位或过肩正反打，远景与大全景横向铺开、交代城市地标与空间纵深，人物沿
  画面横向走位；9:16 竖屏时沿用本方言其余规则。画幅比例由请求参数单独传给
  视频模型，正文只写构图本身。
- 一场戏的第一段——本段 previous_continuity_memo 为空，或本段
  source_segment_indexes 与上一段的 source_segment_indexes 不同，两者之一
  即视为换场——第一个镜头必须是能看清全部关键家具与关键道具摆位的定场
  镜别（全景或中景），逐一写出关键道具在画面里的具体位置（例如「会议桌
  左侧的椅子上搁着一只网状猫包」）；同一场戏后续各段各镜头里的道具位置，
  只从这份定场摆位或 continuity_memo 里取，不得新增原文没有交代过的家具
  或道具——这是本项目真实投诉过的问题：相邻两段之间猫一会儿在车底、一会
  儿在后备箱，猫包一会儿是网状背包、一会儿是透明背包，根源就是没有一镜
  先把家具与道具摆位钉死。
- 本段开头第一个镜头画的画面，必须从 continuity_memo 起手：continuity_memo
  记录着上一段结束时每件关键道具的外观/位置/状态（props）与人物-人物、
  人物-家具的相对位置（layout）；本段以这个状态为起点，第一个镜头就开始本段
  原文里的新动作或新台词，不能忽略备忘、凭空另起一套布局或道具外观。起点是
  「状态」不是「情节」：上一段已经演过的动作、已经说过的台词一律不再重演、
  不再复述（实测相邻两段镜头1 反复出现同一个人握镜疾驰、喊同一句话，成片像
  卡带）；上一段结尾已离场的人物，本段任何镜头都不得再出现，除非本段原文
  写了他回来。
- 有人物行进的镜头写清屏幕走向：同一场戏里同行的人物在每个镜头里保持同一屏幕行进
  方向（例如「一行人自画左向画右沿山路行进」），跟拍、拉远、切换机位都不反向；走向
  只描述本段镜头真实发生的位移，写进 continuity_memo.travel_direction，没有位移
  写「静止」——不是上一段的默认延续，哪怕这段恰好也朝同一方向走，也要由本段画面
  本身确认，不能直接照抄上一段的记录（实测第 2 集走山路时三个人各走各的方向）。
- 每个镜头里出现的人物都要写清脚下与依托：站在什么地面上、坐在哪把椅子上、
  手撑在哪件家具的哪一侧（例如「站在长桌一端的地面上，上身前倾，双手撑在
  桌沿」「坐在会议桌侧边的黑色转椅上」）。人只站在地面或坐在椅子上，桌面只
  放道具和动物（橘座跳上桌是剧情，人不上桌）——实测「将一叠文件狠狠砸在
  桌上」没写站位，模型把人物直接放在了桌面上，双腿穿进了桌子。
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
- relevant_assets.scenes 里某个场景带 reverse_angle_available=true 时，它的
  scene_canonical 是主视角机位看到的画面，reverse_angle_view 是转过身朝原机位
  方向看到的另一侧。本段在这个场景里有两人面对面对话、或人物回头望向来处时，把
  「背对主视角机位的那一方」拍正脸的镜头、以及望向来处的镜头写成反打：在那一镜
  的场景部分写 @场景名·反打（名字逐字取自这条 scene 的 display_name，紧跟这三个
  字，不加空格、不改写），这一镜的背景按 reverse_angle_view 来写，会换用这个场景
  的反打视角参考图；其余镜头照常直接写场景描述，不加这个后缀。某个场景的
  reverse_angle_available 不是 true 时不写「·反打」——没有对应反打图的点名会被
  原样剥除成普通文字，变不成一张真实参考图。
- relevant_assets.props 里每件道具若带 appearance 字段，且内容是一段具体
  描述，那就是这件道具在本集的标准外观锚点：第一次出现时必须逐字沿用这段
  描述本身，不得改写、精简或按本段情境调整（与角色 appearance 同一条规则，
  见前面「外观锚点」那条），此后每镜只写一到两个连续性特征（材质、颜色、
  拉链开合这类跨镜头必须不变的东西）。appearance 字段为空、或内容是「没有
  标准外观……」这类说明文字的道具，由你自行确定至少三项可视觉验证的特征
  （材质、颜色、形状/尺寸、随身携带方式里挑），并让同一件道具在本集所有
  出现它的镜头里沿用同一套自定特征——网状包不会自己变成透明包，除非本段
  原文明确写出更换道具本身这件事。道具的特写子镜只在原文写到针对它的动作
  或变化时用（拿起、端详、交接、镜面映出人脸这类节拍），相邻两段不重复同一
  道具的特写；其余镜头里道具随人物出现即可，不必每段以道具特写收尾——实测
  第 4 集前 12 镜有 8 镜以逐字相同的铜镜特写收尾，成片开头一分多钟全是铜镜。
- 情绪一律写成面部肌肉动作和肢体动作（例如「眉毛拧起、嘴大张、眼睛瞪圆」），
  不写抽象情绪词（「惊恐」「释然」这类词模型没有稳定映射）。每个镜头挑一个
  核心表演加一个关键动作就够——「喉结滚动＋眉头越皱越紧＋眼眶泛红＋下颌绷紧」
  四个微表情挤进同一个 4 秒镜头，模型哪个都做不完整。
- 血腥与凶险只用光影、人物神色与环境音表现（冷调天光、人物脸色发白、远处
  惨叫），不写「血迹」「血肉」「尸体」「穿透」这类直白血腥词，也不写伤口与
  流血的特写——视频网关的内容审核会整段拒收这类描写，且这一版画风本就不
  出血腥画面；死亡用倒地不动、旁人色变、镜头切走来交代，剧情节拍一样不少。
  实测第 11 集镜 5「地面散落血迹，整体氛围凶险肃杀」一句让整段被拒五次，去掉
  这句立即通过。
- 承担叙事功能的关键道具第一次出现时，必须有一个专门交代它来历/身份的
  建立镜头（写清谁把它拿出来、从哪里拿出来、这是什么东西），不能让道具
  凭空出现在角色手里——实测：葫芦在段 1 第一次出现时已经在角色手中，此后
  被反复特写却从没有一镜交代过它是什么、从哪来，观众全程不知道这件道具的
  身份，直到段 6 被扔掉都不知道那意味着什么。
- 关键道具在它被使用、发生关键动作的那一镜，要把它写成构图约束（例如
  「深褐色葫芦始终位于画面中心清晰可见」），不能只当动作的宾语写一笔带过
  （「玉佩砸入水面」会让道具直接消失在水花里，这是本项目已经吃过一次的
  教训）。这一镜也不要同时塞进另一个大幅度运镜或另一个空间——实测段 6
  镜 3「拉远＋远景＋葫芦下坠落水随流飘走＋山顶目送」一镜四件事、跨两个
  空间，远景里葫芦只剩几个像素，构图约束根本落不下去，全片核心道具动作
  因此报废，这正是「玉佩砸入水面」教训的重演。一个镜头锁一件道具就够，
  每镜都喊一遍「始终清晰可见」会把真正要紧的那件稀释掉。
- 群像在镜头描述里正向写出这一镜有几个人、分别是谁（如「画面里两名绿袍修士并肩而立」）；
  全段名单与总人数由系统段尾统一锁定，这里不重复；同框超过 4 人必须拆成多镜头或分批入画，
  不要塞下 5 人以上——实测段 14 镜 1 同框 5 人叠加高速飞行，
  新增的每张脸都必然崩坏。
- 高速运动或被外力裹挟（御风飞行、坠落、被气浪掀起这类）的镜头，必须写出
  受力后的具体特征（例如「衣袍在气流中向后绷直并高频抖动、头发完全被吹向
  脑后」），只写「谁站在哪个位置」会生成一张没有速度感、像摆拍合影的画面。
- 一段之内如果两个说话人分处不同空间（崖顶与崖下裂缝这类有落差或有阻隔
  的位置），段内必须安排至少一个镜头交代两人的空间关系——例如过肩俯视：
  从 A 的肩后越过崖边俯视下方的 B——不能让两人全程不同框、只靠画外音串联
  对话，观众会不知道两人相隔多远、处于什么相对方位。
- 神通/异能等超自然效果用物理描述代替文化词（「化作长虹」→「一道细长银白
  光带以极高速度横穿画面并留下拖影」）。
- 若这是全片收尾段，最后一镜必须是大远景或缓慢升起拉远的格局镜，不能停在
  人物中近景上。
- dialogue[] 每句明确声明说话人、delivery 与 delivery_kind，区分画内对白、
  人物画外对白、内心独白和旁白。本人出镜与是否开口分别由 visibility 与发声方式决定。
  画内对白用 delivery=spoken_dialogue，其余声道用 delivery=offscreen_voice。
- 在每句发生的「镜头N」动作链准确位置写 {{{{speech:U01}}}} 等占位符，每句一次。
  原话只写在 dialogue[].line，系统从同一合同展开中文声道标签、原话和口型要求。
  系统按句号、问号、感叹号拆成一句一个引号，保持同一说话人、同一条 dialogue[]。
  画外音展开后人物嘴唇闭合无张合动作；画内对白只有发声主体开口。
  全片贯穿段只汇总环境音、配乐、风格和约束，台词占位符留在对应的镜头中。
- 关键台词写 {{{{speech:U01}}}} 占位符之前，在同一个「镜头N」动作链里先写一句
  这句话具体怎么说出来的可执行描述：音量（压低声音/提高音量/维持平常音量）、
  语速（放慢语速/连贯快速/一字一顿）、停顿（说完前半句停顿一拍再继续/抢在
  对方说完前打断）、气息（喘着气说/声音发颤/贴近耳边低声说）四项里至少选一项，
  写成具体动作（例如「压低声音，放慢语速，一字一顿」），不写「愤怒地说」
  「无奈地说」这类抽象情绪副词——这类词模型没有稳定映射，说出来的语气和文字
  描述的表情一样容易失控。
- 本段 required_dialogue 给出的台词是上一阶段已经按 15 秒容量分配好的必保
  台词，原话完整写入 dialogue[]，每句占位符写进 prompt_text，不得因为篇幅紧张
  自行取舍或省略；只有当你还想在这些必保台词之外再补充原文里的其它对话、
  而本段容量确实装不下全部时，才需要自己取舍——优先保留一到两句最要紧的，
  其余改用画面交代（张嘴又闭上、摇头、把东西递过去、转身就走），留给后面的
  段落。
- 结尾必须有一段「全片贯穿：环境音……；配乐……；风格……；约束……」，环境音与配乐不能留空，
  约束里必须包含「面部一致、手指正确、人数锁定、台词只出声不出字幕、无水印、人物与家具不穿插」；这几个词原样写出，
  不展开成带人名/人数的句子——人数锁定由系统段尾统一写入，这里不必重复。这一段只负责环境音、配乐、风格与约束这四类信息，
  不写任何台词原话——每一句台词已经写在它发生的那个「镜头N」动作链里，这里不用引号重复台词，
  也不必写「XX说话声」这类概括去代替它；系统展开 dialogue[] 与逐镜占位符后，逐镜原话与台词合同完全一致。
- 本段所有台词加起来不超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 个字（只数
  汉字与字母数字，不数标点和说话人名）。这是 15 秒能说完的物理容量
  （约 {config.SPOKEN_CHARS_PER_5_SECONDS / 5:.1f} 字/秒），不是风格偏好：
  超出的部分模型只能抢读、糊读或整句吞掉，而它吞哪一句你无法预测。
- 台词只以声音呈现：画面上不叠加字幕、台词文本、说话人名条或标题条，观众靠声音听台词，字幕由后期功能另行添加。
  画面里不出现需要观众看清、读出内容的文字（手机/电脑屏幕、通话或结束界面、招牌、黑板、书信这类）：带屏幕的
  物体一律从背面、侧面或半虚焦角度拍摄，不给屏幕正面清晰入镜；招牌、黑板、书信这类背景物体只写材质与外形
  （例如「木质招牌，边角磨损」），不写它上面的具体字样，原文没有写到的招牌/告示不要自行添加；文字本该承载的
  信息（地名、通话内容、金额）改由这一镜的台词或画外音说出来，不能只让画面替观众读——2026-09-28 用户拍板改写：
  旧规则要求画面文字由视频模型直接生成，但该模型中文字形极不稳定，《顾念长安》第 1 集生产实测手机界面、招牌与
  黑板字全部是乱码，招牌还是模型在原文未提及时自行加的。原文里的「【后期字幕】」「【字幕】」标注是后期指令，
  不写进画面；不得写「画面某角浮现文字/字样/文字标识」「标题条」「名条」这类叠加式呈现。
- 若本段原文本身是叙述者对多年经历的总结、回忆列举，或跨越多个时间点的排比
  （例如「我八岁的时候被诊断出长不高……我十三岁离开家……我三十五岁，把它抱
  在怀里」），这一段的镜头就应该按它列举的时间点分拍，每个时间点各给一个
  「镜头N」、各自的场景与画面，而不是把整段总结硬塞进某一个和内容无关的
  单一场景——实测：这类段落被配成「校园食堂」，是因为模型把总结性文字里
  出现的第一个具体地点当成了整段的场景，其余时间点的画面全部丢失。判据是
  原文段落本身列举了多个时间点/事项，不是「这一段有画外音」——同一段落里
  人物停在原地的第一人称内心独白（即使整句都是 offscreen_voice）仍然只有
  一个时空，必须保持单一场景，不要因为台词是画外音就顺带拆成多镜头总结。
- required_dialogue 里带 speaker 的台词，说话人就是它（原文里引号前后的归属，程序已核对），
  dialogue[] 的 speaker_identity_id 用 relevant_assets.characters 里这个正名对应的 identity_id，
  prompt_text 里这句也由同一个人说；画外音只能用本段原文里的句子，不得改写或另造。
- required_dialogue 没有明确 speaker 的引号台词，依据原文确认独立发声主体，
  可使用 relevant_assets.functional_extras 的 visual_entity_id。原文听者证据已随
  台词账本保留；听者只负责听。未知说话人保留独立称谓与 attribution_evidence。
  旁白对应叙述者；人物 OS/VO 与是否使用引号无关，按原文显式声道保留。
- speaker 与画面里出场的人物一律使用 relevant_assets.characters 给出的正名，
  逐字取用；原文只有称谓、relevant_assets.characters 查不到的人才用称谓本身
  （前面「没有参考图」那条已经讲过怎么处理这类人）。旁白是叙述声音、不是
  画面里的人，绝不能出现在画面主体或人物清单里，也不能给旁白安排出场镜头。
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
- Frame every shot for the task aspect_ratio (this project's canvas): when it
  is 16:9 landscape, stage two-person dialogue side by side in one frame or as
  over-the-shoulder shot/reverse-shot, spread wide and extreme-wide shots
  horizontally to show city landmarks and depth, and move characters laterally
  across the frame; when it is 9:16 portrait the rest of this dialect applies
  unchanged. The ratio itself is sent as a request parameter, so the prose
  describes composition only.
- integrated_multimodal_description opens with "[Shot 1]" (no timestamp),
  first declaring the overall style (e.g. "Live-action, cinematic" or
  "2D-animated"), then subsequent shots use "[Shot N] At 00:SS.sss, the
  camera cuts to ..." with strictly increasing timestamps. This segment is a
  fixed 15 seconds; write 2-4 Shots total. Let this segment's information
  density decide the shot count and each shot's length, not a target
  duration: a dialogue exchange or a held emotional gaze can run as one
  long shot, but a long shot must be filled with watchable performance
  content (micro-expression progression, an action carried through its
  stages) -- 2 shots is enough for a tight dialogue exchange
  (shot/reverse-shot); use 3-4 shorter shots wherever the segment is dense
  with action or information. Do not manufacture a long, static,
  information-empty shot just to fill time -- a real case wrote 9 seconds
  on a static cuff close-up with nothing changing on screen, and the model
  then inserted its own cut inside that shot, so the final render's shot
  numbers no longer matched the storyboard and that one shot could not be
  singled out for a retry.
- Write all descriptive prose in English. Keep dialogue and any on-screen
  text verbatim in their original language -- do not translate them.
- Camera moves are one natural English sentence combining type + amplitude +
  speed (e.g. "The camera pushes in with small amplitude at slow speed
  toward her hands"), not a stack of tags at the end of the sentence. One
  dominant camera move per shot.
- Each dialogue entry declares utterance_id, speaker_identity_id, delivery,
  and delivery_kind. Off-screen lines use delivery=offscreen_voice;
  on-screen speech uses delivery=spoken_dialogue. Place {{{{speech:U01}}}} once inside its specific [Shot N]
  integrated_multimodal_description at the intended speaking time.
  Keep the original words only in dialogue[].line. The system renders stable
  (S1) speaker IDs, says: <d>[Chinese] original words</d> blocks, and mouth
  instructions from this same contract. An off-screen voiceover states that
  all on-screen lips remain fully closed with no movement.
- Before the {{{{speech:U01}}}} placeholder for a key line, write one executable
  sentence in the same [Shot N] description stating exactly how it is spoken:
  pick at least one of volume (lowers voice / raises voice / keeps a normal
  volume), pace (slows down / speaks in a rapid rush / speaks haltingly, word
  by word), a pause (holds for a beat before continuing / cuts in before the
  other finishes), or breath (speaks between gasps / voice breaking / leans in
  close and speaks quietly), and phrase it as a concrete action (e.g. "lowers
  her voice, slows down, and speaks word by word"). Do not write an abstract
  mood adverb such as "angrily" or "helplessly" -- those have no stable
  mapping for the model, the same risk as an abstract emotion word for a
  facial expression.
- Every required_dialogue line appears verbatim in dialogue[] and has one
  placeholder in integrated_multimodal_description. The system inserts each
  exact line there after validation. Additional dialogue stays within the
  segment's spoken-word budget and refers to this segment's source text.
- All dialogue in this segment adds up to at most
  {config.MAX_SPOKEN_CHARS_PER_SHOT} characters (count CJK characters and
  alphanumerics only, not punctuation or speaker names). That is how much
  speech physically fits in 15 seconds (about
  {config.SPOKEN_CHARS_PER_5_SECONDS / 5:.1f} characters per second), not a
  style preference: anything beyond it gets rushed, slurred, or silently
  dropped, and you cannot predict which line the model drops.
- A prop that carries narrative weight needs an establishing shot the first
  time it appears -- state who produces it, where it comes from, and what it
  is -- instead of letting it appear already in a character's hand with no
  explanation. (Real failure: a gourd appeared already in a character's
  hand in segment 1, got repeated close-ups afterward, and was never once
  explained; viewers never learned what it was.)
- In the shot where a key prop undergoes its key action, write the prop as a
  framing constraint (e.g. "the dark brown gourd stays centered and clearly
  visible throughout the shot"), not merely as the object of a verb ("a jade
  pendant smashes into the water" makes the prop vanish into the splash at
  the exact moment it matters -- this project has already paid for that
  lesson once). Do not also cram a big camera move or a second location into
  that same shot -- a real case stacked "pull back + wide shot + the gourd
  falls, hits the water, and drifts off + a farewell gaze from the
  mountaintop" into one shot spanning two locations; in the resulting wide
  shot the gourd was a few pixels and effectively vanished, wrecking the
  film's one load-bearing prop action -- the jade-pendant lesson repeating
  itself. One shot locks one prop; repeating "always clearly visible" on
  every shot dilutes the one that actually matters.
- Cap any single frame at 4 people shown together; once a shot would need 5
  or more, split it into multiple shots or stagger the characters' entries
  instead of putting them all in one frame -- a real case put 5 people in
  one frame during high-speed flight and every added face came out
  deformed.
- A shot with high-speed motion, or a character being swept by an external
  force (riding wind, falling, caught in a blast), must state the resulting
  force effects explicitly (e.g. "robes snapped taut and vibrating rapidly
  in the airflow, hair blown completely backward") -- stating only who is
  positioned where produces a static-looking group photo with no sense of
  speed.
- When a segment's two speakers are in different spaces (e.g. a cliff top
  and a crevice below, or any layout with a drop or a barrier between
  them), include at least one shot that establishes their spatial
  relationship -- e.g. an over-the-shoulder high angle from behind A's
  shoulder, looking down past the edge at B below. Do not let the two carry
  the whole exchange off-screen-voice-only without ever sharing an
  establishing shot; the audience has no way to tell how far apart they are
  or how they are positioned relative to each other.
- overall_soundscape must never be left empty -- H3 is audio-visual joint
  generation and an empty field means the model invents uncontrolled sound.
  Only write "N/A" if the user explicitly wants total silence.
- non_diegetic_music: instrument / tempo / rhythm / dynamics language, not
  abstract mood words ("sad music" is invalid; "a slow solo piano note with
  a swelling low string" is valid). No music -> write "N/A".
- Do not put text the audience needs to read on screen (a phone/computer screen, a call or end-call UI, a
  signboard, a blackboard, a letter). Frame any screened device from behind, from the side, or slightly out of
  focus -- never a clear frontal shot of its screen; a signboard/blackboard/letter keeps only its material and
  shape in the description (e.g. "a weathered wooden signboard"), spells out no characters, and is never added
  unless the source text mentions it; put the information such text would carry (a place name, a call's content,
  an amount) into that Shot's dialogue or voiceover instead. (2026-09-28: replaces the previous rule that had the
  model render exact on-screen text directly -- this model's Chinese glyphs are unstable, and real renders showed
  a phone call screen, an end-call screen, a cafe signboard, and a blackboard all come out as garbled characters.)
- A cut must carry new information (subject/space/state/viewpoint/time).
  Reframing alone is a camera move, not a cut.
- Reference character/scene images are attached separately by the platform,
  not embedded as an alignment instruction line; refer to them inline by
  role, e.g. "the character shown in the reference image, wearing ..." --
  give each reference material exactly one stated role, never let two
  references' roles overlap. The one exception to "no @ token in this
  dialect": if relevant_assets.scenes marks a scene reverse_angle_available=
  true (its scene_canonical is what the primary camera sees; reverse_angle_view
  is the opposite side seen after turning around), then in a face-to-face
  dialogue in that scene the Shot showing the face of the person whose back is
  to the primary camera, and a Shot looking back toward where someone came
  from, are reverse-angle Shots: write "@SceneName·反打" for that Shot's scene
  mention and describe its background from reverse_angle_view -- copy the scene's display_name verbatim,
  followed by these two Chinese characters, no space -- and this one token
  switches that Shot to the scene's reverse-angle reference image; every
  other Shot that still frames the scene from its primary angle keeps the
  plain scene description with no @ token. This token is kept identical
  across both dialects (rather than invented as an English "by role" phrase)
  because a scene has no natural role phrase that a bare-string check could
  verify without becoming a keyword blacklist. Do not write the suffix when
  reverse_angle_available is not true, or when the Shot is still framed from
  the primary angle -- an unmatched mention gets silently stripped back to
  plain text and never becomes a real reference image.
- If this segment's source text is itself a narrator's summary, a
  reminiscence list, or a series spanning multiple points in time (e.g. "At
  eight I was diagnosed as unable to grow taller... at thirteen I left home...
  at thirty-five I held it in my arms"), split the Shots along those time
  points -- each point gets its own [Shot N] with its own space and picture --
  instead of forcing the whole summary into one location that only matches
  its first concrete noun. (Real failure: such a passage got rendered as a
  single school-cafeteria shot because the model latched onto the first place
  name mentioned, and every other time point's picture was lost.) The trigger
  is the source paragraph itself enumerating multiple time points or items --
  not "this shot has an off-screen line." A character's first-person interior
  monologue delivered while stationary (even if entirely off-screen-voice)
  is still one time and one place; keep it a single consistent scene, do not
  split it into a multi-time summary just because the line is off-screen.
- Preserve explicit OS/VO source attribution and source speaker evidence.
  Unnamed speakers use their own functional_extras visual_entity_id or source
  label with attribution_evidence. Narration is a narrator, never an unknown
  character fallback. Follow the shared speech-token contract for every line.
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


def dialect_literal_for_target_video_model(target_video_model: str) -> str:
    """公开只读查询：视频供应商 key 对应的分镜提示词方言字面值（"seedance_2" |
    "minimax_h3"）。

    复用 ``_dialect_for_target_video_model`` 同一份供应商→方言解析，只取字面值，
    供 app.media_exec（入队守卫）与 app.domain.storyboard_ops（切换接口的陈旧
    提示）比较「段落持久化的方言」与「当前/目标供应商的方言」时使用，不在各自
    调用方再抄一份映射表。
    """
    _, target_model_literal, _ = _dialect_for_target_video_model(target_video_model)
    return target_model_literal


def render_montage_beat_shots(beats: list[MontageBeat], *, duration_s: int) -> str:
    """Render the deterministic "镜头1（约0-Xs）……镜头2……" skeleton for a
    montage-form segment's ``beats``, splitting ``duration_s`` evenly.

    This is the code-side counterpart to the montage guidance embedded in the
    two dialect blocks above: once a caller has resolved a segment to
    ``form == "montage"`` with 1-3 ``MontageBeat`` entries (schema/validation
    contract in app.schemas.shot_montage / app.validators.storyboard_montage),
    this composes the per-beat time-sliced skeleton that the free-text
    prompt_text should follow. It does not decide *whether* a segment is a
    montage, does not call the model, and is not wired into
    app.production.storyboard_pack's own prompt assembly (that module owns
    the stage-1/stage-2 calls and is out of this change's file scope) --
    it only owns the deterministic beats-to-timeslice text so that wiring
    step has a tested, ready building block instead of ad-hoc string glue.
    """
    if not beats:
        return ""
    beat_count = len(beats)
    slice_s = max(1, duration_s // beat_count)
    lines: list[str] = []
    start = 0
    for index, beat in enumerate(beats, start=1):
        end = duration_s if index == beat_count else min(duration_s, start + slice_s)
        descriptor = "、".join(
            part for part in (beat.time_anchor, beat.scene_name, beat.visual) if part
        )
        lines.append(f"镜头{index}（约{start}-{end}秒）：{descriptor}")
        start = end
    return "\n".join(lines)


_IDENTITY_PREFIXED_MENTION_RE = re.compile(r"@(?:bible|entity):\S+")


def prompt_reference_prefix_errors(prompt_text: str) -> list[str]:
    """prompt_text 里 @ 后面跟了 identity_id 前缀（@bible:黄总、@entity:…）时阻断。

    EP1 重跑实测（2026-09-03）：模型从第 5 段起把 @李麦麦 写成 @bible:李麦麦，
    打包时 @名字→@图片N 的替换落空，Seedance 收到一串无绑定的 @bible:xxx，人物
    长相失去来源。这是「下一环节用不了」的形状问题，与 prompt_text 为空同类。
    """
    tokens = sorted(set(_IDENTITY_PREFIXED_MENTION_RE.findall(prompt_text or "")))
    if not tokens:
        return []
    shown = "、".join(tokens[:6])
    return [
        f"prompt_text 里的 @ 引用带了 identity_id 前缀：{shown}；@ 后面只能直接跟 "
        "relevant_assets.characters 的 display_name（例如 @黄总），bible:/entity: 前缀只用于 "
        "speaker_identity_id 与 resources，不进正文"
    ]


def reference_mention_errors(prompt_text: str, resources: Any) -> list[str]:
    """本段 resources.characters 里有 portrait_id 的 bible 角色，正文必须以 @显示名
    点名至少一次（只以画外音出场的可写成「画外音（显示名）」）。

    EP1 第三次重跑实测：第 6 段整段没有一个 @，模型把「中年男性黄总」「幼橘猫」当
    普通描述写，打包时 @名字→@图片N 没有可替换的目标，人物参考图对这一段完全不
    起作用。display_name 从 identity_id 的 bible: 前缀后取（人物谱正名即 id 主体）。
    """
    missing: list[str] = []
    for character in getattr(resources, "characters", []) or []:
        if getattr(character, "visibility", "unknown") == "voice_only":
            continue
        identity_id = str(getattr(character, "identity_id", "") or "")
        if not getattr(character, "portrait_id", None) or not identity_id.startswith("bible:"):
            continue
        name = identity_id.split(":", 1)[1].strip()
        if name and f"@{name}" not in prompt_text and f"画外音（{name}）" not in prompt_text:
            missing.append(name)
    if not missing:
        return []
    shown = "、".join(f"@{name}" for name in missing)
    return [
        f"prompt_text 没有用 @ 点名有参考图的角色：{shown}；本段 resources.characters 里带 portrait_id "
        "的角色，正文里至少要以 @显示名 出现一次（只以画外音出场的写「画外音（显示名）」），"
        "否则打包时人物参考图绑不到画面"
    ]
