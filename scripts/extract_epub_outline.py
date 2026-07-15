"""Extract an EPUB's source navigation tree to an atomic UTF-8 JSON file."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from epub_core import DC_NS, EPUB_NS, HarnessError, clean_text, element_text, local_name, safe_zip_target


def _direct(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if local_name(child.tag) == name.casefold()]


def _first_direct(element: ET.Element, names: set[str]) -> ET.Element | None:
    return next((child for child in element if local_name(child.tag) in names), None)


def _outline_from_nav(root: ET.Element) -> list[dict[str, Any]]:
    navs = [
        item
        for item in root.iter()
        if local_name(item.tag) == "nav"
        and "toc" in item.attrib.get(f"{{{EPUB_NS}}}type", "").split()
    ]
    if not navs:
        return []
    root_list = _first_direct(navs[0], {"ol", "ul"})
    if root_list is None:
        return []

    def parse_list(container: ET.Element) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for list_item in _direct(container, "li"):
            label_node = _first_direct(list_item, {"a", "span"})
            if label_node is None:
                continue
            label = element_text(label_node)
            if not label:
                continue
            href = label_node.attrib.get("href", "").strip()
            child_list = _first_direct(list_item, {"ol", "ul"})
            items.append(
                {
                    "label": label,
                    "href": href,
                    "children": parse_list(child_list) if child_list is not None else [],
                }
            )
        return items

    return parse_list(root_list)


def _outline_from_ncx(root: ET.Element) -> list[dict[str, Any]]:
    nav_map = next((item for item in root.iter() if local_name(item.tag) == "navmap"), None)
    if nav_map is None:
        return []

    def parse_points(parent: ET.Element) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for point in _direct(parent, "navpoint"):
            label_node = next(
                (item for item in point.iter() if local_name(item.tag) == "text"), None
            )
            content = _first_direct(point, {"content"})
            label = element_text(label_node) if label_node is not None else ""
            if not label:
                continue
            items.append(
                {
                    "label": label,
                    "href": content.attrib.get("src", "").strip() if content is not None else "",
                    "children": parse_points(point),
                }
            )
        return items

    return parse_points(nav_map)


def outline_stats(items: list[dict[str, Any]]) -> tuple[int, int]:
    if not items:
        return 0, 0
    count = 0
    depth = 0

    def visit(nodes: list[dict[str, Any]], level: int) -> None:
        nonlocal count, depth
        for item in nodes:
            count += 1
            depth = max(depth, level)
            visit(item.get("children", []), level + 1)

    visit(items, 1)
    return count, depth


def _metadata(root: ET.Element, name: str) -> str:
    node = next(iter(root.iter(f"{{{DC_NS}}}{name}")), None)
    return clean_text("".join(node.itertext())) if node is not None else ""


def extract_outline(epub_path: Path) -> dict[str, Any]:
    if not epub_path.is_file():
        raise HarnessError(f"Source EPUB is missing: {epub_path}")
    try:
        archive = zipfile.ZipFile(epub_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HarnessError(f"Cannot open source EPUB: {exc}") from exc
    with archive:
        if archive.testzip() is not None:
            raise HarnessError("Source EPUB failed its ZIP CRC check")
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError) as exc:
            raise HarnessError(f"Cannot parse META-INF/container.xml: {exc}") from exc
        rootfiles = [item for item in container.iter() if local_name(item.tag) == "rootfile"]
        if len(rootfiles) != 1:
            raise HarnessError(f"Expected one OPF rootfile; found {len(rootfiles)}")
        opf_member = rootfiles[0].attrib.get("full-path", "")
        if not opf_member:
            raise HarnessError("container.xml rootfile has no full-path")
        try:
            opf_root = ET.fromstring(archive.read(opf_member))
        except (KeyError, ET.ParseError) as exc:
            raise HarnessError(f"Cannot parse OPF package {opf_member!r}: {exc}") from exc

        candidates: list[tuple[int, str, str]] = []
        for item in opf_root.iter():
            if local_name(item.tag) != "item" or not item.attrib.get("href"):
                continue
            try:
                member, fragment = safe_zip_target(opf_member, item.attrib["href"])
            except HarnessError:
                continue
            if fragment:
                continue
            properties = item.attrib.get("properties", "").split()
            media_type = item.attrib.get("media-type", "")
            if "nav" in properties:
                candidates.append((0, member, "nav.xhtml"))
            elif media_type == "application/x-dtbncx+xml":
                candidates.append((1, member, "toc.ncx"))

        outline: list[dict[str, Any]] = []
        source_kind = "none"
        source_member = ""
        parse_failures: list[str] = []
        for _, member, kind in sorted(candidates):
            try:
                root = ET.fromstring(archive.read(member))
                candidate_outline = (
                    _outline_from_nav(root) if kind == "nav.xhtml" else _outline_from_ncx(root)
                )
            except (KeyError, ET.ParseError) as exc:
                parse_failures.append(f"{member}: {exc}")
                continue
            if candidate_outline:
                outline = candidate_outline
                source_kind = kind
                source_member = member
                break
        if not outline:
            detail = "; ".join(parse_failures) if parse_failures else "no usable nav or NCX item"
            raise HarnessError(f"Could not extract a semantic source outline: {detail}")

        count, depth = outline_stats(outline)
        return {
            "format": "EPUB source outline",
            "source_file": epub_path.name,
            "outline_source": source_kind,
            "outline_member": source_member,
            "metadata": {
                "title": _metadata(opf_root, "title"),
                "creator": _metadata(opf_root, "creator"),
                "language": _metadata(opf_root, "language"),
                "identifier": _metadata(opf_root, "identifier"),
            },
            "item_count": count,
            "max_depth": depth,
            "items": outline,
        }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path, help="Source EPUB to inspect.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path. Defaults to SOURCE_NAME.outline.json beside the source.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    epub_path = args.epub.resolve()
    output = (
        args.output.resolve()
        if args.output is not None
        else epub_path.with_suffix(epub_path.suffix + ".outline.json")
    )
    try:
        result = extract_outline(epub_path)
        atomic_write_json(output, result)
    except HarnessError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(
        f"PASS: items={result['item_count']} depth={result['max_depth']} "
        f"source={result['outline_source']} output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
