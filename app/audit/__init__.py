"""操作审计 + 最近活跃（app/LAYERS.toml 声明 "app.audit" = 2 前缀，
"app.audit.api" = 5）。

非 api.py 的子模块只 import app.db / app.config / app.auth.principal /
app.monitor_audit_buffer + stdlib；绝不 import app.capabilities.* / app.errors /
app.main 等高层模块——总线钩子（app/capabilities/bus.py）把 status/summary
等以纯字符串传给这里的记录器，依赖方向永远是「高层 import 本包」，不能反过来。
后台巡检循环的失败用 stdlib logging 记录，不借道 app.errors。
"""
from __future__ import annotations
