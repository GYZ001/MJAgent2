"""领域 Command Handler 包（PRD M2+M3）。

每个子模块按领域拆分（project/bible/scene/episode/screenplay/storyboard/video/
delivery/run/system），只调用 `app/api.py`、`app/planning.py`、
`app/orchestration/*`、`app/delivery.py`、`app/worker.py`、`app/system_api.py`
中已存在的函数，禁止用 httpx 反向回调本机 REST。

`app/capabilities/catalog.py` 在注册每个 `CommandSpec` 时导入并挂载对应 handler。
"""
from __future__ import annotations
