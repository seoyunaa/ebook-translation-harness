from __future__ import annotations

import argparse
import json
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pdfplumber


# Running heads and page numbers normally sit inside the outermost margin.
# Keeping this deliberately narrow avoids deleting figures/tables that begin near
# (but not inside) the page margin.
DEFAULT_EDGE_FRACTION = 0.08
PAGE_NUMBER_EDGE_FRACTION = 0.12
DEFAULT_MIN_REPEAT = 3
MAX_RUNNING_HEAD_LENGTH = 200


@dataclass(frozen=True)
class PositionedLine:
    """One visually reconstructed PDF line and its page coordinates."""

    text: str
    top: float
    bottom: float
    x0: float = 0.0
    x1: float = 0.0
    font_size: float | None = None


@dataclass(frozen=True)
class PageRecord:
    """The positioned lines extracted from one physical PDF page."""

    page_number: int
    width: float
    height: float
    lines: tuple[PositionedLine, ...]
    used_text_fallback: bool = False


@dataclass(frozen=True)
class RemovedLine:
    page: int
    region: str
    reason: str
    text: str
    normalized_key: str
    top: float
    bottom: float


RemovalMap = dict[tuple[int, int], dict[str, str]]


PAGE_NUMBER_RE = re.compile(
    r"^\s*[\[\](){}/\\<>|:;,.\-\u2013\u2014\u00b7\u2022]*\s*"
    r"(?:\d{1,4}|[ivxlcdm]{1,10})"
    r"\s*[\[\](){}/\\<>|:;,.\-\u2013\u2014\u00b7\u2022]*\s*$",
    re.IGNORECASE,
)


def is_isolated_page_number(text: str) -> bool:
    """Return True for a number/roman numeral standing alone at a page edge."""

    return bool(PAGE_NUMBER_RE.fullmatch(unicodedata.normalize("NFKC", text)))


def normalize_running_head(text: str) -> str:
    """Normalize a page-edge line so changing page numbers do not hide repeats."""

    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^\s*(?:page|p\.?|쪽)\s*\d{1,4}\s*", "", value)
    value = re.sub(r"^\s*\d{1,4}\s+(?=\D)", "", value)
    value = re.sub(r"(?<=\D)\s+\d{1,4}\s*$", "", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip(" _")


def _line_region(
    line: PositionedLine, page_height: float, edge_fraction: float
) -> str | None:
    top_boundary = page_height * edge_fraction
    bottom_boundary = page_height * (1.0 - edge_fraction)
    if line.top <= top_boundary:
        return "top"
    if line.bottom >= bottom_boundary:
        return "bottom"
    return None


def detect_repeated_edge_lines(
    pages: list[PageRecord],
    *,
    edge_fraction: float = DEFAULT_EDGE_FRACTION,
    min_repeat: int = DEFAULT_MIN_REPEAT,
) -> RemovalMap:
    """Find page numbers and repeated running heads/feet at physical page edges.

    A repeated phrase is removed only when it occurs on at least ``min_repeat``
    distinct pages in the same edge region. Repeated prose in the page body is
    intentionally ignored.
    """

    if not 0.0 < edge_fraction < 0.5:
        raise ValueError("edge_fraction must be between 0 and 0.5")
    if min_repeat < 2:
        raise ValueError("min_repeat must be at least 2")

    occurrences: dict[tuple[str, str], set[int]] = defaultdict(set)
    candidates: dict[tuple[int, int], tuple[str, str]] = {}
    removals: RemovalMap = {}

    for page in pages:
        for line_index, line in enumerate(page.lines):
            text = line.text.strip()
            region = _line_region(line, page.height, edge_fraction)
            page_number_region = _line_region(
                line,
                page.height,
                max(edge_fraction, PAGE_NUMBER_EDGE_FRACTION),
            )
            if not text:
                continue

            if page_number_region is not None and is_isolated_page_number(text):
                removals[(page.page_number, line_index)] = {
                    "region": page_number_region,
                    "reason": "page_number",
                    "normalized_key": text.casefold(),
                }
                continue

            if region is None:
                continue
            if len(text) > MAX_RUNNING_HEAD_LENGTH:
                continue
            key = normalize_running_head(text)
            if len(key) < 2:
                continue
            occurrence_key = (region, key)
            occurrences[occurrence_key].add(page.page_number)
            candidates[(page.page_number, line_index)] = occurrence_key

    repeated_keys = {
        key for key, page_numbers in occurrences.items() if len(page_numbers) >= min_repeat
    }
    for location, (region, key) in candidates.items():
        if (region, key) not in repeated_keys:
            continue
        removals[location] = {
            "region": region,
            "reason": (
                "repeated_running_header"
                if region == "top"
                else "repeated_running_footer"
            ),
            "normalized_key": key,
        }
    return removals


def _words_to_positioned_lines(words: list[dict[str, Any]]) -> tuple[PositionedLine, ...]:
    if not words:
        return ()

    heights = [
        max(float(word.get("bottom", 0.0)) - float(word.get("top", 0.0)), 0.1)
        for word in words
    ]
    median_height = statistics.median(heights)
    y_tolerance = max(2.0, min(5.0, median_height * 0.35))

    groups: list[dict[str, Any]] = []
    ordered_words = sorted(
        words,
        key=lambda word: (float(word.get("top", 0.0)), float(word.get("x0", 0.0))),
    )
    for word in ordered_words:
        top = float(word.get("top", 0.0))
        bottom = float(word.get("bottom", top))
        target: dict[str, Any] | None = None
        for group in reversed(groups[-4:]):
            overlaps = min(bottom, group["bottom"]) - max(top, group["top"])
            near_baseline = abs(top - group["anchor_top"]) <= y_tolerance
            if overlaps > 0 or near_baseline:
                target = group
                break
        if target is None:
            groups.append(
                {
                    "anchor_top": top,
                    "top": top,
                    "bottom": bottom,
                    "words": [word],
                }
            )
        else:
            target["words"].append(word)
            target["top"] = min(target["top"], top)
            target["bottom"] = max(target["bottom"], bottom)

    lines: list[PositionedLine] = []
    for group in groups:
        line_words = sorted(group["words"], key=lambda word: float(word.get("x0", 0.0)))
        text = " ".join(str(word.get("text", "")).strip() for word in line_words).strip()
        if not text:
            continue
        sizes = [
            float(word["size"])
            for word in line_words
            if isinstance(word.get("size"), (int, float))
        ]
        lines.append(
            PositionedLine(
                text=text,
                top=float(group["top"]),
                bottom=float(group["bottom"]),
                x0=min(float(word.get("x0", 0.0)) for word in line_words),
                x1=max(float(word.get("x1", 0.0)) for word in line_words),
                font_size=statistics.median(sizes) if sizes else None,
            )
        )
    return tuple(sorted(lines, key=lambda line: (line.top, line.x0)))


def _fallback_text_lines(text: str, page_height: float) -> tuple[PositionedLine, ...]:
    """Keep text when a PDF exposes no usable positioned words."""

    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not raw_lines:
        return ()
    step = page_height / (len(raw_lines) + 1)
    return tuple(
        PositionedLine(
            text=line,
            top=step * index,
            bottom=step * index + min(12.0, step * 0.8),
        )
        for index, line in enumerate(raw_lines, start=1)
    )


def _text_map_positioned_lines(page: Any) -> tuple[PositionedLine, ...]:
    """Use pdfplumber's reading-order text map while retaining page coordinates.

    Word positions are still extracted below and remain the fallback. The text
    map is preferred for body rendering because it handles mixed font baselines
    and footnotes more faithfully than hand-joining every word on the same y
    coordinate.
    """

    try:
        raw_lines = page.extract_text_lines(strip=True, return_chars=True)
    except (AttributeError, KeyError, TypeError, ValueError):
        return ()

    lines: list[PositionedLine] = []
    for item in raw_lines:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        chars = item.get("chars") or []
        sizes = [
            float(char["size"])
            for char in chars
            if isinstance(char.get("size"), (int, float))
        ]
        lines.append(
            PositionedLine(
                text=text,
                top=float(item.get("top", 0.0)),
                bottom=float(item.get("bottom", item.get("top", 0.0))),
                x0=float(item.get("x0", 0.0)),
                x1=float(item.get("x1", 0.0)),
                font_size=statistics.median(sizes) if sizes else None,
            )
        )
    return tuple(lines)


def _extract_page_record(page: Any, page_number: int) -> PageRecord:
    width = float(page.width)
    height = float(page.height)
    try:
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=True,
            extra_attrs=["size"],
        )
    except (KeyError, TypeError, ValueError):
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=True,
        )
    word_positioned_lines = _words_to_positioned_lines(words)
    lines = _text_map_positioned_lines(page) or word_positioned_lines
    used_fallback = False
    if not lines:
        lines = _fallback_text_lines(page.extract_text() or "", height)
        used_fallback = bool(lines)
    return PageRecord(
        page_number=page_number,
        width=width,
        height=height,
        lines=lines,
        used_text_fallback=used_fallback,
    )


def render_markdown_pages(pages: list[PageRecord], removals: RemovalMap) -> str:
    """Render cleaned pages, keeping physical page boundaries as HTML comments."""

    rendered: list[str] = []
    for page in pages:
        rendered.append(f"<!-- PDF_PAGE: {page.page_number} -->")
        kept_lines = [
            line
            for index, line in enumerate(page.lines)
            if (page.page_number, index) not in removals
        ]
        if kept_lines:
            line_heights = [max(line.bottom - line.top, 0.1) for line in kept_lines]
            paragraph_gap = max(7.0, statistics.median(line_heights) * 0.65)
            previous: PositionedLine | None = None
            for line in kept_lines:
                if previous is not None and line.top - previous.bottom > paragraph_gap:
                    rendered.append("")
                rendered.append(line.text)
                previous = line
        rendered.append("")
    return "\n".join(rendered).rstrip() + "\n"


def _removed_line_records(
    pages: list[PageRecord], removals: RemovalMap
) -> list[RemovedLine]:
    records: list[RemovedLine] = []
    for page in pages:
        for index, line in enumerate(page.lines):
            decision = removals.get((page.page_number, index))
            if decision is None:
                continue
            records.append(
                RemovedLine(
                    page=page.page_number,
                    region=decision["region"],
                    reason=decision["reason"],
                    text=line.text,
                    normalized_key=decision["normalized_key"],
                    top=round(line.top, 3),
                    bottom=round(line.bottom, 3),
                )
            )
    return records


def _markdown_report(result: dict[str, object]) -> str:
    reason_counts = result["removed_by_reason"]
    removed_lines = result["removed_lines"]
    lines = [
        "# PDF extraction cleanup report",
        "",
        f"- Input: `{result['pdf']}`",
        f"- Output: `{result['output']}`",
        f"- Selected pages: {result['start']}-{result['end']} "
        f"({result['selected_pages']} pages)",
        f"- Extracted lines: {result['lines_total']}",
        f"- Removed edge lines: {result['lines_removed']}",
        f"- Positioned-word fallback pages: {result['fallback_pages']}",
        "",
        "## Removal summary",
        "",
    ]
    if isinstance(reason_counts, dict) and reason_counts:
        for reason, count in sorted(reason_counts.items()):
            lines.append(f"- `{reason}`: {count}")
    else:
        lines.append("- No page-edge lines were removed.")

    lines.extend(
        [
            "",
            "## Removed lines",
            "",
            "| PDF page | Region | Reason | Text |",
            "|---:|---|---|---|",
        ]
    )
    if isinstance(removed_lines, list) and removed_lines:
        for item in removed_lines:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {item.get('page', '')} | {item.get('region', '')} | "
                f"{item.get('reason', '')} | {text} |"
            )
    else:
        lines.append("| - | - | - | No removals |")
    lines.append("")
    return "\n".join(lines)


def extract_pdf_to_markdown(
    pdf_path: Path,
    output_path: Path,
    *,
    start: int = 1,
    end: int | None = None,
    report_json: Path | None = None,
    report_markdown: Path | None = None,
) -> dict[str, object]:
    """Extract a PDF page range to cleaned Markdown and return a cleanup report."""

    pdf_path = Path(pdf_path)
    output_path = Path(output_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        first_page = max(int(start), 1)
        last_page = page_count if end is None else min(int(end), page_count)
        if first_page > last_page:
            raise ValueError(
                f"Invalid page range: {first_page}-{last_page} for {page_count} pages"
            )
        pages = [
            _extract_page_record(pdf.pages[page_number - 1], page_number)
            for page_number in range(first_page, last_page + 1)
        ]

    removals = detect_repeated_edge_lines(pages)
    removed = _removed_line_records(pages, removals)
    markdown = render_markdown_pages(pages, removals)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    reason_counts = Counter(item.reason for item in removed)
    result: dict[str, object] = {
        "pdf": str(pdf_path.resolve()),
        "output": str(output_path.resolve()),
        "page_count": page_count,
        "start": first_page,
        "end": last_page,
        "selected_pages": len(pages),
        "lines_total": sum(len(page.lines) for page in pages),
        "lines_removed": len(removed),
        "removed_by_reason": dict(sorted(reason_counts.items())),
        "fallback_pages": [
            page.page_number for page in pages if page.used_text_fallback
        ],
        "edge_fraction": DEFAULT_EDGE_FRACTION,
        "page_number_edge_fraction": PAGE_NUMBER_EDGE_FRACTION,
        "minimum_repeat_pages": DEFAULT_MIN_REPEAT,
        "removed_lines": [asdict(item) for item in removed],
    }
    if report_json is not None:
        report_json = Path(report_json)
        report_json.parent.mkdir(parents=True, exist_ok=True)
        report_json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if report_markdown is not None:
        report_markdown = Path(report_markdown)
        report_markdown.parent.mkdir(parents=True, exist_ok=True)
        report_markdown.write_text(_markdown_report(result), encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract selected PDF pages to UTF-8 Markdown while removing repeated "
            "running heads, running feet, and isolated page numbers."
        )
    )
    parser.add_argument("pdf", help="Input PDF path")
    parser.add_argument("output", help="Output Markdown path")
    parser.add_argument("--start", type=int, default=1, help="First PDF page, 1-based")
    parser.add_argument("--end", type=int, default=None, help="Last PDF page, 1-based")
    parser.add_argument("--report-json", help="Optional JSON cleanup report path")
    parser.add_argument(
        "--report-markdown",
        "--report-md",
        dest="report_markdown",
        help="Optional Markdown cleanup report path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = extract_pdf_to_markdown(
            Path(args.pdf),
            Path(args.output),
            start=args.start,
            end=args.end,
            report_json=Path(args.report_json) if args.report_json else None,
            report_markdown=(
                Path(args.report_markdown) if args.report_markdown else None
            ),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(f"wrote={result['output']}")
    print(
        f"pages={result['start']}-{result['end']} of {result['page_count']} "
        f"removed_edge_lines={result['lines_removed']}"
    )
    if args.report_json:
        print(f"report_json={Path(args.report_json).resolve()}")
    if args.report_markdown:
        print(f"report_markdown={Path(args.report_markdown).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
