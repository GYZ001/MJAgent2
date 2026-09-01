"""映射包发布后的后台补图触发。"""
from __future__ import annotations


def start_background_portraits(project_id: str) -> None:
    """映射包发布之后，把缺图的定妆照交给已有的后台补图任务，不等它跑完。

    出图此前是在映射台内联做的，占了约三分之二的供应商时间（实测 EP1：
    image 469.6s / 全部调用 725.9s，映射墙钟 611s），用户按下"映射"要干等十
    分钟。映射台的职责因此收缩成纯文本——发现、建卡、绑别名；定妆照挪到这里
    起后台任务。

    复用 ``_start_refs_generation``：它本来就是异步任务、带 refs_status 进度，
    界面可以直接显示"定妆照后台生成中"，不另造一套后台机制。``resume=True``
    只补缺的、不重出已有的；已经在跑时它返回 None，什么都不做即可——那说明
    后台已经在处理同一批缺口。

    触发点放在 domain 层而不是 app.production.prep_pack 里：后者是 L4，引
    app.domain 是上行边（分层闸门实测拦下过）。编排本来就该由上层做。

    异常一律吞掉：后台补图起不来不该让映射本身失败——映射的产物是卡片，已经
    落库了；缺图会在发起付费视频时被参考图就绪校验拦住（单镜走
    _assert_shot_generation_gate，整集走 asset_gaps），不会静默流到生成台。
    """
    if not project_id:
        return
    try:
        from app.domain.bible_ops.refs_generation import _start_refs_generation

        _start_refs_generation(project_id, None, resume=True)
    except Exception:  # noqa: BLE001 - 见 docstring：不得让后台补图拖垮映射
        pass
