"""用户主动选择的高层 Prompt 模板（PRD §9.4）。

Prompt 不携带任何额外权限——它只是拼装出引导性文本，真正的读写仍必须
经由 Resources/Tools 走一遍完整的 Policy/Approval。
"""
from __future__ import annotations

from typing import Any


class PromptError(Exception):
    pass


def _arg(name: str, description: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "description": description, "required": required}


_PROMPTS: dict[str, dict[str, Any]] = {
    "continue_project": {
        "title": "继续制作这个项目",
        "description": "分析当前项目状态，给出最合理的下一步建议（只读分析，不自动执行）",
        "arguments": [_arg("project_id", "项目 ID")],
    },
    "diagnose_run": {
        "title": "诊断失败/暂停的 Run",
        "description": "解释某个 workflow run 为什么失败或暂停，并给出可恢复方案",
        "arguments": [_arg("run_id", "Workflow Run ID")],
    },
    "revise_shot": {
        "title": "修订单镜",
        "description": "基于原文、剧本与问题反馈，给出单镜修改建议（不直接写库，需调用 shot.update 确认）",
        "arguments": [
            _arg("shot_id", "镜头 ID"),
            _arg("issue", "希望解决的问题描述", required=False),
        ],
    },
    "prepare_episode_delivery": {
        "title": "准备本集交付",
        "description": "检查镜头采用、拼接、readiness 与交付缺口，列出仍需处理的事项",
        "arguments": [_arg("episode_id", "剧集 ID")],
    },
    "cost_preview": {
        "title": "预估生成成本",
        "description": "在不执行任何生成的情况下，估算指定范围的预计花费",
        "arguments": [
            _arg("project_id", "项目 ID", required=False),
            _arg("episode_id", "剧集 ID", required=False),
        ],
    },
}


def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "title": spec["title"],
            "description": spec["description"],
            "arguments": spec["arguments"],
        }
        for name, spec in _PROMPTS.items()
    ]


def get_prompt(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = _PROMPTS.get(name)
    if spec is None:
        raise KeyError(f"unknown prompt: {name}")
    arguments = arguments or {}
    for arg in spec["arguments"]:
        if arg["required"] and not str(arguments.get(arg["name"]) or "").strip():
            raise PromptError(f"prompt {name} 缺少必填参数：{arg['name']}")
    return {
        "description": spec["description"],
        "messages": [
            {"role": "user", "content": {"type": "text", "text": _render(name, arguments)}}
        ],
    }


def _render(name: str, arguments: dict[str, Any]) -> str:
    if name == "continue_project":
        project_id = arguments["project_id"]
        return (
            f"请分析项目 {project_id} 的当前状态：先读取 manju://projects/{project_id} "
            "及其人物谱、场景、分集资源，指出尚未完成或失败的环节，并给出最多 5 步的下一步建议。"
            "只做只读分析和建议，不要在获得用户批准前调用任何写命令（Tool）。"
        )
    if name == "diagnose_run":
        run_id = arguments["run_id"]
        return (
            f"请读取 manju://runs/{run_id} 与 manju://runs/{run_id}/events，"
            "解释该 Run 为什么失败或暂停，并说明是否可以安全恢复（resume/retry）以及所需前置条件。"
        )
    if name == "revise_shot":
        shot_id = arguments["shot_id"]
        issue = arguments.get("issue") or "（未说明具体问题，请先了解镜头现状）"
        return (
            f"请读取 manju://shots/{shot_id}，结合原文与当前问题：{issue}，"
            "给出具体的修订建议（台词/动作/镜头描述）。"
            "确认修改内容后再调用 shot.update 保存，不要假设内容已经生效。"
        )
    if name == "prepare_episode_delivery":
        episode_id = arguments["episode_id"]
        return (
            f"请检查剧集 {episode_id} 的交付准备情况：读取 manju://episodes/{episode_id}/delivery，"
            "列出未采用镜头、未拼接内容和 readiness 阻塞项，并给出处理顺序建议。"
        )
    if name == "cost_preview":
        scope = arguments.get("episode_id") or arguments.get("project_id") or "未指定范围"
        return (
            f"请在不执行任何生成任务的前提下，估算 {scope} 的预计生成花费："
            "读取相关资源了解待办镜头/待办剧本数量，并对相应 Tool 使用 dry_run 预检获取 "
            "estimated_cost_cny，不要实际创建任何付费任务。"
        )
    return ""
