"""从完整原文保留对话块听者证据，避免阶段二裁剪后丢失归属。"""
import re

QUOTES = re.compile(r'[「“『"]([^」”』"]{2,})[」”』"]')
SOURCE_TAG = re.compile(r"\[段\d+(?:·S\d+)?\]")


def following_dialogue_block(text: str, quote_end: int) -> str:
    """跳过连续引句，停在第一段叙事；不越过任何非标点叙事内容。"""
    remainder = text[quote_end:]
    while True:
        found = QUOTES.search(remainder)
        if found is None:
            return SOURCE_TAG.sub("", remainder)
        prefix = SOURCE_TAG.sub("", remainder[:found.start()])
        if re.search(r"\w", prefix):
            return SOURCE_TAG.sub("", remainder[:found.start()])
        remainder = remainder[found.end():]


def dialogue_listeners(text: str, quote_start: int, quote_end: int, names: list[str]) -> list[str]:
    """紧随整个对话块的具名听者。返回否定归属证据，不据此猜测发声者。"""
    enclosing = next((m for m in QUOTES.finditer(text) if m.start() <= quote_start < m.end()), None)
    after = following_dialogue_block(text, enclosing.end() if enclosing else quote_end).lstrip('」”』"，。！？…、 \n\t')
    return [name for name in names if name and re.match(re.escape(name) + r".{0,6}(?:听|闻)", after)]


def note_delivery_kind(speaker: str, note: str) -> str:
    """显式剧本声道标记优先于引号样式；无标记时不猜测画内/画外。"""
    if speaker in {"旁白", "叙述者", "narrator"}:
        return "narration"
    if re.search(r"(?i)(?:\bOS\b|内心|心声|独白)", note):
        return "inner_monologue"
    if re.search(r"(?i)(?:\bVO\b|画外)", note):
        return "offscreen_dialogue"
    return ""
