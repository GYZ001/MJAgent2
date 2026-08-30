"""人物谱生成——旁文本净化范围裁决与净化调用。"""
from __future__ import annotations

import asyncio
import time


from app import config
from app.db import get_conn, log_provider_call

from .bible_shared import (
    BIBLE_PARATEXT_BUDGET_S,
    BIBLE_PARATEXT_CHAPTER_TIMEOUT_S,
    BIBLE_PARATEXT_CONCURRENCY,
    BIBLE_PARATEXT_MARGIN_CHAPTERS,
    BIBLE_SOURCE_BUDGET_CHARS,
    _bible_source_plan,
)
from .common import BIBLE_HEAD_CHAPTERS, BIBLE_LOOKAHEAD_CHAPTERS


def _bible_paratext_scope(valid: list[dict]) -> list[int]:
    """人物谱真正会读到的章（下标落在 `valid` 上）：头部 + 后段抽样 + 必收统计窗口。

    净化按章一次模型调用，范围必须跟「读了什么」对齐，否则代价随书长线性增长
    而收益为零。
    """
    plan = _bible_source_plan(valid, BIBLE_SOURCE_BUDGET_CHARS, BIBLE_HEAD_CHAPTERS)
    scope = {index for index, _, _ in plan}
    head_end = max((index for index, _, excerpt in plan if not excerpt), default=-1) + 1
    # 净化只会让正文变短，头部因此可能比按原文规划时多吃进几章；同时
    # `_recurring_character_names` 的逐字统计窗口是前 HEAD+LOOKAHEAD 章，
    # 必收名单正是被旁文本污染的那一环，这段必须整段净化。
    window = max(
        head_end + BIBLE_PARATEXT_MARGIN_CHAPTERS,
        BIBLE_HEAD_CHAPTERS + BIBLE_LOOKAHEAD_CHAPTERS,
    )
    scope |= set(range(min(window, len(valid))))
    return sorted(index for index in scope if 0 <= index < len(valid))


async def _chapters_without_paratext(chapters: list[dict]) -> list[dict]:
    """把作者的话等旁文本从章节正文里剔掉，再交给人物谱这条链路。

    生产缺陷 R9：网文章节正文里直接粘着作者的话（求票、感谢读者、活动公告）。
    `_recurring_character_names` 按**原文逐字出现次数**产出「必收名单」，
    而提示词明令「名单里的每个名字…不得改写、合并或省略」——于是作者笔名
    在统计窗口里出现 27 次排第 4（高于真配角王有材 17 次），进入必收名单，
    **模型是被程序命令**建出那张人物卡的。它照办的同时把关系写成
    「创作者，在故事外注视并推动主角命运」，等于自己标注了疑虑。

    所以这不是模型幻觉，是程序把旁文本当成了正文来统计。判据与叙事蓝图
    共用一份（`app/source_paratext.PARATEXT_RULE`）。

    净化失败一律退回原文：人物谱不能因为这一步判不出来就产不出来。

    **2026-08-25 事故（run_8388b4e31301）**：这里原本 `for ch in chapters` 串行
    净化**全书**，一章一次模型调用。643 章的项目在 15 分钟闸门内只跑完 126 次
    （其中 3 次读超时各 152s），人物谱本体一次调用都没轮上，整轮超时作废；
    而这本书人物谱真正读到的只有 33 章，610 次调用是纯浪费。因此这里定死三条：
    只净化 `_bible_paratext_scope` 圈出的章、并发跑、整段封顶
    `BIBLE_PARATEXT_BUDGET_S`。净化本来就是「判不出就退回原文」的净化步骤而不是
    闸门，超时未完成的章原样进入下游，绝不能再把人物谱拖死。

    **2026-08-27（paratext 按章一次、持久化，见
    logs/paratext_single_source_plan.md）**：净化结果现在读/写
    `chapters.paratext_json`，不再每次都直接问模型——首次跑某个项目仍要
    为 scope 内每章各发一次模型调用（跟改造前一样受
    `BIBLE_PARATEXT_BUDGET_S` 封顶），但算完就永久落库；同一项目重新谱写
    人物谱（打回重生、脚本重试）时，这些章大概率命中缓存，`chat 调用数`
    应趋近于零，这一步的墙钟耗时应趋近于"读库"而不是"等模型"。缺
    `id` 的章节（测试用的合成 dict）无法持久化，退化为每次都重算，行为
    与改造前完全一致，不影响正确性。
    """
    from app.source_paratext import chapter_paratext_offsets, remove_offsets

    positions = [i for i, ch in enumerate(chapters) if (ch.get("content") or "").strip()]
    valid = [chapters[i] for i in positions]
    if not valid:
        return chapters
    scope = _bible_paratext_scope(valid)
    conn = get_conn()
    limiter = asyncio.Semaphore(BIBLE_PARATEXT_CONCURRENCY)
    started = time.time()

    async def _clean(slot: int) -> tuple[int, str, bool]:
        chapter = valid[slot]
        async with limiter:
            try:
                regions, cache_hit = await asyncio.wait_for(
                    chapter_paratext_offsets(
                        conn, chapter,
                        operation_id=f"bible.paratext:{chapter.get('id') or positions[slot]}",
                    ),
                    timeout=BIBLE_PARATEXT_CHAPTER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return slot, chapter.get("content") or "", False
        stripped = remove_offsets(chapter.get("content") or "", regions)
        return slot, stripped, cache_hit

    tasks = [asyncio.create_task(_clean(slot)) for slot in scope]
    try:
        done, pending = await asyncio.wait(tasks, timeout=BIBLE_PARATEXT_BUDGET_S)
    except BaseException:  # 外层取消/关服：不留悬挂任务
        for task in tasks:
            task.cancel()
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    cleaned = list(chapters)
    changed = 0
    cache_hits = 0
    for task in done:
        if task.cancelled() or task.exception() is not None:
            continue
        slot, stripped, cache_hit = task.result()
        if cache_hit:
            cache_hits += 1
        original = valid[slot]
        if stripped != (original.get("content") or ""):
            cleaned[positions[slot]] = {**original, "content": stripped}
            changed += 1
    log_provider_call(
        "character_bible_paratext", config.MODEL_TEXT,
        "OK", None, int((time.time() - started) * 1000),
        meta={
            "chapters_total": len(valid),
            "chapters_in_scope": len(scope),
            "chapters_stripped": changed,
            "unfinished": len(pending),
            "budget_s": BIBLE_PARATEXT_BUDGET_S,
            # 命中持久化缓存 vs 真正发起模型调用的两类计数（见方案文档
            # "改动清单"一节）：重跑同一项目时前者应趋近 chapters_in_scope，
            # 后者应趋近 0——这两个数字是判断"120s 预算有没有真的降下来"
            # 的直接依据，不用再去 provider_calls 表里数。
            "cache_hits": cache_hits,
            "model_calls": len(done) - cache_hits,
            "degraded_to_original": len(pending),
            "outcome": "best_effort_bypass" if pending else "complete",
        },
    )
    return cleaned


BIBLE_DETAIL_EVIDENCE_MAX_CHARS = 12000
BIBLE_DETAIL_EVIDENCE_MAX_SEGMENTS = 12
BIBLE_ROSTER_INPUT_MAX_CHARS = 16000
BIBLE_DETAIL_TIMEOUT_S = 90.0
BIBLE_DETAIL_MAX_ATTEMPTS = 3
BIBLE_DETAIL_MAX_TOKENS = 4096
# 单角色详情比点名/裁决长，20s 首字会把已发出的成功流误杀；60s 仍切断 0 字节空等。
BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S = 60.0

# appearance_canonical 的写作规则。提成常量是为了能被测试直接断言：这段文字
# 的产出会原封不动流进图像与视频模型（定妆照 prompt 由 app/portraits.py 拼接、
# 分镜 prompt 由 app/production/storyboard_pack.py 要求逐字沿用），所以这里
# 每一句都在约束"最终画到画面上的是什么"，不是普通的文风偏好。
#
# 这条规则原本要求模型在看不出性别时写"原文未点明性别"。意图是对的——不许按
# 名字或常识猜性别，猜出来的是编造。但产出位置错了：那句话是写给人看的元话语，
# 却被逐字拼进了图像 prompt。实测三个角色（靠山老祖/陈凡/何洛华）的定妆照
# prompt 因此变成「单角色全身定妆照：原文未点明性别，是靠山宗掌门……」，图像
# 模型只能把这七个字当成要画的内容。
#
# 所以保留"不许猜"的内核，只改"确实看不出时写什么"：不写元话语，改从看得见的
# 特征起笔，让这段描述在缺性别的情况下依然是一幅能照着画的画像。
BIBLE_APPEARANCE_FIELD_RULE = (
    "appearance_canonical 会被逐字送进图像与视频模型当作画面描述，所以整段"
    "从第一个字起就必须是画得出来的东西。关于原文本身的说明（原文有没有写、"
    "证据够不够、能不能确定）不属于画面，写进去图像模型只会把这几个字当成"
    "要画的内容。\n"
    "性别按证据包原文写：代词、身份称谓（师兄/师姐/公子/姑娘这类本身就分"
    "性别的称呼）、他人对话、外貌描写里凡是点明性别的地方都要照原文来。"
    "证据包里确实看不出性别时就不写性别，直接从看得见的特征起笔——年龄观感、"
    "体态、面部特征、气质、发型与随身物——让这段描述照样是一幅能照着画的"
    "画像。任何情况下都不要按名字或常识猜性别。"
)
