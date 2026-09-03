"""小说领域的非摄入子模块（当前只有 ``structure``：章节结构识别）。

``app.ingest`` 仍是摄入流水线的入口，不从这里再导出任何符号——避免制造
「删一行未使用 import 就打断再导出链」的门面风险（见 CLAUDE.md
「再导出门面不得再长，且必须从真源导出」）。需要结构判据的调用方直接
``from app.novel.structure import ...``。
"""
from __future__ import annotations
