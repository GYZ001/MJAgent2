"""从 ``app.system_api`` 外移出的纯支持函数：文件系统浏览（``fs_browse``）。

``app/system_api.py`` 长期卡在行数基线零余量（``app/FILE_CONVENTIONS.toml``），
这组函数与"模型库"这个主题无关、纯粹是路径合法性判断，且没有被任何测试按
``system_api.<name>`` 之外的路径打桩，是低风险的搬迁对象——搬出后
``app.system_api`` 通过 ``from app.system_ops.fs_browse import name`` 把用得到
的名字重新导入自己的命名空间，``system_api.make_dir`` 这个既有外部调用点
（``app/capabilities/handlers/system.py``）不用改一行。

本包**不**收纳依赖 ``get_conn()`` 的函数：2026-09-23 曾尝试把
``_call_project_id``/``_job_project_id`` 等项目归属解析函数一并搬到这里，
实测炸出 9 个测试文件（``test_monitor_prd.py``/``test_startup_recovery.py``/
``test_provider_call_lifecycle.py``/``test_media_job_recovery.py`` 等）——它们
用 ``monkeypatch.setattr(system_api, "get_conn", fake_conn)`` 把 system_api
内部全部数据库访问重定向到测试自建的连接；这些函数搬到新模块后各自持有自己
的 ``get_conn`` 绑定（``from app.db import get_conn``），system_api 侧的
monkeypatch 完全失效——正是 CLAUDE.md「拆包会静默废掉 monkeypatch」那条的真实
案例，且触发失败的测试文件不在本任务可修改范围内，因此撤回了那部分搬迁，
只保留零 ``get_conn`` 依赖的 ``fs_browse``。教训：判断"能不能安全外移"不能只
grep 函数名本身有没有被打桩，还要 grep 它调用的**共享依赖**（这里是
``get_conn``）有没有被打桩。
"""
from __future__ import annotations
