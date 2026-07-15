"""Shared, book-agnostic primitives for structure-safe EPUB production.

This module deliberately contains no title, author, or book identifier.  A
project supplies those values in ``book_meta.json`` and supplies the exact
reader-facing navigation tree in ``toc_contract.json``.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

BOOK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
DEFAULT_TOC_LEVELS = {2: 1, 3: 2, 4: 3}
AI_NOTICE = "이 전자책은 AI 윤문 번역본입니다."
DEFAULT_FORBIDDEN_PATTERNS = (
    r"(?i)\bPDF\s*PAGE\s*\d+\b",
    r"(?i)\b(?:worker|task|chunk)[-_ ]?\d+\b",
    r"(?i)\b(?:draft|polished)\s+(?:translation|text)\b",
    r"(?i)\bAI[- ]?(?:translated|polished|translation)\b",
    r"PDF\s*페이지\s*\d+",
    r"(?:작업|번역)\s*(?:청크|조각)\s*\d+",
    r"AI\s*(?:윤문|초벌)?\s*번역",
    r"(?i)\b(?:continued|continuation)\b(?:\s+\d+)?$",
    r"계속\s*\d*$",
    r"(?i)^(?:part|section)?\s*[A-Z]{2,}[-_]\d+(?:[-_]\d+)*$",
)


class HarnessError(ValueError):
    """Raised when an input violates an explicit build contract."""


@dataclass(frozen=True)
class BookMeta:
    book_key: str
    title: str
    author: str
    language: str
    identifier: str
    toc_levels: dict[int, int]
    forbidden_patterns: tuple[str, ...]


@dataclass
class TocNode:
    label: str
    target: str
    children: list["TocNode"] = field(default_factory=list)


@dataclass(frozen=True)
class MarkdownDocument:
    filename: str
    title: str
    markdown: str
    first_heading_id: str
    in_navigation: bool


@dataclass(frozen=True)
class ParsedBook:
    title_document: MarkdownDocument
    documents: tuple[MarkdownDocument, ...]
    toc: tuple[TocNode, ...]


def clean_text(value: str) -> str:
    """Collapse whitespace without changing visible words."""
    return " ".join(value.split())


def plain_label(value: str) -> str:
    """Return the visible label represented by a Markdown heading."""
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_`~]", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    return clean_text(html.unescape(value))


def slugify(value: str, *, fallback: str = "section") -> str:
    value = plain_label(value).casefold()
    value = re.sub(r"[^\w\-\u0080-\uffff]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or fallback


def require_book_key(value: str) -> str:
    if not BOOK_KEY_RE.fullmatch(value):
        raise HarnessError(
            f"Invalid book key {value!r}; use lowercase ASCII letters, digits, '_' or '-'."
        )
    return value


def project_path(project_root: Path, configured: str | Path) -> Path:
    path = Path(configured)
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise HarnessError(f"Required file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Cannot read valid UTF-8 JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"Expected a JSON object in {path}")
    return value


def _required_string(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not clean_text(value):
        raise HarnessError(f"{path}: {key!r} must be a non-empty string")
    return clean_text(value)


def resolve_book_config_dir(config_dir: Path, book_key: str) -> Path:
    """Resolve either ``<book_key>`` or the conventional ``<book_id>`` folder.

    A Korean output key commonly ends in ``_ko`` while its assets directory
    uses the stable source/book identifier without that suffix.  Ambiguity is
    blocked instead of silently choosing one directory.
    """
    require_book_key(book_key)
    candidate_names = [book_key]
    if book_key.endswith("_ko") and len(book_key) > 3:
        candidate_names.append(book_key[:-3])
    candidates = [config_dir / name for name in candidate_names]
    existing = [path for path in candidates if path.is_dir()]
    if len(existing) > 1:
        raise HarnessError(
            f"Ambiguous configuration for {book_key!r}; both folders exist: {existing}"
        )
    if existing:
        return existing[0]
    expected = ", ".join(str(path) for path in candidates)
    raise HarnessError(f"No configuration directory for {book_key!r}; expected one of: {expected}")


def parse_toc_levels(raw: Any) -> dict[int, int]:
    """Parse heading-level to TOC-depth mapping.

    Supported forms are ``{"2": 1, "3": 2}`` and ``[2, 3]``.  The latter
    maps the first heading level to depth one, the next to depth two, and so on.
    """
    if raw is None:
        return dict(DEFAULT_TOC_LEVELS)
    if isinstance(raw, list):
        if not raw or not all(isinstance(item, int) for item in raw):
            raise HarnessError("toc heading levels must be a non-empty list of integers")
        mapping = {level: index for index, level in enumerate(raw, 1)}
    elif isinstance(raw, dict):
        mapping: dict[int, int] = {}
        for key, value in raw.items():
            try:
                level = int(key)
            except (TypeError, ValueError) as exc:
                raise HarnessError(f"Invalid Markdown heading level: {key!r}") from exc
            if not isinstance(value, int):
                raise HarnessError(f"TOC depth for H{level} must be an integer")
            mapping[level] = value
    else:
        raise HarnessError("toc_levels/toc_heading_levels must be a list or object")

    if not mapping:
        raise HarnessError("At least one TOC heading level is required")
    if any(level < 2 or level > 6 for level in mapping):
        raise HarnessError("TOC headings must use Markdown H2 through H6; H1 is the book title")
    ordered = sorted(mapping.items())
    depths = [depth for _, depth in ordered]
    if depths != list(range(1, len(depths) + 1)):
        raise HarnessError(
            "TOC depths must increase contiguously with heading level (1, 2, 3, ...)"
        )
    return dict(ordered)


def load_book_meta(config_dir: Path, book_key: str) -> BookMeta:
    require_book_key(book_key)
    book_config_dir = resolve_book_config_dir(config_dir, book_key)
    path = book_config_dir / "book_meta.json"
    data = _load_json_object(path)
    configured_key = data.get("book_key")
    if configured_key is not None and configured_key != book_key:
        raise HarnessError(
            f"{path}: book_key {configured_key!r} does not match requested key {book_key!r}"
        )
    title = _required_string(data, "title", path)
    author = _required_string(data, "author", path)
    language = clean_text(str(data.get("language", "ko")))
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise HarnessError(f"{path}: language must be a valid BCP-47-style tag")
    identifier_raw = data.get("identifier")
    if identifier_raw is None:
        identifier = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'ebook-harness:{book_key}')}"
    elif isinstance(identifier_raw, str) and clean_text(identifier_raw):
        identifier = clean_text(identifier_raw)
    else:
        raise HarnessError(f"{path}: identifier must be a non-empty string")

    raw_levels = data.get("toc_levels", data.get("toc_heading_levels"))
    toc_levels = parse_toc_levels(raw_levels)
    extra_patterns = data.get("forbidden_patterns", [])
    if not isinstance(extra_patterns, list) or not all(
        isinstance(item, str) and item for item in extra_patterns
    ):
        raise HarnessError(f"{path}: forbidden_patterns must be a list of regex strings")
    contract_path = book_config_dir / "toc_contract.json"
    contract_patterns: list[str] = []
    if contract_path.is_file():
        contract_data = _load_json_object(contract_path)
        raw_contract_patterns = contract_data.get("forbidden_label_patterns", [])
        if not isinstance(raw_contract_patterns, list) or not all(
            isinstance(item, str) and item for item in raw_contract_patterns
        ):
            raise HarnessError(
                f"{contract_path}: forbidden_label_patterns must be a list of regex strings"
            )
        contract_patterns = raw_contract_patterns
    patterns = (
        tuple(DEFAULT_FORBIDDEN_PATTERNS)
        + tuple(extra_patterns)
        + tuple(contract_patterns)
    )
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise HarnessError(f"{path}: invalid forbidden pattern {pattern!r}: {exc}") from exc
    return BookMeta(
        book_key=book_key,
        title=title,
        author=author,
        language=language,
        identifier=identifier,
        toc_levels=toc_levels,
        forbidden_patterns=patterns,
    )


def _parse_contract_items(raw: Any, *, path: str = "items") -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HarnessError(f"toc_contract.json: {path} must be a list")
    parsed: list[dict[str, Any]] = []
    labels: set[str] = set()
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise HarnessError(f"toc_contract.json: {item_path} must be an object")
        unknown = set(item) - {"label", "children"}
        if unknown:
            raise HarnessError(
                f"toc_contract.json: {item_path} has unsupported fields {sorted(unknown)}"
            )
        label = item.get("label")
        if not isinstance(label, str) or not clean_text(label):
            raise HarnessError(f"toc_contract.json: {item_path}.label must be a string")
        label = clean_text(label)
        if label in labels:
            raise HarnessError(
                f"toc_contract.json: duplicate same-parent label {label!r} at {item_path}"
            )
        labels.add(label)
        parsed.append(
            {
                "label": label,
                "children": _parse_contract_items(
                    item.get("children", []), path=f"{item_path}.children"
                ),
            }
        )
    return parsed


def load_toc_contract(config_dir: Path, book_key: str) -> list[dict[str, Any]]:
    require_book_key(book_key)
    path = resolve_book_config_dir(config_dir, book_key) / "toc_contract.json"
    data = _load_json_object(path)
    allowed_fields = {
        "version",
        "book_key",
        "items",
        "forbidden_label_patterns",
        "source_scope",
    }
    unknown_fields = set(data) - allowed_fields
    if unknown_fields:
        raise HarnessError(
            f"{path}: unsupported top-level fields {sorted(unknown_fields)}"
        )
    if data.get("version") != 1:
        raise HarnessError(f"{path}: version must be 1")
    configured_key = data.get("book_key")
    if configured_key is not None and configured_key != book_key:
        raise HarnessError(
            f"{path}: book_key {configured_key!r} does not match requested key {book_key!r}"
        )
    forbidden_patterns = data.get("forbidden_label_patterns")
    if not isinstance(forbidden_patterns, list) or not all(
        isinstance(item, str) and item for item in forbidden_patterns
    ):
        raise HarnessError(
            f"{path}: forbidden_label_patterns must be a list of non-empty regex strings"
        )
    for pattern in forbidden_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise HarnessError(f"{path}: invalid forbidden pattern {pattern!r}: {exc}") from exc
    source_scope = data.get("source_scope")
    if source_scope is not None and not isinstance(source_scope, dict):
        raise HarnessError(f"{path}: source_scope must be an object when supplied")
    items = _parse_contract_items(data.get("items"))
    if not items:
        raise HarnessError(f"{path}: the exact TOC contract must contain at least one item")
    return items


def flatten_contract(items: Iterable[dict[str, Any]], depth: int = 1) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for item in items:
        result.append((depth, str(item["label"])))
        result.extend(flatten_contract(item.get("children", []), depth + 1))
    return result


def toc_signature(nodes: Iterable[TocNode]) -> list[dict[str, Any]]:
    return [
        {"label": node.label, "children": toc_signature(node.children)} for node in nodes
    ]


def _check_forbidden(labels: Iterable[str], patterns: Iterable[str]) -> None:
    compiled = [re.compile(pattern) for pattern in patterns]
    for label in labels:
        matches = [pattern.pattern for pattern in compiled if pattern.search(label)]
        if matches:
            raise HarnessError(
                f"Reader-facing TOC label {label!r} matches forbidden pattern(s): {matches}"
            )


def _fence_state(line: str, current: str | None) -> str | None:
    stripped = line.lstrip()
    marker_match = re.match(r"(`{3,}|~{3,})", stripped)
    if not marker_match:
        return current
    marker = marker_match.group(1)
    if current is None:
        return marker[0]
    return None if marker[0] == current else current


def parse_combined_markdown(
    text: str,
    meta: BookMeta,
    contract: list[dict[str, Any]],
) -> ParsedBook:
    """Split one combined Markdown book and enforce its exact outline contract."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.splitlines()
    headings: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        old_fence = fence
        fence = _fence_state(line, fence)
        if old_fence is not None or fence is not None and old_fence != fence:
            continue
        match = HEADING_RE.fullmatch(line.strip())
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))

    h1 = [(line, title) for line, level, title in headings if level == 1]
    if len(h1) != 1:
        raise HarnessError(f"Combined Markdown must contain exactly one H1 book title; found {len(h1)}")
    h1_line, raw_h1 = h1[0]
    if plain_label(raw_h1) != meta.title:
        raise HarnessError(
            f"H1 title {plain_label(raw_h1)!r} does not match book_meta.json title {meta.title!r}"
        )
    mapped = [item for item in headings if item[1] in meta.toc_levels]
    if not mapped:
        raise HarnessError("No headings use the configured TOC heading levels")
    if h1_line > mapped[0][0]:
        raise HarnessError("The H1 book title must appear before every TOC heading")
    notice_count = normalized.count(AI_NOTICE)
    if notice_count != 1:
        raise HarnessError(
            f"Combined Markdown must contain the exact AI notice once; found {notice_count}"
        )
    if any(plain_label(title) == AI_NOTICE for _, _, title in headings):
        raise HarnessError("The AI notice must be body text or a blockquote, never a heading")
    first_navigation_offset = sum(len(line) + 1 for line in lines[: mapped[0][0]])
    if normalized.find(AI_NOTICE) >= first_navigation_offset:
        raise HarnessError("The AI notice must appear before the first navigation heading")

    first_mapped_line = mapped[0][0]
    title_markdown = "\n".join(lines[:first_mapped_line]).strip()
    title_document = MarkdownDocument(
        filename="title.xhtml",
        title=meta.title,
        markdown=title_markdown,
        first_heading_id="book-title",
        in_navigation=False,
    )

    toc_roots: list[TocNode] = []
    toc_stack: dict[int, TocNode] = {}
    documents: list[MarkdownDocument] = []
    sibling_labels: dict[int, set[str]] = {}
    used_anchors: set[str] = set()

    for ordinal, (start, heading_level, raw_title) in enumerate(mapped, 1):
        end = mapped[ordinal][0] if ordinal < len(mapped) else len(lines)
        label = plain_label(raw_title)
        if not label:
            raise HarnessError(f"Empty visible heading at Markdown line {start + 1}")
        depth = meta.toc_levels[heading_level]
        if depth > 1 and depth - 1 not in toc_stack:
            raise HarnessError(
                f"TOC hierarchy skips a parent before {label!r} at Markdown line {start + 1}"
            )
        parent_identity = id(toc_stack[depth - 1]) if depth > 1 else 0
        labels = sibling_labels.setdefault(parent_identity, set())
        if label in labels:
            raise HarnessError(f"Duplicate same-parent TOC label {label!r}")
        labels.add(label)

        base_anchor = slugify(label, fallback=f"section-{ordinal}")
        anchor = base_anchor
        suffix = 2
        while anchor in used_anchors:
            anchor = f"{base_anchor}-{suffix}"
            suffix += 1
        used_anchors.add(anchor)
        filename = f"section-{ordinal:04d}.xhtml"
        target = f"{filename}#{anchor}"
        node = TocNode(label=label, target=target)
        if depth == 1:
            toc_roots.append(node)
        else:
            toc_stack[depth - 1].children.append(node)
        toc_stack[depth] = node
        for stale_depth in [key for key in toc_stack if key > depth]:
            del toc_stack[stale_depth]
        documents.append(
            MarkdownDocument(
                filename=filename,
                title=label,
                markdown="\n".join(lines[start:end]).strip(),
                first_heading_id=anchor,
                in_navigation=True,
            )
        )

    actual = toc_signature(toc_roots)
    if actual != contract:
        expected_flat = flatten_contract(contract)
        actual_flat = flatten_contract(actual)
        mismatch = next(
            (
                index
                for index, pair in enumerate(zip(expected_flat, actual_flat))
                if pair[0] != pair[1]
            ),
            min(len(expected_flat), len(actual_flat)),
        )
        expected_item = expected_flat[mismatch] if mismatch < len(expected_flat) else "<end>"
        actual_item = actual_flat[mismatch] if mismatch < len(actual_flat) else "<end>"
        raise HarnessError(
            "Markdown TOC does not exactly match toc_contract.json at item "
            f"{mismatch + 1}: expected {expected_item!r}, found {actual_item!r}"
        )
    _check_forbidden((label for _, label in flatten_contract(actual)), meta.forbidden_patterns)
    return ParsedBook(
        title_document=title_document,
        documents=tuple(documents),
        toc=tuple(toc_roots),
    )


def safe_zip_target(source_member: str, href: str) -> tuple[str, str]:
    """Resolve a package-relative URI while rejecting archive traversal."""
    split = urlsplit(href)
    if split.scheme or split.netloc:
        raise HarnessError(f"Expected an internal EPUB link, found external URI {href!r}")
    decoded = unquote(split.path)
    if decoded.startswith("/"):
        raise HarnessError(f"Absolute EPUB path is forbidden: {href!r}")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(source_member), decoded))
    if target == ".." or target.startswith("../"):
        raise HarnessError(f"EPUB link escapes the archive root: {href!r}")
    return target, unquote(split.fragment)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def element_text(element: Any) -> str:
    return clean_text("".join(element.itertext()))
