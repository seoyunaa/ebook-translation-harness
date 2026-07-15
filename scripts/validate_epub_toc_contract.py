"""Validate EPUB structure, links, metadata, and an exact nested TOC contract."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from epub_core import (
    AI_NOTICE,
    CONTAINER_NS,
    DC_NS,
    EPUB_NS,
    OPF_NS,
    BookMeta,
    HarnessError,
    TocNode,
    clean_text,
    element_text,
    flatten_contract,
    load_book_meta,
    load_toc_contract,
    local_name,
    project_path,
    safe_zip_target,
    toc_signature,
)


XML_MEDIA_TYPES = {
    "application/xhtml+xml",
    "application/x-dtbncx+xml",
    "application/oebps-package+xml",
    "image/svg+xml",
}
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "urn"}
DEFAULT_CONFIG_DIR = "03_outputs/translations/assets"
DEFAULT_OUTPUT_DIR = "03_outputs/translations/epub"
DEFAULT_COMBINED_DIR = "03_outputs/translations/combined"


@dataclass
class ValidationResult:
    epub: str
    combined: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    toc_items: int = 0
    toc_depth: int = 0
    xhtml_notice_count: int = 0
    nav_notice_count: int = 0
    combined_notice_count: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "PASS" if not self.errors else "BLOCKED"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status
        return value


def _direct_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if local_name(child.tag) == name.casefold()]


def _first_direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if local_name(child.tag) == name.casefold()), None)


def _parse_nav(root: ET.Element) -> list[TocNode]:
    navs = [
        element
        for element in root.iter()
        if local_name(element.tag) == "nav"
        and "toc" in element.attrib.get(f"{{{EPUB_NS}}}type", "").split()
    ]
    if len(navs) != 1:
        raise HarnessError(f"Expected exactly one nav epub:type='toc'; found {len(navs)}")
    root_list = _first_direct(navs[0], "ol")
    if root_list is None:
        raise HarnessError("The EPUB navigation document has no root <ol>")

    def parse_list(ordered_list: ET.Element) -> list[TocNode]:
        nodes: list[TocNode] = []
        sibling_labels: set[str] = set()
        for item in _direct_children(ordered_list, "li"):
            link = _first_direct(item, "a")
            if link is None:
                raise HarnessError("Every navigation <li> must contain a direct <a>")
            label = element_text(link)
            target = link.attrib.get("href", "").strip()
            if not label or not target:
                raise HarnessError("Navigation links require non-empty labels and href values")
            if label in sibling_labels:
                raise HarnessError(f"Duplicate same-parent navigation label: {label!r}")
            sibling_labels.add(label)
            child_list = _first_direct(item, "ol")
            nodes.append(
                TocNode(
                    label=label,
                    target=target,
                    children=parse_list(child_list) if child_list is not None else [],
                )
            )
        return nodes

    return parse_list(root_list)


def _parse_ncx(root: ET.Element) -> list[TocNode]:
    nav_maps = [item for item in root.iter() if local_name(item.tag) == "navmap"]
    if len(nav_maps) != 1:
        raise HarnessError(f"Expected exactly one NCX navMap; found {len(nav_maps)}")

    def parse_points(parent: ET.Element) -> list[TocNode]:
        nodes: list[TocNode] = []
        sibling_labels: set[str] = set()
        for point in _direct_children(parent, "navpoint"):
            label_element = next(
                (item for item in point.iter() if local_name(item.tag) == "text"), None
            )
            content = _first_direct(point, "content")
            label = element_text(label_element) if label_element is not None else ""
            target = content.attrib.get("src", "").strip() if content is not None else ""
            if not label or not target:
                raise HarnessError("Each NCX navPoint requires a label and content src")
            if label in sibling_labels:
                raise HarnessError(f"Duplicate same-parent NCX label: {label!r}")
            sibling_labels.add(label)
            nodes.append(TocNode(label, target, parse_points(point)))
        return nodes

    return parse_points(nav_maps[0])


def _flatten_nodes(nodes: Iterable[TocNode], depth: int = 1) -> list[tuple[int, TocNode]]:
    result: list[tuple[int, TocNode]] = []
    for node in nodes:
        result.append((depth, node))
        result.extend(_flatten_nodes(node.children, depth + 1))
    return result


def _normalized_navigation(
    nodes: Iterable[TocNode], source_member: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        target, fragment = safe_zip_target(source_member, node.target)
        normalized_target = target + (f"#{fragment}" if fragment else "")
        result.append(
            {
                "label": node.label,
                "target": normalized_target,
                "children": _normalized_navigation(node.children, source_member),
            }
        )
    return result


def _has_id(root: ET.Element, fragment: str) -> bool:
    return any(element.attrib.get("id") == fragment for element in root.iter())


def _is_external(href: str) -> bool:
    split = urlsplit(href)
    return split.scheme.casefold() in EXTERNAL_SCHEMES or bool(split.netloc)


def _validate_link(
    *,
    source_member: str,
    href: str,
    members: set[str],
    xml_roots: dict[str, ET.Element],
    errors: list[str],
) -> None:
    if not href:
        errors.append(f"Empty link in {source_member}")
        return
    if _is_external(href):
        return
    try:
        target, fragment = safe_zip_target(source_member, href)
    except HarnessError as exc:
        errors.append(f"{source_member}: {exc}")
        return
    if not urlsplit(href).path:
        target = source_member
    if target not in members:
        errors.append(f"Broken internal link in {source_member}: {href!r} -> {target!r}")
        return
    if fragment:
        root = xml_roots.get(target)
        if root is None:
            errors.append(
                f"Fragment link in {source_member} targets non-XML resource: {href!r}"
            )
        elif not _has_id(root, fragment):
            errors.append(f"Missing fragment target in {source_member}: {href!r}")


def _metadata_values(opf_root: ET.Element, local: str) -> list[str]:
    tag = f"{{{DC_NS}}}{local}"
    return [clean_text("".join(item.itertext())) for item in opf_root.iter(tag)]


def _unique_metadata(
    opf_root: ET.Element, local: str, errors: list[str]
) -> str:
    values = [value for value in _metadata_values(opf_root, local) if value]
    if len(values) != 1:
        errors.append(f"OPF metadata requires exactly one non-empty dc:{local}; found {len(values)}")
        return values[0] if values else ""
    return values[0]


def _validate_navigation_spine(
    nodes: Iterable[TocNode],
    *,
    nav_member: str,
    spine_targets: list[str],
    errors: list[str],
) -> None:
    """Require navigation documents to form an ordered subsequence of the spine."""
    spine_positions: dict[str, int] = {}
    for position, target in enumerate(spine_targets):
        spine_positions.setdefault(target, position)

    ordered_targets: list[tuple[int, str, str]] = []
    for _, node in _flatten_nodes(nodes):
        try:
            target, _ = safe_zip_target(nav_member, node.target)
        except HarnessError as exc:
            errors.append(
                f"Navigation target must be an internal OPF spine document: "
                f"{node.target!r} ({exc})"
            )
            continue
        position = spine_positions.get(target)
        if position is None:
            errors.append(
                f"Navigation target document is not in the OPF spine: "
                f"{node.label!r} -> {target!r}"
            )
            continue
        ordered_targets.append((position, node.label, target))

    for previous, current in zip(ordered_targets, ordered_targets[1:]):
        if current[0] < previous[0]:
            errors.append(
                "Navigation document order does not match OPF spine order: "
                f"{previous[1]!r} -> {previous[2]!r} appears before "
                f"{current[1]!r} -> {current[2]!r}"
            )
            break


def _validate_combined_notice(result: ValidationResult, combined_path: Path) -> None:
    """Validate the source Markdown notice when the CLI is using a harness layout."""
    result.combined = str(combined_path)
    if not combined_path.is_file():
        result.errors.append(f"Combined Markdown is missing: {combined_path}")
        return
    try:
        text = combined_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        result.errors.append(f"Cannot read combined Markdown {combined_path}: {exc}")
        return
    result.combined_notice_count = text.count(AI_NOTICE)
    if result.combined_notice_count != 1:
        result.errors.append(
            "Combined Markdown must contain the exact AI notice exactly once; "
            f"found {result.combined_notice_count}"
        )


def validate_epub(
    path: Path,
    meta: BookMeta,
    contract: list[dict[str, Any]],
) -> ValidationResult:
    result = ValidationResult(epub=str(path))
    if not path.is_file():
        result.errors.append(f"EPUB is missing: {path}")
        return result

    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        result.errors.append(f"Cannot open EPUB ZIP archive: {exc}")
        return result

    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        members = set(names)
        if not infos or infos[0].filename != "mimetype":
            result.errors.append("The first ZIP member must be 'mimetype'")
        else:
            if infos[0].compress_type != zipfile.ZIP_STORED:
                result.errors.append("The mimetype member must be stored without compression")
            try:
                mimetype = archive.read("mimetype")
            except KeyError:
                mimetype = b""
            if mimetype != b"application/epub+zip":
                result.errors.append("The mimetype member has incorrect content")
        if len(names) != len(set(names)):
            result.errors.append("The EPUB ZIP contains duplicate member names")
        if len(names) != len({name.casefold() for name in names}):
            result.errors.append("The EPUB ZIP contains case-insensitive duplicate names")
        for name in names:
            normalized = posixpath.normpath(name)
            if name.startswith("/") or normalized == ".." or normalized.startswith("../"):
                result.errors.append(f"Unsafe ZIP member path: {name!r}")
        bad_member = archive.testzip()
        if bad_member is not None:
            result.errors.append(f"ZIP CRC check failed for {bad_member!r}")
        if "META-INF/container.xml" not in members:
            result.errors.append("META-INF/container.xml is missing")
            return result

        xml_roots: dict[str, ET.Element] = {}
        for name in names:
            if Path(name).suffix.casefold() not in {
                ".xml",
                ".xhtml",
                ".html",
                ".opf",
                ".ncx",
                ".svg",
            }:
                continue
            try:
                xml_roots[name] = ET.fromstring(archive.read(name))
            except (ET.ParseError, KeyError, UnicodeError) as exc:
                result.errors.append(f"Malformed XML in {name}: {exc}")

        container = xml_roots.get("META-INF/container.xml")
        if container is None:
            return result
        rootfiles = [item for item in container.iter() if local_name(item.tag) == "rootfile"]
        if len(rootfiles) != 1:
            result.errors.append(f"container.xml must declare exactly one rootfile; found {len(rootfiles)}")
            return result
        opf_member = rootfiles[0].attrib.get("full-path", "")
        if not opf_member or opf_member not in members:
            result.errors.append(f"container.xml points to missing OPF: {opf_member!r}")
            return result
        opf_root = xml_roots.get(opf_member)
        if opf_root is None:
            result.errors.append(f"The OPF package is not valid XML: {opf_member}")
            return result

        title = _unique_metadata(opf_root, "title", result.errors)
        author = _unique_metadata(opf_root, "creator", result.errors)
        language = _unique_metadata(opf_root, "language", result.errors)
        identifier = _unique_metadata(opf_root, "identifier", result.errors)
        result.metadata = {
            "title": title,
            "author": author,
            "language": language,
            "identifier": identifier,
        }
        expected_metadata = {
            "title": meta.title,
            "author": meta.author,
            "language": meta.language,
            "identifier": meta.identifier,
        }
        for key, expected in expected_metadata.items():
            if result.metadata.get(key) != expected:
                result.errors.append(
                    f"Metadata {key} mismatch: {result.metadata.get(key)!r} != {expected!r}"
                )
        unique_id = opf_root.attrib.get("unique-identifier", "")
        identifier_nodes = [
            item
            for item in opf_root.iter(f"{{{DC_NS}}}identifier")
            if item.attrib.get("id") == unique_id
        ]
        if not unique_id or len(identifier_nodes) != 1:
            result.errors.append("OPF unique-identifier must reference exactly one dc:identifier id")

        modified = [
            clean_text("".join(item.itertext()))
            for item in opf_root.iter()
            if local_name(item.tag) == "meta" and item.attrib.get("property") == "dcterms:modified"
        ]
        if len(modified) != 1 or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", modified[0] if modified else ""
        ):
            result.errors.append("OPF requires one valid dcterms:modified UTC timestamp")

        manifests = [item for item in opf_root.iter() if local_name(item.tag) == "manifest"]
        spines = [item for item in opf_root.iter() if local_name(item.tag) == "spine"]
        if len(manifests) != 1 or len(spines) != 1:
            result.errors.append("OPF requires exactly one manifest and one spine")
            return result
        manifest_items = _direct_children(manifests[0], "item")
        manifest_by_id: dict[str, dict[str, str]] = {}
        manifest_target_by_id: dict[str, str] = {}
        manifest_targets: set[str] = set()
        opf_base = posixpath.dirname(opf_member)
        nav_items: list[tuple[str, dict[str, str]]] = []
        ncx_items: list[tuple[str, dict[str, str]]] = []
        for item in manifest_items:
            item_id = item.attrib.get("id", "")
            href = item.attrib.get("href", "")
            media_type = item.attrib.get("media-type", "")
            if not item_id or not href or not media_type:
                result.errors.append("Every OPF manifest item needs id, href, and media-type")
                continue
            if item_id in manifest_by_id:
                result.errors.append(f"Duplicate manifest id: {item_id!r}")
                continue
            try:
                target, fragment = safe_zip_target(opf_member, href)
            except HarnessError as exc:
                result.errors.append(f"Manifest item {item_id!r}: {exc}")
                continue
            if fragment:
                result.errors.append(f"Manifest href must not contain a fragment: {href!r}")
            if target in manifest_targets:
                result.errors.append(f"Duplicate manifest target: {target!r}")
            manifest_targets.add(target)
            if target not in members:
                result.errors.append(f"Manifest target is missing from ZIP: {target!r}")
            manifest_by_id[item_id] = dict(item.attrib)
            manifest_target_by_id[item_id] = target
            properties = item.attrib.get("properties", "").split()
            if "nav" in properties:
                nav_items.append((target, dict(item.attrib)))
            if media_type == "application/x-dtbncx+xml":
                ncx_items.append((target, dict(item.attrib)))

        if len(nav_items) != 1:
            result.errors.append(f"Manifest requires exactly one navigation item; found {len(nav_items)}")
        if len(ncx_items) != 1:
            result.errors.append(f"Manifest requires exactly one NCX item; found {len(ncx_items)}")

        spine = spines[0]
        spine_refs = _direct_children(spine, "itemref")
        spine_targets: list[str] = []
        if not spine_refs:
            result.errors.append("OPF spine must contain at least one itemref")
        for itemref in spine_refs:
            idref = itemref.attrib.get("idref", "")
            manifest_item = manifest_by_id.get(idref)
            if manifest_item is None:
                result.errors.append(f"Spine idref has no manifest item: {idref!r}")
            elif manifest_item.get("media-type") != "application/xhtml+xml":
                result.errors.append(f"Spine item {idref!r} is not XHTML")
            else:
                target = manifest_target_by_id.get(idref)
                if target is not None:
                    spine_targets.append(target)
        spine_toc = spine.attrib.get("toc", "")
        if len(ncx_items) == 1:
            ncx_id = next(
                (
                    item_id
                    for item_id, item in manifest_by_id.items()
                    if item.get("media-type") == "application/x-dtbncx+xml"
                ),
                "",
            )
            if spine_toc != ncx_id:
                result.errors.append(f"Spine toc {spine_toc!r} does not reference NCX id {ncx_id!r}")

        nav_nodes: list[TocNode] = []
        ncx_nodes: list[TocNode] = []
        nav_member = nav_items[0][0] if len(nav_items) == 1 else ""
        ncx_member = ncx_items[0][0] if len(ncx_items) == 1 else ""

        xhtml_members = {
            name
            for name in names
            if Path(name).suffix.casefold() in {".xhtml", ".html"}
        }
        xhtml_members.update(
            manifest_target_by_id[item_id]
            for item_id, item in manifest_by_id.items()
            if item.get("media-type") == "application/xhtml+xml"
            and item_id in manifest_target_by_id
        )
        for member in sorted(xhtml_members):
            if member not in members or member in xml_roots:
                continue
            try:
                xml_roots[member] = ET.fromstring(archive.read(member))
            except (ET.ParseError, KeyError, UnicodeError) as exc:
                result.errors.append(f"Malformed XHTML in {member}: {exc}")
        result.xhtml_notice_count = sum(
            element_text(xml_roots[member]).count(AI_NOTICE)
            for member in xhtml_members
            if member in xml_roots
        )
        if result.xhtml_notice_count != 1:
            result.errors.append(
                "EPUB XHTML must contain the exact AI notice exactly once; "
                f"found {result.xhtml_notice_count}"
            )
        if nav_member and nav_member in xml_roots:
            result.nav_notice_count = element_text(xml_roots[nav_member]).count(AI_NOTICE)
            if result.nav_notice_count != 0:
                result.errors.append(
                    "The navigation document must not contain the AI notice; "
                    f"found {result.nav_notice_count}"
                )

        if nav_member:
            nav_root = xml_roots.get(nav_member)
            if nav_root is None:
                result.errors.append(f"Navigation document is not valid XML: {nav_member}")
            else:
                try:
                    nav_nodes = _parse_nav(nav_root)
                except HarnessError as exc:
                    result.errors.append(str(exc))
        if ncx_member:
            ncx_root = xml_roots.get(ncx_member)
            if ncx_root is None:
                result.errors.append(f"NCX document is not valid XML: {ncx_member}")
            else:
                try:
                    ncx_nodes = _parse_ncx(ncx_root)
                except HarnessError as exc:
                    result.errors.append(str(exc))

        if nav_nodes:
            actual_contract = toc_signature(nav_nodes)
            if actual_contract != contract:
                result.errors.append("nav.xhtml tree does not exactly match toc_contract.json")
            flattened = _flatten_nodes(nav_nodes)
            result.toc_items = len(flattened)
            result.toc_depth = max((depth for depth, _ in flattened), default=0)
            for _, node in flattened:
                for pattern in meta.forbidden_patterns:
                    if re.search(pattern, node.label):
                        result.errors.append(
                            f"Forbidden reader-facing TOC label {node.label!r} matches {pattern!r}"
                        )
            for _, node in flattened:
                _validate_link(
                    source_member=nav_member,
                    href=node.target,
                    members=members,
                    xml_roots=xml_roots,
                    errors=result.errors,
                )
            _validate_navigation_spine(
                nav_nodes,
                nav_member=nav_member,
                spine_targets=spine_targets,
                errors=result.errors,
            )
        if ncx_nodes:
            for _, node in _flatten_nodes(ncx_nodes):
                _validate_link(
                    source_member=ncx_member,
                    href=node.target,
                    members=members,
                    xml_roots=xml_roots,
                    errors=result.errors,
                )
        if nav_nodes and ncx_nodes:
            try:
                nav_normalized = _normalized_navigation(nav_nodes, nav_member)
                ncx_normalized = _normalized_navigation(ncx_nodes, ncx_member)
                if nav_normalized != ncx_normalized:
                    result.errors.append("nav.xhtml and toc.ncx labels/order/targets do not agree")
            except HarnessError as exc:
                result.errors.append(str(exc))

        for member, root in xml_roots.items():
            if Path(member).suffix.casefold() not in {".xhtml", ".html", ".svg"}:
                continue
            for element in root.iter():
                for attribute in ("href", "src"):
                    href = element.attrib.get(attribute)
                    if href is not None:
                        _validate_link(
                            source_member=member,
                            href=href,
                            members=members,
                            xml_roots=xml_roots,
                            errors=result.errors,
                        )

    result.errors = list(dict.fromkeys(result.errors))
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--combined-dir",
        help=(
            "Optional combined Markdown directory. The default harness layout is "
            "checked automatically; custom config/output layouts remain standalone "
            "unless this option is supplied."
        ),
    )
    keys = parser.add_mutually_exclusive_group(required=True)
    keys.add_argument("--only", action="append", metavar="BOOK_KEY")
    keys.add_argument("--book-key", action="append", metavar="BOOK_KEY")
    parser.add_argument(
        "--label",
        help="Compatibility label; when supplied, writes reports/<label>_toc_contract_validation.json.",
    )
    parser.add_argument(
        "--json-report",
        help="Optional JSON report path, resolved relative to the project root.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve()
    config_dir = project_path(project_root, args.config_dir)
    output_dir = project_path(project_root, args.output_dir)
    use_default_harness_layout = (
        args.config_dir == DEFAULT_CONFIG_DIR and args.output_dir == DEFAULT_OUTPUT_DIR
    )
    combined_dir = (
        project_path(project_root, args.combined_dir or DEFAULT_COMBINED_DIR)
        if args.combined_dir is not None or use_default_harness_layout
        else None
    )
    results: list[ValidationResult] = []
    seen: set[str] = set()
    requested_keys = args.only or args.book_key or []
    for book_key in requested_keys:
        try:
            if book_key in seen:
                raise HarnessError(f"Duplicate --only value: {book_key}")
            seen.add(book_key)
            meta = load_book_meta(config_dir, book_key)
            contract = load_toc_contract(config_dir, book_key)
            result = validate_epub(output_dir / f"{book_key}.epub", meta, contract)
            if combined_dir is not None:
                _validate_combined_notice(
                    result,
                    combined_dir / f"{book_key}.md",
                )
        except HarnessError as exc:
            result = ValidationResult(
                epub=str(output_dir / f"{book_key}.epub"), errors=[str(exc)]
            )
        results.append(result)
        print(
            f"{result.status} {book_key}: items={result.toc_items} "
            f"depth={result.toc_depth} xhtml_notice={result.xhtml_notice_count} "
            f"nav_notice={result.nav_notice_count} "
            f"combined_notice={result.combined_notice_count} "
            f"errors={len(result.errors)}"
        )
        for error in result.errors:
            print(f"  - {error}")
    report_setting = args.json_report
    if report_setting is None and args.label:
        report_setting = f"03_outputs/translations/quality_reports/{args.label}_toc_contract_validation.json"
    if report_setting:
        report_path = project_path(project_root, report_setting)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"report={report_path}")
    return 0 if all(not result.errors for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
