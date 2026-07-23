"""领域 Command Handlers：直接调用现有 Python 领域函数，禁止 HTTP 自调用。"""
from __future__ import annotations

from typing import Any, Callable

from app.capabilities.handlers import common
from app.capabilities.schemas import CommandResult, StandardCommandInput


async def _wrap(summary: str, coro_or_fn, *args, accepted: bool = False, run_id_key: str = "run_id", **kwargs) -> CommandResult:
    outcome = await common.call_guarded(coro_or_fn, *args, **kwargs)
    if isinstance(outcome, CommandResult):
        return outcome
    data = outcome if isinstance(outcome, dict) else {"result": outcome}
    run_id = data.get(run_id_key) if isinstance(data, dict) else None
    factory = common.accepted if accepted or run_id else common.succeeded
    return factory(summary, data=data if isinstance(data, dict) else {}, run_id=run_id)


# —— project / production ——

async def project_import_novel(args: StandardCommandInput) -> CommandResult:
    from app.api import _create_project_core
    from app.capabilities.attachments import consume

    try:
        filename, raw = consume(getattr(args, "attachment_token"))
    except KeyError as exc:
        return common.failed(str(exc), error_code="attachment_invalid")
    return await _wrap("小说已导入", _create_project_core, getattr(args, "name", None), filename, raw)


async def project_delete(args: StandardCommandInput) -> CommandResult:
    from app.api import _delete_project_core
    return await _wrap("项目已删除", _delete_project_core, args.project_id)  # type: ignore[attr-defined]


async def production_auto_start(args: StandardCommandInput) -> CommandResult:
    from app.api import _start_auto_core
    return await _wrap(
        "一键全自动已启动",
        _start_auto_core,
        args.project_id,  # type: ignore[attr-defined]
        getattr(args, "directory_grant", None),
        accepted=True,
    )


async def production_auto_cancel(args: StandardCommandInput) -> CommandResult:
    from app import auto
    from app.api import _project_or_404

    async def _cancel():
        _project_or_404(args.project_id)  # type: ignore[attr-defined]
        stopped = await auto.cancel(args.project_id)  # type: ignore[attr-defined]
        return {"stopped": bool(stopped)}

    return await _wrap("已请求停止一键全自动", _cancel)


# —— bible / portrait ——

async def bible_generate(args: StandardCommandInput) -> CommandResult:
    from app.api import _start_bible_core
    feedback = getattr(args, "feedback", "") or ""
    return await _wrap("人物谱生成已启动", _start_bible_core, args.project_id, feedback, accepted=True)  # type: ignore[attr-defined]


async def bible_cancel(args: StandardCommandInput) -> CommandResult:
    from app.api import _cancel_bible_core
    return await _wrap("已停止人物谱生成", _cancel_bible_core, args.project_id)  # type: ignore[attr-defined]


async def bible_update(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _edit():
        return api_mod.edit_bible(args.project_id, {"bible": args.bible, "expected_version": args.expected_version})  # type: ignore[attr-defined]

    return await _wrap("人物谱已保存", _edit)


async def portrait_update_prompt(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _edit():
        return api_mod.edit_portrait_prompt(
            args.project_id,  # type: ignore[attr-defined]
            args.character,  # type: ignore[attr-defined]
            {"prompt": args.prompt},  # type: ignore[attr-defined]
        )

    return await _wrap("定妆描述已更新", _edit)


async def portrait_generate(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    body = {}
    character = getattr(args, "character", None)
    if character:
        body["character"] = character
    return await _wrap("定妆生成已启动", api_mod.start_refs, args.project_id, body, accepted=True)  # type: ignore[attr-defined]


async def portrait_cancel(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("已停止定妆生成", api_mod.cancel_refs, args.project_id)  # type: ignore[attr-defined]


# —— scene ——

async def scene_generate_bible(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("场景圣经生成已启动", api_mod.start_scene_bible, args.project_id, accepted=True)  # type: ignore[attr-defined]


async def scene_generate_refs(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    body = {}
    if getattr(args, "scene_name", None):
        body["scene_name"] = args.scene_name  # type: ignore[attr-defined]
    return await _wrap("场景图生成已启动", api_mod.start_scene_refs, args.project_id, body, accepted=True)  # type: ignore[attr-defined]


async def scene_cancel_refs(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("已停止场景图生成", api_mod.cancel_scene_refs, args.project_id)  # type: ignore[attr-defined]


async def scene_update_prompt(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _edit():
        return api_mod.edit_scene_prompt(
            args.project_id,  # type: ignore[attr-defined]
            args.scene_name,  # type: ignore[attr-defined]
            {"prompt": args.prompt},  # type: ignore[attr-defined]
        )

    return await _wrap("场景描述已更新", _edit)


# —— episode / screenplay / storyboard ——

async def episode_plan(args: StandardCommandInput) -> CommandResult:
    from app import planning

    return await _wrap(
        "分集规划已启动",
        planning.start_plan,
        args.project_id,  # type: ignore[attr-defined]
        accepted=True,
    )


async def screenplay_generate(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    body = {"force": bool(getattr(args, "force", False))}
    return await _wrap("剧本生成已启动", api_mod.start_screenplay, args.episode_id, body, accepted=True)  # type: ignore[attr-defined]


async def screenplay_generate_batch(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("批量剧本已启动", api_mod.start_screenplay_all, args.project_id, accepted=True)  # type: ignore[attr-defined]


async def screenplay_cancel(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    if hasattr(args, "episode_id") and getattr(args, "episode_id", None):
        return await _wrap("已取消剧本生成", api_mod.cancel_screenplay, args.episode_id)
    return await _wrap("已取消批量剧本", api_mod.cancel_screenplay_all, args.project_id)  # type: ignore[attr-defined]


async def screenplay_update(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _edit():
        return api_mod.edit_screenplay(
            args.episode_id,  # type: ignore[attr-defined]
            {"screenplay": args.screenplay, "expected_version": args.expected_version},  # type: ignore[attr-defined]
        )

    return await _wrap("剧本已保存", _edit)


async def storyboard_generate(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    mode = getattr(args, "mode", "fresh")
    if mode == "resume":
        return await _wrap("分镜续跑已启动", api_mod.resume_storyboard, args.episode_id, accepted=True)  # type: ignore[attr-defined]
    return await _wrap("分镜生成已启动", api_mod.start_storyboard, args.episode_id, accepted=True)  # type: ignore[attr-defined]


async def storyboard_generate_batch(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("批量分镜已启动", api_mod.start_storyboard_all, args.project_id, accepted=True)  # type: ignore[attr-defined]


async def storyboard_cancel(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("已取消分镜", api_mod.cancel_storyboard, args.episode_id)  # type: ignore[attr-defined]


async def shot_update(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _edit():
        body = dict(getattr(args, "patch", {}) or {})
        if args.expected_version is not None:
            body["expected_version"] = args.expected_version
        return api_mod.edit_shot(args.shot_id, body)  # type: ignore[attr-defined]

    return await _wrap("镜头已保存", _edit)


async def storyboard_confirm(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _confirm():
        return api_mod.confirm_episode(args.episode_id)  # type: ignore[attr-defined]

    return await _wrap("分镜已确认", _confirm)


# —— video / reference ——

async def video_generate_episode(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("整集视频已入队", api_mod.generate_episode, args.episode_id, None, accepted=True)  # type: ignore[attr-defined]


async def video_generate_shot(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    body = {
        "prompt_override": getattr(args, "prompt_override", None),
        "reroll": bool(getattr(args, "reroll", False)),
        "critique": getattr(args, "critique", None),
        "with_critique": bool(getattr(args, "critique", None)),
    }
    return await _wrap("单镜视频已入队", api_mod.generate_shot, args.shot_id, body, accepted=True)  # type: ignore[attr-defined]


async def video_stop_shot(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _stop():
        return api_mod.stop_shot_video(args.shot_id)  # type: ignore[attr-defined]

    return await _wrap("已请求停止单镜视频", _stop)


async def video_adopt_version(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _adopt():
        return api_mod.adopt_version(
            args.shot_id,  # type: ignore[attr-defined]
            {"version_id": args.version_id, "reason": args.reason},  # type: ignore[attr-defined]
        )

    return await _wrap("已采用视频版本", _adopt)


async def video_clear_episode(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _clear():
        return api_mod.clear_episode_artifacts(args.episode_id)  # type: ignore[attr-defined]

    return await _wrap("已清空整集媒体", _clear)


async def video_clear_shot(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _clear():
        return api_mod.clear_shot_artifacts(args.shot_id)  # type: ignore[attr-defined]

    return await _wrap("已清空单镜媒体", _clear)


async def video_delete_version(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _delete():
        return api_mod.delete_version(args.version_id)  # type: ignore[attr-defined]

    return await _wrap("已删除视频版本", _delete)


async def video_resume_episode(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    return await _wrap("已恢复集视频任务", api_mod.resume_episode, args.episode_id, accepted=True)  # type: ignore[attr-defined]


async def reference_review(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod
    action = getattr(args, "action", "discard")
    if action == "restore":
        def _restore():
            return api_mod.restore_reference_image(
                args.version_id,  # type: ignore[attr-defined]
                args.ref_id,  # type: ignore[attr-defined]
                {"override_reason": getattr(args, "override_reason", None) or args.reason},
            )
        return await _wrap("参考图已恢复", _restore)

    def _discard():
        return api_mod.discard_reference_image(args.version_id, args.ref_id)  # type: ignore[attr-defined]

    return await _wrap("参考图已废弃", _discard)


# —— delivery ——

async def delivery_concatenate(args: StandardCommandInput) -> CommandResult:
    from app import api as api_mod

    def _concat():
        return api_mod.concatenate(args.episode_id)  # type: ignore[attr-defined]

    return await _wrap("成片拼接已完成或已受理", _concat)


async def delivery_check(args: StandardCommandInput) -> CommandResult:
    from app import delivery

    def _check():
        return delivery.delivery_readiness(args.episode_id)  # type: ignore[attr-defined]

    return await _wrap("交付就绪检查完成", _check)


async def delivery_create_package(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch

    return await _wrap(
        "交付候选已创建",
        orch.create_delivery_package,
        args.episode_id,  # type: ignore[attr-defined]
        None,
        accepted=True,
    )


async def delivery_review(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch
    body = {
        "package_id": getattr(args, "package_id", None),
        "decision": getattr(args, "decision", None),
        "reason": args.reason,
        "accepted_risk": getattr(args, "accepted_risk", None),
    }
    return await _wrap("交付审批已记录", orch.decide_delivery, args.episode_id, body)  # type: ignore[attr-defined]


async def delivery_submit_feedback(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch
    body = {
        "package_id": getattr(args, "package_id", None),
        "feedback": getattr(args, "feedback", None) or args.reason,
        "request_revision": bool(getattr(args, "request_revision", True)),
    }
    return await _wrap("客户反馈已提交", orch.create_customer_feedback, args.episode_id, body)  # type: ignore[attr-defined]


# —— run / job ——

async def run_control(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch
    action = getattr(args, "action", "cancel")
    run_id = args.run_id  # type: ignore[attr-defined]
    if action == "resume":
        return await _wrap("Run 已恢复", orch.resume_run, run_id, accepted=True)
    if action == "retry":
        return await _wrap("Run 已重试", orch.retry_run, run_id, accepted=True)
    return await _wrap("Run 已取消", orch.cancel_run, run_id)


async def run_create(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch
    payload = getattr(args, "payload", {}) or {}
    return await _wrap("Run 已创建", orch.create_run, payload, accepted=True)


async def job_cancel(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch
    return await _wrap("媒体 Job 已取消", orch.cancel_media_job, args.job_id)  # type: ignore[attr-defined]


# —— system ——

async def system_update_settings(args: StandardCommandInput) -> CommandResult:
    from app import system_api

    def _put():
        return system_api.put_settings(getattr(args, "patch", {}) or {})

    return await _wrap("设置已更新", _put)


async def system_model_create(args: StandardCommandInput) -> CommandResult:
    from app import system_api
    return await _wrap("模型已添加", system_api.add_model, getattr(args, "model", {}) or {})


async def system_model_update(args: StandardCommandInput) -> CommandResult:
    from app import system_api

    def _upd():
        return system_api.update_model(args.model_id, getattr(args, "patch", {}) or {})  # type: ignore[attr-defined]

    return await _wrap("模型已更新", _upd)


async def system_model_delete(args: StandardCommandInput) -> CommandResult:
    from app import system_api

    def _del():
        return system_api.delete_model(args.model_id)  # type: ignore[attr-defined]

    return await _wrap("模型已删除", _del)


async def system_model_test(args: StandardCommandInput) -> CommandResult:
    from app import system_api
    model_id = getattr(args, "model_id", None)
    draft = getattr(args, "draft", None)
    if model_id:
        return await _wrap("模型连通性测试完成", system_api.test_saved_model, model_id)
    return await _wrap("模型连通性测试完成", system_api.test_model_connection, draft or {})


async def system_set_engine(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch

    def _set():
        return orch.set_project_engine(args.project_id, {"enabled": bool(getattr(args, "enabled", True))})  # type: ignore[attr-defined]

    return await _wrap("Engine 开关已更新", _set)


async def system_mkdir(args: StandardCommandInput) -> CommandResult:
    from app import system_api
    body = {"parent": getattr(args, "parent_grant", None), "name": getattr(args, "name", None)}
    return await _wrap("目录已创建", system_api.make_dir, body)


async def system_run_benchmark(args: StandardCommandInput) -> CommandResult:
    from app.orchestration import api as orch
    return await _wrap("Benchmark 已启动", orch.run_benchmark, getattr(args, "payload", {}) or {}, accepted=True)


HANDLER_MAP: dict[str, Callable[..., Any]] = {
    "project.import_novel": project_import_novel,
    "project.delete": project_delete,
    "production.auto_start": production_auto_start,
    "production.auto_cancel": production_auto_cancel,
    "bible.generate": bible_generate,
    "bible.cancel": bible_cancel,
    "bible.update": bible_update,
    "portrait.update_prompt": portrait_update_prompt,
    "portrait.generate": portrait_generate,
    "portrait.cancel": portrait_cancel,
    "scene.generate_bible": scene_generate_bible,
    "scene.generate_refs": scene_generate_refs,
    "scene.cancel_refs": scene_cancel_refs,
    "scene.update_prompt": scene_update_prompt,
    "episode.plan": episode_plan,
    "screenplay.generate": screenplay_generate,
    "screenplay.generate_batch": screenplay_generate_batch,
    "screenplay.cancel": screenplay_cancel,
    "screenplay.update": screenplay_update,
    "storyboard.generate": storyboard_generate,
    "storyboard.generate_batch": storyboard_generate_batch,
    "storyboard.cancel": storyboard_cancel,
    "shot.update": shot_update,
    "storyboard.confirm": storyboard_confirm,
    "video.generate_episode": video_generate_episode,
    "video.generate_shot": video_generate_shot,
    "video.stop_shot": video_stop_shot,
    "video.adopt_version": video_adopt_version,
    "video.clear_episode": video_clear_episode,
    "video.clear_shot": video_clear_shot,
    "video.delete_version": video_delete_version,
    "video.resume_episode": video_resume_episode,
    "reference.review": reference_review,
    "delivery.concatenate": delivery_concatenate,
    "delivery.check": delivery_check,
    "delivery.create_package": delivery_create_package,
    "delivery.review": delivery_review,
    "delivery.submit_feedback": delivery_submit_feedback,
    "run.control": run_control,
    "run.create": run_create,
    "job.cancel": job_cancel,
    "system.update_settings": system_update_settings,
    "system.model_create": system_model_create,
    "system.model_update": system_model_update,
    "system.model_delete": system_model_delete,
    "system.model_test": system_model_test,
    "system.set_engine": system_set_engine,
    "system.mkdir": system_mkdir,
    "system.run_benchmark": system_run_benchmark,
}
