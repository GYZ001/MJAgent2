"""成片台字幕嵌入（PRD/成片台字幕嵌入_台词对齐字幕PRD.md）。

分工：``align``（台词 × ASR 逐字对齐，L1）、``cues``（对齐 → 字幕条，L1）、
``timeline``（镜内时间 → 整集时间轴，L1）、``ass``（ASS/SRT 渲染与 ffmpeg 滤镜串，L1）、
``asr_worker``（子进程里跑 sherpa-onnx，主进程永不 import 它的依赖，L1）、
``store``（对齐结果缓存表，L2）、``engine``（wav 抽取 + 子进程调度，L4）、
``episode``（整集编排，L4）。本文件不做任何再导出。
"""
