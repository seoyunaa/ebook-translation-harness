"""Small, explicit helpers for normalizing legacy Markdown heading structure.

These helpers make narrow, caller-directed edits.  They never guess chapter
names, invent missing headings, or contain rules for a particular book.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")


def _rewrite_outside_fences(text: str, transform: object) -> str:
    output: list[str] = []
    fence: str | None = None
    callback = transform
    if not callable(callback):
        raise TypeError("transform must be callable")
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            char = marker.group(1)[0]
            fence = char if fence is None else None if char == fence else fence
            output.append(line)
            continue
        output.append(line if fence is not None else callback(line))
    return "\n".join(output).strip()


def prepend_heading(text: str, heading: str) -> str:
    """Prepend one already-formatted Markdown heading."""
    if not HEADING_RE.fullmatch(heading.strip()):
        raise ValueError(f"Expected a Markdown heading, found {heading!r}")
    body = text.strip()
    return f"{heading.strip()}\n\n{body}" if body else heading.strip()


def remove_exact_headings(text: str, titles: Iterable[str]) -> str:
    """Remove headings whose visible source text exactly matches a supplied title."""
    targets = {title.strip() for title in titles}

    def transform(line: str) -> str:
        match = HEADING_RE.fullmatch(line.strip())
        return "" if match and match.group(2).strip() in targets else line

    return _rewrite_outside_fences(text, transform)


def remove_exact_plain_lines(text: str, values: Iterable[str]) -> str:
    """Remove explicitly listed non-heading lines outside code fences."""
    targets = {value.strip() for value in values}
    return _rewrite_outside_fences(
        text, lambda line: "" if line.strip() in targets else line
    )


def remap_heading_levels(text: str, mapping: Mapping[int, int]) -> str:
    """Map selected Markdown heading levels without changing their titles."""
    normalized = dict(mapping)
    for source, target in normalized.items():
        if source not in range(1, 7) or target not in range(1, 7):
            raise ValueError("Heading levels must be integers from 1 through 6")

    def transform(line: str) -> str:
        match = HEADING_RE.fullmatch(line.strip())
        if match is None:
            return line
        source = len(match.group(1))
        target = normalized.get(source, source)
        return f"{'#' * target} {match.group(2).strip()}"

    return _rewrite_outside_fences(text, transform)


def replace_first_headings(text: str, canonical_heading: str, count: int) -> str:
    """Replace exactly the first ``count`` headings with one canonical heading."""
    if count < 1:
        raise ValueError("count must be positive")
    if not HEADING_RE.fullmatch(canonical_heading.strip()):
        raise ValueError("canonical_heading must be a Markdown heading")
    removed = 0

    def transform(line: str) -> str:
        nonlocal removed
        if removed < count and HEADING_RE.fullmatch(line.strip()):
            removed += 1
            return ""
        return line

    body = _rewrite_outside_fences(text, transform)
    if removed != count:
        raise ValueError(f"Expected {count} headings to replace; found {removed}")
    return prepend_heading(body, canonical_heading)
