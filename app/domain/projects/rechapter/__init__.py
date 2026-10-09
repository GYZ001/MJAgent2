"""存量项目切章修正：冻结已进入生产的集与章节，只对尾部未产出区间重切。

背景与方案见 2026-10 analysis 轮的交付报告（未落盘为文档，见派单历史）：
``app/novel/structure.py``/``app/ingest.py`` 的切章 bug 修复（890a5237）之后，
已导入项目里按旧算法切出来的章节仍带着"残片+标题"粘连；但一旦某集已经产生
剧本/分镜/视频等任何产物，就不能再改它绑定的章节一个字节——改了会让已发布
的指纹（``app.storyboard_workspace.episode_fingerprint`` 等）失配，进而让已
confirm 的人工验收作废。

这个包只处理"冻结生产前沿、只重切未产出尾部"这一种安全策略（对应分析报告
里的方案 A），不提供"全量重切、连已产出集一起作废重算"的方案 B——那个选项
留给用户在更高层显式拍板，不由这里的代码自动执行。

子模块：
- ``models``：计划阶段使用的纯数据结构（不含任何 I/O）。
- ``frozen``：冻结判据——命中产物信号的集 + 它们绑定/被分镜引用的章节。
- ``segment``：把非冻结章节切成「最大连续段」，并对每段调用修好的切章器
  （只调用 ``app.ingest._find_heading_matches``/``_split_chapters_with_removed``
  两个函数，不复刻切章算法本身）重切、做残片剔除与 idx 稳定性校验。
- ``plan``：只读编排——产出一份完整的 ``ProjectPlan``，不写库。
- ``report``：把 ``ProjectPlan`` 渲染成中文可读报告/JSON，并扫描世界书里落
  在受影响章节号上的引用（只报告，不改）。
- ``apply``：唯一的写库点——在一个 ``BEGIN IMMEDIATE`` 事务内重新核验计划仍
  然成立，再写 chapters/episodes，最后提交后才记审计。

这里刻意不做再导出门面（不写 ``__all__``，不 ``from .x import y as y``）：
调用方（``scripts/rechapter_project.py``）直接按子模块路径导入需要的符号。
"""
from __future__ import annotations
