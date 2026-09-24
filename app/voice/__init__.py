"""声音生成：给角色设计固定音色（本单只做模型层）。

背景：短剧角色需要「凭文字描述生成一个新音色」——生成一段试听音频 + 一个
供应商音色 ID，之后固定绑定到人物卡，跨集复用。本单交付的是模型适配层
（``app.voice.providers``）：统一的连接/请求/结果类型、按协议分发的适配器、
以及接入 ``app.models_registry.routing`` 的 purpose 选路，让「换绑定即换
供应商，业务代码不改」成立。人物卡音色绑定、试听/裁片/校验、视频请求接入
（参考音频传给 Seedance）都是后续单元，尚未在本包出现——见
``docs/角色固定音色_声音生成接口调研与实施方案_2026-09-23.md`` 第 5.1/5.2/5.3
节与第 8 节的单元拆分（U1/U3）。

``app.voice`` 包根目前没有代码，只为后续那些业务模块预留层号（见
``app/LAYERS.toml`` 的 ``"app.voice" = 4``）。本包有意不做再导出门面
（CLAUDE.md「再导出门面不得再长，且必须从真源导出」）：调用方直接写
``from app.voice.providers.dispatch import design_voice``，不经过这里。
"""
from __future__ import annotations
