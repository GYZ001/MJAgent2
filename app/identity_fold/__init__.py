"""身份权威冲突里"注册路由 vs 本批临时折叠路由"的确定性裁决（WS2-C）。

背景：``app.identity_authority.identity_authority_registry`` 把每条身份决议
按 ``authority_id`` 归拢进它所属的 ``identity_group`` 集合，同一 authority_id
横跨多个 identity_group 时判定为 ``canonical_identity_multiple_identity_
groups`` 冲突、抛 ``IdentityAuthorityConflictError`` 判死整批。这个判据在两条
路由的语义地位并不对等时会误伤：``bible:<name>`` 这种"自身即注册路由"（角色
圣经登记，或某次决议已确认具名绑定）的 group 天然跨集稳定；而 discovery
projector 给模型批内分组打的 token（形如 ``current-1:F18``，可能再叠一段
``identity_scope_fingerprint`` 前缀）只在这一次判别调用内有意义。当同一
authority_id 同时落进这两类 group、且除了自身注册路由外其余全部是这种批内
临时折叠 token，责任在折叠误把批内分组并进了已有角色，不在注册路由本身——
真实案例：神墓（proj_f28fc90b014d ep_55968c58391a），authority_id=bible:雨馨
同时挂着注册路由 ``bible:雨馨`` 与折叠路由
``1d80620bd1947717f334afabaae29aaacdb762769443bb4650c395ea51229a70:current-
1:F18``（雨馨本集被回忆提及、未登场），此前判死整集剧本。

判据是结构性的，不针对任何具体人名/称谓：``current-\\d+:F\\d+`` 是 discovery
projector 自己的批内分组命名格式（``app.identity_authority`` 内多处文档字符
串点名同一格式），不是从本仓库现存数据反推的黑名单。其它冲突形态（两个具名
canonical_name 挤在一个 identity_group、同一 canonical_name 对应多个 named
authority）不属于这条规则，原样交给调用方的 issues 判死。

层号：随 ``app.identity_authority``（L1，app/LAYERS.toml）声明为同层——零
app 内部依赖，只被 identity_authority 单向导入。不放 app/ 根目录散文件（新模
块须进包，见 app/LAYERS.toml「app.novel」的同类先例）。
"""
from __future__ import annotations

import re
from typing import Any

_BATCH_FOLD_GROUP_RE = re.compile(r"current-\d+:F\d+", re.I)


def reconcile_registered_authority_folds(
    groups_by_authority: dict[str, set[str]],
    entries: dict[str, dict[str, Any]],
) -> None:
    """就地裁决：保留注册路由、拒绝本批折叠，并在 entries 上留可见 note。

    ``groups_by_authority``/``entries`` 由调用方（identity_authority_
    registry）在生成冲突 issues 之前原地传入，本函数原地修改后返回 None；
    只处理"自身即注册路由 + 其余全部是批内折叠 token"这一种结构，其它冲突
    形态不改。
    """
    for authority_id, identity_groups in groups_by_authority.items():
        if authority_id not in identity_groups or len(identity_groups) <= 1:
            continue
        fold_groups = {
            group for group in identity_groups
            if group != authority_id and _BATCH_FOLD_GROUP_RE.search(group)
        }
        other_groups = identity_groups - {authority_id} - fold_groups
        if not fold_groups or other_groups:
            continue
        groups_by_authority[authority_id] = {authority_id}
        entry = entries.get(authority_id)
        if entry is not None:
            entry.setdefault("conflict_notes", []).append(
                f"本批折叠路由 {sorted(fold_groups)} 与已注册身份 {authority_id} "
                "冲突，已保留注册路由、拒绝本次折叠"
            )


__all__ = ["reconcile_registered_authority_folds"]
