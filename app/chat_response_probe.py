"""对 chat 响应体的纯判定：有没有思考、是否思考耗尽预算、空 content 的交付状态。

从 ``app.hiagent`` 搬出的四个纯函数（2026-09-03，为「思考失控」降级重发腾出行数）。它们只读
响应 dict、不碰网络与库，也不依赖 ``ProviderError``——异常的构造仍留在 ``app.hiagent``。
"""
from __future__ import annotations


def _reasoning_present(data: dict) -> bool:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    return bool(message.get("reasoning") or message.get("reasoning_content"))


def _reasoning_used_all_output_budget(data: dict) -> bool:
    """判断推理模型是否在生成正文前已用完输出预算。

    OpenRouter 的 reasoning 与 message.content 共用 max_tokens。部分模型会在
    finish_reason=length 时只返回 reasoning、将 content 留为 null。
    """
    try:
        finish_reason = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return False
    return finish_reason == "length" and _reasoning_present(data)


def _empty_content_detail(data: dict) -> str:
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        return "响应结构中无可用 choice"
    finish_reason = choice.get("finish_reason") or "unknown"
    reasoning_present = bool(message.get("reasoning") or message.get("reasoning_content"))
    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    return (f"finish_reason={finish_reason}, reasoning_present={reasoning_present}, "
            f"completion_tokens={completion_tokens}")


def _content_delivery_absent(data: dict) -> bool:
    """Whether an OpenAI 兼容响应完全没有交付证据（既非答案也非可诊断的拒答）。

    一次真正跑完的补全，无论模型说了什么，最终 chunk 都会盖上一个终态
    ``finish_reason``（stop/length/content_filter/tool_calls/...），并且
    ``usage.completion_tokens`` 会如实反映它花掉的预算——即便答案是"什么都
    不说"。当这两个证据同时缺席（没有 finish_reason，且已入账的
    completion_tokens 为 0 或未上报）、content 又为空时，供应商没有对这次请求
    做出任何决定：这与 ``_stream_chat_completion`` 里"流在 [DONE] 前中断"
    （见 ``provider_answer_undelivered``）是同一种情况，只是这次流最终还是吐出
    了 ``[DONE]``，因此被记成了正常的 200。既然没有答案可挑选、也没有判断可
    保留，原样重放这份确定性请求一次是安全的——与 ``_reject_truncated_chat_response``
    对 ``OUTPUT_TRUNCATED`` 的论证同构，只是这里对应的是生成中途夭折，而不是
    预算耗尽。

    反之，只要 finish_reason 或 completion_tokens 任一项证明供应商确实跑完了
    这次请求（哪怕答案就是空字符串），就不属于这里——那是一次交付了的坏答案，
    必须继续 fail-closed，不能被这条重放豁免。
    """
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    if choice.get("finish_reason"):
        return False
    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    return not (isinstance(completion_tokens, int) and completion_tokens > 0)
