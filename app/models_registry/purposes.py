"""purpose 目录：从数据推导，不写枚举。L3（与 app.model_registry 同层）。

EP-05 §4 要求 purpose 合法值来自"能力目录 + 已有调用点"，新增阶段自动出现在
目录里、不改代码。本仓库当前的选路粒度仍是按 kind（text/vlm/video/image，见
``app/hiagent.py::active_provider``），还没有真正按阶段分流的调用点——把
"阶段"这个维度做成从 ``app.hiagent`` 调用栈静态扫描出来是过度设计（那意味着
50+ 个调用点全部要害改造才能验证，超出本阶段范围，见交付报告）。这里退一步、
但仍然完全数据驱动：

- **必须有 priority=0 绑定的"必需 purpose"** = 模型库里所有已注册模型条目
  实际声明过的 kind，各自要求一条 ``<kind>:default``。新增第五种 kind（比如
  "audio"）只需要有人在模型中心加一条带这个 kind 的模型，下次自检自动纳入，
  不改这个文件一个字符。
- **已知但非必需的 purpose** = ``model_bindings`` 表里已经存在的全部 purpose
  （含未来任何人通过 API 建的 ``text:screenplay`` 这类按阶段覆盖）——只要有人
  为某个阶段建过一条绑定，它就进入目录，这就是"新增阶段自动出现在目录里"在
  当前实现粒度下的体现。
"""
from __future__ import annotations

import logging

from app.models_registry import bindings

_logger = logging.getLogger(__name__)


def required_kind_purposes() -> set[str]:
    """模型库里至少一条条目声明过的 kind，各自要求 ``<kind>:default``。"""
    from app import model_registry  # 同层（L3），见文件顶部说明

    kinds: set[str] = set()
    for item in model_registry.catalog_items():
        for kind in item.get("kinds") or []:
            if isinstance(kind, str) and kind.strip():
                kinds.add(kind.strip())
    return {f"{kind}:default" for kind in kinds}


def known_purposes() -> set[str]:
    """必需 purpose ∪ 已经有绑定的 purpose。"""
    return required_kind_purposes() | bindings.distinct_purposes()


def missing_priority_zero_purposes() -> list[str]:
    """已知 purpose 里没有 priority=0 绑定的那些——对应阶段当前"未配置"。"""
    return [
        purpose for purpose in sorted(known_purposes())
        if bindings.get_priority_zero(purpose) is None
    ]


def startup_self_check() -> list[str]:
    """启动时调用一次。有缺口只记 WARNING、不阻止启动——模型库为空的新部署
    本来就什么都没配，这是如实报告而不是崩溃条件（EP-05 §11 陷阱 6）。"""
    missing = missing_priority_zero_purposes()
    if missing:
        _logger.warning(
            "模型路由自检：以下 purpose 缺少 priority=0 绑定，对应阶段将报"
            '"未配置模型"：%s', ", ".join(missing),
        )
    return missing
