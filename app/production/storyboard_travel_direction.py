"""分镜台屏幕行进方向（travel_direction）：规则文案 + 提示词回填判据。

拆出原因：``app.production.storyboard_continuity_memo`` 已经在 500 行的文件
行数硬顶上（``app/FILE_CONVENTIONS.toml`` 默认 ``max_lines_python=500``，零
余量），travel_direction 这一个维度需要的新增判据放不进去；见 CLAUDE.md
「装不下时先想怎么拆，不要先想加基线」。

2026-09-28 修法（《顾念长安（第二版）》EP1 opus5.5 分镜实测：30 段里至少
14 段段尾的「本段行进方向」不是本段的走位，而是更早段落的原话逐字带过来
的，且常与本段正文自己写的「本段人物不走动」「全部镜头固定机位」这类静止
描述矛盾——第 3 段「温念自画左（床）向画右（窗下墙角）移动，之后静止蹲在
墙角」被逐字带到第 4-9、14 段的咖啡馆/走廊段）：

- **规则文案改写**：travel_direction 此前的文案是「有上一段就默认沿用，
  原文写明变化才可改」——与 wardrobe/layout 同一套「默认继承」形状。但走位
  与服装不是同一类东西：服装是持续状态（没交代脱换就该一直穿着同一件），
  走位是这一段镜头里发生的一次性动作，只取决于这一段本身怎么演，与上一段
  是否也在走无关——哪怕这一段恰好也朝同一方向走，也必须是本段画面自己的
  观察结果，不能把上一段的记录当默认起点直接照抄。新规则不再提供「默认
  继承」这个起点，只要求描述本段真实发生的位移，没有就写「静止」。
- **回填判据同步改**：``ensure_travel_direction_in_prompt`` 此前只要
  ``continuity_memo.travel_direction`` 非「静止」且正文没写走向词就无条件
  回填进提示词——但模型即使被新规则要求本段独立判断，仍可能把 payload 里
  上一段的原话誊抄进本段字段（训练分布/旧习惯），这不是本段的真实声明，
  回填只会把陈旧走位钉进提示词。判据从数据推导：本段值与上一段逐字相同、
  且本段正文自己没有任何走向词，视为沿用而非本段声明，不回填，记一条
  ``degraded_capabilities`` 告警（模型自己的原话，不发明内容，供人工核查
  本段是否真的仍是这个走向）；本段正文已经用「静止」这个受控词自陈时同样
  不回填，避免钉入的走位与自己写的静止描述打架——「静止」是 travel_direction
  字段本身封闭取值域里的规范词（不是地名/家具那类开放词猜测），检查这个
  词是否已经出现在正文里，判据仍然是从数据（字段自身的取值约定）推导。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: 提示词里表示屏幕走向的用词——就是方言规则示例里教模型写的那套（「自画左向画右」等）。
_DIRECTION_WORDS = ("画左", "画右", "画近", "画远", "向左", "向右", "自左", "自右", "屏幕左", "屏幕右")


def travel_direction_rule(previous: str) -> str:
    """屏幕行进方向的正面陈述：只描述本段真实发生的位移，不继承上一段的记录。"""
    context = (
        f"（仅供参考：上一段记录的屏幕行进方向是「{previous}」，本段镜头如何接续可以留意，"
        "但不是本段的默认值）"
        if previous.strip() else ""
    )
    return (
        f"continuity_memo.travel_direction 只描述本段镜头里真实发生的位移{context}——谁、"
        "自画面哪一侧走到哪一侧、途经本段场景里的什么（例如「一行人自画左向画右沿山路"
        "行进」）：这是本段自己观察到的结果，只看本段原文与镜头怎么演，不像 wardrobe 是"
        "持续到换掉为止的状态——走位是一次性动作，哪怕这一段恰好也朝同一方向走，也要由"
        "本段的画面重新确认，不能直接照抄上一段的记录。本段镜头里没有人物位移（原地站定、"
        "坐着、镜头只是推拉摇移）时，travel_direction 写「静止」。"
    )


def ensure_travel_direction_in_prompt(draft: Any, previous_memo: Any) -> list[str]:
    """备忘里声明了行进方向、提示词却没写走向词时，把模型自己声明的方向追加进提示词。

    2026-09-14 第 8 集实测：16 段里 14 段的 continuity_memo.travel_direction 非静止
    且跨段逐字沿用，但只有 3 段的 prompt_text 出现走向词——视频模型只看提示词，方向
    留在备忘里等于没写。2026-09-28 顾念长安 EP1 实测：这个「跨段逐字沿用」本身就是
    问题所在——模型偶尔仍会把 payload 里上一段的原话誊抄进本段字段，不是本段的真实
    声明，回填只会把陈旧走位钉进提示词、常与模型自己写的静止描述打架，因此改成先判断
    是否为沿用（见模块 docstring），是沿用就不回填、只记告警；本段正文已用「静止」
    自陈时同样不回填，避免矛盾。

    2026-09-28 修正判定顺序：「沿用检测」必须先于「正文是否已含‘静止’」判断，不能
    反过来。真实数据里，长篇提示词经常在与本段人物走位完全无关的地方出现「静止」
    （门框风铃「此刻静止不动」、相框「恢复静止」……）；旧顺序是先扫一遍整段正文找
    「静止」，只要命中就直接放行返回，沿用检测根本没机会跑，于是这类段落的陈旧
    走位既不回填、也不记告警——静默漏判，比错误回填更隐蔽。「正文已含‘静止’/走向
    词」这个信号现在只用于决定「沿用检测通过之后，要不要把新方向字面追加进正文」
    这一步（避免注入矛盾文字），不再用来决定要不要记录沿用告警。
    """
    memo = getattr(draft, "continuity_memo", None)
    direction = str(getattr(memo, "travel_direction", "") or "").strip()
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not direction or direction == "静止" or not prompt.strip():
        return []
    has_direction_word = any(word in prompt for word in _DIRECTION_WORDS)
    previous_direction = str(getattr(previous_memo, "travel_direction", "") or "").strip()
    if previous_direction and direction == previous_direction:
        if has_direction_word:
            # 本段正文已经独立写出走向词，无论是否与上一段方向恰好相同，都是本段
            # 自己的观察结果，不是沿用，不需要告警。
            return []
        note = (
            f"[STORYBOARD_TRAVEL_DIRECTION_CARRIED_OVER][未拦截] continuity_memo."
            f"travel_direction「{direction}」与上一段逐字相同，且本段提示词里没有任何走向词"
            "——视为沿用上一段的记录而非本段自己的声明，未回填进提示词；请核对本段是否真的"
            "仍是这个走向，如果是，本段镜头描述里也应该体现这段位移本身。"
        )
        draft.degraded_capabilities = [*getattr(draft, "degraded_capabilities", []), note]
        log.warning("[STORYBOARD_TRAVEL_DIRECTION_CARRIED_OVER] %s", note)
        return []
    if has_direction_word or "静止" in prompt:
        return []
    draft.prompt_text = prompt.rstrip() + f"\n本段行进方向：{direction}；同行人物保持同一走向，跟拍与切换机位不反向。"
    log.info("[STORYBOARD_TRAVEL_DIRECTION_APPENDED] 提示词缺走向词，已按备忘追加：%s", direction[:40])
    return []
