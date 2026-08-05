"""小说摄入：编码识别、章节切分、广告清洗（PRD §4.1）。纯本地处理，不调模型。"""
from __future__ import annotations

import re

_CHAPTER_NUMERALS = "0-9一二三四五六七八九十百千万零〇两壹贰叁肆伍陆柒捌玖拾佰仟"
_CHAPTER_CORE = (
    rf"(?:第[{_CHAPTER_NUMERALS}]+[章卷回节]"
    r"|序章|楔子|引子|前言|后记|尾声|终章|番外(?:[0-9一二三四五六七八九十]+)?"
    r"|Chapter\s+\d+)"
)
CHAPTER_RE = re.compile(
    rf"^\s*[【\[]?\s*({_CHAPTER_CORE}[^\n】\]]{{0,40}}?)\s*[】\]]?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
CHAPTER_ID_RE = re.compile(
    rf"^(第[{_CHAPTER_NUMERALS}]+[章卷回节])(.*)$",
)

AD_MARKERS = (
    "http://", "https://", "www.", "求收藏", "求推荐", "求月票",
    "本章完", "天才一秒记住", "最新章节", "笔趣阁", "顶点小说", "手机阅读",
)
SOCIAL_AD_RE = re.compile(
    r"(?:关注|搜索|添加|加|回复).{0,8}(?:微信公众号|微信号|微信公众平台|QQ群|qq群)"
    r"|(?:微信公众号|微信号|QQ群|qq群).{0,12}(?:关注|搜索|添加|加入|交流|福利|领取)",
)
PUBLISHING_PROMO_RE = re.compile(
    r"(?:推荐票|推荐榜|周推荐|月推荐|会员点击|起点币|新书发布会|发布\s*vip\s*章节|"
    r"免费抽奖|大转盘抽奖|"
    r"ipadmini|支持正版|码字|耳根)"
    r"|(?:求|投|给|支持|呼唤|拜托|别忘|争|拼|保底|双倍|冲).{0,12}月票"
    r"|月票.{0,24}(?:求|投|给|支持|呼唤|拜托|别忘|榜|第一|过万|爆发|订阅|双倍|保底|危机)"
    r"|(?:收藏.{0,24}(?:新书|推荐票|抽奖|奖励|会员点击)|新书.{0,24}收藏)"
    r"|(?:兄弟姐妹|道友们|大大们).{0,40}(?:爆发|更新|推荐|月票|订阅|活动)"
    r"|(?:第[0-9一二三四五六七八九十百]+更|[0-9一二三四五六七八九十]+更(?:送上|爆发))"
    r"|(?:今日|今天|凌晨|晚上|白天).{0,24}(?:爆发|更新|还有[0-9一二三四五六七八九十]+[章更])"
    r"|(?:手游|游戏).{0,24}(?:公测|下载地址|礼包)",
    re.IGNORECASE,
)
AUTHOR_NOTE_RE = re.compile(
    r"(?:书评区|读者|书友|道友|兄弟姐妹|大大|公众(?:威信|微信|号)|公众号|起点|作者|"
    r"写书|本书|本章|章节|更新|爆发|上架|订阅|月票|推荐|点击|收藏|抽奖|活动|"
    r"商城|实体书|码字|老婆|女儿|生日|见面会|签售|公测|游戏)",
    re.IGNORECASE,
)
PROMO_SEPARATOR_RE = re.compile(r"(?:[-_=~*]{4,}|[－—～·]{2,})")
SEPARATOR_ONLY_RE = re.compile(r"^(?:[-_=~*]{4,}|[－—～·]{4,})$")
TRAILING_JUNK_ONLY_RE = re.compile(r"^[;；,，:：|丨]+$")
TRAILING_SERIAL_MARKER_RE = re.compile(
    r"\s*[\(（]?\s*未完待续[\s。.．…·]*[\)）]?\s*$",
)

FALLBACK_CHUNK_CHARS = 3000
STUB_CHAPTER_MAX_CHARS = 120
MAX_NOVEL_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CONTROL_CHAR_RATIO = 0.01


def decode_novel(raw: bytes) -> str:
    bom_encodings = (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            return raw.decode(encoding)
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def validate_novel_text(text: str) -> None:
    """Reject binary/corrupt uploads before they become an unusable project."""
    if not text or not text.strip():
        raise ValueError("文件没有可读取的正文内容，请检查后重新选择")
    if "\x00" in text:
        raise ValueError("文件包含二进制内容，不是可读取的 TXT 小说")
    controls = sum(
        1 for char in text
        if ord(char) < 32 and char not in {"\n", "\r", "\t", "\f"}
    )
    if controls / max(len(text), 1) > MAX_CONTROL_CHAR_RATIO:
        raise ValueError("文件包含过多不可见控制字符，请另存为 UTF-8 TXT 后重试")


def clean_text(text: str) -> tuple[str, int]:
    """去广告行、归一空白。返回 (清洗后文本, 删除行数)。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept, removed = [], 0

    def drop_separator_before_ad() -> int:
        while kept and not kept[-1]:
            kept.pop()
        if kept and SEPARATOR_ONLY_RE.fullmatch(kept[-1]):
            kept.pop()
            return 1
        return 0

    def looks_like_chapter_heading(value: str) -> bool:
        if CHAPTER_RE.fullmatch(value):
            return True
        match = re.match(rf"^第[{_CHAPTER_NUMERALS}]+", value)
        if not match:
            return False
        remainder = value[match.end():].strip()
        return bool(remainder) and len(remainder) <= 48

    # Remove multi-line author-note blocks introduced by a standalone separator.
    # Stop before the next normal or malformed chapter heading so merged source
    # chapters remain available for the splitter to recover.
    index = 0
    while index < len(lines):
        if not SEPARATOR_ONLY_RE.fullmatch(lines[index].strip()):
            index += 1
            continue
        end = index + 1
        probe: list[str] = []
        while end < len(lines):
            candidate = lines[end].strip()
            if candidate and looks_like_chapter_heading(candidate):
                break
            if candidate and len(probe) < 4:
                probe.append(candidate)
            end += 1
        probe_text = "\n".join(probe)
        if probe_text and (
            PUBLISHING_PROMO_RE.search(probe_text) or AUTHOR_NOTE_RE.search(probe_text)
        ):
            removed += sum(1 for value in lines[index:end] if value.strip())
            lines[index:end] = [""] * (end - index)
        index = max(index + 1, end)

    author_note_block = False

    for line in lines:
        stripped = line.strip()
        stripped, serial_marker_count = TRAILING_SERIAL_MARKER_RE.subn("", stripped)
        stripped = stripped.strip()
        if author_note_block:
            if stripped and looks_like_chapter_heading(stripped):
                author_note_block = False
            elif stripped and (
                PUBLISHING_PROMO_RE.search(stripped) or AUTHOR_NOTE_RE.search(stripped)
            ):
                removed += 1
                continue
            else:
                author_note_block = False
        if serial_marker_count and not stripped:
            removed += drop_separator_before_ad()
            removed += 1
            continue
        is_social_ad = bool(
            stripped and len(stripped) <= 160 and SOCIAL_AD_RE.search(stripped)
        )
        is_publishing_promo = bool(
            stripped and len(stripped) <= 800 and PUBLISHING_PROMO_RE.search(stripped)
        )
        prior_nonempty = next((item for item in reversed(kept) if item), "")
        after_separator = bool(prior_nonempty and SEPARATOR_ONLY_RE.fullmatch(prior_nonempty))
        inline_note_prefix = ""
        for match in reversed(list(PROMO_SEPARATOR_RE.finditer(stripped))):
            suffix = stripped[match.end():].strip()
            if suffix and (
                PUBLISHING_PROMO_RE.search(suffix) or AUTHOR_NOTE_RE.search(suffix)
            ):
                inline_note_prefix = stripped[:match.start()].strip()
                break
        is_author_note = bool(
            stripped and len(stripped) <= 1200 and (
                (after_separator and AUTHOR_NOTE_RE.search(stripped))
                or inline_note_prefix
            )
        )
        if stripped and (is_publishing_promo or is_author_note):
            # Scraped chapters often append an author note after a decorative
            # separator on the same line. Preserve the story sentence before it.
            prefix = inline_note_prefix
            if prefix:
                kept.append(prefix)
            else:
                removed += drop_separator_before_ad()
            removed += 1
            author_note_block = after_separator
            continue
        if stripped and (
            any(marker in stripped for marker in AD_MARKERS) or is_social_ad
        ):
            removed += drop_separator_before_ad()
            removed += 1
            continue
        if serial_marker_count:
            removed += 1
        kept.append(stripped)
    while kept:
        if not kept[-1]:
            kept.pop()
            continue
        if SEPARATOR_ONLY_RE.fullmatch(kept[-1]) or TRAILING_JUNK_ONLY_RE.fullmatch(kept[-1]):
            kept.pop()
            removed += 1
            continue
        break
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return cleaned.strip(), removed


def normalize_chapter_title(title: str) -> tuple[str, str]:
    """Return a stable ``(ordinal, subject)`` identity for duplicate-heading checks."""
    compact = re.sub(r"[\s　]+", "", title or "")
    compact = re.sub(r"[！!？?，,。．·：:；;（）()【】\[\]《》〈〉“”\"'‘’—…_\-]+", "", compact)
    match = CHAPTER_ID_RE.match(compact)
    if not match:
        return "", compact
    ordinal, subject = match.group(1), match.group(2)
    # Scraped novels commonly publish a TOC-like heading "某章（上）" immediately
    # before the real body heading "某章".  Ignore only a trailing part marker when
    # comparing a short stub with its adjacent rich chapter.
    subject = re.sub(r"[上下中]$", "", subject)
    return ordinal, subject


def chapter_is_stub(chapter: dict) -> bool:
    """Whether a chapter contains little more than one or two copies of its title."""
    content = re.sub(r"\s+", "", str(chapter.get("content") or ""))
    title = re.sub(r"\s+", "", str(chapter.get("title") or ""))
    return len(content) <= max(STUB_CHAPTER_MAX_CHARS, len(title) * 3)


def chapter_titles_match(left: dict, right: dict) -> bool:
    """High-precision adjacent duplicate match used only when one side is a stub."""
    left_ordinal, left_subject = normalize_chapter_title(str(left.get("title") or ""))
    right_ordinal, right_subject = normalize_chapter_title(str(right.get("title") or ""))
    if not left_ordinal or left_ordinal != right_ordinal:
        return False
    if not left_subject or not right_subject:
        return left_subject == right_subject
    return (
        left_subject == right_subject
        or left_subject in right_subject
        or right_subject in left_subject
    )


def dedupe_stub_chapters(
    chapters: list[dict],
    *,
    reindex: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Drop adjacent title-only duplicates while preserving the richer chapter body.

    We intentionally require both a matching normalized title and a large content
    asymmetry.  This avoids merging legitimate adjacent parts that happen to reuse a
    chapter number in a malformed source file.
    """
    kept: list[dict] = []
    removed: list[dict] = []
    index = 0
    while index < len(chapters):
        current = chapters[index]
        if index + 1 < len(chapters):
            following = chapters[index + 1]
            current_stub = chapter_is_stub(current)
            following_stub = chapter_is_stub(following)
            if chapter_titles_match(current, following) and current_stub != following_stub:
                richer, stub = (following, current) if current_stub else (current, following)
                if len(str(richer.get("content") or "")) >= max(
                    STUB_CHAPTER_MAX_CHARS,
                    len(str(stub.get("content") or "")) * 3,
                ):
                    kept.append(dict(richer))
                    removed.append(dict(stub))
                    index += 2
                    continue
        kept.append(dict(current))
        index += 1
    if reindex:
        for number, chapter in enumerate(kept, start=1):
            chapter["idx"] = number
    return kept, removed


def _split_chapters_with_removed(text: str) -> tuple[list[dict], list[dict]]:
    matches = list(CHAPTER_RE.finditer(text))
    chapters: list[dict] = []
    if matches:
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            remainder = text[m.end():end].strip()
            if remainder:
                chapters.append({"idx": len(chapters) + 1, "title": m.group(1).strip(), "content": body})
        preamble = text[: matches[0].start()].strip()
        if len(preamble) > 200:
            chapters.insert(0, {"idx": 0, "title": "楔子", "content": preamble})
            for n, ch in enumerate(chapters):
                ch["idx"] = n + 1
    if not chapters:
        for i in range(0, len(text), FALLBACK_CHUNK_CHARS):
            chunk = text[i:i + FALLBACK_CHUNK_CHARS].strip()
            if chunk:
                chapters.append({"idx": len(chapters) + 1, "title": f"第{len(chapters) + 1}段（自动切分）", "content": chunk})
    chapters, removed = dedupe_stub_chapters(chapters)
    return chapters, removed


def split_chapters(text: str) -> list[dict]:
    """按章节标题切分；识别不到 2 个标题时按字数等分并提示性命名。"""
    chapters, _ = _split_chapters_with_removed(text)
    return chapters


def ingest_novel(raw: bytes) -> dict:
    if not raw:
        raise ValueError("文件为空，请选择包含正文的 TXT 小说")
    if len(raw) > MAX_NOVEL_UPLOAD_BYTES:
        limit_mb = MAX_NOVEL_UPLOAD_BYTES // (1024 * 1024)
        raise ValueError(f"小说文件超过 {limit_mb} MB，请拆分后再导入")
    text = decode_novel(raw)
    validate_novel_text(text)
    cleaned, removed_lines = clean_text(text)
    if not cleaned:
        raise ValueError("清理广告和空白后没有剩余正文，请检查源文件")
    chapters, duplicate_stubs = _split_chapters_with_removed(cleaned)
    return {
        "total_chars": len(cleaned),
        "removed_lines": removed_lines,
        "chapter_count": len(chapters),
        "deduplicated_stub_chapters": len(duplicate_stubs),
        "auto_split": bool(chapters and "自动切分" in chapters[0]["title"]),
        "chapters": chapters,
    }
