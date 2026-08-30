"""人物谱/别名/状态事实回填共用的原文预算工具：分段计划、原文渲染与 call_meta 归一化。"""
from __future__ import annotations

from typing import Any


from app import config


# 与 _bible_short_json_call_meta / _bible_source_plan / _render_bible_source 同属一组，
# 拆分自 common.py（避免与其产生循环依赖：common._run_with_agent_loop 调用本文件的
# _bible_short_json_call_meta，这组常量因此必须和被调函数同侧）。
BIBLE_SOURCE_BUDGET_CHARS = 60000

_BIBLE_TAIL_SAMPLE_MAX = 12      # 后段最多抽样多少章（取其开头，角色多在章首登场）
_BIBLE_TAIL_SLICE_CHARS = 1500   # 每个抽样章节注入的开头字数

BIBLE_FIRST_TOKEN_TIMEOUT_S = float(config.TIMEOUT_CHAT_FIRST_TOKEN_S)


def _bible_short_json_call_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """人物谱短 JSON 调用：关掉 thinking，并给 0 字节流式空等一个首字上限。

    调用方传入的 ``first_token_timeout_s`` 优先；详情比点名长，需要更宽的首字窗。
    """
    merged = {**meta, "disable_thinking": True}
    if "first_token_timeout_s" not in meta:
        merged["first_token_timeout_s"] = BIBLE_FIRST_TOKEN_TIMEOUT_S
    return merged

# 旁文本净化的三个闸（见 `_chapters_without_paratext` docstring 里的事故口径）：
BIBLE_PARATEXT_MARGIN_CHAPTERS = 3   # 净化只会让正文变短，头部因此可能多吃进几章
BIBLE_PARATEXT_CONCURRENCY = 8       # 净化各章互不依赖，chat 路径也没有全局信号量
# 旁文本只是可选净化，绝不能占据人物谱主链路两分钟。真实调用一般 2~5 秒；
# 首批并发在短预算内能完成多少就采用多少，其余原文直通并留给后续按章缓存。
BIBLE_PARATEXT_BUDGET_S = 15.0
BIBLE_PARATEXT_CHAPTER_TIMEOUT_S = 8.0


def _bible_source_plan(
    valid: list[dict], budget: int, head_chapters: int | None,
) -> list[tuple[int, int, bool]]:
    """规划人物谱源文本读哪几章、每章读多少字：`(章在 valid 里的下标, 截取字数, 是否节选)`。

    渲染（`_render_bible_source`）和「哪几章需要先净化旁文本」
    （`_bible_paratext_scope`）共用这一份规划：口径只有一处，不会漂移出
    「净化了 643 章、真正读的只有 33 章」那种落差。
    """
    plan: list[tuple[int, int, bool]] = []
    # 头部顺序铺设：用至多 70% 预算（其余留给后段抽样）。
    head_budget = int(budget * 0.7)
    if head_chapters:
        # 首版人物谱要求「完整读完前 N 章」。按比例切的头部会随章节长度漂移：
        # 长章小说可能读到第三、四章就把头部预算用光，主要配角整体缺席。
        head_budget = min(budget, max(head_budget, sum(
            len(ch["content"].strip()) for ch in valid[:head_chapters]
        )))
    used = 0
    head_count = 0
    for index, ch in enumerate(valid):
        remain = head_budget - used
        if remain <= 200:
            break
        take = min(len(ch["content"].strip()), remain)
        plan.append((index, take, False))
        used += take
        head_count += 1

    # 后段抽样：在头部未覆盖的章节里均匀取样，注入每章开头若干字，覆盖后期登场人物。
    later = valid[head_count:]
    remain_budget = budget - used
    if later and remain_budget > 200:
        sample_n = min(len(later), _BIBLE_TAIL_SAMPLE_MAX, max(1, remain_budget // _BIBLE_TAIL_SLICE_CHARS))
        if sample_n > 0:
            step = len(later) / sample_n
            picked_idx = sorted({min(len(later) - 1, int(i * step)) for i in range(sample_n)})
            for li in picked_idx:
                if remain_budget <= 200:
                    break
                content = later[li]["content"].strip()
                take = min(len(content), _BIBLE_TAIL_SLICE_CHARS, remain_budget)
                plan.append((head_count + li, take, True))
                remain_budget -= take
    return plan


def _render_bible_source(chapters: list[dict], budget: int = BIBLE_SOURCE_BUDGET_CHARS,
                         *, head_chapters: int | None = None) -> str:
    """为角色圣经渲染源文本：先顺序铺头部（主角通常在前期出场），再在剩余预算里
    跨越全书【抽样后段章节的开头】，让后期才登场的重要角色（如中后段反派）也能进圣经——
    否则分镜阶段引用这些角色会因"不在圣经"而反复返工或被迫漏掉。
    """
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    if not valid:
        return ""

    def _title(ch: dict) -> str:
        return ch.get("title") or f"第{ch.get('idx', '?')}章"

    blocks: list[str] = []
    for index, take, excerpt in _bible_source_plan(valid, budget, head_chapters):
        ch = valid[index]
        content = ch["content"].strip()
        clipped = content[:take]
        if excerpt:
            suffix = "……（节选开头，仅供识别后期登场角色）" if len(content) > take else ""
            blocks.append(f"【{_title(ch)}·节选】\n{clipped}{suffix}")
        else:
            suffix = "……（原文过长已截断）" if len(content) > take else ""
            blocks.append(f"【{_title(ch)}】\n{clipped}{suffix}")

    return "\n\n".join(blocks)


# ---------- A1. 人物别名回填（层一，见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1） ----------

ALIAS_BACKFILL_SOURCE_BUDGET_CHARS = 150000  # 一次性全书扫描，预算高于常规人物谱生成的 60000


def _chapters_by_idx(chapters: list[dict]) -> dict[int, str]:
    """按章节序号建立原文查找表，供别名证据的逐字核验使用（未经预算截断的完整正文）。"""
    result: dict[int, str] = {}
    for chapter in chapters:
        content = (chapter.get("content") or "").strip()
        if not content:
            continue
        try:
            idx = int(chapter.get("idx"))
        except (TypeError, ValueError):
            continue
        result[idx] = content
    return result
