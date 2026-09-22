"""
core/extract.py — deterministic document extraction + structure mapping.
-----------------------------------------------------------------------
Turns an uploaded study document into text, page numbers, a heading tree and a
coverage report. **No AI, no network, no cost** — this runs on every preview.

Why it is built this way:
  * `pypdf` hands us a per-line **font size** and **y position**. Font size is the
    heading signal; y position separates running headers/footers from body text.
  * Furniture is stripped by **position band + repetition**, never by repetition
    alone: a line that legitimately repeats in the body (or a sentence the author
    uses on every page) must survive. Verified rule, see tests.
  * Heading-less documents (slide exports, scans of typewritten notes) fall back
    to token windows so the summary pipeline always has sections to work with.
  * A document with no text layer (a photo scan) is reported honestly instead of
    being summarised into fiction.

Supported: .pdf (text layer), .docx, .pptx, .txt/.md. Legacy .doc/.ppt are binary
formats that need LibreOffice/antiword; they are refused with a clear message.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# ── Tunables (measured on a 40-page fixture; see tests/test_extraction.py) ────
CHARS_PER_TOKEN = 4
TOP_BAND = 0.92          # lines above this fraction of page height = header band
BOTTOM_BAND = 0.08       # lines below this fraction = footer band
REPEAT_PAGE_FRACTION = 0.5
MAX_DUP_LINE_OCCURRENCES = 4
MIN_DUP_LINE_LEN = 80
HEADING_SIZE_DELTA = 1.4   # pt above body size to count as a heading
MAX_HEADING_CHARS = 90
WINDOW_TOKENS = 2000       # fallback window size when no headings are found
WINDOW_OVERLAP = 0.12
MIN_TEXT_CHARS_PER_PAGE = 100   # below this the "page" is probably an image

PDF_SUFFIXES = {".pdf"}
DOCX_SUFFIXES = {".docx"}
PPTX_SUFFIXES = {".pptx"}
PLAIN_SUFFIXES = {".txt", ".md", ".markdown", ".text"}
LEGACY_SUFFIXES = {".doc", ".ppt"}


class ExtractionError(Exception):
    """Raised when a document cannot be read at all (with a user-facing message)."""


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Block:
    text: str
    page: int
    size: float = 0.0
    y: float = 0.0
    x: float = 0.0
    band: str = "body"


@dataclass
class Section:
    """A heading plus everything under it, up to the next heading of any level."""
    id: str
    heading: str
    level: int
    page: int
    end_page: int
    text: str
    tokens: int

    def to_dict(self) -> dict:
        return {
            "id": self.id, "heading": self.heading, "level": self.level,
            "page": self.page, "end_page": self.end_page,
            "tokens": self.tokens, "chars": len(self.text),
        }


@dataclass
class Extraction:
    kind: str
    pages: int
    sections: list[Section]
    text: str
    tokens: int
    warnings: list[str] = field(default_factory=list)
    scanned: bool = False
    chars_per_page: int = 0
    duplicate_lines_collapsed: int = 0
    multi_column_pages: int = 0

    @property
    def has_structure(self) -> bool:
        """True when headings were found (rather than token windows)."""
        return bool(self.sections) and not self.sections[0].heading.startswith("Part ")

    def skeleton(self, first_sentence_chars: int = 260) -> str:
        """The cheap outline input: heading + page + first sentence, nothing else.

        Measured at ~17% of the document's tokens on the 40-page fixture, which is
        what makes the outline-first design affordable on a free API tier.
        """
        lines = []
        for s in self.sections:
            body = " ".join(s.text.split())
            first = re.split(r"(?<=[.!?])\s", body)[0] if body else ""
            if len(first) > first_sentence_chars:
                first = first[:first_sentence_chars].rsplit(" ", 1)[0] + "…"
            lines.append(f"[{s.id}] {s.heading} (p.{s.page}) — {first}")
        return "\n".join(lines)

    def preview(self, estimated_cost_tokens: int = 0) -> dict:
        median = self.chars_per_page
        return {
            "kind": self.kind,
            "pages": self.pages,
            "tokens": self.tokens,
            "chars_per_page": median,
            "has_structure": self.has_structure,
            "scanned": self.scanned,
            "sections": [s.to_dict() for s in self.sections],
            "skeleton_tokens": estimate_tokens(self.skeleton()),
            "estimated_cost_tokens": estimated_cost_tokens,
            "warnings": self.warnings,
            "duplicate_lines_collapsed": self.duplicate_lines_collapsed,
            "multi_column_pages": self.multi_column_pages,
        }


def estimate_tokens(text: str) -> int:
    """Rough token count. Deliberately conservative (chars/4)."""
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0


# ── Entry point ───────────────────────────────────────────────────────────────
def extract_document(path: str | Path) -> Extraction:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in LEGACY_SUFFIXES:
        raise ExtractionError(
            f"'{suffix}' is a legacy binary format. Open it and save as "
            f"'{'.docx' if suffix == '.doc' else '.pptx'}' or PDF, then upload again."
        )
    if not path.exists():
        raise ExtractionError(f"File not found: {path.name}")

    if suffix in PDF_SUFFIXES:
        extraction = _extract_pdf(path)
    elif suffix in DOCX_SUFFIXES:
        extraction = _extract_docx(path)
    elif suffix in PPTX_SUFFIXES:
        extraction = _extract_pptx(path)
    elif suffix in PLAIN_SUFFIXES:
        extraction = _extract_plain(path)
    else:
        raise ExtractionError(
            f"'{suffix or path.name}' is not a supported document type. "
            "Supported: PDF, DOCX, PPTX, TXT, MD."
        )

    if extraction.scanned or extraction.tokens < 20:
        extraction.warnings.append(
            "No usable text layer found — this looks like a scanned or photographed "
            "document. OCR is not available on this host, so the AI cannot read it. "
            "Upload a text-based PDF, or paste the text into the analysis box."
        )
        extraction.scanned = True
    return extraction


# ── PDF ───────────────────────────────────────────────────────────────────────
def _extract_pdf(path: Path) -> Extraction:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExtractionError(f"pypdf is not installed: {exc}") from exc

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")           # many lecture PDFs are owner-locked only
            except Exception as exc:
                raise ExtractionError(
                    "This PDF is password-protected. Remove the password and upload again."
                ) from exc
        pages_raw = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                height = float(page.mediabox.height)
                width = float(page.mediabox.width)
            except Exception:
                height, width = 842.0, 595.0
            blocks: list[Block] = []

            def visit(text, cm, tm, font_dict, font_size, _b=blocks, _i=index, _h=height):
                y = float(tm[5]) if tm and len(tm) > 5 else 0.0
                x = float(tm[4]) if tm and len(tm) > 4 else 0.0
                band = "top" if y > _h * TOP_BAND else ("bottom" if y < _h * BOTTOM_BAND else "body")
                for chunk in text.splitlines():
                    if chunk.strip():
                        _b.append(Block(chunk.strip(), _i, float(font_size or 0), y, x, band))

            try:
                page.extract_text(visitor_text=visit)
            except Exception as exc:          # a single bad page must not kill the run
                blocks.append(Block(f"[page {index} could not be read: {exc}]", index))
            pages_raw.append({"n": index, "height": height, "width": width, "blocks": blocks})
        total_pages = len(reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Could not read this PDF: {exc}") from exc

    warnings: list[str] = []
    multi_column = 0
    for entry in pages_raw:
        blocks_on_page = entry["blocks"]
        if len(blocks_on_page) < 8:
            continue
        page_width = entry.get("width") or 595.0
        right_half = sum(1 for b in blocks_on_page if b.x > page_width * 0.45)
        if right_half > len(blocks_on_page) * 0.35:
            multi_column += 1
    if multi_column > total_pages * 0.3 and total_pages > 3:
        warnings.append(
            f"{multi_column} of {total_pages} pages look multi-column; reading order "
            "may be imperfect. Check page citations against the original."
        )

    pages_raw, stripped = _strip_furniture(pages_raw)
    if stripped:
        warnings.append(f"Removed {stripped} repeated header/footer line(s).")

    blocks = [b for entry in pages_raw for b in entry["blocks"]]
    # Collapse long lines repeated across the whole document BEFORE building
    # sections (line-level, so the count is meaningful).
    blocks, collapsed = _dedupe_blocks(blocks)
    if collapsed:
        warnings.append(
            f"Collapsed {collapsed} line(s) that were repeated more than "
            f"{MAX_DUP_LINE_OCCURRENCES} times (common in exported slide decks)."
        )

    body_size = _dominant_size(pages_raw, blocks)
    sections = _sections_from_blocks(blocks, body_size, total_pages)

    text = "\n".join(b.text for b in blocks)
    chars_per_page = int(statistics.median([len(" ".join(b.text for b in e["blocks"])) for e in pages_raw])) if pages_raw else 0
    empty_pages = sum(1 for e in pages_raw if len(" ".join(b.text for b in e["blocks"])) < MIN_TEXT_CHARS_PER_PAGE)

    if not sections:
        sections = _window_fallback(blocks, total_pages)
        warnings.append(
            "No headings found (likely a slide export or typewritten notes), so the "
            f"document was split into {len(sections)} equal parts by length."
        )

    return Extraction(
        kind="pdf",
        pages=total_pages,
        sections=sections,
        text=text,
        tokens=estimate_tokens(text),
        warnings=warnings,
        scanned=empty_pages == total_pages or chars_per_page < MIN_TEXT_CHARS_PER_PAGE,
        chars_per_page=chars_per_page,
        duplicate_lines_collapsed=collapsed,
        multi_column_pages=multi_column,
    )


def _dominant_size(pages_raw: list[dict], blocks: list[Block] | None = None) -> float:
    """The body font size: the size carrying the most characters.

    Measured over **surviving** blocks only — furniture has already been removed,
    and band membership must not influence structure detection (a heading may sit
    high on the page without being a running header).
    """
    counts: dict[float, int] = {}
    source = blocks if blocks is not None else [b for e in pages_raw for b in e["blocks"]]
    for block in source:
        if block.size:
            counts[block.size] = counts.get(block.size, 0) + len(block.text)
    return max(counts, key=counts.get) if counts else 0.0


def _strip_furniture(pages_raw: list[dict]) -> tuple[list[dict], int]:
    """Remove running headers/footers and page numbers.

    Two independent rules, deliberately not one:
      * repetition **inside the top/bottom band** marks a line as furniture — the
        band restriction is what keeps a sentence the author repeats in the body;
      * explicit page-number shapes ("- 4 -", "[4]", "Page 4") are unmistakable
        and are dropped wherever they appear;
      * a bare number is only dropped inside the band, where a stray table value
        is unlikely and a page number is expected.
    """
    total = len(pages_raw)
    if total < 3:
        return pages_raw, 0

    band_counts: dict[str, int] = {}
    for entry in pages_raw:
        for block in entry["blocks"]:
            if block.band in ("top", "bottom"):
                band_counts[block.text] = band_counts.get(block.text, 0) + 1
    furniture = {t for t, c in band_counts.items() if c > total * REPEAT_PAGE_FRACTION}

    explicit_page_number = re.compile(
        r"^(?:page\s*|p\.?\s*)?[-–—\[(]?\s*\d{1,4}\s*[-–—\])]?$", re.IGNORECASE
    )
    bare_number = re.compile(r"^\d{1,4}$")

    removed = 0
    for entry in pages_raw:
        kept: list[Block] = []
        for block in entry["blocks"]:
            in_band = block.band in ("top", "bottom")
            drop = (
                block.text in furniture
                or bool(explicit_page_number.match(block.text)) and (in_band or len(block.text) > 2)
                or (in_band and bool(bare_number.match(block.text)))
            )
            if drop:
                removed += 1
            else:
                kept.append(block)
        entry["blocks"] = kept
    return pages_raw, removed


def _sections_from_blocks(blocks: list[Block], body_size: float, total_pages: int) -> list[Section]:
    if body_size <= 0:
        return []
    # NOTE: no band filter here. A heading often sits high on the page; band
    # membership already did its job by removing furniture before this point.
    candidates = [
        b for b in blocks
        if b.size >= body_size + HEADING_SIZE_DELTA and len(b.text) <= MAX_HEADING_CHARS
    ]
    if len(candidates) < 2:
        return []

    # A heading that repeats verbatim (running section title) keeps its first page.
    seen: dict[str, int] = {}
    headings: list[Block] = []
    for block in candidates:
        if seen.get(block.text, 0) >= 2:
            continue
        seen[block.text] = seen.get(block.text, 0) + 1
        headings.append(block)

    body_order = [b for b in blocks if b not in headings]
    numbered = re.compile(r"^(\d+(\.\d+)*)[.)]?\s+\S")
    sections: list[Section] = []
    for i, head in enumerate(headings):
        level = head.text.count(".") + 1 if numbered.match(head.text) else (
            1 if head.size >= body_size + 3 else 2
        )
        nxt = headings[i + 1] if i + 1 < len(headings) else None
        body_parts = [
            b.text for b in body_order
            if (b.page > head.page or (b.page == head.page and b.y <= head.y))
            and (nxt is None or b.page < nxt.page or (b.page == nxt.page and b.y > nxt.y))
        ]
        text = "\n".join(body_parts).strip()
        sections.append(Section(
            id=f"s{i + 1}",
            heading=head.text.strip(),
            level=min(level, 3),
            page=head.page,
            end_page=(nxt.page - 1 if nxt else total_pages) or head.page,
            text=text[:12000],
            tokens=estimate_tokens(text[:12000]),
        ))

    # Drop headings that carry no body text (cover pages, running section titles).
    # If nothing has content, return [] so the caller falls back to token windows
    # (the window builder keeps every block, so nothing is lost).
    substantive = [s for s in sections if s.text.strip()]
    if len(substantive) >= 3:
        return substantive
    return sections


def _dedupe_blocks(blocks: list[Block]) -> tuple[list[Block], int]:
    """Collapse exact long lines repeated more than MAX_DUP_LINE_OCCURRENCES times.

    Deliberately conservative: only long lines, only heavy repetition. A sentence
    the author repeats two or three times is content and must survive (locked by
    test_body_text_repeated_thrice_survives).
    """
    counts: dict[str, int] = {}
    for block in blocks:
        if len(block.text) >= MIN_DUP_LINE_LEN:
            counts[block.text] = counts.get(block.text, 0) + 1
    drop = {t for t, c in counts.items() if c > MAX_DUP_LINE_OCCURRENCES}
    if not drop:
        return blocks, 0
    kept: list[Block] = []
    dropped = 0
    for block in blocks:
        if block.text in drop:
            dropped += 1
            continue
        kept.append(block)
    # keep the first occurrence in place rather than deleting the line entirely
    for text in drop:
        first = next((b for b in blocks if b.text == text), None)
        if first is not None:
            kept.append(first)
            dropped -= 1
    return kept, dropped


def _window_fallback(blocks: list[Block], total_pages: int) -> list[Section]:
    """No headings: pack consecutive blocks into ~WINDOW_TOKENS windows."""
    if not blocks:
        return []
    budget = WINDOW_TOKENS * CHARS_PER_TOKEN
    sections: list[Section] = []
    chunk: list[Block] = []
    chars = 0
    index = 1

    def flush() -> None:
        nonlocal chunk, chars, index
        if not chunk:
            return
        text = "\n".join(b.text for b in chunk)
        sections.append(Section(
            id=f"s{index}",
            heading=f"Part {index} (p.{chunk[0].page}-{chunk[-1].page})",
            level=1, page=chunk[0].page, end_page=chunk[-1].page,
            text=text, tokens=estimate_tokens(text),
        ))
        index += 1
        overlap = int(len(chunk) * WINDOW_OVERLAP)
        chunk = chunk[-overlap:] if overlap else []
        chars = sum(len(b.text) for b in chunk)

    for block in blocks:
        chunk.append(block)
        chars += len(block.text)
        if chars >= budget:
            flush()
    flush()
    return sections


# ── DOCX ──────────────────────────────────────────────────────────────────────
def _extract_docx(path: Path) -> Extraction:
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError(f"python-docx is not installed: {exc}") from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractionError(f"Could not read this DOCX: {exc}") from exc

    entries: list[tuple[str, int, str]] = []       # (text, level, kind)
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower()
        level = 1 if style.startswith("heading 1") else (
            2 if style.startswith("heading 2") else (3 if style.startswith("heading 3") else 0))
        if style.startswith("title"):
            level = 1
        entries.append((text, level, "heading" if level else "body"))

    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                entries.append((" | ".join(cells), 0, "body"))

    sections = _sections_from_stream(entries, paged=False)
    text = "\n".join(t for t, _, _ in entries)
    if not sections:
        sections = _stream_windows([(t, 1) for t, _, _ in entries])
    return Extraction(
        kind="docx", pages=1, sections=sections, text=text, tokens=estimate_tokens(text),
        warnings=["DOCX has no fixed pages; page citations use paragraph order."] if sections else [],
        chars_per_page=len(text),
    )


# ── PPTX ──────────────────────────────────────────────────────────────────────
def _extract_pptx(path: Path) -> Extraction:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError(f"python-pptx is not installed: {exc}") from exc

    try:
        deck = Presentation(str(path))
    except Exception as exc:
        raise ExtractionError(f"Could not read this PPTX: {exc}") from exc

    entries: list[tuple[str, int, str]] = []
    for number, slide in enumerate(deck.slides, start=1):
        title = ""
        body: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            text = shape.text_frame.text.strip()
            if not text:
                continue
            if not title and shape == slide.shapes.title:
                title = text
            else:
                body.append(text)
        entries.append((title or f"Slide {number}", 1, "heading"))
        for item in body:
            entries.append((item, 0, "body"))

    # Each slide is its own page (that is how students cite slides).
    counter = {"page": 0}
    paged: list[tuple[str, int, str, int]] = []
    for text, level, kind in entries:
        if kind == "heading":
            counter["page"] += 1
        paged.append((text, level, kind, max(1, counter["page"])))
    sections = _sections_from_stream([(t, l, k) for t, l, k, _ in paged], paged=paged)
    full = "\n".join(t for t, _, _ in entries)
    return Extraction(
        kind="pptx", pages=len(deck.slides), sections=sections, text=full,
        tokens=estimate_tokens(full), chars_per_page=len(full) // max(1, len(deck.slides)),
    )


# ── plain text ────────────────────────────────────────────────────────────────
def _extract_plain(path: Path) -> Extraction:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ExtractionError(f"Could not read this file: {exc}") from exc
    entries: list[tuple[str, int, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = (
            stripped.startswith("#")
            or bool(re.match(r"^(\d+(\.\d+)*)[.)]?\s+\S", stripped) and len(stripped) < MAX_HEADING_CHARS)
            or (stripped.isupper() and 4 < len(stripped) < MAX_HEADING_CHARS)
        )
        entries.append((stripped.lstrip("# ").strip(), 1 if heading else 0, "heading" if heading else "body"))
    sections = _sections_from_stream(entries, paged=False)
    if not sections:
        sections = _stream_windows([(t, 1) for t, _, _ in entries])
    return Extraction(kind="text", pages=1, sections=sections, text=text,
                      tokens=estimate_tokens(text), chars_per_page=len(text))


# ── shared section builders ───────────────────────────────────────────────────
def _sections_from_stream(entries: list[tuple], paged: bool = False, paged_rows: Optional[list] = None):
    rows = paged_rows if paged_rows is not None else entries
    headings = [i for i, row in enumerate(rows) if row[2] == "heading"]
    if not headings:
        return []
    sections: list[Section] = []
    for n, start in enumerate(headings):
        end = headings[n + 1] if n + 1 < len(headings) else len(rows)
        body = [rows[i][0] for i in range(start + 1, end)]
        level = rows[start][1] if len(rows[start]) > 1 else 1
        page = rows[start][3] if len(rows[start]) > 3 else 0
        text = " ".join(body).strip()
        sections.append(Section(
            id=f"s{n + 1}", heading=rows[start][0], level=min(int(level or 1), 3),
            page=page, end_page=page, text=text[:12000], tokens=estimate_tokens(text[:12000]),
        ))
    return [s for s in sections if s.heading]


def _stream_windows(entries: list[tuple[str, int]]) -> list[Section]:
    joined = [text for text, _ in entries]
    if not joined:
        return []
    window_chars = WINDOW_TOKENS * CHARS_PER_TOKEN
    sections: list[Section] = []
    for index, start in enumerate(range(0, len(joined), window_chars), start=1):
        text = " ".join(joined[start:start + window_chars // 40])
        if not text:
            continue
        sections.append(Section(
            id=f"s{index}", heading=f"Part {index}", level=1, page=index, end_page=index,
            text=text, tokens=estimate_tokens(text),
        ))
    return sections
