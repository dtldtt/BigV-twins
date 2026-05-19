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
