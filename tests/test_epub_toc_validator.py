from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_epubs_from_combined as builder  # noqa: E402
import epub_core  # noqa: E402
import validate_epub_toc_contract as validator  # noqa: E402


NOTICE = "이 전자책은 AI 윤문 번역본입니다."


class EpubTocValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        translations = self.root / "03_outputs" / "translations"
        self.combined = translations / "combined"
        self.config = translations / "assets"
        self.output = translations / "epub"
        self.book_key = "fictional_book_ko"
        self.meta_data = {
            "book_key": self.book_key,
            "title": "별빛 기록관",
            "author": "가상 작가",
            "language": "ko",
            "toc_heading_levels": [2, 3],
        }
        self.contract_data = {
            "version": 1,
            "items": [
                {"label": "머리말", "children": []},
                {
                    "label": "제1부",
                    "children": [{"label": "제1장: 첫 기록", "children": []}],
                },
                {"label": "맺음말", "children": []},
            ],
            "forbidden_label_patterns": [r"계속\s+\d+$"],
        }
        self.markdown = """# 별빛 기록관

> 이 전자책은 AI 윤문 번역본입니다.

## 머리말

가상의 머리말이다.

## 제1부

### 제1장: 첫 기록

가상의 첫 기록이다.

## 맺음말

가상의 맺음말이다.
"""
        self._write_project(self.config, self.combined)

    def _write_project(self, config: Path, combined: Path) -> None:
        book_dir = config / self.book_key
        book_dir.mkdir(parents=True, exist_ok=True)
        combined.mkdir(parents=True, exist_ok=True)
        (book_dir / "book_meta.json").write_text(
            json.dumps(self.meta_data, ensure_ascii=False), encoding="utf-8"
        )
        (book_dir / "toc_contract.json").write_text(
            json.dumps(self.contract_data, ensure_ascii=False), encoding="utf-8"
        )
        (combined / f"{self.book_key}.md").write_text(
            self.markdown, encoding="utf-8"
        )

    def _build(
        self,
        *,
        combined: Path | None = None,
        config: Path | None = None,
        output: Path | None = None,
    ) -> Path:
        return builder.build_one(
            self.book_key,
            combined_dir=combined or self.combined,
            config_dir=config or self.config,
            output_dir=output or self.output,
        )

    def _validate(self, epub: Path) -> validator.ValidationResult:
        meta = epub_core.load_book_meta(self.config, self.book_key)
        contract = epub_core.load_toc_contract(self.config, self.book_key)
        return validator.validate_epub(epub, meta, contract)

    def _rewrite_members(
        self,
        epub: Path,
        transforms: dict[str, Callable[[str], str]],
    ) -> None:
        rewritten = epub.with_name(f".{epub.name}.rewrite")
        changed: set[str] = set()
        with zipfile.ZipFile(epub, "r") as source, zipfile.ZipFile(rewritten, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                transform = transforms.get(info.filename)
                if transform is not None:
                    text = data.decode("utf-8")
                    updated = transform(text)
                    self.assertNotEqual(text, updated, f"mutation did not change {info.filename}")
                    data = updated.encode("utf-8")
                    changed.add(info.filename)
                target.writestr(info, data)
        self.assertEqual(set(transforms), changed)
        rewritten.replace(epub)

    def _assert_error(self, result: validator.ValidationResult, message: str) -> None:
        self.assertEqual("BLOCKED", result.status)
        self.assertTrue(
            any(message in error for error in result.errors),
            f"{message!r} not found in {result.errors!r}",
        )

    def test_valid_epub_passes_spine_and_notice_checks(self) -> None:
        result = self._validate(self._build())

        self.assertEqual([], result.errors)
        self.assertEqual("PASS", result.status)
        self.assertEqual(1, result.xhtml_notice_count)
        self.assertEqual(0, result.nav_notice_count)

    def test_nav_target_document_must_be_in_spine(self) -> None:
        epub = self._build()
        self._rewrite_members(
            epub,
            {
                "EPUB/package.opf": lambda text: text.replace(
                    '    <itemref idref="doc-0001" />\n', "", 1
                )
            },
        )

        self._assert_error(self._validate(epub), "not in the OPF spine")

    def test_nav_document_order_must_match_spine_order(self) -> None:
        epub = self._build()
        self._rewrite_members(
            epub,
            {
                "EPUB/package.opf": lambda text: text.replace(
                    '    <itemref idref="doc-0001" />\n'
                    '    <itemref idref="doc-0002" />',
                    '    <itemref idref="doc-0002" />\n'
                    '    <itemref idref="doc-0001" />',
                    1,
                )
            },
        )

        self._assert_error(
            self._validate(epub),
            "Navigation document order does not match OPF spine order",
        )

    def test_epub_xhtml_notice_must_appear_exactly_once(self) -> None:
        for case in ("missing", "duplicate"):
            with self.subTest(case=case):
                epub = self._build()
                if case == "missing":
                    transforms = {
                        "EPUB/title.xhtml": lambda text: text.replace(
                            NOTICE, "가상의 다른 안내문입니다.", 1
                        )
                    }
                    expected_count = 0
                else:
                    transforms = {
                        "EPUB/section-0001.xhtml": lambda text: text.replace(
                            "</body>", f"<p>{NOTICE}</p></body>", 1
                        )
                    }
                    expected_count = 2
                self._rewrite_members(epub, transforms)

                result = self._validate(epub)

                self.assertEqual(expected_count, result.xhtml_notice_count)
                self._assert_error(result, "exact AI notice exactly once")

    def test_navigation_document_must_not_contain_notice(self) -> None:
        epub = self._build()
        self._rewrite_members(
            epub,
            {
                "EPUB/nav.xhtml": lambda text: text.replace(
                    "</body>", f"<p>{NOTICE}</p></body>", 1
                )
            },
        )

        result = self._validate(epub)

        self.assertEqual(1, result.nav_notice_count)
        self._assert_error(result, "navigation document must not contain the AI notice")

    def _run_cli(self, *arguments: str) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = validator.main(["--project-root", str(self.root), *arguments])
        return status, stream.getvalue()

    def test_default_cli_layout_requires_combined_markdown_with_one_notice(self) -> None:
        self._build()
        status, _ = self._run_cli("--only", self.book_key)
        self.assertEqual(0, status)

        combined_path = self.combined / f"{self.book_key}.md"
        combined_path.unlink()
        status, output = self._run_cli("--only", self.book_key)
        self.assertEqual(2, status)
        self.assertIn("Combined Markdown is missing", output)

        combined_path.write_text(self.markdown + f"\n> {NOTICE}\n", encoding="utf-8")
        status, output = self._run_cli("--only", self.book_key)

        self.assertEqual(2, status)
        self.assertIn("Combined Markdown must contain the exact AI notice exactly once", output)

    def test_custom_config_and_output_remain_standalone_without_combined_dir(self) -> None:
        custom_config = self.root / "examples"
        custom_combined = self.root / "example-source"
        custom_output = self.root / "dist"
        self._write_project(custom_config, custom_combined)
        self._build(
            combined=custom_combined,
            config=custom_config,
            output=custom_output,
        )
        (custom_combined / f"{self.book_key}.md").unlink()

        status, _ = self._run_cli(
            "--config-dir",
            "examples",
            "--output-dir",
            "dist",
            "--only",
            self.book_key,
        )

        self.assertEqual(0, status)

    def test_explicit_combined_dir_enables_check_for_custom_layout(self) -> None:
        custom_config = self.root / "examples"
        custom_combined = self.root / "example-source"
        custom_output = self.root / "dist"
        self._write_project(custom_config, custom_combined)
        self._build(
            combined=custom_combined,
            config=custom_config,
            output=custom_output,
        )

        status, _ = self._run_cli(
            "--config-dir",
            "examples",
            "--output-dir",
            "dist",
            "--combined-dir",
            "example-source",
            "--only",
            self.book_key,
        )

        self.assertEqual(0, status)


if __name__ == "__main__":
    unittest.main()
