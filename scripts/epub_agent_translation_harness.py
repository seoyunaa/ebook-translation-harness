"""Generic coordinator for preparing, tracking, checking, and combining books.

The coordinator manages local work products only.  It does not translate text
and it never invents a TOC contract.  Existing task files are not overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from ebook_structure_qc import audit, render_markdown
from epub_core import (
    HEADING_RE,
    BookMeta,
    HarnessError,
    clean_text,
    load_book_meta,
    load_toc_contract,
    local_name,
    parse_combined_markdown,
    require_book_key,
    safe_zip_target,
)
from extract_epub_outline import atomic_write_json, extract_outline


AI_NOTICE = "이 전자책은 AI 윤문 번역본입니다."
PDF_MARKER_RE = re.compile(r"^(?:#{1,6}\s*)?PDF\s*PAGE\s*\d+", re.IGNORECASE)
WORK_HEADING_RE = re.compile(
    r"^(?:AI\s*)?(?:draft|translation\s*draft|polished\s*translation|worker\s*output|"
    r"초벌\s*번역|번역\s*초안|윤문\s*번역|작업\s*결과)$",
    re.IGNORECASE,
)
BOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TRANSLATION_MINIMUM_MODELS = {
    "gpt": "gpt-5.6-terra",
    "claude": "claude-opus-4-8",
}
AUTO_WORKER_FALLBACK = 4
AUTO_WORKER_BYTES = 2 * 1024**3
FOOTNOTE_MARKER_RE = re.compile(r"\[\^[^\]\n]+\]")
NUMBER_PATTERN = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?"
NUMBER_TOKEN_RE = re.compile(NUMBER_PATTERN)
QUANTITY_TOKEN_RE = re.compile(
    NUMBER_PATTERN
    + r"\s*(?:"
    r"percent|degrees?|kilometers?|centimeters?|millimeters?|meters?|miles?|"
    r"feet|foot|inches?|pounds?|ounces?|kilograms?|grams?|liters?|"
    r"hours?|minutes?|seconds?|km²|cm²|mm²|m²|km³|cm³|mm³|m³|"
    r"km|cm|mm|kg|mg|lb|oz|ml|µm|μm|ha|mph|kph|Hz|kHz|MHz|GHz|"
    r"%|‰|℃|℉|°C|°F|년|월|일|세기|쪽|페이지|장|절|권|호|명|개|건|회|차|배"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)
CURRENCY_TOKEN_RE = re.compile(
    rf"(?:[$€£¥₩]\s*{NUMBER_PATTERN}|(?:USD|EUR|GBP|JPY|KRW)\s*{NUMBER_PATTERN}|"
    rf"{NUMBER_PATTERN}\s*(?:USD|EUR|GBP|JPY|KRW|달러|유로|원|엔))(?![A-Za-z])",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s)>\]}]+", re.IGNORECASE)
MARKDOWN_LINK_TARGET_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*<?([^\s)>]+)>?(?:\s+['\"][^'\"\n]*['\"])?\s*\)"
)
REFERENCE_LINK_TARGET_RE = re.compile(
    r"^\s*\[(?!\^)[^\]\n]+\]:\s*<?([^\s>]+)>?",
    re.MULTILINE,
)
HTML_TARGET_RE = re.compile(
    r"\b(href|src|id)\s*=\s*['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)
INLINE_QUOTE_RE = re.compile(
    r'“[^”\n]+”|‘[^’\n]+’|"[^"\n]+"|'
    r"「[^」\n]+」|『[^』\n]+』|〈[^〉\n]+〉|《[^》\n]+》"
)
GLOSSARY_TEMPLATE = """# 책별 용어집과 문체표

병렬 작업자는 배치 중 이 파일을 읽기 전용으로 사용합니다. 충돌이나 새 용어는 보고하고,
배치 사이에 한 명의 조정자만 합의된 변경을 반영합니다.

| 원어·한자 | 한국어 표기 | 첫 등장 병기 | 설명·근거 |
|---|---|---|---|

## 문체

- 기본 문체:
- 인명·지명 표기 원칙:
- 인용·각주 처리 원칙:
"""


@dataclass(frozen=True)
class Workspace:
    project_root: Path

    @property
    def translations(self) -> Path:
        return self.project_root / "03_outputs" / "translations"

    @property
    def assets(self) -> Path:
        return self.translations / "assets"

    @property
    def combined(self) -> Path:
        return self.translations / "combined"

    @property
    def epub(self) -> Path:
        return self.translations / "epub"

    @property
    def reports(self) -> Path:
        return self.translations / "quality_reports"

    def book(self, book_id: str) -> Path:
        return self.translations / book_id


def require_book_id(value: str) -> str:
    if not BOOK_ID_RE.fullmatch(value):
        raise HarnessError(
            f"Invalid book id {value!r}; use lowercase ASCII letters, digits, '_' or '-'."
        )
    return value


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _workers_value(value: str) -> str | int:
    if value == "auto":
        return value
    return _positive_int(value)


def _available_memory_bytes() -> int | None:
    """Return currently available physical memory using only the standard library."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullAvailPhys)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None
    available = pages * page_size
    return available if available > 0 else None


def _resolve_next_plan(
    *, workers: str | int, limit: int | None, remaining: int
) -> tuple[int, str, str]:
    """Choose how many distinct ready tasks to list; this never starts workers."""
    if remaining <= 0:
        return 0, "none", "no ready tasks"
    if limit is not None:
        requested = limit
        mode = "legacy-limit"
        reason = f"explicit --limit={limit} overrides --workers"
    elif workers != "auto":
        requested = int(workers)
        mode = "manual"
        reason = f"explicit --workers={requested}"
    else:
        available = _available_memory_bytes()
        mode = "auto"
        if available is None:
            requested = AUTO_WORKER_FALLBACK
            reason = (
                "available memory and platform slots unavailable; "
                f"conservative fallback={AUTO_WORKER_FALLBACK}"
            )
        else:
            memory_budget = max(1, available // AUTO_WORKER_BYTES)
            requested = min(memory_budget, AUTO_WORKER_FALLBACK)
            reason = (
                f"available_memory={available / 1024**3:.2f}GiB; "
                "2GiB/worker; unverified platform slots cap=4"
            )
    planned = min(requested, remaining)
    if planned < requested:
        reason += f"; capped_by_ready_tasks={remaining}"
    return planned, mode, reason


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _book_context(args: argparse.Namespace) -> tuple[Workspace, str, str]:
    workspace = Workspace(Path(args.project_root).resolve())
    book_key = require_book_key(args.book_key)
    book_id = require_book_id(args.book_id or (book_key[:-3] if book_key.endswith("_ko") else book_key))
    return workspace, book_key, book_id


def _translation_policy(args: argparse.Namespace) -> dict[str, Any]:
    minimum_model = TRANSLATION_MINIMUM_MODELS[args.translation_platform]
    requested_model = clean_text(args.translation_model or minimum_model)
    if not requested_model:
        raise HarnessError("Translation model must not be empty")
    if args.translation_platform == "gpt" and requested_model.startswith("claude-"):
        raise HarnessError("A Claude model cannot be recorded for the GPT/Codex platform")
    if args.translation_platform == "claude" and requested_model.startswith("gpt-"):
        raise HarnessError("A GPT model cannot be recorded for the Claude platform")
    minimum_status = (
        "recorded_default"
        if requested_model == minimum_model
        else "custom_model_unverified"
    )
    return {
        "platform": args.translation_platform,
        "requested_model": requested_model,
        "minimum_model": minimum_model,
        "minimum_status": minimum_status,
        "requested_effort": args.translation_effort,
        "selection_surface": "external_subscription_session",
        "runtime_verified": False,
    }


def _translation_policy_summary(manifest: dict[str, Any]) -> str:
    policy = manifest.get("translation_policy")
    if not isinstance(policy, dict):
        return "unrecorded"
    platform = policy.get("platform", "unknown")
    model = policy.get("requested_model", "unknown")
    minimum = policy.get("minimum_model", "unknown")
    minimum_status = policy.get("minimum_status", "unknown")
    effort = policy.get("requested_effort", "unknown")
    surface = policy.get("selection_surface", "unknown")
    verified = str(bool(policy.get("runtime_verified", False))).lower()
    return (
        f"{platform}/{model} minimum={minimum} minimum_status={minimum_status} effort={effort} "
        f"surface={surface} runtime_verified={verified}"
    )


def _manifest_path(workspace: Workspace, book_id: str) -> Path:
    return workspace.book(book_id) / "manifest.json"


def load_manifest(workspace: Workspace, book_id: str) -> dict[str, Any]:
    path = _manifest_path(workspace, book_id)
    if not path.is_file():
        raise HarnessError(f"Manifest is missing: {path}. Run prepare first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise HarnessError(f"Invalid manifest structure: {path}")
    return data


def load_bound_manifest(
    workspace: Workspace,
    *,
    book_key: str,
    book_id: str,
) -> dict[str, Any]:
    """Load a manifest only when it belongs to the requested book identity."""
    manifest = load_manifest(workspace, book_id)
    recorded_key = manifest.get("book_key")
    recorded_id = manifest.get("book_id")
    mismatches: list[str] = []
    if recorded_key != book_key:
        mismatches.append(f"book_key {recorded_key!r} != {book_key!r}")
    if recorded_id != book_id:
        mismatches.append(f"book_id {recorded_id!r} != {book_id!r}")
    if mismatches:
        raise HarnessError(
            "Manifest identity does not match the requested book; refusing cross-book access: "
            + "; ".join(mismatches)
        )
    return manifest


def resolve_coordinator_config_dir(
    workspace: Workspace,
    *,
    book_key: str,
    book_id: str,
) -> Path:
    """Resolve one unambiguous assets folder, including an explicit custom id."""
    names = [book_id, book_key]
    if book_key.endswith("_ko") and len(book_key) > 3:
        names.append(book_key[:-3])
    candidates: list[Path] = []
    for name in names:
        candidate = workspace.assets / name
        if candidate not in candidates:
            candidates.append(candidate)
    existing = [candidate for candidate in candidates if candidate.is_dir()]
    if len(existing) > 1:
        raise HarnessError(
            f"Ambiguous configuration for {book_key!r}/{book_id!r}; "
            f"multiple assets folders exist: {existing}"
        )
    if not existing:
        raise HarnessError(
            f"No configuration directory for {book_key!r}/{book_id!r}; "
            f"expected one of: {candidates}"
        )
    return existing[0]


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise HarnessError(f"Required file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Cannot read valid UTF-8 JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"Expected a JSON object in {path}")
    return value


def load_coordinator_configuration(
    workspace: Workspace,
    *,
    book_key: str,
    book_id: str,
) -> tuple[BookMeta, list[dict[str, Any]], Path]:
    """Load strict metadata/contract from the uniquely resolved assets folder.

    The core loader intentionally resolves folders from ``book_key``.  For an
    explicit custom ``book_id``, expose the selected files through a temporary
    book-key-shaped view so the exact same validation rules remain in force.
    """
    config_dir = resolve_coordinator_config_dir(
        workspace, book_key=book_key, book_id=book_id
    )
    conventional_names = {book_key}
    if book_key.endswith("_ko") and len(book_key) > 3:
        conventional_names.add(book_key[:-3])
    if config_dir.name in conventional_names:
        return (
            load_book_meta(workspace.assets, book_key),
            load_toc_contract(workspace.assets, book_key),
            config_dir,
        )

    meta_path = config_dir / "book_meta.json"
    meta_data = _read_json_object(meta_path)
    if meta_data.get("book_id") not in {None, book_id}:
        raise HarnessError(
            f"{meta_path}: book_id {meta_data.get('book_id')!r} does not match {book_id!r}"
        )
    with tempfile.TemporaryDirectory(prefix="ebook-config-view-") as temp_dir:
        adapter = Path(temp_dir) / book_key
        adapter.mkdir()
        shutil.copyfile(meta_path, adapter / "book_meta.json")
        shutil.copyfile(
            config_dir / "toc_contract.json", adapter / "toc_contract.json"
        )
        adapted_root = Path(temp_dir)
        meta = load_book_meta(adapted_root, book_key)
        contract = load_toc_contract(adapted_root, book_key)
    return meta, contract, config_dir


def _stage_path(book_dir: Path, task: dict[str, Any], stage: str) -> Path:
    value = task.get(stage)
    if not isinstance(value, str) or not value:
        raise HarnessError(f"Task {task.get('id', '<unknown>')} has no {stage} path")
    path = (book_dir / value).resolve()
    try:
        path.relative_to(book_dir.resolve())
    except ValueError as exc:
        raise HarnessError(f"Task path escapes the book directory: {value!r}") from exc
    return path


def _translation_exists(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    return bool(text and text not in {"TODO", "TBD", "번역 예정", "윤문 예정"})


def _split_long(value: str, maximum: int) -> list[str]:
    if len(value) <= maximum:
        return [value]
    sentences = re.split(r"(?<=[.!?。！？])\s+", value)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > maximum:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(sentence[index : index + maximum] for index in range(0, len(sentence), maximum))
        elif current and len(current) + 1 + len(sentence) > maximum:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def split_markdown(text: str, maximum: int) -> list[str]:
    if maximum < 1000:
        raise HarnessError("--max-chars must be at least 1000")
    paragraphs = re.split(r"\n[ \t]*\n", text.strip())
    expanded: list[str] = []
    for paragraph in paragraphs:
        if paragraph.strip():
            expanded.extend(_split_long(paragraph.strip(), maximum))
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in expanded:
        addition = len(paragraph) + (2 if current else 0)
        if current and size + addition > maximum:
            chunks.append("\n\n".join(current).strip())
            current = []
            size = 0
        current.append(paragraph)
        size += addition
    if current:
        chunks.append("\n\n".join(current).strip())
    if not chunks:
        raise HarnessError("Source extraction produced no readable text")
    return chunks


def _element_text(element: ET.Element) -> str:
    return clean_text("".join(element.itertext()))


def _markdown_table(element: ET.Element) -> str:
    """Flatten an XHTML table without dropping any textual cell content."""
    rows: list[list[tuple[str, str]]] = []
    for row in (node for node in element.iter() if local_name(node.tag) == "tr"):
        cells = [
            (local_name(cell.tag), _element_text(cell))
            for cell in row
            if local_name(cell.tag) in {"th", "td"}
        ]
        if cells:
            rows.append(cells)
    if not rows:
        text = _element_text(element)
        return text

    width = max(len(row) for row in rows)

    def escaped(value: str) -> str:
        return value.replace("\\", "\\\\").replace("|", "\\|")

    first_is_header = any(kind == "th" for kind, _ in rows[0])
    if first_is_header:
        header_values = [value for _, value in rows[0]]
        body_rows = rows[1:]
    else:
        header_values = [""] * width
        body_rows = rows
    header_values += [""] * (width - len(header_values))
    rendered = [
        "| " + " | ".join(escaped(value) for value in header_values) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in body_rows:
        values = [value for _, value in row]
        values += [""] * (width - len(values))
        rendered.append("| " + " | ".join(escaped(value) for value in values) + " |")
    caption = next(
        (
            _element_text(node)
            for node in element
            if local_name(node.tag) == "caption" and _element_text(node)
        ),
        "",
    )
    table = "\n".join(rendered)
    return f"{caption}\n\n{table}" if caption else table


def _xhtml_to_markdown(root: ET.Element) -> str:
    blocks: list[str] = []

    def visit(element: ET.Element) -> None:
        name = local_name(element.tag)
        text = _element_text(element)
        if name in {"script", "style", "nav"}:
            return
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"} and text:
            blocks.append(f"{'#' * int(name[1])} {text}")
            return
        if name == "p" and text:
            blocks.append(text)
            return
        if name == "li" and text:
            blocks.append(f"- {text}")
            return
        if name == "blockquote" and text:
            blocks.append("\n".join(f"> {line}" for line in text.splitlines()))
            return
        if name == "table":
            table = _markdown_table(element)
            if table:
                blocks.append(table)
            return
        if name in {"figcaption", "caption", "dt", "dd"} and text:
            blocks.append(text)
            return
        for child in element:
            visit(child)

    body = next((item for item in root.iter() if local_name(item.tag) == "body"), root)
    for child in body:
        visit(child)
    return "\n\n".join(blocks).strip()


def extract_epub_markdown(path: Path) -> str:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HarnessError(f"Cannot open source EPUB: {exc}") from exc
    with archive:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError) as exc:
            raise HarnessError(f"Cannot parse source EPUB container: {exc}") from exc
        rootfiles = [item for item in container.iter() if local_name(item.tag) == "rootfile"]
        if len(rootfiles) != 1:
            raise HarnessError(f"Expected one OPF rootfile; found {len(rootfiles)}")
        opf_member = rootfiles[0].attrib.get("full-path", "")
        try:
            opf = ET.fromstring(archive.read(opf_member))
        except (KeyError, ET.ParseError) as exc:
            raise HarnessError(f"Cannot parse source EPUB OPF: {exc}") from exc
        manifest: dict[str, str] = {}
        for item in opf.iter():
            if local_name(item.tag) == "item" and item.attrib.get("id") and item.attrib.get("href"):
                target, fragment = safe_zip_target(opf_member, item.attrib["href"])
                if not fragment:
                    manifest[item.attrib["id"]] = target
        documents: list[str] = []
        for itemref in (item for item in opf.iter() if local_name(item.tag) == "itemref"):
            member = manifest.get(itemref.attrib.get("idref", ""))
            if not member:
                continue
            try:
                root = ET.fromstring(archive.read(member))
            except (KeyError, ET.ParseError):
                continue
            markdown = _xhtml_to_markdown(root)
            if markdown:
                documents.append(markdown)
        if not documents:
            raise HarnessError("No readable XHTML spine content was extracted from the source EPUB")
        return "\n\n".join(documents)


def extract_pdf_markdown(path: Path) -> str:
    try:
        from extract_pdf_pages import extract_pdf_to_markdown
    except ImportError as exc:
        raise HarnessError(
            "prepare-pdf requires pdfplumber; install requirements.txt first"
        ) from exc
    with tempfile.TemporaryDirectory(prefix="ebook-pdf-extract-") as temp_dir:
        output = Path(temp_dir) / "extracted.md"
        try:
            extract_pdf_to_markdown(path, output)
        except Exception as exc:
            raise HarnessError(f"Layout-aware PDF extraction failed: {exc}") from exc
        text = output.read_text(encoding="utf-8")
    visible = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    if len(visible) < 100:
        raise HarnessError("The PDF has no usable text layer; OCR review is required before preparation")
    return text


def _prepare(
    args: argparse.Namespace,
    *,
    source_path: Path,
    source_type: str,
    extracted: str,
    source_outline: dict[str, Any] | None = None,
) -> int:
    workspace, book_key, book_id = _book_context(args)
    translation_policy = _translation_policy(args)
    book_dir = workspace.book(book_id)
    manifest_path = _manifest_path(workspace, book_id)
    if manifest_path.exists() or (book_dir.exists() and any(book_dir.iterdir())):
        raise HarnessError(
            f"Refusing to overwrite existing prepared work: {book_dir}. Choose a new book id or resume it."
        )
    chunks = split_markdown(extracted, args.max_chars)
    asset_dir = workspace.assets / book_id
    meta_path = asset_dir / "book_meta.json"
    meta_value = {
        "book_key": book_key,
        "book_id": book_id,
        "title": clean_text(args.title_ko),
        "author": clean_text(args.author_ko),
        "language": "ko",
        "toc_heading_levels": [2, 3, 4],
        "ai_notice": AI_NOTICE,
    }
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HarnessError(f"Cannot verify existing metadata {meta_path}: {exc}") from exc
        if existing != meta_value:
            raise HarnessError(f"Existing metadata conflicts with preparation request: {meta_path}")

    source_dir = book_dir / "source"
    draft_dir = book_dir / "draft_ko"
    polished_dir = book_dir / "polished_ko"
    source_dir.mkdir(parents=True, exist_ok=False)
    draft_dir.mkdir()
    polished_dir.mkdir()
    tasks: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, 1):
        task_id = f"segment-{index:04d}"
        source_relative = f"source/{task_id}.md"
        source_text = f"<!-- task-id: {task_id} -->\n\n{chunk.strip()}\n"
        atomic_write_text(book_dir / source_relative, source_text)
        tasks.append(
            {
                "id": task_id,
                "order": index,
                "source": source_relative,
                "draft": f"draft_ko/{task_id}.md",
                "polished": f"polished_ko/{task_id}.md",
            }
        )
    manifest = {
        "version": 1,
        "book_key": book_key,
        "book_id": book_id,
        "title": clean_text(args.title_ko),
        "author": clean_text(args.author_ko),
        "source": str(source_path.resolve()),
        "source_type": source_type,
        "translation_policy": translation_policy,
        "polish_policy": {
            "version": 1,
            "draft_comparison_required": True,
            "glossary": f"assets/{book_id}/glossary_ko.md",
        },
        "task_count": len(tasks),
        "tasks": tasks,
    }
    atomic_write_json(manifest_path, manifest)
    if source_outline is not None:
        atomic_write_json(book_dir / "source_outline.json", source_outline)

    asset_dir.mkdir(parents=True, exist_ok=True)
    if not meta_path.exists():
        atomic_write_json(meta_path, meta_value)
    glossary_path = asset_dir / "glossary_ko.md"
    if not glossary_path.exists():
        atomic_write_text(glossary_path, GLOSSARY_TEMPLATE)
    print(f"PASS prepared {book_key}: tasks={len(tasks)} work={book_dir}")
    if source_outline is not None:
        print(f"source_outline={book_dir / 'source_outline.json'}")
    print(f"TOC contract still requires human review: {asset_dir / 'toc_contract.json'}")
    print(f"Glossary/style sheet: {glossary_path}")
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    path = Path(args.epub).resolve()
    if not path.is_file():
        raise HarnessError(f"Source EPUB is missing: {path}")
    return _prepare(
        args,
        source_path=path,
        source_type="epub",
        extracted=extract_epub_markdown(path),
        source_outline=extract_outline(path),
    )


def command_prepare_md(args: argparse.Namespace) -> int:
    path = Path(args.markdown).resolve()
    if not path.is_file():
        raise HarnessError(f"Source Markdown/TXT is missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"Cannot read UTF-8 source {path}: {exc}") from exc
    leaked = [line for line in text.splitlines() if PDF_MARKER_RE.match(line.strip())]
    if leaked:
        raise HarnessError(
            "prepare-md rejects raw PDF page dumps. Use prepare-pdf for layout-aware provenance."
        )
    return _prepare(args, source_path=path, source_type="markdown", extracted=text)


def command_prepare_pdf(args: argparse.Namespace) -> int:
    path = Path(args.pdf).resolve()
    if not path.is_file():
        raise HarnessError(f"Source PDF is missing: {path}")
    return _prepare(
        args,
        source_path=path,
        source_type="pdf",
        extracted=extract_pdf_markdown(path),
    )


def command_status(args: argparse.Namespace) -> int:
    workspace, book_key, book_id = _book_context(args)
    manifest = load_bound_manifest(
        workspace, book_key=book_key, book_id=book_id
    )
    book_dir = workspace.book(book_id)
    total = len(manifest["tasks"])
    draft = sum(_translation_exists(_stage_path(book_dir, task, "draft")) for task in manifest["tasks"])
    polished = sum(
        _translation_exists(_stage_path(book_dir, task, "polished")) for task in manifest["tasks"]
    )
    config_dir = resolve_coordinator_config_dir(
        workspace, book_key=book_key, book_id=book_id
    )
    contract = config_dir / "toc_contract.json"
    print(
        f"{book_key}: draft={draft}/{total} polished={polished}/{total} "
        f"toc_contract={'present' if contract.is_file() else 'missing'} "
        f"translation_policy={_translation_policy_summary(manifest)}"
    )
    return 0


def command_next(args: argparse.Namespace) -> int:
    workspace, book_key, book_id = _book_context(args)
    manifest = load_bound_manifest(
        workspace, book_key=book_key, book_id=book_id
    )
    book_dir = workspace.book(book_id)
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        if _translation_exists(_stage_path(book_dir, task, args.stage)):
            continue
        if args.stage == "polished" and not _translation_exists(
            _stage_path(book_dir, task, "draft")
        ):
            blocked.append(task)
        else:
            ready.append(task)
    planned, mode, reason = _resolve_next_plan(
        workers=args.workers,
        limit=args.limit,
        remaining=len(ready),
    )
    selected = ready[:planned]
    for task in selected:
        source = (
            _stage_path(book_dir, task, "draft")
            if args.stage == "polished"
            else book_dir / task["source"]
        )
        print(
            f"{task['id']}: source={source} "
            f"target={_stage_path(book_dir, task, args.stage)}"
        )
    for task in blocked:
        if not isinstance(manifest.get("polish_policy"), dict):
            print(
                f"MIGRATION_REQUIRED {task['id']}: "
                f"create_draft={_stage_path(book_dir, task, 'draft')} "
                f"from_source={book_dir / task['source']} before_polished="
                f"{_stage_path(book_dir, task, 'polished')}"
            )
        else:
            print(
                f"BLOCKED {task['id']}: missing_draft={_stage_path(book_dir, task, 'draft')} "
                f"target={_stage_path(book_dir, task, 'polished')}"
            )
    print(
        f"next={len(selected)} stage={args.stage} book={book_key} "
        f"workers={planned} mode={mode} blocked_missing_draft={len(blocked)} "
        f"reason={reason}"
    )
    return 0


def _stage_qc(
    workspace: Workspace,
    book_id: str,
    stage: str,
    manifest: dict[str, Any],
) -> list[str]:
    book_dir = workspace.book(book_id)
    errors: list[str] = []
    for task in manifest["tasks"]:
        path = _stage_path(book_dir, task, stage)
        if not _translation_exists(path):
            errors.append(f"{task['id']}: missing or empty {stage} file {path}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{task['id']}: cannot read UTF-8 file: {exc}")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if PDF_MARKER_RE.match(line.strip()):
                errors.append(f"{task['id']}:{line_number}: visible PDF page marker")
            heading = HEADING_RE.fullmatch(line.strip())
            if heading and WORK_HEADING_RE.fullmatch(heading.group(2).strip()):
                errors.append(f"{task['id']}:{line_number}: worker-stage heading")
        if stage == "polished":
            draft_path = _stage_path(book_dir, task, "draft")
            if not _translation_exists(draft_path):
                polish_policy = manifest.get("polish_policy")
                requires_draft = isinstance(polish_policy, dict) and bool(
                    polish_policy.get("draft_comparison_required", False)
                )
                if requires_draft:
                    errors.append(
                        f"{task['id']}: polished QC requires a non-empty draft file {draft_path}"
                    )
                continue
            try:
                draft_text = draft_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"{task['id']}: cannot read UTF-8 draft file: {exc}")
                continue
            errors.extend(_polish_integrity_errors(task["id"], draft_text, text))
    return errors


def _stage_qc_warnings(
    workspace: Workspace,
    book_id: str,
    stage: str,
    manifest: dict[str, Any],
) -> list[str]:
    if stage != "polished" or isinstance(manifest.get("polish_policy"), dict):
        return []
    book_dir = workspace.book(book_id)
    missing_drafts = [
        task["id"]
        for task in manifest["tasks"]
        if _translation_exists(_stage_path(book_dir, task, "polished"))
        and not _translation_exists(_stage_path(book_dir, task, "draft"))
    ]
    if not missing_drafts:
        return []
    return [
        "legacy manifest has polished files without draft evidence; "
        f"automatic draft-to-polished integrity comparison was unavailable for {len(missing_drafts)} task(s)"
    ]


def _heading_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if HEADING_RE.fullmatch(line.strip())
    ]


def _blockquote_blocks(text: str) -> list[str]:
    """Return quote content while ignoring harmless Markdown line wrapping."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            content = stripped[1:].lstrip()
            if content:
                current.append(content)
            elif current:
                blocks.append(" ".join(current))
                current = []
        elif current:
            blocks.append(" ".join(current))
            current = []
    if current:
        blocks.append(" ".join(current))
    return blocks


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]
    return cells if len(cells) >= 2 else None


def _is_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _table_shape(text: str) -> list[tuple[int, int]]:
    lines = text.splitlines()
    shape: list[tuple[int, int]] = []
    index = 1
    while index < len(lines):
        header = _table_cells(lines[index - 1])
        separator = _table_cells(lines[index])
        if (
            header is None
            or separator is None
            or len(header) != len(separator)
            or not _is_table_separator(separator)
        ):
            index += 1
            continue
        columns = len(header)
        rows = 2
        cursor = index + 1
        while cursor < len(lines):
            cells = _table_cells(lines[cursor])
            if cells is None or len(cells) != columns:
                break
            rows += 1
            cursor += 1
        shape.append((rows, columns))
        index = cursor
    return shape


def _polish_integrity_errors(task_id: str, draft: str, polished: str) -> list[str]:
    """Check invariants that a sentence-level polish must not change."""
    draft_clean = re.sub(r"<!--.*?-->", "", _clean_stage_text(draft), flags=re.DOTALL)
    polished_clean = re.sub(r"<!--.*?-->", "", _clean_stage_text(polished), flags=re.DOTALL)
    errors: list[str] = []

    if _heading_lines(draft_clean) != _heading_lines(polished_clean):
        errors.append(f"{task_id}: polished headings differ from draft headings")

    protected_extractors = (
        ("number values", lambda value: NUMBER_TOKEN_RE.findall(value)),
        (
            "number/unit tokens",
            lambda value: [
                re.sub(r"\s+", "", match.group(0)).lower()
                for match in QUANTITY_TOKEN_RE.finditer(value)
            ],
        ),
        (
            "currency tokens",
            lambda value: [
                re.sub(r"\s+", "", match.group(0)).lower()
                for match in CURRENCY_TOKEN_RE.finditer(value)
            ],
        ),
        ("footnote markers", lambda value: FOOTNOTE_MARKER_RE.findall(value)),
        ("URLs", lambda value: URL_RE.findall(value)),
        ("Markdown link targets", lambda value: MARKDOWN_LINK_TARGET_RE.findall(value)),
        ("reference link targets", lambda value: REFERENCE_LINK_TARGET_RE.findall(value)),
        (
            "HTML link/anchor targets",
            lambda value: [
                f"{match.group(1).lower()}={match.group(2)}"
                for match in HTML_TARGET_RE.finditer(value)
            ],
        ),
        (
            "inline quotations",
            lambda value: INLINE_QUOTE_RE.findall(
                re.sub(r"<[^>\n]*>", "", value)
            ),
        ),
    )
    for label, extractor in protected_extractors:
        draft_tokens = extractor(draft_clean)
        polished_tokens = extractor(polished_clean)
        if draft_tokens != polished_tokens:
            errors.append(f"{task_id}: polished {label} differ from draft")

    if _blockquote_blocks(draft_clean) != _blockquote_blocks(polished_clean):
        errors.append(f"{task_id}: polished block quotations differ from draft")
    if _table_shape(draft_clean) != _table_shape(polished_clean):
        errors.append(f"{task_id}: polished table row/column shape differs from draft")

    if draft_clean:
        change_rate = 1.0 - SequenceMatcher(
            None, draft_clean, polished_clean, autojunk=False
        ).ratio()
        if change_rate > 0.50:
            errors.append(
                f"{task_id}: polished change rate {change_rate:.1%} exceeds the 50% rollback gate"
            )
    return errors


def command_qc(args: argparse.Namespace) -> int:
    workspace, book_key, book_id = _book_context(args)
    manifest = load_bound_manifest(
        workspace, book_key=book_key, book_id=book_id
    )
    errors = _stage_qc(workspace, book_id, args.stage, manifest)
    warnings = _stage_qc_warnings(workspace, book_id, args.stage, manifest)
    if errors:
        print(f"BLOCKED {book_key} {args.stage}: errors={len(errors)}")
        for error in errors:
            print(f"  - {error}")
        return 2
    for warning in warnings:
        print(f"WARNING {book_key} {args.stage}: {warning}")
    print(f"PASS {book_key} {args.stage}")
    return 0


def _clean_stage_text(text: str) -> str:
    text = re.sub(
        r"<!--\s*(?:task-id|source[-_ ]?page|pdf[-_ ]?page)\s*:.*?-->",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    output: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()
        notice_text = re.sub(r"^[>\s]+", "", stripped)
        if notice_text == AI_NOTICE:
            continue
        heading = HEADING_RE.fullmatch(stripped)
        if heading and WORK_HEADING_RE.fullmatch(heading.group(2).strip()):
            continue
        output.append(line)
    return "\n".join(output).strip()


def _build_with_configuration(
    *,
    workspace: Workspace,
    book_key: str,
    meta: BookMeta,
    contract: list[dict[str, Any]],
) -> Path:
    """Atomically build using configuration already bound to an explicit id."""
    import build_epubs_from_combined as builder
    from validate_epub_toc_contract import validate_epub

    markdown_path = workspace.combined / f"{book_key}.md"
    text = markdown_path.read_text(encoding="utf-8")
    text = builder.strip_markdown_html_comments(text)
    parsed = parse_combined_markdown(text, meta, contract)
    images, image_map = builder.collect_images(text, markdown_path)
    workspace.epub.mkdir(parents=True, exist_ok=True)
    final_path = workspace.epub / f"{book_key}.epub"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{book_key}.", suffix=".tmp.epub", dir=workspace.epub
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        builder.write_epub_archive(
            temporary_path, parsed, meta, images, image_map
        )
        result = validate_epub(temporary_path, meta, contract)
        if result.errors:
            detail = "\n  - ".join(result.errors)
            raise HarnessError(f"Temporary EPUB failed validation:\n  - {detail}")
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return final_path


def command_combine(args: argparse.Namespace) -> int:
    workspace, book_key, book_id = _book_context(args)
    manifest = load_bound_manifest(
        workspace, book_key=book_key, book_id=book_id
    )
    errors = _stage_qc(workspace, book_id, args.stage, manifest)
    if errors:
        raise HarnessError("Cannot combine until stage QC passes:\n  - " + "\n  - ".join(errors))
    for warning in _stage_qc_warnings(workspace, book_id, args.stage, manifest):
        print(f"WARNING {book_key} {args.stage}: {warning}")
    book_dir = workspace.book(book_id)
    bodies: list[str] = []
    for task in manifest["tasks"]:
        path = _stage_path(book_dir, task, args.stage)
        body = _clean_stage_text(path.read_text(encoding="utf-8"))
        if re.search(r"^#[ \t]+", body, re.MULTILINE):
            raise HarnessError(
                f"{task['id']} contains an H1. The combined book reserves H1 for the book title; "
                "normalize chapter headings to H2 or below."
            )
        bodies.append(body)
    meta, contract, _config_dir = load_coordinator_configuration(
        workspace, book_key=book_key, book_id=book_id
    )
    combined_text = (
        f"# {meta.title}\n\n> {AI_NOTICE}\n\n" + "\n\n".join(bodies).strip() + "\n"
    )
    if combined_text.count(AI_NOTICE) != 1:
        raise HarnessError("Combined Markdown must contain the AI notice exactly once")
    parse_combined_markdown(combined_text, meta, contract)
    destination = workspace.combined / f"{book_key}.md"
    atomic_write_text(destination, combined_text)
    print(f"PASS combined {book_key}: {destination}")
    if args.build or args.output_format == "epub":
        output = _build_with_configuration(
            workspace=workspace,
            book_key=book_key,
            meta=meta,
            contract=contract,
        )
        print(f"PASS built {book_key}: {output}")
    return 0


def command_structure_qc(args: argparse.Namespace) -> int:
    workspace, book_key, book_id = _book_context(args)
    load_bound_manifest(workspace, book_key=book_key, book_id=book_id)
    markdown_path = workspace.combined / f"{book_key}.md"
    _, _, config_dir = load_coordinator_configuration(
        workspace, book_key=book_key, book_id=book_id
    )
    conventional_names = {book_key}
    if book_key.endswith("_ko") and len(book_key) > 3:
        conventional_names.add(book_key[:-3])
    if config_dir.name in conventional_names:
        report = audit(
            book_key=book_key,
            markdown_path=markdown_path,
            config_dir=workspace.assets,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="ebook-config-view-") as temp_dir:
            adapter = Path(temp_dir) / book_key
            adapter.mkdir()
            shutil.copyfile(config_dir / "book_meta.json", adapter / "book_meta.json")
            shutil.copyfile(
                config_dir / "toc_contract.json", adapter / "toc_contract.json"
            )
            report = audit(
                book_key=book_key,
                markdown_path=markdown_path,
                config_dir=Path(temp_dir),
            )
    workspace.reports.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", args.label or book_key).strip("_") or book_key
    json_path = workspace.reports / f"{label}_structure_qc.json"
    markdown_report = workspace.reports / f"{label}_structure_qc.md"
    atomic_write_json(json_path, report.to_dict())
    atomic_write_text(markdown_report, render_markdown(report))
    print(f"{report.status} {book_key}: report={markdown_report}")
    return 2 if report.status == "BLOCKED" else 0


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--book-key", required=True)
    parser.add_argument("--book-id")


def _add_prepare_common(parser: argparse.ArgumentParser) -> None:
    _add_identity(parser)
    parser.add_argument("--title-ko", required=True)
    parser.add_argument("--author-ko", required=True)
    parser.add_argument("--max-chars", type=_positive_int, default=8000)
    parser.add_argument(
        "--translation-platform",
        choices=tuple(TRANSLATION_MINIMUM_MODELS),
        default="gpt",
        help="External subscription session that will perform translation.",
    )
    parser.add_argument(
        "--translation-model",
        help="Requested model; defaults to the platform minimum recorded in the manifest.",
    )
    parser.add_argument(
        "--translation-effort",
        choices=("high", "xhigh", "max"),
        default="high",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Prepare a source EPUB.")
    _add_prepare_common(prepare)
    prepare.add_argument("--epub", required=True)
    prepare.set_defaults(handler=command_prepare)

    prepare_md = subparsers.add_parser("prepare-md", help="Prepare UTF-8 Markdown or TXT.")
    _add_prepare_common(prepare_md)
    prepare_md.add_argument("--markdown", required=True)
    prepare_md.set_defaults(handler=command_prepare_md)

    prepare_pdf = subparsers.add_parser("prepare-pdf", help="Prepare a text-layer PDF.")
    _add_prepare_common(prepare_pdf)
    prepare_pdf.add_argument("--pdf", required=True)
    prepare_pdf.set_defaults(handler=command_prepare_pdf)

    status = subparsers.add_parser("status")
    _add_identity(status)
    status.set_defaults(handler=command_status)

    next_parser = subparsers.add_parser("next")
    _add_identity(next_parser)
    next_parser.add_argument("--stage", choices=("draft", "polished"), default="polished")
    next_parser.add_argument(
        "--workers",
        type=_workers_value,
        default="auto",
        metavar="auto|N",
        help="Plan distinct next targets automatically or for N workers; does not start models.",
    )
    next_parser.add_argument(
        "--limit",
        type=_positive_int,
        help="Legacy explicit target count; when supplied, overrides --workers.",
    )
    next_parser.set_defaults(handler=command_next)

    qc = subparsers.add_parser("qc")
    _add_identity(qc)
    qc.add_argument("--stage", choices=("draft", "polished"), default="polished")
    qc.set_defaults(handler=command_qc)

    combine = subparsers.add_parser("combine")
    _add_identity(combine)
    combine.add_argument("--stage", choices=("draft", "polished"), default="polished")
    output = combine.add_mutually_exclusive_group()
    output.add_argument(
        "--build",
        action="store_true",
        help="Compatibility alias for --output-format epub.",
    )
    output.add_argument(
        "--output-format",
        choices=("md", "epub"),
        help="Final deliverable format; EPUB builds retain combined Markdown as validation evidence.",
    )
    combine.set_defaults(handler=command_combine)

    structure = subparsers.add_parser("structure-qc")
    _add_identity(structure)
    structure.add_argument("--label")
    structure.set_defaults(handler=command_structure_qc)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except HarnessError as exc:
        print(f"BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
