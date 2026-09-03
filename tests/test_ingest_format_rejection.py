"""WS1：格式拒绝——RTF/HTML 混充 TXT 上传必须在 ingest 层被拦下并给出路。

背景（生产实测，项目「橘座在上」proj_5a2ab19ef388）：用户把 RTF 文件另存/改名成
.txt 上传，``validate_novel_filename`` 只查后缀通过，RTF 控制字（
``{\\rtf1\\ansi\\ansicpg936\\cocoartf2870...``）被当纯文本，全书按 3000 字自动
切分成 12 段无标题「章节」，11/11 次下游剧本生成失败。这里直接测
``app.ingest.ingest_novel``——它是 ``_read_novel_upload``（附件预检）与
``_create_project_core``（正式导入）共用的领域入口，两条路径的拒绝行为完全一致。
"""
from __future__ import annotations

import pytest

from app.ingest import ingest_novel

# 与生产项目「橘座在上」chapters[idx=1].content 的真实开头逐字一致（取前 120 字）。
_REAL_RTF_HEAD = (
    r"{\rtf1\ansi\ansicpg936\cocoartf2870"
    "\n"
    r"\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\froman\fcharset0 Times-Bold;"
    r"\f1\froman\fcharset0 Times-Roman;}"
    "\n"
    r"{\colortbl;\red255\green255\blue255;}"
)


def test_ingest_rejects_rtf_masquerading_as_txt() -> None:
    with pytest.raises(ValueError, match="RTF"):
        ingest_novel(_REAL_RTF_HEAD.encode("utf-8"))


def test_ingest_rtf_rejection_gives_a_way_out() -> None:
    with pytest.raises(ValueError) as exc_info:
        ingest_novel(_REAL_RTF_HEAD.encode("utf-8"))
    message = str(exc_info.value)
    # 必须同时给出至少一条可执行的路径，不能只说「不支持」把用户晾在原地。
    assert "TXT" in message
    assert "EPUB" in message


def test_ingest_rejects_html_document() -> None:
    html = "<!DOCTYPE html>\n<html><head><title>x</title></head><body>正文</body></html>"
    with pytest.raises(ValueError, match="HTML|网页"):
        ingest_novel(html.encode("utf-8"))


def test_ingest_rejects_bare_html_tag_without_doctype() -> None:
    html = "<html>\n<body>缺少 DOCTYPE 声明的网页正文</body>\n</html>"
    with pytest.raises(ValueError, match="HTML|网页"):
        ingest_novel(html.encode("utf-8"))


def test_ingest_does_not_reject_prose_that_merely_mentions_html() -> None:
    """锚定在文档开头，避免正文里偶然出现「html」类词语被误拦。"""
    text = (
        "第一章 相遇\n"
        "林舟打开电脑，随手写了一段 html 代码给沈青看，两人相视一笑。\n"
        "「这就是网页吗？」沈青问。"
    )
    result = ingest_novel(text.encode("utf-8"))
    assert result["chapter_count"] == 1
    assert "html" in result["chapters"][0]["content"]


def test_ingest_still_rejects_genuine_binary_payload() -> None:
    """既有二进制拒绝逻辑不受 RTF/HTML 检测影响（回归锁定）。"""
    with pytest.raises(ValueError, match="二进制内容"):
        ingest_novel(b"\x00\x01\x02\x03not-a-novel")
