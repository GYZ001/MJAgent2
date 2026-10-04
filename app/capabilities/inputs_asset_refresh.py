"""「参考资产已更新」面板两个写命令的输入模型——独立小文件，不放进
``app/capabilities/inputs.py``：该文件当前 499/500 行，已经没有余量（CLAUDE.md
「装不下时先想怎么拆，不要先想加基线」，同款先例见 ``inputs_portrait_neutral_
identity.py`` 的模块文档）。
"""
from __future__ import annotations

from pydantic import Field

from app.capabilities.inputs import EpisodeScopedInput


class VideoAssetRefreshRegenerateInput(EpisodeScopedInput):
    """为空表示对本集全部"参考资产已更新"分组一次性重生成，非空只处理这些
    ``entity_key``（见 ``app.domain.video_ops.asset_drift._diff_item``）。"""

    entity_keys: list[str] = Field(default_factory=list)
    qualification_version: str | None = None


class VideoAssetRefreshAdoptInput(EpisodeScopedInput):
    """``versions`` 是 ``{shot_id: version_id}``——所有权显式：由调用方（面
    板）指定每段具体采用哪个版本，后端不猜"最新"（CLAUDE.md「所有权必须显
    式」）。``reason`` 复用 ``StandardCommandInput`` 已有字段，与单镜采用
    （``video.adopt_version``）同一条"必须说明理由"的约束。"""

    entity_key: str
    versions: dict[str, str] = Field(default_factory=dict)
    qualification_version: str | None = None
