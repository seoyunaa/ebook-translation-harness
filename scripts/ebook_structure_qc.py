"""Run deterministic structure checks before publishing a combined Markdown EPUB."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from epub_core import (
    AI_NOTICE,
    HEADING_RE,
    HarnessError,
    clean_text,
    flatten_contract,
    load_book_meta,
    load_toc_contract,
    parse_combined_markdown,
    project_path,
)


PDF_MARKER_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:=+\s*)?(?:PDF\s*PAGE|PDF\s*페이지)\s*\d+(?:\s*=+)?$",
    re.IGNORECASE,
)
WORK_HEADING_RE = re.compile(
    r"^(?:AI\s*)?(?:draft|translation\s*draft|polished\s*translation|worker\s*output|"
    r"초벌\s*번역|번역\s*초안|윤문\s*번역|작업\s*결과)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    line: int | None = None
    sample: str = ""


@dataclass
class Report:
    book_key: str
    source: str
    sha256: str
    heading_count: int
    contract_items: int
    contract_depth: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(issue.severity == "error" for issue in self.issues):
            return "BLOCKED"
        if self.issues:
            return "WARN"
        return "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "book_key": self.book_key,
            "source": self.source,
            "sha256": self.sha256,
            "heading_count": self.heading_count,
            "contract_items": self.contract_items,
            "contract_depth": self.contract_depth,
            "issues": [asdict(issue) for issue in self.issues],
        }


def _contract_stats(items: list[dict[str, Any]]) -> tuple[int, int]:
    flat = flatten_contract(items)
    return len(flat), max((depth for depth, _ in flat), default=0)


def _paragraphs(text: str) -> list[tuple[int, str]]:
    paragraphs: list[tuple[int, str]] = []
    current: list[str] = []
    start = 1
    fence: str | None = None
    for line_number, line in enumerate(text.splitlines() + [""], 1):
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            char = marker.group(1)[0]
            fence = char if fence is None else None if char == fence else fence
        if fence is not None:
            if not current:
                start = line_number
            current.append(line)
            continue
        if line.strip():
            if not current:
                start = line_number
            current.append(line)
        elif current:
            paragraphs.append((start, "\n".join(current).strip()))
            current = []
    return paragraphs


def audit(
    *,
    book_key: str,
    markdown_path: Path,
    config_dir: Path,
) -> Report:
    try:
        text = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"Cannot read UTF-8 Markdown from {markdown_path}: {exc}") from exc
    meta = load_book_meta(config_dir, book_key)
    contract = load_toc_contract(config_dir, book_key)
    contract_count, contract_depth = _contract_stats(contract)
    headings = [
        (line_number, match)
        for line_number, line in enumerate(text.splitlines(), 1)
        if (match := HEADING_RE.fullmatch(line.strip()))
    ]
    report = Report(
        book_key=book_key,
        source=str(markdown_path),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        heading_count=len(headings),
        contract_items=contract_count,
        contract_depth=contract_depth,
    )

    notice_count = text.count(AI_NOTICE)
    if notice_count != 1:
        report.issues.append(
            Issue(
                "error",
                "AI_NOTICE_COUNT",
                f"The exact AI notice must appear once; found {notice_count}.",
            )
        )
    navigation_lines = [
        line_number
        for line_number, match in headings
        if len(match.group(1)) in meta.toc_levels
    ]
    notice_line = next(
        (line_number for line_number, line in enumerate(text.splitlines(), 1) if AI_NOTICE in line),
        None,
    )
    if notice_line is not None and navigation_lines and notice_line >= navigation_lines[0]:
        report.issues.append(
            Issue(
                "error",
                "AI_NOTICE_POSITION",
                "The AI notice must appear before the first navigation heading.",
                notice_line,
            )
        )
    for line_number, match in headings:
        if clean_text(match.group(2).replace("*", "").replace("_", "").replace("`", "")) == AI_NOTICE:
            report.issues.append(
                Issue(
                    "error",
                    "AI_NOTICE_HEADING",
                    "The AI notice must not be a Markdown heading.",
                    line_number,
                    match.group(0),
                )
            )

    try:
        parsed = parse_combined_markdown(text, meta, contract)
        if len(parsed.documents) != contract_count:
            report.issues.append(
                Issue(
                    "error",
                    "OUTLINE_COUNT",
                    f"Parsed {len(parsed.documents)} navigation documents but contract has {contract_count} items.",
                )
            )
    except HarnessError as exc:
        report.issues.append(Issue("error", "CONTRACT", str(exc)))

    fence: str | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            char = marker.group(1)[0]
            fence = char if fence is None else None if char == fence else fence
            continue
        if fence is not None:
            continue
        if PDF_MARKER_RE.fullmatch(stripped):
            report.issues.append(
                Issue("error", "PDF_MARKER", "PDF page marker leaked into reader text.", line_number, stripped)
            )
        heading = HEADING_RE.fullmatch(stripped)
        if heading and WORK_HEADING_RE.fullmatch(heading.group(2).strip()):
            report.issues.append(
                Issue(
                    "error",
                    "WORK_HEADING",
                    "A worker-stage heading leaked into reader-facing structure.",
                    line_number,
                    stripped,
                )
            )
    if fence is not None:
        report.issues.append(Issue("error", "FENCE", "Markdown contains an unclosed code fence."))

    standalone_numbers = [
        (line_number, line.strip())
        for line_number, line in enumerate(text.splitlines(), 1)
        if re.fullmatch(r"[1-9]\d{1,3}", line.strip())
    ]
    if len(standalone_numbers) >= 3:
        first_line, sample = standalone_numbers[0]
        report.issues.append(
            Issue(
                "warning",
                "PAGE_NUMBERS",
                f"Found {len(standalone_numbers)} standalone numbers that may be print page numbers.",
                first_line,
                sample,
            )
        )

    duplicates: dict[str, list[int]] = defaultdict(list)
    for line_number, paragraph in _paragraphs(text):
        if paragraph.startswith(("#", ">", "|", "```", "~~~", "<!--")):
            continue
        normalized = re.sub(r"\s+", " ", paragraph).strip().casefold()
        if len(normalized) >= 200:
            duplicates[normalized].append(line_number)
    for normalized, lines in duplicates.items():
        if len(lines) > 1:
            report.issues.append(
                Issue(
                    "error",
                    "DUPLICATE_PROSE",
                    f"An exact long prose paragraph is repeated at lines {lines}.",
                    lines[1],
                    normalized[:160],
                )
            )
    return report


def render_markdown(report: Report) -> str:
    lines = [
        f"# Structure QC: {report.status}",
        "",
        f"- Book key: `{report.book_key}`",
        f"- Source: `{report.source}`",
        f"- SHA-256: `{report.sha256}`",
        f"- Markdown headings: {report.heading_count}",
        f"- Contract items: {report.contract_items}",
        f"- Contract depth: {report.contract_depth}",
        "",
        "| Severity | Rule | Line | Message | Sample |",
        "|---|---|---:|---|---|",
    ]
    if not report.issues:
        lines.append("| pass | - | - | No structural problems detected. | - |")
    for issue in report.issues:
        message = issue.message.replace("|", "\\|")
        sample = issue.sample.replace("|", "\\|").replace("\n", " ")[:180]
        lines.append(
            f"| {issue.severity} | `{issue.code}` | {issue.line or ''} | {message} | {sample} |"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--combined-dir", default="03_outputs/translations/combined")
    parser.add_argument("--config-dir", default="03_outputs/translations/assets")
    parser.add_argument("--report-dir", default="03_outputs/translations/quality_reports")
    parser.add_argument("--only", action="append", required=True, metavar="BOOK_KEY")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).resolve()
    combined_dir = project_path(root, args.combined_dir)
    config_dir = project_path(root, args.config_dir)
    report_dir = project_path(root, args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    reports: list[Report] = []
    for book_key in args.only:
        markdown_path = combined_dir / f"{book_key}.md"
        try:
            report = audit(book_key=book_key, markdown_path=markdown_path, config_dir=config_dir)
        except HarnessError as exc:
            report = Report(book_key, str(markdown_path), "", 0, 0, 0, [Issue("error", "INPUT", str(exc))])
        reports.append(report)
        json_path = report_dir / f"{book_key}.structure.json"
        markdown_report_path = report_dir / f"{book_key}.structure.md"
        json_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        markdown_report_path.write_text(render_markdown(report), encoding="utf-8")
        print(
            f"{report.status} {book_key}: errors="
            f"{sum(issue.severity == 'error' for issue in report.issues)} report={json_path}"
        )
    return 0 if all(report.status != "BLOCKED" for report in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
