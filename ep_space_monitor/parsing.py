"""Document parsing: agendas (OJ) and minutes (PV) into agenda items.

HTML agendas are reduced to text lines, then split into numbered agenda
items. Minutes arrive as PDF or DOCX; their extracted text is split the
same way. The parser is deliberately tolerant: numbered lines (``12. …``)
open a new item, following lines attach to it, and known boilerplate
(session headers, markers, room info) is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO

BLOCK_TAGS = {
    "p", "div", "tr", "li", "table", "td", "th", "section", "article",
    "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "blockquote", "dd", "dt",
}
SKIP_TAGS = {"script", "style", "noscript", "template"}

ITEM_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.*)$")
# Number rendered alone in its own table cell: "1." on one line, title on the next.
LONE_NUMBER_RE = re.compile(r"^\s*(\d{1,2})[.)]?\s*$")

# "Wednesday 25 June 2025, 9.00 – 12.30" style session separators.
SESSION_RE = re.compile(
    r"^(?:mon|tues?|wednes?|thurs?|fri|satur?|sun)day\s+\d{1,2}\s+\w+\.?\s+\d{4}\b",
    re.IGNORECASE,
)
# "25 June 2025, 9.00 – 12.30" style session separators (no weekday prefix).
TIME_HEADER_RE = re.compile(r"^\d{1,2}\s+\w+\.?\s+\d{4},\s*\d{1,2}[.:]\d{2}\b")
# "* * *" and "*** Electronic vote ***" markers.
MARKER_RE = re.compile(r"^\*[\s*]*\*[\s*]*\*.*$|^\*\*\*.*\*\*\*$")
NOISE_RE = re.compile(
    r"^(draft\s+)?agenda$|^committee\s+meeting$|^minutes of the sitting$"
    r"|^brussels$|^strasbourg$|^remote participation$|^room\s*:.*$"
    r"|^see separate draft programme$|^committee on .*$",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    """Extract visible text lines from HTML, skipping script/style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0

    def _flush(self) -> None:
        text = "".join(self._current).strip()
        if text:
            self.lines.append(text)
        self._current = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._current.append(data)

    def result(self) -> list[str]:
        self._flush()
        return self.lines


def html_to_lines(html: str) -> list[str]:
    """Strip an HTML document down to a list of visible text lines."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.result()


@dataclass(frozen=True)
class AgendaItem:
    """One agenda/minutes item."""

    index: int  # 1-based position in the document
    title: str  # first line of the item
    text: str   # full item text (title plus attached sub-lines)


def _is_noise(line: str) -> bool:
    return bool(
        SESSION_RE.match(line)
        or TIME_HEADER_RE.match(line)
        or MARKER_RE.match(line)
        or NOISE_RE.match(line)
    )


def split_items(lines: list[str]) -> list[AgendaItem]:
    """Split text lines into numbered agenda items.

    Lines before the first numbered item are treated as header and
    dropped. Noise lines are dropped everywhere; every other line
    attaches to the current item. An item number standing alone on its
    line (separate table cell) is joined with the following line.
    """
    cleaned: list[str] = []
    for raw in lines:
        line = " ".join(raw.split())
        if line:
            cleaned.append(line)

    items: list[list[str]] = []
    current: list[str] | None = None
    i = 0
    total = len(cleaned)
    while i < total:
        line = cleaned[i]
        m = ITEM_RE.match(line)
        if m:
            current = [m.group(2)]
            items.append(current)
            i += 1
            continue
        lone = LONE_NUMBER_RE.match(line)
        if lone and i + 1 < total:
            nxt = cleaned[i + 1]
            if nxt and not ITEM_RE.match(nxt) and not _is_noise(nxt):
                current = [nxt]
                items.append(current)
                i += 2
                continue
        if _is_noise(line):
            i += 1
            continue
        if current is not None:
            current.append(line)
        i += 1

    result: list[AgendaItem] = []
    for idx, lines_of_item in enumerate(items, start=1):
        result.append(AgendaItem(index=idx, title=lines_of_item[0], text=" — ".join(lines_of_item)))
    return result


def pdf_bytes_to_text(data: bytes) -> str:
    """Extract text from a PDF document."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def docx_bytes_to_text(data: bytes) -> str:
    """Extract text from a DOCX document."""
    import docx

    document = docx.Document(BytesIO(data))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def parse_document(content: bytes, content_type: str = "") -> list[AgendaItem]:
    """Parse any EP committee document (HTML/PDF/DOCX) into agenda items."""
    head = content[:2048].lstrip().lower()
    if head.startswith(b"%pdf"):
        text = pdf_bytes_to_text(content)
        lines = text.splitlines()
    elif head.startswith(b"pk"):  # DOCX is a ZIP archive
        text = docx_bytes_to_text(content)
        lines = text.splitlines()
    elif b"<html" in head or "html" in content_type.lower() or head.startswith(b"<!doctype"):
        lines = html_to_lines(content.decode("utf-8", errors="replace"))
    else:
        # Last resort: treat as plain text.
        lines = content.decode("utf-8", errors="replace").splitlines()
    return split_items(lines)
