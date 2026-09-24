"""视频供应商请求提交前的本地形态校验（从 ``app.hiagent.create_video_task``
搬出，2026-09-24 角色固定音色 U3：扩展支持 ``reference_audio``）。

包根不做再导出门面（CLAUDE.md「再导出门面不得再长，且必须从真源导出」，与
``app.voice`` 同一先例）：调用方直接写
``from app.video_submission.guard import ...``，不经过这里。
"""
from __future__ import annotations
