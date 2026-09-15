"""终剪 xfade 输入链的顺序守卫。

FFmpeg 7 的 setpts 会把输出帧率标成未知（1/0），xfade 随即报「inputs needs to be a constant frame rate」；
fps 只有排在 setpts 之后才能重新声明恒定帧率（2026-09-15 在 B 的 ffmpeg 7.0.2 上三种顺序实测：fps 在前失败、
fps 在后成功、不加失败）。冒烟测试在无 CJK 字体的机器上会跳过，所以这里直接看源码里的链路模板。
"""

import re
from pathlib import Path

from app import final_edit


def test_xfade_video_chain_puts_fps_after_setpts() -> None:
    source = Path(final_edit.__file__).read_text(encoding="utf-8")
    chains = re.findall(r'\[\{index\}:v\]([^"]+)\[v\{index\}\]', source)
    assert chains, "找不到 xfade 视频输入链模板"
    for chain in chains:
        steps = [step.split("=")[0] for step in chain.split(",")]
        assert "fps" in steps and "setpts" in steps, chain
        assert steps.index("fps") > steps.index("setpts"), f"fps 必须排在 setpts 之后：{chain}"
