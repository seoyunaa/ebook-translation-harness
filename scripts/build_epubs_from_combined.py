"""Build contract-checked EPUBs from combined Markdown files.

The command intentionally has no "build everything" mode.  Every invocation
must name one or more book keys with ``--only`` and every named book must have
both ``book_meta.json`` and ``toc_contract.json``.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from epub_core import (
    BookMeta,
    HarnessError,
    MarkdownDocument,
    ParsedBook,
    TocNode,
    clean_text,
    load_book_meta,
    load_toc_contract,
    parse_combined_markdown,
    project_path,
    require_book_key,
    slugify,
)


IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+['\"][^)]*['\"])?\s*\)"
)
LINK_OR_CODE_RE = re.compile(
    r"(`[^`\n]+`|!\[[^\]]*\]\(\s*(?:<[^>]+>|[^\s)]+)(?:\s+['\"][^)]*['\"])?\s*\)|\[[^\]]+\]\([^)]*\))"
)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
LIST_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+(.+)$")
TABLE_DIVIDER_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)
CSS_IMPORT_RE = re.compile(r"@import\b", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class ImageAsset:
    source: Path
    archive_name: str
    media_type: str


def strip_markdown_html_comments(text: str) -> str:
    """Remove Markdown HTML comments before structure or body rendering.

    Comments are removed outside fenced code blocks, including comments that
    span several lines. Keeping the original line endings prevents unrelated
    paragraphs from being joined. An unclosed comment blocks the build rather
    than silently hiding the remainder of a book.
    """

    output: list[str] = []
    in_comment = False
    fence: str | None = None

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line) :]

        if fence is not None:
            output.append(raw_line)
            marker = re.match(r"^[ \t]*([\x60]{3,}|~{3,})", line)
            if marker and marker.group(1)[0] == fence:
                fence = None
            continue

        cleaned: list[str] = []
        cursor = 0
        while cursor < len(line):
            if in_comment:
                end = line.find("-->", cursor)
                if end < 0:
                    cursor = len(line)
                else:
                    in_comment = False
                    cursor = end + 3
                continue

            start = line.find("<!--", cursor)
            if start < 0:
                cleaned.append(line[cursor:])
                cursor = len(line)
            else:
                cleaned.append(line[cursor:start])
                in_comment = True
                cursor = start + 4

        cleaned_line = "".join(cleaned)
        output.append(cleaned_line + ending)
        if not in_comment:
            marker = re.match(r"^[ \t]*([\x60]{3,}|~{3,})", cleaned_line)
            if marker:
                fence = marker.group(1)[0]

    if in_comment:
        raise HarnessError("Unclosed HTML comment in combined Markdown")
    return "".join(output)


def _svg_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].casefold()


def _validate_svg_css(value: str, source: Path, context: str) -> None:
    normalized = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    if CSS_IMPORT_RE.search(normalized):
        raise HarnessError(f"Unsafe SVG {source.name!r}: CSS @import in {context}")
    for match in CSS_URL_RE.finditer(normalized):
        target = match.group(1).strip().strip("\"'").strip()
        if not target.startswith("#"):
            raise HarnessError(
                f"Unsafe SVG {source.name!r}: non-local CSS url() in {context}"
            )


def validate_svg_asset(source: Path) -> None:
    """Reject active or externally loaded content in a local SVG image."""

    try:
        root = ET.fromstring(source.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise HarnessError(f"Invalid SVG {source.name!r}: {exc}") from exc

    for element in root.iter():
        tag = _svg_local_name(str(element.tag))
        if tag in {"script", "foreignobject"}:
            raise HarnessError(f"Unsafe SVG {source.name!r}: forbidden <{tag}> element")

        if tag == "style":
            _validate_svg_css("".join(element.itertext()), source, "<style>")

        for raw_name, value in element.attrib.items():
            name = _svg_local_name(str(raw_name))
            if name.startswith("on"):
                raise HarnessError(
                    f"Unsafe SVG {source.name!r}: event attribute {name!r}"
                )
            if name == "href":
                reference = re.sub(r"[\x00-\x20]+", "", value)
                if reference and not reference.startswith("#"):
                    raise HarnessError(
                        f"Unsafe SVG {source.name!r}: non-local href {value!r}"
                    )
            _validate_svg_css(value, source, f"attribute {name!r}")


def _outside_fences(text: str) -> Iterable[str]:
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            char = marker.group(1)[0]
            fence = char if fence is None else None if fence == char else fence
            continue
        if fence is None:
            yield line


def _image_target(match: re.Match[str]) -> str:
    return unquote(match.group(2) or match.group(3) or "")


def collect_images(text: str, markdown_path: Path) -> tuple[list[ImageAsset], dict[str, str]]:
    """Collect local Markdown images and map their source targets into the EPUB."""
    text = strip_markdown_html_comments(text)
    base = markdown_path.parent.resolve()
    assets: list[ImageAsset] = []
    target_map: dict[str, str] = {}
    source_map: dict[Path, str] = {}
    for line in _outside_fences(text):
        for match in IMAGE_RE.finditer(line):
            raw_target = _image_target(match)
            if raw_target in target_map:
                continue
            split = urlsplit(raw_target)
            if split.scheme or split.netloc or raw_target.startswith("/") or Path(raw_target).is_absolute():
                raise HarnessError(
                    f"Images must be local paths beneath the Markdown directory: {raw_target!r}"
                )
            source = (base / split.path).resolve()
            try:
                source.relative_to(base)
            except ValueError as exc:
                raise HarnessError(f"Image path escapes the Markdown directory: {raw_target!r}") from exc
            if not source.is_file():
                raise HarnessError(f"Referenced image does not exist: {source}")
            if source in source_map:
                archive_name = source_map[source]
            else:
                media_type, _ = mimetypes.guess_type(source.name)
                if media_type not in {
                    "image/png",
                    "image/jpeg",
                    "image/gif",
                    "image/svg+xml",
                    "image/webp",
                }:
                    raise HarnessError(
                        f"Unsupported EPUB image type for {source.name!r}: {media_type or 'unknown'}"
                    )
                if media_type == "image/svg+xml":
                    validate_svg_asset(source)
                suffix = source.suffix.casefold() or mimetypes.guess_extension(media_type) or ".bin"
                archive_name = f"images/image-{len(assets) + 1:04d}{suffix}"
                assets.append(ImageAsset(source, archive_name, media_type))
                source_map[source] = archive_name
            target_map[raw_target] = archive_name
    return assets, target_map


def _emphasis(escaped: str) -> str:
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<!_)_([^_]+?)_(?!_)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"<del>\1</del>", escaped)
    return escaped


def _split_destination(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    match = re.match(r"([^\s]+)", raw)
    return match.group(1) if match else ""


def inline_markdown(value: str, image_map: dict[str, str]) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in LINK_OR_CODE_RE.finditer(value):
        pieces.append(_emphasis(html.escape(value[cursor : match.start()])))
        token = match.group(0)
        if token.startswith("`"):
            pieces.append(f"<code>{html.escape(token[1:-1])}</code>")
        elif token.startswith("!"):
            image_match = IMAGE_RE.fullmatch(token)
            if image_match is None:
                pieces.append(html.escape(token))
            else:
                alt = image_match.group(1)
                raw_target = _image_target(image_match)
                target = image_map.get(raw_target)
                if target is None:
                    raise HarnessError(f"Image was not packaged: {raw_target!r}")
                pieces.append(
                    f'<img src="{html.escape(target, quote=True)}" '
                    f'alt="{html.escape(alt, quote=True)}" />'
                )
        else:
            link = re.fullmatch(r"\[([^\]]+)\]\((.*)\)", token)
            if link is None:
                pieces.append(html.escape(token))
            else:
                label, destination_raw = link.groups()
                destination = _split_destination(destination_raw)
                if not destination:
                    raise HarnessError(f"Empty Markdown link destination in {token!r}")
                pieces.append(
                    f'<a href="{html.escape(destination, quote=True)}">'
                    f"{_emphasis(html.escape(label))}</a>"
                )
        cursor = match.end()
    pieces.append(_emphasis(html.escape(value[cursor:])))
    return "".join(pieces)


def _table_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def markdown_to_xhtml_body(
    markdown: str,
    *,
    first_heading_id: str,
    image_map: dict[str, str],
) -> str:
    markdown = strip_markdown_html_comments(markdown)
    lines = markdown.splitlines()
    output: list[str] = []
    used_ids: set[str] = set()
    first_heading = True
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        fence = re.match(r"^[ \t]*(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker = fence.group(1)[0]
            language = clean_text(fence.group(2))
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not re.match(
                rf"^[ \t]*{re.escape(marker)}{{3,}}[ \t]*$", lines[index]
            ):
                code_lines.append(lines[index])
                index += 1
            if index >= len(lines):
                raise HarnessError("Unclosed fenced code block in combined Markdown")
            class_attr = (
                f' class="language-{html.escape(language, quote=True)}"' if language else ""
            )
            output.append(
                f"<pre><code{class_attr}>{html.escape(chr(10).join(code_lines))}</code></pre>"
            )
            index += 1
            continue

        heading = HEADING_RE.fullmatch(stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2)
            base_id = first_heading_id if first_heading else slugify(title)
            anchor = base_id
            suffix = 2
            while anchor in used_ids:
                anchor = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(anchor)
            output.append(
                f'<h{level} id="{html.escape(anchor, quote=True)}">'
                f"{inline_markdown(title, image_map)}</h{level}>"
            )
            first_heading = False
            index += 1
            continue

        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            output.append("<hr />")
            index += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote_lines.append(lines[index].lstrip()[1:].lstrip())
                index += 1
            output.append(
                "<blockquote><p>"
                + inline_markdown(" ".join(quote_lines), image_map)
                + "</p></blockquote>"
            )
            continue

        list_match = LIST_RE.match(line)
        if list_match:
            ordered = bool(re.match(r"^[ \t]*\d+[.)]", line))
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                current = LIST_RE.match(lines[index])
                if current is None:
                    break
                current_ordered = bool(re.match(r"^[ \t]*\d+[.)]", lines[index]))
                if current_ordered != ordered:
                    break
                items.append(f"<li>{inline_markdown(current.group(1), image_map)}</li>")
                index += 1
            output.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue

        if index + 1 < len(lines) and "|" in line and TABLE_DIVIDER_RE.fullmatch(lines[index + 1]):
            headers = _table_cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index]))
                index += 1
            width = len(headers)
            if width < 2 or any(len(row) != width for row in rows):
                raise HarnessError("Markdown table rows must all have the same number of columns")
            head = "".join(f"<th>{inline_markdown(cell, image_map)}</th>" for cell in headers)
            body = "".join(
                "<tr>"
                + "".join(f"<td>{inline_markdown(cell, image_map)}</td>" for cell in row)
                + "</tr>"
                for row in rows
            )
            output.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        paragraph: list[str] = [stripped]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            candidate_stripped = candidate.strip()
            if not candidate_stripped:
                break
            if (
                HEADING_RE.fullmatch(candidate_stripped)
                or re.match(r"^[ \t]*(`{3,}|~{3,})", candidate)
                or candidate_stripped.startswith(">")
                or LIST_RE.match(candidate)
                or re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", candidate_stripped)
                or (
                    index + 1 < len(lines)
                    and "|" in candidate
                    and TABLE_DIVIDER_RE.fullmatch(lines[index + 1])
                )
            ):
                break
            paragraph.append(candidate_stripped)
            index += 1
        output.append(f"<p>{inline_markdown(' '.join(paragraph), image_map)}</p>")
    return "\n".join(output)


def render_document(document: MarkdownDocument, meta: BookMeta, image_map: dict[str, str]) -> str:
    body = markdown_to_xhtml_body(
        document.markdown,
        first_heading_id=document.first_heading_id,
        image_map=image_map,
    )
    lang = html.escape(meta.language, quote=True)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(document.title)}</title>
  <link rel="stylesheet" type="text/css" href="styles.css" />
</head>
<body>
{body}
</body>
</html>
'''


def _render_nav_nodes(nodes: Iterable[TocNode], indent: int = 4) -> str:
    pad = " " * indent
    lines = [f"{pad}<ol>"]
    for node in nodes:
        lines.append(
            f'{pad}  <li><a href="{html.escape(node.target, quote=True)}">'
            f"{html.escape(node.label)}</a>"
        )
        if node.children:
            lines.append(_render_nav_nodes(node.children, indent + 4))
        lines.append(f"{pad}  </li>")
    lines.append(f"{pad}</ol>")
    return "\n".join(lines)


def render_nav(parsed: ParsedBook, meta: BookMeta) -> str:
    lang = html.escape(meta.language, quote=True)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
<head><meta charset="utf-8" /><title>Contents</title><link rel="stylesheet" type="text/css" href="styles.css" /></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
{_render_nav_nodes(parsed.toc, 4)}
  </nav>
</body>
</html>
'''


def _render_ncx_nodes(nodes: Iterable[TocNode], counter: list[int], indent: int = 4) -> str:
    lines: list[str] = []
    pad = " " * indent
    for node in nodes:
        counter[0] += 1
        number = counter[0]
        lines.extend(
            [
                f'{pad}<navPoint id="nav-{number}" playOrder="{number}">',
                f"{pad}  <navLabel><text>{html.escape(node.label)}</text></navLabel>",
                f'{pad}  <content src="{html.escape(node.target, quote=True)}" />',
            ]
        )
        lines.append(_render_ncx_nodes(node.children, counter, indent + 2))
        lines.append(f"{pad}</navPoint>")
    return "\n".join(line for line in lines if line)


def render_ncx(parsed: ParsedBook, meta: BookMeta) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{html.escape(meta.identifier, quote=True)}" /></head>
  <docTitle><text>{html.escape(meta.title)}</text></docTitle>
  <navMap>
{_render_ncx_nodes(parsed.toc, [0], 4)}
  </navMap>
</ncx>
'''


def render_opf(parsed: ParsedBook, meta: BookMeta, images: list[ImageAsset]) -> str:
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document_items = [parsed.title_document, *parsed.documents]
    manifest = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />',
        '    <item id="css" href="styles.css" media-type="text/css" />',
    ]
    spine: list[str] = []
    for index, document in enumerate(document_items):
        item_id = f"doc-{index:04d}"
        manifest.append(
            f'    <item id="{item_id}" href="{html.escape(document.filename, quote=True)}" '
            'media-type="application/xhtml+xml" />'
        )
        spine.append(f'    <itemref idref="{item_id}" />')
    for index, image in enumerate(images, 1):
        manifest.append(
            f'    <item id="image-{index:04d}" href="{html.escape(image.archive_name, quote=True)}" '
            f'media-type="{html.escape(image.media_type, quote=True)}" />'
        )
    lang = html.escape(meta.language)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="book-id" version="3.0" xml:lang="{lang}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{html.escape(meta.identifier)}</dc:identifier>
    <dc:title>{html.escape(meta.title)}</dc:title>
    <dc:creator>{html.escape(meta.author)}</dc:creator>
    <dc:language>{lang}</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine)}
  </spine>
</package>
'''


def stylesheet() -> str:
    return """body { font-family: serif; line-height: 1.65; margin: 5%; }
h1, h2, h3, h4, h5, h6 { line-height: 1.3; margin-top: 1.5em; }
p { margin: 0.8em 0; }
blockquote { border-left: 0.25em solid #999; margin-left: 0; padding-left: 1em; }
img { display: block; height: auto; margin: 1em auto; max-width: 100%; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #888; padding: 0.4em; vertical-align: top; }
pre { overflow-x: auto; white-space: pre-wrap; }
nav ol { list-style-type: none; padding-left: 1.2em; }
"""


def container_xml() -> str:
    return '''<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml" />
  </rootfiles>
</container>
'''


def write_epub_archive(
    destination: Path,
    parsed: ParsedBook,
    meta: BookMeta,
    images: list[ImageAsset],
    image_map: dict[str, str],
) -> None:
    with zipfile.ZipFile(destination, "w") as archive:
        mimetype_info = zipfile.ZipInfo("mimetype")
        mimetype_info.compress_type = zipfile.ZIP_STORED
        archive.writestr(mimetype_info, "application/epub+zip")
        archive.writestr("META-INF/container.xml", container_xml(), zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/package.opf", render_opf(parsed, meta, images), zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/nav.xhtml", render_nav(parsed, meta), zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/toc.ncx", render_ncx(parsed, meta), zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/styles.css", stylesheet(), zipfile.ZIP_DEFLATED)
        for document in [parsed.title_document, *parsed.documents]:
            archive.writestr(
                f"EPUB/{document.filename}",
                render_document(document, meta, image_map),
                zipfile.ZIP_DEFLATED,
            )
        for image in images:
            archive.write(image.source, f"EPUB/{image.archive_name}", zipfile.ZIP_DEFLATED)


def build_one(
    book_key: str,
    *,
    combined_dir: Path,
    config_dir: Path,
    output_dir: Path,
) -> Path:
    require_book_key(book_key)
    markdown_path = combined_dir / f"{book_key}.md"
    if not markdown_path.is_file():
        raise HarnessError(f"Combined Markdown is missing: {markdown_path}")
    meta = load_book_meta(config_dir, book_key)
    contract = load_toc_contract(config_dir, book_key)
    try:
        text = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"Cannot read UTF-8 Markdown from {markdown_path}: {exc}") from exc
    text = strip_markdown_html_comments(text)
    parsed = parse_combined_markdown(text, meta, contract)
    images, image_map = collect_images(text, markdown_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / f"{book_key}.epub"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{book_key}.", suffix=".tmp.epub", dir=output_dir
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        write_epub_archive(temporary_path, parsed, meta, images, image_map)
        from validate_epub_toc_contract import validate_epub

        result = validate_epub(temporary_path, meta, contract)
        if result.errors:
            detail = "\n  - ".join(result.errors)
            raise HarnessError(f"Temporary EPUB failed validation:\n  - {detail}")
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return final_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Project root used to resolve relative directories.",
    )
    parser.add_argument("--combined-dir", default="03_outputs/translations/combined")
    parser.add_argument("--config-dir", default="03_outputs/translations/assets")
    parser.add_argument("--output-dir", default="03_outputs/translations/epub")
    parser.add_argument(
        "--only",
        action="append",
        required=True,
        metavar="BOOK_KEY",
        help="Build exactly this key. Repeat the option to build several books.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve()
    combined_dir = project_path(project_root, args.combined_dir)
    config_dir = project_path(project_root, args.config_dir)
    output_dir = project_path(project_root, args.output_dir)
    failures: list[str] = []
    seen: set[str] = set()
    for book_key in args.only:
        if book_key in seen:
            failures.append(f"{book_key}: duplicate --only value")
            continue
        seen.add(book_key)
        try:
            path = build_one(
                book_key,
                combined_dir=combined_dir,
                config_dir=config_dir,
                output_dir=output_dir,
            )
            print(f"PASS {book_key}: {path}")
        except (HarnessError, OSError, zipfile.BadZipFile) as exc:
            failures.append(f"{book_key}: {exc}")
            print(f"BLOCKED {book_key}: {exc}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
