"""Split a Markdown runbook into chunks for retrieval.

A runbook is organised by headings ("Database", then "Connection pool", then the steps), and a
search hit is only useful if it carries that context. So the split follows the structure:

1. Cut at headings (levels 1-4). Each section remembers its heading path ("Database > Connection pool").
2. A section that fits in ``max_chars`` is one chunk.
3. A longer section is cut at blank lines into blocks that are packed up to ``max_chars``. A fenced code block
   is one block and is never cut at a blank line inside it.
4. A single block still longer than ``max_chars`` is cut into windows that overlap by ``overlap`` characters, so
   a sentence on a boundary appears whole in one of them.

Headings inside fenced code (a ``# comment`` in a shell snippet) are not headings. A heading with no text
under it adds nothing by itself, but still names the sections below it.
"""

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP = 150

_HEADING = re.compile(r"^(#{1,4})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    ordinal: int  # position in the runbook, from 0
    heading_path: str  # "Database > Connection pool", or "" before the first heading
    content: str

    @property
    def embed_text(self) -> str:
        """What is embedded: the content with its heading path in front, so a query about the topic matches."""
        return f"{self.heading_path}\n{self.content}" if self.heading_path else self.content


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading path, body) in document order."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        sections.append((" > ".join(title for _, title in stack), "\n".join(body)))
        body.clear()

    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading.group(2).strip()))
        else:
            body.append(line)
    flush()
    return sections


def _blocks(body: str) -> list[str]:
    """Paragraphs separated by blank lines; a fenced code block stays in one piece."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        if line.strip() == "" and not in_fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def _windows(block: str, max_chars: int, overlap: int) -> list[str]:
    step = max(1, max_chars - overlap)
    pieces = []
    for start in range(0, len(block), step):
        pieces.append(block[start : start + max_chars].strip())
        if start + max_chars >= len(block):
            break
    return [p for p in pieces if p]


def _pack(body: str, max_chars: int, overlap: int) -> list[str]:
    body = body.strip()
    if not body:
        return []
    if len(body) <= max_chars:
        return [body]
    packed: list[str] = []
    current = ""
    for block in _blocks(body):
        if len(block) > max_chars:
            if current:
                packed.append(current)
                current = ""
            packed.extend(_windows(block, max_chars, overlap))
        elif not current:
            current = block
        elif len(current) + 2 + len(block) <= max_chars:
            current = f"{current}\n\n{block}"
        else:
            packed.append(current)
            current = block
    if current:
        packed.append(current)
    return packed


def split_markdown(text: str, *, max_chars: int = DEFAULT_MAX_CHARS, overlap: int = DEFAULT_OVERLAP) -> list[Chunk]:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    if not 0 <= overlap < max_chars // 2:
        raise ValueError("overlap must be at least 0 and less than half of max_chars")
    chunks: list[Chunk] = []
    for path, body in _sections(text.replace("\r\n", "\n").replace("\r", "\n")):
        for piece in _pack(body, max_chars, overlap):
            chunks.append(Chunk(ordinal=len(chunks), heading_path=path, content=piece))
    return chunks
