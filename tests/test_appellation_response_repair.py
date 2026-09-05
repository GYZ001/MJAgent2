"""WS2-2a：称谓解析响应条目缺 raw_label 的确定性修补
（app.production.prep_pack.appellation_response_repair）。

真实故障形态：B 库 7 天内多次撞见 `appellations.N.raw_label Field
required`——``_AppellationVerdict.raw_label`` 是 pydantic 必填字段，模型
偶发漏填。这里验证：能借用兄弟条目 raw_label 的救回来、救不了的丢弃并
留痕、不影响本来就合法的响应、且不放松 schema（真正畸形的输入仍然让
pydantic 校验失败）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.production.prep_pack import appellation_resolve as ar
from app.production.prep_pack.appellation_response_repair import repair_appellation_payload


def test_missing_raw_label_borrows_from_identical_sibling():
    """真实故障形态：模型对同一次申报重复输出两遍，其中一遍漏填
    raw_label——修补后借用兄弟条目的 raw_label，原校验（pydantic
    model_validate）通过。"""
    payload = {"appellations": [
        {"identity": "里奥", "evidence": "我八岁的时候被诊断出长不高。", "segment_indexes": [2]},
        {
            "raw_label": "八岁男孩", "identity": "里奥",
            "evidence": "我八岁的时候被诊断出长不高。", "segment_indexes": [2],
        },
    ]}
    repaired = repair_appellation_payload(payload)
    response = ar._AppellationResolutionResponse.model_validate(repaired)
    assert len(response.appellations) == 2
    assert {item.raw_label for item in response.appellations} == {"八岁男孩"}


def test_missing_raw_label_without_sibling_is_dropped_not_blanked():
    """补不上的条目丢弃，不是拿空字符串顶替——原校验（pydantic）通过，
    且丢弃后的条目数量减少，不静默保留一条空 raw_label。"""
    payload = {"appellations": [
        {"raw_label": "众猴", "identity": ar.COLLECTIVE, "segment_indexes": [1]},
        {"identity": "里奥", "evidence": "无可借用的兄弟条目", "segment_indexes": [3]},
    ]}
    repaired = repair_appellation_payload(payload)
    response = ar._AppellationResolutionResponse.model_validate(repaired)
    assert len(response.appellations) == 1
    assert response.appellations[0].raw_label == "众猴"


def test_legal_payload_is_untouched():
    """合法输入（全部条目都带 raw_label）：修补是空操作，返回同一个对象，
    不产生任何改写。"""
    payload = {"appellations": [
        {"raw_label": "少年", "identity": "里奥", "evidence": "", "segment_indexes": [1]},
    ]}
    repaired = repair_appellation_payload(payload)
    assert repaired is payload


def test_malformed_top_level_shape_is_left_for_original_validation_to_reject():
    """修不了的形态：appellations 不是数组这种更严重的畸形，不在这个模块的
    职责范围内，原样返回交还给原有格式重试处理——原校验（pydantic）仍然
    报错，不被这里悄悄放过。"""
    payload = {"appellations": "不是数组"}
    repaired = repair_appellation_payload(payload)
    assert repaired == payload
    with pytest.raises(ValidationError):
        ar._AppellationResolutionResponse.model_validate(repaired)


def test_blank_raw_label_without_matching_signature_is_also_dropped():
    """raw_label 是空白字符串（不是完全缺失 key）同样视为缺失，走丢弃分支
    而不是被当成"模型申报了一个空标签"放行。"""
    payload = {"appellations": [
        {"raw_label": "   ", "identity": ar.UNRESOLVED, "evidence": "", "segment_indexes": [1]},
    ]}
    repaired = repair_appellation_payload(payload)
    response = ar._AppellationResolutionResponse.model_validate(repaired)
    assert response.appellations == []
