"""视频供应商内容拒绝的落地文案：拒绝针对生成出来的画面时，出路是直接重新生成
（2026-10-05，《顾念长安》第1集）。

第32、33段被供应商以「output video may be related to copyright restrictions」连续
拒绝 3 次后，原样重新生成照样通过——拒绝针对的是生成出来的画面，每次画面都不同。
旧文案一律断言「直接点重新生成…仍会给出同样的拒绝」，对这类拒绝是假话，并把用户
引向改血腥字眼、改台词这两条无关出路。代码不按关键词给供应商原文分类（见
``app.hiagent.has_repeated_terminal_poll_failure`` 的约束），所以文案给出可由用户
对照原文判断的分支：原文说的是输出画面时直接重新生成；说的是提交的内容时才改
描述或台词。既有文案约束（不出现接口层措辞、点名「修订台词」等）见
``tests/test_video_provider_rejection_guidance.py``。
"""
from __future__ import annotations

from app import worker
from app.hiagent import ProviderError, ProviderFailure

REAL_COPYRIGHT_MESSAGE = (
    "The request failed because the output video may be related to copyright "
    "restrictions. Request id: 02179126169144000000000000000000000ffffac18056abb27af"
)


def _content_rejected_message() -> str:
    from app.media_exec.job_state import PROVIDER_CONTENT_REJECTED_KIND

    exc = ProviderError(
        f"视频模型 任务失败：{REAL_COPYRIGHT_MESSAGE}",
        raw=REAL_COPYRIGHT_MESSAGE,
        failure=ProviderFailure.model_rejection(PROVIDER_CONTENT_REJECTED_KIND),
    )
    _, message = worker._video_model_rejection_guidance({}, exc)
    return message


def test_provider_text_is_quoted_verbatim():
    assert REAL_COPYRIGHT_MESSAGE in _content_rejected_message()


def test_output_video_rejection_branch_says_regenerate_directly():
    message = _content_rejected_message()
    assert "生成出来的画面" in message and "output video" in message
    assert "直接点「重新生成」再试" in message


def test_same_rejection_claim_is_scoped_to_submitted_content_branches_only():
    """「仍会给出同样的拒绝」只能针对提交内容（画面描述、台词）的两种情况，
    不能再是对所有内容拒绝的断言。"""
    message = _content_rejected_message()
    assert "这两种情况直接点「重新生成」会原样重提同一段内容，供应商仍会给出同样的拒绝" in message
    assert "直接点「重新生成」会原样重提同一段提示词，供应商仍会给出同样的拒绝" not in message
