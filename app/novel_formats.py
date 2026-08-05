"""小说文件格式适配：将受支持的文件转换为统一的纯文本摄入内容。"""
from __future__ import annotations

import codecs
import posixpath
import re
import zlib
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, LargeZipFile, ZipFile, ZipInfo

from app.ingest import MAX_NOVEL_UPLOAD_BYTES


SUPPORTED_NOVEL_SUFFIXES = frozenset({".txt", ".epub"})
SUPPORTED_NOVEL_LABEL = "TXT 或 EPUB"

_CONTAINER_PATH = "META-INF/container.xml"
_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_CONTAINER_BYTES = 1024 * 1024
_MAX_PACKAGE_BYTES = 4 * 1024 * 1024
_MAX_SPINE_DOCUMENTS = 5_000
_MARKUP_ENCODING_RE = re.compile(
    br"""(?:encoding\s*=\s*|charset\s*=\s*)["']?\s*([A-Za-z0-9._:-]+)""",
    re.IGNORECASE,
)
_HTML_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_HTML_SUFFIXES = frozenset({".xhtml", ".html", ".htm"})


def novel_file_suffix(filename: str | None) -> str:
    return Path(filename or "").suffix.lower()


def validate_novel_filename(filename: str | None) -> str:
    safe_name = Path(filename or "novel.txt").name
    if novel_file_suffix(safe_name) not in SUPPORTED_NOVEL_SUFFIXES:
        raise ValueError(f"仅支持 {SUPPORTED_NOVEL_LABEL} 小说，请重新选择文件")
    return safe_name


def prepare_novel_bytes(filename: str, raw: bytes) -> bytes:
    """返回可直接交给 ``ingest_novel`` 的小说纯文本字节。"""
    safe_name = validate_novel_filename(filename)
    if not raw:
        raise ValueError(f"文件为空，请选择包含正文的 {SUPPORTED_NOVEL_LABEL} 小说")
    if len(raw) > MAX_NOVEL_UPLOAD_BYTES:
        limit_mb = MAX_NOVEL_UPLOAD_BYTES // (1024 * 1024)
        raise ValueError(f"小说文件超过 {limit_mb} MB，请拆分后再导入")
    if novel_file_suffix(safe_name) == ".txt":
        return raw
    return extract_epub_text(raw).encode("utf-8")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _validate_member_path(value: str) -> str:
    candidate = value.replace("\\", "/")
    parts = PurePosixPath(candidate).parts
    if not candidate or candidate.startswith("/") or ".." in parts:
        raise ValueError("EPUB 包含不安全的内部文件路径，无法导入")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."} or normalized.startswith("../"):
        raise ValueError("EPUB 包含不安全的内部文件路径，无法导入")
    return normalized


def _archive_members(archive: ZipFile) -> dict[str, ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise ValueError("EPUB 内部文件过多，请检查文件是否损坏")
    members: dict[str, ZipInfo] = {}
    for info in infos:
        normalized = _validate_member_path(info.filename)
        if info.is_dir():
            continue
        if normalized in members:
            raise ValueError("EPUB 包含重复的内部文件，无法确认正文版本")
        members[normalized] = info
    return members


def _read_member(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    member_path: str,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    info = members.get(member_path)
    if info is None:
        raise ValueError(f"EPUB 缺少{label}，文件结构不完整")
    if info.flag_bits & 0x1:
        raise ValueError(f"EPUB 的{label}已加密，暂不支持导入带 DRM 的正文")
    if info.file_size > max_bytes:
        raise ValueError(f"EPUB 的{label}体积异常，请检查文件是否损坏")
    with archive.open(info) as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"EPUB 的{label}体积异常，请检查文件是否损坏")
    return content


def _parse_xml(raw: bytes, *, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError(f"EPUB 的{label}无法解析，文件可能已损坏") from exc


def _resolve_member_path(base_path: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise ValueError("EPUB 正文引用了外部网页，无法作为本地小说导入")
    relative_path = unquote(parsed.path)
    if not relative_path:
        raise ValueError("EPUB 正文清单包含空路径")
    joined = posixpath.join(posixpath.dirname(base_path), relative_path)
    return _validate_member_path(joined)


def _package_path(container: ElementTree.Element) -> str:
    for element in container.iter():
        if _local_name(element.tag) != "rootfile":
            continue
        full_path = str(element.attrib.get("full-path") or "").strip()
        if full_path:
            return _validate_member_path(unquote(full_path))
    raise ValueError("EPUB 缺少正文包清单，文件结构不完整")


def _spine_document_paths(package: ElementTree.Element, package_path: str) -> list[str]:
    manifest = next(
        (element for element in package.iter() if _local_name(element.tag) == "manifest"),
        None,
    )
    spine = next(
        (element for element in package.iter() if _local_name(element.tag) == "spine"),
        None,
    )
    if manifest is None or spine is None:
        raise ValueError("EPUB 缺少正文清单或阅读顺序，文件结构不完整")

    items: dict[str, tuple[str, str]] = {}
    for element in manifest:
        if _local_name(element.tag) != "item":
            continue
        item_id = str(element.attrib.get("id") or "").strip()
        href = str(element.attrib.get("href") or "").strip()
        media_type = str(element.attrib.get("media-type") or "").strip().lower()
        if item_id and href:
            items[item_id] = (href, media_type)

    ordered: list[str] = []
    auxiliary: list[str] = []
    for element in spine:
        if _local_name(element.tag) != "itemref":
            continue
        item = items.get(str(element.attrib.get("idref") or "").strip())
        if item is None:
            continue
        href, media_type = item
        suffix = PurePosixPath(urlsplit(href).path).suffix.lower()
        if media_type not in _HTML_MEDIA_TYPES and suffix not in _HTML_SUFFIXES:
            continue
        resolved = _resolve_member_path(package_path, href)
        target = auxiliary if str(element.attrib.get("linear") or "yes").lower() == "no" else ordered
        if resolved not in target:
            target.append(resolved)

    document_paths = ordered or auxiliary
    if not document_paths:
        raise ValueError("EPUB 阅读顺序中没有可读取的正文章节")
    if len(document_paths) > _MAX_SPINE_DOCUMENTS:
        raise ValueError("EPUB 正文章节数量异常，请检查文件是否损坏")
    return document_paths


def _decode_markup(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    declaration = _MARKUP_ENCODING_RE.search(raw[:1024])
    if declaration:
        encoding = declaration.group(1).decode("ascii", "ignore")
        try:
            codecs.lookup(encoding)
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("EPUB 正文章节的文字编码无法识别")


class _BodyTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset({
        "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "ol", "p", "pre", "section", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    })
    _SKIP_TAGS = frozenset({"head", "math", "nav", "noscript", "script", "style", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def _line_break(self) -> None:
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        name = tag.lower()
        if name in self._SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if name == "br":
            self._line_break()
        elif name in self._BLOCK_TAGS:
            self._line_break()

    def handle_startendtag(self, tag: str, attrs) -> None:
        name = tag.lower()
        if self.skip_depth or name in self._SKIP_TAGS:
            return
        if name == "br" or name in self._BLOCK_TAGS:
            self._line_break()

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in self._SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if name in self._BLOCK_TAGS:
            self._line_break()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        collapsed = re.sub(r"\s+", " ", data.replace("\xa0", " "))
        if collapsed:
            self.parts.append(collapsed)

    def text(self) -> str:
        lines = []
        for line in "".join(self.parts).splitlines():
            normalized = re.sub(r"[ \t\f\v]+", " ", line).strip()
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)


def _extract_html_text(raw: bytes) -> str:
    parser = _BodyTextExtractor()
    try:
        parser.feed(_decode_markup(raw))
        parser.close()
    except ValueError as exc:
        raise ValueError("EPUB 正文章节无法解析") from exc
    return parser.text()


def extract_epub_text(raw: bytes) -> str:
    """按 EPUB spine 阅读顺序提取正文，忽略样式、脚本和导航内容。"""
    try:
        with ZipFile(BytesIO(raw)) as archive:
            members = _archive_members(archive)
            container_raw = _read_member(
                archive,
                members,
                _CONTAINER_PATH,
                max_bytes=_MAX_CONTAINER_BYTES,
                label="容器清单",
            )
            package_path = _package_path(_parse_xml(container_raw, label="容器清单"))
            package_raw = _read_member(
                archive,
                members,
                package_path,
                max_bytes=_MAX_PACKAGE_BYTES,
                label="正文包清单",
            )
            document_paths = _spine_document_paths(
                _parse_xml(package_raw, label="正文包清单"),
                package_path,
            )

            chapters: list[str] = []
            total_bytes = 0
            seen: set[str] = set()
            for document_path in document_paths:
                if document_path in seen:
                    continue
                seen.add(document_path)
                document_raw = _read_member(
                    archive,
                    members,
                    document_path,
                    max_bytes=MAX_NOVEL_UPLOAD_BYTES,
                    label="正文章节",
                )
                chapter = _extract_html_text(document_raw).strip()
                if not chapter:
                    continue
                total_bytes += len(chapter.encode("utf-8"))
                if total_bytes > MAX_NOVEL_UPLOAD_BYTES:
                    limit_mb = MAX_NOVEL_UPLOAD_BYTES // (1024 * 1024)
                    raise ValueError(f"EPUB 提取后的正文超过 {limit_mb} MB，请拆分后再导入")
                chapters.append(chapter)
    except ValueError:
        raise
    except (BadZipFile, LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise ValueError("EPUB 文件损坏或压缩格式不受支持，请重新导出后再导入") from exc

    text = "\n\n".join(chapters).strip()
    if not text:
        raise ValueError("EPUB 中没有可读取的正文内容，请检查书籍文件")
    return text
