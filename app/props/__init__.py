"""世界书物件库（道具跨集一致性锚点，与 ``app.scenes`` 场景库同构）。

道具此前只有映射台（prep pack）抽出的 ``asset_manifest.props``（label+
description 文字描述，无素材库）——这正是用户投诉的根因：相邻两段视频里同一件
道具形态漂移（猫包一会儿网状一会儿透明），因为出图时没有任何跨集锚点可依赖。
本包给"关键道具"（判据见 ``judge.is_key_prop_mention`` docstring）补上
appearance_canonical 锚点串 + 纯色背景参考图，登记进 ``Bible.props`` 与
``prop_references`` 表（自建，不进 ``app/db.py``，见 ``store.py`` docstring）。

公开入口三个：``ensure_props_for_labels``（映射台反应式登记，供
``app.production.prep_pack.discovery`` 调用）、``prop_reference_for_episode``/
``props_for_project``（按集查询/列表，供 API 用）。``regenerate_prop_reference``
是列表页"重新生成参考图"按钮的服务端实现，一并导出。
"""
from __future__ import annotations

from .service import (
    ensure_props_for_labels as ensure_props_for_labels,
    props_for_project as props_for_project,
    regenerate_prop_reference as regenerate_prop_reference,
)
from .store import (
    prop_reference_for_episode as prop_reference_for_episode,
)
