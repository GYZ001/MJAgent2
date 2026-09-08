"""逐段提示词输出合同，字段说明与结构化身份/发声渲染一致。"""
from app.production.storyboard_continuity_memo import continuity_memo_output_contract_text


def segment_output_contract(source_indexes: list[int], *, min_shots: int, max_shots: int) -> dict:
    """每次调用传入实际来源范围与镜头预算，避免另造固定值。"""
    return {
        "prompt_text": "完整镜头文本，每句发声位置用 speech 占位符，系统校验后展开为可复制提示词",
        "shot_count": f"{min_shots}-{max_shots} 之间的整数，须与 prompt_text 里实际写的镜头数一致",
        "dialogue": (
            "本段实际出现的台词；required_dialogue 里的每一条必须逐句出现（原话"
            "逐字保留），除此之外可以是原文其它对话的压缩/改写，"
            "不要求逐字，但不得偏离本段剧情；每条必须给 utterance_id、delivery_kind 和 speaker_identity_id（引用"
            "已确认角色的 identity_id、群演 visual_entity_id 或未收录主体的原文称谓）与 source_segment_index"
            f"（这句话对应原文的哪一段，必须在 {source_indexes} 范围内）"
        ),
        "resources": "本段实际用到的人物/场景/道具，角色 identity_id 与场景 scene_id 取自 relevant_assets，未收录群演的 identity_id 保留原文称谓；素材库没有对应图的（scene_reference_id 或 portrait_id 为空）如实留空，不得编造",
        "degraded_capabilities": "本段因模型能力缺失而做的降级处理清单（例如 Seedance 侧的屏上文字改「无字」+ 后期合成说明）；没有降级则留空数组，不得留空字符串占位",
        "camera_digest": "本段实际选用的开场景别（opening_shot_size）、开场运镜（opening_camera_move），以及本段与上一段之间的转场类型（transition_from_previous，本集第一段留空）；只用于给接下来几段做参考，不进入分镜产出契约",
        "camera_repetition_rationale": "只在本段开场确实沿用了 recent_camera_language 里出现过的机位时才写理由；没有重复就留空，不得编造理由",
        "continuity_memo": continuity_memo_output_contract_text(),
    }
