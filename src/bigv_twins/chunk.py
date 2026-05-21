"""HTML stripping + paragraph-aware character chunking for Chinese text."""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass

_TAG_IMG = re.compile(r"<\s*img[^>]*/?>", re.IGNORECASE)
_TAG_BR = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG_BLOCK_OPEN = re.compile(
    r"<\s*(p|div|h[1-6]|li|blockquote|pre)\b[^>]*>", re.IGNORECASE
)
_TAG_BLOCK_CLOSE = re.compile(
    r"</\s*(p|div|h[1-6]|li|blockquote|pre)\s*>", re.IGNORECASE
)
_TAG_ANY = re.compile(r"<[^>]+>")
_HSPACE = re.compile(r"[ \t　]+")
_MULTI_NL = re.compile(r"\n{3,}")


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = _TAG_IMG.sub("", s)
    s = _TAG_BR.sub("\n", s)
    s = _TAG_BLOCK_OPEN.sub("\n", s)
    s = _TAG_BLOCK_CLOSE.sub("\n", s)
    s = _TAG_ANY.sub("", s)
    s = html_lib.unescape(s)
    s = _HSPACE.sub(" ", s)
    s = _MULTI_NL.sub("\n\n", s)
    return s.strip()


@dataclass(frozen=True)
class Chunk:
    text: str
    chunk_index: int


def chunk_text(text: str, *, size: int, overlap: int) -> list[Chunk]:
    """
    Greedy paragraph packing. If a paragraph alone exceeds `size`, slide a
    `size`-wide window with `overlap` over it. Paragraph boundaries are not
    overlapped — overlap only matters inside oversized paragraphs.
    """
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    out: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            out.append(Chunk(text="\n\n".join(buf), chunk_index=len(out)))
            buf = []
            buf_len = 0

    for p in paragraphs:
        if len(p) > size:
            flush()
            step = max(1, size - overlap)
            i = 0
            while i < len(p):
                seg = p[i : i + size]
                out.append(Chunk(text=seg, chunk_index=len(out)))
                if i + size >= len(p):
                    break
                i += step
            continue

        sep = 2 if buf else 0
        if buf_len + sep + len(p) > size:
            flush()
            buf = [p]
            buf_len = len(p)
        else:
            buf.append(p)
            buf_len += sep + len(p)

    flush()
    return out


# ---------------------------------------------------------------- markdown

_MD_HEADER = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Section:
    """One markdown section: the header line + everything until the next header
    of the SAME-OR-HIGHER level. ``title`` is the header text minus the leading
    ``#`` markers; ``level`` is the count of ``#`` characters; ``body`` is the
    text between this header and the next boundary header (excluding the
    header line itself). ``chunks`` is the body fed through ``chunk_text``.
    """
    level: int
    title: str
    body: str
    chunks: list[Chunk]


def split_markdown_sections(
    md: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    min_level: int = 1,
    max_level: int = 6,
) -> list[Section]:
    """Split a markdown document on header lines and return ``Section`` objects.

    Header detection uses ``^#{1,6} ...$`` (ATX style only). Setext headers and
    numbered headers like ``### 1、xxx`` (used by the BRK Q&A files) are
    captured by the same regex because the ``###`` prefix is what counts.

    Each section's body is chunked through ``chunk_text(size, overlap)``. If
    a section is empty after stripping (header with no content before next
    header), it's skipped.

    ``min_level`` / ``max_level`` control which headers are treated as section
    boundaries. E.g., to split BRK letters on ``#`` and ``##`` only, pass
    ``min_level=1, max_level=2``.
    """
    text = md.strip()
    if not text:
        return []

    matches = list(_MD_HEADER.finditer(text))
    boundaries: list[tuple[int, int, str, int]] = []
    for m in matches:
        lvl = len(m.group(1))
        if min_level <= lvl <= max_level:
            boundaries.append((m.start(), m.end(), m.group(2).strip(), lvl))

    if not boundaries:
        # No headers in range — treat the whole doc as one untitled section.
        return [
            Section(
                level=0,
                title="(untitled)",
                body=text,
                chunks=chunk_text(text, size=chunk_size, overlap=chunk_overlap),
            )
        ]

    sections: list[Section] = []
    # Optional preamble before the first matching header
    if boundaries[0][0] > 0:
        preamble = text[: boundaries[0][0]].strip()
        if preamble:
            sections.append(
                Section(
                    level=0,
                    title="(preamble)",
                    body=preamble,
                    chunks=chunk_text(preamble, size=chunk_size, overlap=chunk_overlap),
                )
            )

    for i, (start, header_end, title, lvl) in enumerate(boundaries):
        body_start = header_end
        body_end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        body = text[body_start:body_end].strip()
        if not body:
            continue
        sections.append(
            Section(
                level=lvl,
                title=title,
                body=body,
                chunks=chunk_text(body, size=chunk_size, overlap=chunk_overlap),
            )
        )
    return sections
