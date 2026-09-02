"""人物谱生成——角色详情生成与 generate_bible/generate_scene_bible 主入口。

架构转向（2026-08-31 用户第二次拍板，推翻同日更早的中间方案）：`generate_bible`
不再点名角色（第一次拍板已定），也不再为 era/genre/统一画风发一次「轻量模型
判定」调用——那个中间方案本身就是错的，见该函数 docstring 的完整理由（简言
之：画风由用户在导入面板选定，问模型是多余的；这次多余调用在真实项目上直接
触发 HiAgent 内容审核 content_filter，把用户卡死在 bible_status=failed 且没有
出路）。

`_generate_character_detail` / `_generate_character_detail_batch` 两个单角色
详情生成原语已随点名/详情生成退场删除（生产零调用，`generate_bible` 从不
调用它们）。旧点名链已于 2026-09-01 整体退场：8 个协作模块（点名 → 结构闸 →
独立裁决闸 → 归并 → 排序 → 旁文本净化）逐个 grep 复核确认彼此连通、除测试外
零生产调用方，本轮随测试一并删除。
"""
from __future__ import annotations

import hashlib
import time  # noqa: F401 -- 本模块自身不再用，经 __init__ 再导出；tests/test_blueprint_shard_budget.py
# 仍会 monkeypatch.setattr(stages.time, ...)（time 是共享单例模块对象，见
# tests/test_stages_monkeypatch_guard.py 模块 docstring 的说明），必须保留可解析入口

from pydantic import BaseModel

from app.db import get_setting
from app.loops import AgentLoop, AgentLoopPolicy
from app.schemas import (Bible, Character, Scene, World)
# _appearance_evidence_verified/_validate_appearance_evidence 本模块自身已不再
# 调用（外观证据核验随详情生成一起归了映射台），但 app/stages/__init__.py 仍从
# 本模块透传导出它们，且被 app/portraits/cards.py（生产代码）与多个测试文件
# 直接消费，删掉会静默打断这些既有引用，因此标 noqa 保留。
from app.scene_contract import SCENE_ONE_LOCATION_RULE
from app.validators import (validate_bible,  # noqa: F401 -- re-exported by app/stages/__init__.py
                            validate_scene_bible)

from .alias_backfill import (  # noqa: F401 -- re-exported, see note above
    _verify_character_aliases_for_subset,
    _verify_character_aliases_in_place,
)
from .bible_models import (  # noqa: F401 -- re-exported, see note above
    _BibleRosterEntry,
    _CharacterDetail,
    _sanitize_character_detail_payload,
)
from .bible_shared import _render_bible_source
from .common import _run_with_agent_loop
from .identity_evidence import (  # noqa: F401 -- _validate_appearance_evidence 经 __init__ 再导出
    _appearance_evidence_verified,
    _validate_appearance_evidence,
)


# ---------- A0. 世界观（首版人物谱唯一产出；不点名、不生成角色） ----------


def _carry_forward_existing_bible_assets(
    previous_bible: dict | None,
) -> tuple[list[Character], list[Scene]]:
    """重新判定世界观时原样带出已有角色/场景，不重新生成也不清空。

    早期实现在没有 ``previous_bible`` 与"重新判定世界观"两种情况下都直接
    ``characters=[]``，把"首次生成、没有候选可点名"和"重新判定世界观、
    角色早已由映射台/分集反应式建卡积累出来"错误地合并成同一种「清空」——
    人工核验点出这会把用户攒了几十集的角色卡（连同人物谱里登记的场景卡）
    随手一个「重新判定世界观」按钮清零，已改正。

    ``previous_bible`` 是 ``json.loads(projects.bible_json)`` 的原始 dict（见
    app/domain/bible_ops/task_run.py 的 ``_bible_task``），没有旧 bible（真正
    首次生成）时两个列表都为空——这才是"没有候选可点名"的正确空态，不是
    「重新判定世界观也清空」。"""
    if not previous_bible:
        return [], []
    characters = [
        Character.model_validate(item) for item in previous_bible.get("characters", []) or []
    ]
    scenes = [
        Scene.model_validate(item) for item in previous_bible.get("scenes", []) or []
    ]
    return characters, scenes


async def generate_bible(chapters: list[dict], feedback: str = "", previous_bible: dict | None = None,
                         project_id: str | None = None,
                         visual_style_prompt: str | None = None) -> Bible:
    """首版人物谱只把用户在导入面板选定的画风写进 ``world.visual_style_canonical``；
    不点名角色、不生成角色详情、也**不再为此发起任何模型调用**。首次生成时
    产出 ``characters=[]``；换画风时原样带出 ``previous_bible`` 已有的
    characters/scenes（见下）。``chapters``/``feedback``/``project_id`` 只为
    调用方签名兼容保留，本函数不再读取原文、不再消费打回意见。

    架构转向（2026-08-31 用户第二次拍板，推翻同日更早「发一次轻量模型调用
    判定 era/genre/画风」的中间方案）：那版的前提就是错的——用户此前已经
    明确「这个按钮就是选择画风的按钮」，画风由用户选定、``visual_style_prompt``
    就是选定结果本身（``app.domain.bible_ops.primitives.
    _visual_style_prompt_or_default`` 已把 style_name 解析成固定文案），再问
    模型一次答不出比用户给定值更好的答案。这次「可有可无」的调用在真实项目
    上直接把用户拦停：《我欲封天》原文触发 HiAgent 内容审核
    （``finish_reason=content_filter``），项目卡在 ``bible_status=failed``
    且没有重新发起入口。

    era/genre 不再由模型判定，也不在这里编造兜底（CLAUDE.md 禁止黑白名单/
    臆造兜底）：下游几乎无人真正依赖它们——``validate_bible`` 只做占位符
    校验，``generate_scene_bible``/角色详情提示词只当参考，角色详情生成本身
    已归映射台。没有 ``previous_bible``（真正首次生成）时留空字符串，如实
    反映「没有判定过」；换画风（有 ``previous_bible``）时原样带出旧的
    era/genre——这次调用根本没有重新判断过它们，写成空字符串反而会覆盖掉
    曾经真实判定过的值。

    ``previous_bible``（换画风时的旧 bible_json）用于**原样带出**已有
    ``characters``/``scenes``（见 `_carry_forward_existing_bible_assets`），
    不重新生成也不清空：新架构下角色卡/场景卡是随分集陆续积累出来的
    （映射台提名 ``POST /projects/{project_id}/characters/nominate`` 或分镜
    展开前反应式建卡 ``ensure_character_card``/``assess_new_scene``），换画风
    这个动作只替换 world，绝不能把用户攒了几十集的角色卡和场景卡清零。
    """
    carried_characters, carried_scenes = _carry_forward_existing_bible_assets(previous_bible)
    previous_world = (previous_bible or {}).get("world") or {}
    world = World(
        era=str(previous_world.get("era") or ""),
        genre=str(previous_world.get("genre") or ""),
        visual_style_canonical=(visual_style_prompt or "").strip(),
    )
    return Bible(world=world, characters=carried_characters, scenes=carried_scenes)


# ---------- A2. 场景圣经（场景图素材库的规范场景，跨集场景一致性核心） ----------

class _SceneBibleDraft(BaseModel):
    """场景圣经输出合同（仅生成期使用）：一组规范场景。"""

    scenes: list[Scene]


async def generate_scene_bible(chapters: list[dict], bible: Bible,
                               feedback: str = "", project_id: str | None = None) -> list[Scene]:
    """从原文提取「规范场景」清单，作为场景图素材库的底稿（与 generate_bible 同构）。
    每个场景给 name（稳定短标签）+ scene_canonical（固定场景锚点串，画风约束与人物锚点一致，
    按 bible.world.visual_style_canonical 是否为照片级真人摄影预设二选一：非摄影风格必须
    CG/动画/漫画类非真人风格，否则后续 Seedance/Seedream 易因疑似真人报错；摄影风格则相反，
    要求真实材质与摄影级细节）。"""
    from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS
    from app.visual_styles import is_photographic_style_prompt
    chapters_text = _render_bible_source(chapters)
    style = bible.world.visual_style_canonical
    genre = bible.world.genre or ""
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"\n人工打回重生要求（最高优先级）：\n{feedback.strip()}\n"
    if is_photographic_style_prompt(style):
        scene_style_rule = (
            f'4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是照片级摄影质感的'
            "实景环境描述，允许并鼓励真实材质、自然光影与摄影级细节；场景本身仍是虚构地点，"
            "不指向可识别的真实地标、真实机构或真实商业品牌名称。"
        )
    else:
        scene_style_rule = (
            f'4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是 CG/动画/漫画/插画类的'
            '非真人渲染场景（写实质感氛围词可保留），严禁"真人实拍/实景照片/摄影棚实拍"这类描述'
            "（否则后续图像/视频接口会因疑似真人实景报错）。"
        )
    prompt = f"""任务：从小说文本中提取【规范场景清单】，用于后续 AI 视频生成的场景一致性控制（场景图素材库）。

全片画风（场景锚点必须与之一致）：{style}
题材：{genre or '（未标注）'}

要求：
1. 只收录【反复出现 / 有戏份 / 画面感强】的关键场景（如主角居所、宗门广场、山中密林、朝堂等），最多 12 个；一次性出现的过场地点不要收录。{SCENE_ONE_LOCATION_RULE}
2. name：稳定的场景短标签（4~10 字，如"宗门广场""破败客栈内"），后续所有分镜的场景都收敛到这些名字，便于跨集复用同一张场景图。name 之间不要语义重复。
3. scene_canonical 是该场景的"固定场景锚点串"：{SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字（这是硬门禁，多一个字整份清单都会被拒收，写完请数一遍），必须包含 地点/室内外/典型光线时段/标志性陈设或建筑/整体氛围色调。只写视觉可见的环境信息，不写人物、不写剧情动作。原著未描写处按题材与画风合理补全并保持内部一致。
{scene_style_rule}
5. location_kind 取"室内/室外/其他"之一。

小说文本：
{chapters_text}{feedback_part}

输出 JSON Schema：
{{"scenes": [{{"name": str, "scene_canonical": str, "location_kind": "室内|室外|其他"}}]}}"""
    loop = AgentLoop(
        stage_key="scene_bible",
        contract_key="scene_bible",
        goal="从原文章节提取跨集复用、来源可追溯的规范场景",
        scope_type="project",
        scope_id=project_id or hashlib.sha256((chapters_text + style).encode("utf-8")).hexdigest()[:16],
        artifact_type="scene_bible",
        policy=AgentLoopPolicy(
            max_iterations=min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
        ),
    )
    draft = await _run_with_agent_loop(
        "场景圣经", "scene_bible", prompt, _SceneBibleDraft,
        lambda d: validate_scene_bible(d.scenes), loop=loop, temperature=0.5,
        # 与人物谱同因：修复轮不能把小说正文截掉，否则只会反复重排开头几个场景。
        repair_user_prompt_limit=None,
    )
    return list(draft.scenes)
