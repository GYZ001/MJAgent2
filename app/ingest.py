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

SEPARATOR_ONLY_RE = re.compile(r"^(?:[-_=~*]{4,}|[－—～·]{4,})$")
TRAILING_JUNK_ONLY_RE = re.compile(r"^[;；,，:：|丨]+$")

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
    """Normalize layout without deleting prose according to its vocabulary."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped and (
            SEPARATOR_ONLY_RE.fullmatch(stripped)
            or TRAILING_JUNK_ONLY_RE.fullmatch(stripped)
        ):
            removed += 1
            continue
        kept.append(stripped)
    while kept and not kept[-1]:
        kept.pop()
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


def _parse_chapter_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
        "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    }
    units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
        elif char in units:
            section += (number or 1) * units[char]
            number = 0
        else:
            return None
    return total + section + number


def _chapter_ordinal(title: str) -> int | None:
    matches = list(re.finditer(rf"第([{_CHAPTER_NUMERALS}]+)章", title or ""))
    return _parse_chapter_number(matches[-1].group(1)) if matches else None


def _recover_missing_unit_headings(chapters: list[dict]) -> list[dict]:
    """Recover standalone headings such as ``第五十三标题`` using ordinal context.

    A missing unit is accepted only when it is exactly the current ordinal + 1
    and the next recognized chapter proves that an ordinal was skipped.
    """
    recovered: list[dict] = []
    for index, chapter in enumerate(chapters):
        next_ordinal = (
            _chapter_ordinal(str(chapters[index + 1].get("title") or ""))
            if index + 1 < len(chapters) else None
        )
        fragments = [dict(chapter)]
        while next_ordinal is not None:
            current = fragments[-1]
            current_ordinal = _chapter_ordinal(str(current.get("title") or ""))
            if current_ordinal is None or next_ordinal <= current_ordinal + 1:
                break
            expected = current_ordinal + 1
            lines = str(current.get("content") or "").splitlines()
            split_at = None
            normalized_title = ""
            for line_index, line in enumerate(lines[1:], start=1):
                candidate = line.strip()
                recognized = list(
                    re.finditer(rf"第([{_CHAPTER_NUMERALS}]+)章", candidate)
                )
                if (
                    recognized
                    and _parse_chapter_number(recognized[-1].group(1)) == expected
                    and len(candidate) <= 80
                ):
                    split_at = line_index
                    normalized_title = candidate
                    break
                match = re.match(rf"^第([{_CHAPTER_NUMERALS}]+)", candidate)
                if not match or _parse_chapter_number(match.group(1)) != expected:
                    continue
                remainder = candidate[match.end():].strip()
                if not remainder or remainder[0] in "章卷回节" or len(remainder) > 48:
                    continue
                if remainder[0] in "步拜层阵剑声拳刀峰海关息日次色":
                    continue
                split_at = line_index
                normalized_title = f"{match.group(0)}章{remainder}"
                break
            if split_at is None:
                break
            left, _ = clean_text("\n".join(lines[:split_at]))
            right_lines = lines[split_at:]
            right_lines[0] = normalized_title
            right, _ = clean_text("\n".join(right_lines))
            if len(left) < STUB_CHAPTER_MAX_CHARS or len(right) < STUB_CHAPTER_MAX_CHARS:
                break
            current["content"] = left
            current["char_count"] = len(left)
            fragments.append({
                "idx": 0,
                "title": normalized_title,
                "content": right,
                "char_count": len(right),
            })
        recovered.extend(fragments)
    for number, chapter in enumerate(recovered, start=1):
        chapter["idx"] = number
    return recovered


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
    chapters = _recover_missing_unit_headings(chapters)
    chapters, removed = dedupe_stub_chapters(chapters)
    return chapters, removed


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
