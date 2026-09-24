"""文本模型免费调用：由人物卡外观/性格/语风/定位/年代写出声音描述与试听台词。

写完整的正面陈述（CLAUDE.md「Prompts」），不写禁令式约束。同项目已有角色的
``voice_prompt`` 作为上下文一并给出，要求互相区分——探针实测（见方案 §10）
三个角色两两声纹相似度都在 0.3 以下，说明"要求区分"这句话对模型确实有效。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.schemas import Character

MAX_TOKENS = 500
TEMPERATURE = 0.3


class _VoiceDescriptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_prompt: str
    preview_text: str


def _existing_block(existing_prompts: dict[str, str]) -> str:
    if not existing_prompts:
        return "本项目暂无其它角色的声音描述，无需刻意区分。"
    lines = "\n".join(f"- {name}：{prompt}" for name, prompt in existing_prompts.items())
    return f"本项目已有以下角色的声音描述，新设计要能与它们听辨区分：\n{lines}"


def _prompt(character: Character, *, era: str, existing_prompts: dict[str, str]) -> str:
    return f"""请为以下人物设计声音特征描述与一句试听台词，供声音生成模型使用。

人物档案：
- 姓名：{character.name}
- 定位：{character.role or "未标注"}
- 外观：{character.appearance_canonical or "未标注"}
- 性格：{character.personality or "未标注"}
- 语风：{character.speech_style or "未标注"}
- 故事年代背景：{era or "未标注"}

{_existing_block(existing_prompts)}

请输出两项：
1. voice_prompt：约 100 字的声音特征描述，逐项具体写出性别、年龄段、音高
   （偏高/中/偏低）、音色质感（如清亮/沙哑/浑厚/软糯）、语速（偏快/适中/
   偏慢）、情绪基调、口音（没有特别设定就写"标准普通话"）这七项取值，
   与上面列出的其它角色相比要能听辨出明显差异，避免用相近的音色质感或语速。
2. preview_text：一两句符合这个人物说话风格的中性台词，18～30 个汉字，
   语气与用词贴合人物的性格与语风，只用于试听，不涉及任何具体剧情事件，
   也不出现人物姓名或其它专有名词。

只输出符合 Schema 的 JSON。"""


async def generate_voice_description(
    character: Character, *, era: str, existing_prompts: dict[str, str],
) -> tuple[str, str]:
    """返回 ``(voice_prompt, preview_text)``，均已 strip；不做额外语义校验——
    交给下游生成流程与语音识别核验兜底，本函数只负责"问出一个建议"。"""
    prompt = _prompt(character, era=era, existing_prompts=existing_prompts)
    schema = _VoiceDescriptionResponse.model_json_schema()
    operation_id = "voice_description:" + evidence_repository.content_hash({
        "character": character.name, "era": era, "existing": sorted(existing_prompts),
    })
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_VoiceDescriptionResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        output_schema=schema,
        call_meta={"stage_key": "voice_description", "character_name": character.name},
    )
    return response.voice_prompt.strip(), response.preview_text.strip()
