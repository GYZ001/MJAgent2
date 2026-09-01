"""映射包发布后的后台补图触发：定妆照与场景图共用同一个入口。"""
from __future__ import annotations

import json

from app.db import get_conn


def _bible_collection(project_id: str, key: str) -> list:
    """读世界书某个顶层集合（``characters``/``scenes``），解析失败或缺失都当空。

    只用来判断"有没有东西可补"，不做 Pydantic 校验——校验失败本就该在
    ``generate_refs``/``generate_scene_refs`` 内部报，这里只负责空集合时不
    触发后台任务，不重复它们的校验逻辑。
    """
    row = get_conn().execute(
        "SELECT bible_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if not row or not row["bible_json"]:
        return []
    try:
        data = json.loads(row["bible_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def start_background_portraits(project_id: str) -> None:
    """映射包发布之后，把缺图的定妆照与场景图一并交给各自已有的后台补图
    任务，不等它们跑完。

    出图（角色定妆照 + 场景参考图）此前都在映射台内联做，实测占了映射台
    约三分之二的供应商时间（EP1：image 469.6s / 全部调用 725.9s，映射墙钟
    611s），用户按下"映射"要干等十分钟。映射台的职责因此收缩成纯文本——
    发现、建卡/建库、绑别名；出图挪到这里统一起两个后台任务。

    复用既有的 ``_start_refs_generation`` / ``_start_scene_refs_generation``：
    都是本来就存在的异步任务，各自带 refs_status/scene_refs_status 进度，
    界面可以直接显示"后台生成中"，不另造第二套后台机制。``resume=True``
    只补缺的、不重出已有的；已经在跑时它们返回 False/None，什么都不做即可
    ——那说明后台已经在处理同一批缺口。

    触发前各自先查世界书对应集合是否为空（ERR-20260831-a144a0 回归：这一轮
    映射在建卡之前就失败，世界书是 0 角色 0 场景，无条件触发导致后台任务
    ``_scene_refs_task`` 跑到 ``generate_scene_refs`` 内部
    ``if not bible.scenes: raise ValueError("还没有场景圣经，请先生成场景
    清单")``——异常发生在后台任务自己的调用栈上，不在本函数的 try/except
    范围内，最终把 ``scene_refs_status`` 写成 ``failed`` 并弹一条「请把错误
    码反馈给技术人员」的假报警）。"没有场景/角色要补"是正常的空集合，不是
    错误——CLAUDE.md「空集合不等于『无需检查』」讲的是校验场景不能因为集合
    空就跳过检查，这里是生成场景，没有素材要生成就是真的没事可做，两者不
    冲突。检查放在触发前而不是改 ``generate_scene_refs``/``generate_refs``
    本身：那两个函数也服务用户在场景库/人物谱页手动点「生成」的入口，那条
    路径下"还没有场景圣经"是一句有意义的、该显示给用户的提示，不能连带吃掉。
    角色侧 ``generate_refs`` 恰好已经对
    ``only_characters=[]`` 有 no-op 早退（服务另一个需求：「暂无具备定妆
    资格的角色」），实测 EP1 也确实落到了 ``ready``；这里仍然一并加同样的
    前置判断，一是消除"依赖另一个函数的副作用恰好兜住"这种脆弱的隐式耦合，
    二是避免在明知没有角色时还去起一个必然空转的后台任务。

    两条后台任务各自独立 try/except：一个起不来不该拖累另一个，也不该
    拖累映射本身——映射的产物是卡片/场景条目，已经落库了。缺图会在发起
    付费视频时被参考图就绪校验拦住（单镜走 _assert_shot_generation_gate，
    整集走 asset_gaps；场景走分镜台的场景主图校验），不会静默流到生成台。

    触发点放在 domain 层而不是 app.production.prep_pack 里：后者是 L4，引
    app.domain 是上行边（分层闸门实测拦下过）。编排本来就该由上层做。

    调用方 ``app/domain/screenplay_ops/task_body.py::_screenplay_task``
    在 ``finally`` 里无条件调用本函数（成功/失败/用户取消都触发，只排除
    进程热更/停机那一支）：卡片是在 prep_pack 内部的 discovery 阶段就建好
    的，闸门缺图报错、并发围栏冲突等任何 prep_pack 内部失败都不影响卡片
    已经落库这一事实。此前触发器只挂在成功路径上，一旦 prep_pack 因为
    "闸门要图但图还没生成"这类原因抛异常，触发器就永远不会跑，图也就永远
    补不上——ERR-20260831-63a9d2 实证：EP1 建出 3 张角色卡，
    character_portraits 却是 0 行，portraits_status 停在 idle。
    """
    if not project_id:
        return
    if _bible_collection(project_id, "characters"):
        try:
            from app.domain.bible_ops.refs_generation import _start_refs_generation

            _start_refs_generation(project_id, None, resume=True)
        except Exception:  # noqa: BLE001 - 见 docstring：不得让后台补图拖垮映射
            pass
    if _bible_collection(project_id, "scenes"):
        try:
            from app.domain.bible_ops.scene_bible_prep import _start_scene_refs_generation

            _start_scene_refs_generation(project_id, None, resume=True)
        except Exception:  # noqa: BLE001 - 见 docstring：不得让后台补图拖垮映射
            pass
