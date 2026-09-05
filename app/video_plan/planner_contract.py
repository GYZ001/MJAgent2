"""规划器（episode_video_mode_plan）与模型之间的契约，两侧同源。

合法值从 ``PlannerShotAnalysis`` 的 Literal 派生；提示词里的正面陈述、输出契约、
缓存复用判据都从这里取，任何一侧都不可能比另一侧宽。

2026-09-03（ep_8db333a7187c，run_0455fd2cf8e4）：模型把 ``state_dependency`` 填成不存在的
``end_only``——提示词只给了一串 ``a|b|c``，没说每个值是什么意思，模型按字面推演出「只依赖
结尾」。更糟的是 ``generate_episode_plan`` 会把同一份提示词的历史响应原样复用（只看供应商
调用 status=OK，不看语义），坏响应对同一集反复生效，用户重试也一模一样地失败。
这里补两件事：带语义的合法值陈述，以及缓存复用前跑与新鲜输出相同的逐镜校验。
"""
from __future__ import annotations

from typing import Any, get_args

from pydantic import ValidationError

from app.schemas import extract_json

from .models import PlannerShotAnalysis, SHOT_RELATION_ENUM_CONTRACT
from .normalize import normalize_ai_shot_plan_candidate


def _literal_values(field: str) -> list[str]:
    return list(get_args(PlannerShotAnalysis.model_fields[field].annotation))


# 单一真源：直接取 Pydantic 字面量，schema 与提示词不会漂移。
DEPENDENCY_ENUM_CONTRACT: dict[str, list[str]] = {
    "state_dependency": _literal_values("state_dependency"),
    "motion_dependency": _literal_values("motion_dependency"),
}

# 只写正面陈述：每个值是什么、从哪里取、以及「结尾要接下一镜」该写在哪里。
# 下游（media_exec.enqueue_context）只区分 none 与非 none：非 none 一律表示本镜需要
# 上一镜的采用尾帧起画，所以这里的语义必须都落在「对上一镜的依赖」上。
DEPENDENCY_SEMANTICS = (
    "state_dependency 描述本镜画面状态对上一镜结尾状态的依赖，只看上一镜："
    "none=本镜可独立起画，不需要接上一镜的任何状态；"
    "start_only=本镜开头必须接上一镜结尾的状态（人物位置、姿态、道具、光线），之后自由发展；"
    "start_and_end=本镜开头接上一镜结尾，且本镜结尾状态也被剧情钉死；"
    "full_trajectory=本镜从头到尾的状态都必须与上一镜连续（同一个连续动作被切成两镜）。"
    "「本镜结尾要接下一镜」不是本字段的取值——这种约束写在下一镜的 state_dependency 里"
    "（下一镜填 start_only 或更强）。"
    "motion_dependency 描述本镜运动对上一镜的依赖："
    "none=无；pose=起始姿态要接上一镜；trajectory=运动方向与轨迹要延续上一镜；"
    "camera=镜头运动要延续上一镜；rhythm=剪辑节奏要与上一镜匹配；audio=以声音、口型或音乐节拍对齐上一镜。"
    "state_dependency 与 motion_dependency 必须逐字使用 dependency_enum_contract 对应数组中的值，"
    "拿不准时填 none 并把该维度写进 unknown_dimensions。"
)


def _pipes(values: list[str]) -> str:
    return "|".join(values)


def planner_system_prompt() -> str:
    return (
        "你是视频生产工具层规划器，只能引用输入中的 shot/database_shot ID，不得改写剧情、"
        "新增/删除/调换镜头。输入可能是整集的一个按请求体大小切分的窗口，只输出输入 shots。"
        "你只负责分析每镜的时空、剪辑、动作阶段、状态依赖、运动依赖与置信度；"
        "不得输出执行模式、镜头依赖、视频输入意图或 required_assets。"
        "执行模式和素材合同由程序根据真实场景顺序统一编译。关系判断只基于时空、剪辑、动作阶段、"
        "状态依赖和运动依赖，禁止按人物名、地点名、题材、打斗词或动作词表决定模式。"
        "new_domain、new_space 或 scene_cut 表示新场景首镜。"
        "relations 四个字段必须逐字使用 relation_enum_contract 对应数组中的枚举值，禁止自造同义词。"
        + DEPENDENCY_SEMANTICS
        + "只输出 JSON，不要 Markdown。"
    )


def planner_output_contract() -> str:
    rel = SHOT_RELATION_ENUM_CONTRACT
    dep = DEPENDENCY_ENUM_CONTRACT
    return (
        "\n输出：{\"shots\":[{\"shot_id\":数据库或发布shot ID,"
        f"\"relations\":{{\"temporal\":\"{_pipes(rel['temporal'])}\","
        f"\"spatial\":\"{_pipes(rel['spatial'])}\","
        f"\"edit\":\"{_pipes(rel['edit'])}\","
        f"\"action\":\"{_pipes(rel['action'])}\"}},"
        f"\"state_dependency\":\"{_pipes(dep['state_dependency'])}\","
        f"\"motion_dependency\":\"{_pipes(dep['motion_dependency'])}\","
        "\"reason_codes\":[通用关系码],\"confidence\":0到1,\"unknown_dimensions\":[],"
        "\"estimated_latency_ms\":整数}]}"
    )


def cached_window_is_valid(response_text: str) -> bool:
    """缓存复用前跑与新鲜输出相同的逐镜契约校验；任一镜不合法就不复用。

    这不是放松校验：新鲜输出走的也是同一条 ``normalize → PlannerShotAnalysis`` 路径，
    这里只是把它提前到「决定复用之前」。返回 False 的响应仍留在 provider_calls 台账里
    作为证据，只是不再被当成答案。
    """
    try:
        parsed: Any = extract_json(response_text)
    except ValueError:
        return False  # 台账里连 JSON 都不是的历史响应，同样不能当答案
    shots = parsed.get("shots") if isinstance(parsed, dict) else None
    if not isinstance(shots, list) or not shots:
        return False
    for raw in shots:
        if not isinstance(raw, dict):
            return False
        candidate, _changes = normalize_ai_shot_plan_candidate(raw)
        try:
            PlannerShotAnalysis.model_validate(candidate)
        except (TypeError, ValueError, ValidationError):
            return False
    return True

def window_shots_from_planner_response(payload: Any) -> list[Any] | None:
    """规划器一个窗口的镜头数组。契约要求 ``{"shots": [...]}``，但模型偶尔直接返回镜头
    数组（2026-09-04 我欲封天 12/13/19 集，09-05 第 17 集再次）——内容完全合法只是外层形状
    不同，按同一份数组处理，不整集打回。

    ``payload`` 可以是原始文本或已解析对象：``extract_json`` 只认第一个 JSON **对象**，
    遇到裸数组会把第一个镜头当成整份输出（这就是第一版修复没生效的原因），所以文本先
    整体按 JSON 解析一次，再退回 ``extract_json``。其它形状返回 None 由调用方判
    AI_PLAN_SCHEMA_INVALID。"""
    import json
    import re

    parsed: Any = payload
    if isinstance(payload, str):
        cleaned = re.sub(r"```(?:json)?\s*", "", payload, flags=re.IGNORECASE).replace("```", "").strip()
        cleaned = re.split(r"</think[^>]*>", cleaned, flags=re.IGNORECASE)[-1].strip()
        try:
            parsed = json.loads(cleaned)
        except ValueError:
            from app.schemas.json_extract import extract_json

            try:
                parsed = extract_json(cleaned)
            except ValueError:
                return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        shots = parsed.get("shots")
        return shots if isinstance(shots, list) else None
    return None
