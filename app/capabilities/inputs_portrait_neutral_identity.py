"""中性身份定妆照两个写命令（stage/adopt）的输入模型——独立小文件，不放进
``app/capabilities/inputs.py``：该文件当前 499/500 行，只剩 1 行余量，装不下
两个新类（CLAUDE.md「装不下时先想怎么拆，不要先想加基线」）。不放进
``app/capabilities/commands/bible.py``：该文件与 ``app/capabilities/handlers/
bible.py`` 互相在模块顶层引用对方（``commands.bible`` import ``handlers.bible``
以挂 handler，``handlers.bible`` 需要引用这两个 Input 类），两边谁定义这两个
类都会造成循环 import；独立成零依赖的第三个模块是最小改动。
"""
from __future__ import annotations

from app.capabilities.schemas import StandardCommandInput


class PortraitStageNeutralIdentityInput(StandardCommandInput):
    """``fingerprint`` 必须逐字回传 precheck 的返回值：范围漂移（人物谱外观
    改动/集号变化）会让服务端重算的指纹不再匹配而拒绝，见
    app.portraits.neutral_identity 模块 docstring。"""

    project_id: str; names: list[str]; from_episode: int; fingerprint: str


class PortraitAdoptNeutralIdentityInput(StandardCommandInput):
    project_id: str; character_name: str; from_episode: int
