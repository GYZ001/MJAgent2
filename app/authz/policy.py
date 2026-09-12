"""EP-01 权限判定：给定已解析的权限点集合，回答"这一个具体动作能不能做"。

纯函数，零 db、零 ``app.capabilities`` 依赖——真 L1（见
``app/LAYERS.toml::app.authz.policy``）。判定用的权限点集合由更高层（
``app.auth.sessions.resolve_session`` 汇总 ``app.orgs.store`` 的角色数据）
算好后注入 ``Principal.permission_keys``，本模块只做集合成员判断，不查库、
不认识 registry。

权限点语法（真源见 ``app/authz/catalog.py`` 顶部文档，与 EP-01 §5 一致）：
    permission_key ::= <command_name>            # Command Bus 命令
                      | "route:<METHOD> <path>"   # 豁免路由（目录/角色种子用，见下）
                      | "read:project"            # GET 缺省读面

``command_allowed``/``read_allowed`` 分别消费前两种形态；``"route:<METHOD>
<path>"`` 这种权限点目前只在 ``app.authz.catalog.build_permission_catalog()``
的目录展示与 ``org_admin``/``owner`` 模板的权限点集合里作为数据存在，没有
对应的判定函数（EP-01 第二阶段实测复核：曾经有一个 ``route_allowed()``，
2026-09-11 因为从未被任何调用方接线而删除——见该次交付报告"两条挂账"一节：
83 条豁免路由横跨 payments 公开回调、``require_system_admin`` 专属运维端点、
与挂在 ``_PROJECT_OWNER_DEPS`` 上的普通业务路由三类，统一按路由模板做权限点
判定是一次跨越整个 HTTP 层的行为变更，会让 producer/reviewer/viewer
——它们的内置模板目前一个 ``route:*`` 权限点都没有——对这 83 条路由从"不受
角色约束"直接变成"全部 403"，这是需要产品拍板并配一次全量回归的独立决策，
不是本次可以顺手做的判定层修补；真正接线前不要恢复这个函数，加回来又没有
调用方只会重新制造同一个死代码）。
"""
from __future__ import annotations

READ_PROJECT_KEY = "read:project"

#: 五个内置角色模板的 (key, 展示名, 说明)。放在 L1 是因为它要同时被 L2 的
#: ``app.orgs.store``（建种子行需要 key/name/description）与 L5 的
#: ``app.authz.catalog``（按 key 推导权限点集合）引用——两者不能互相 import
#: （L2 不能依赖 L5），共同的低层依赖只能放这里，避免同一张表在两处各写一份、
#: 将来改一处忘另一处。文案本身只是 UI 展示元数据，不是权限分类依据（分类
#: 依据见 ``app.authz.catalog`` 模块文档，全部来自 CommandSpec 结构化字段）。
BUILTIN_ROLE_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("org_admin", "组织管理员", "本组织全部命令（不含系统管理员专属）；不能改模型配置与系统设置"),
    ("producer", "制作", "全部 R0–R2 生产命令；不能审批交付、不能删除整集"),
    ("reviewer", "审校", "只读 + 分镜确认/版本采纳/参考图复核/停止任务 + 交付审批；不能发起付费生成"),
    ("viewer", "只读", "仅项目读面"),
    ("owner", "项目所有者", "本项目全部命令（不含系统管理员专属）；迁移后每个用户对自己名下项目自动持有"),
)

BUILTIN_ROLE_KEYS: frozenset[str] = frozenset(key for key, _, _ in BUILTIN_ROLE_TEMPLATES)


def command_allowed(permission_keys: frozenset[str], command_name: str) -> bool:
    """Command Bus 命令级判定：permission_key 必须逐字等于 command_name。

    空集合不等于放行——``command_name not in frozenset()`` 天然返回
    ``False``，不需要额外的"空集合特判"分支（CLAUDE 记录的"空集合不等于无需
    检查"在这里是集合成员判断的自然结果，不是需要额外写的 if）。
    """
    return command_name in permission_keys


def read_allowed(permission_keys: frozenset[str]) -> bool:
    """GET 缺省读面：命中通用的 ``"read:project"`` 权限点即放行。"""
    return READ_PROJECT_KEY in permission_keys
