"""``new_id``/``now`` 两个零依赖叶子原语——主键生成与时间戳，仅 stdlib。

从 ``app/db.py`` 抽出（该文件在架构复测中被点名：``app.db`` 与
``app.quota``/``app.quota_addon``/``app.quota_tiers``/``app.orgs.*``/
``app.quota_policy.*`` 等模块构成的基础设施循环团里，这两个函数是最常见的
反向依赖来源——那些模块只是要造一个主键或取一个时间戳，却因此在依赖图上背
上一条指向 ``app.db``（L2，持久化）的边）。

零 app 内部依赖：只 import ``time``/``uuid``，可被任何层号的模块安全导入而
不产生上行边或参与循环。``app/db.py`` 从这里 re-export（``from
app.db_primitives import new_id as new_id, now as now``），db.py 现有的 120+
个调用方继续用 ``from app.db import new_id, now`` 不受影响；新代码、以及本次
一并改掉的 ``app.quota``/``app.quota_addon``/``app.quota_policy.plans``/
``app.quota_policy.storage``/``app.quota_policy.allocation``/``app.orgs.store``
六个模块，直接从本模块导入。

不要往这里加任何非纯函数的内容——一旦这个模块开始 import 别的 app 内部模块，
它作为「循环团断点」的价值就没有了。
"""
from __future__ import annotations

import time
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()
