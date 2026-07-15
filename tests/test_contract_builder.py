from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_epubs_from_combined as builder  # noqa: E402
import epub_core  # noqa: E402
import validate_epub_toc_contract as validator  # noqa: E402


NOTICE = "> 이 전자책은 AI 윤문 번역본입니다."


class ContractBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.combined = self.root / "combined"
        self.config = self.root / "books"
        self.output = self.root / "dist"
        self.book_key = "fictional_book_ko"
        self.book_dir = self.config / self.book_key
        self.combined.mkdir()
        self.book_dir.mkdir(parents=True)
        self.meta_data = {
            "book_key": self.book_key,
            "title": "별빛 기록관",
            "author": "가상 작가",
            "language": "ko",
            "toc_heading_levels": [2, 3, 4],
        }
        self.contract_data = {
            "version": 1,
            "items": [
                {"label": "머리말", "children": []},
                {
                    "label": "제1부: 문을 열며",
                    "children": [
                        {
                            "label": "제1장: 첫 기록",
                            "children": [
                                {"label": "1.1 작은 표식", "children": []}
                            ],
                        }
                    ],
                },
                {"label": "맺음말", "children": []},
            ],
            "forbidden_label_patterns": [r"계속\s+\d+$", r"^TASK-"],
        }
        self.markdown = """# 별빛 기록관

> 이 전자책은 AI 윤문 번역본입니다.

## 머리말

이 문장은 공개 테스트를 위해 새로 만든 가상 문장이다.

## 제1부: 문을 열며

### 제1장: 첫 기록

#### 1.1 작은 표식

기록자는 순서와 관계를 차례대로 확인했다.

## 맺음말

검사가 끝난 뒤에만 문을 닫았다.
"""
        self._write_project()

    def _write_project(self) -> None:
        (self.book_dir / "book_meta.json").write_text(
            json.dumps(self.meta_data, ensure_ascii=False), encoding="utf-8"
        )
        (self.book_dir / "toc_contract.json").write_text(
            json.dumps(self.contract_data, ensure_ascii=False), encoding="utf-8"
        )
        (self.combined / f"{self.book_key}.md").write_text(
            self.markdown, encoding="utf-8"
        )

    def _build(self) -> Path:
        return builder.build_one(
            self.book_key,
            combined_dir=self.combined,
            config_dir=self.config,
            output_dir=self.output,
        )

    def test_exact_nested_contract_builds_and_validates(self) -> None:
        epub = self._build()
        meta = epub_core.load_book_meta(self.config, self.book_key)
        contract = epub_core.load_toc_contract(self.config, self.book_key)
        result = validator.validate_epub(epub, meta, contract)

        self.assertEqual([], result.errors)
        self.assertEqual("PASS", result.status)
        self.assertEqual(5, result.toc_items)
        self.assertEqual(3, result.toc_depth)

    def test_contract_label_mismatch_blocks_build(self) -> None:
        self.contract_data["items"][1]["children"][0]["label"] = "제1장: 다른 제목"
        self._write_project()

        with self.assertRaisesRegex(epub_core.HarnessError, "contract|TOC|outline"):
            self._build()

    def test_missing_contract_blocks_targeted_build(self) -> None:
        (self.book_dir / "toc_contract.json").unlink()

        with self.assertRaisesRegex(epub_core.HarnessError, "Required file is missing"):
            self._build()

    def test_contract_version_and_unknown_fields_are_rejected(self) -> None:
        self.contract_data["version"] = 2
        self.contract_data["unexpected"] = "not allowed"
        self._write_project()

        with self.assertRaisesRegex(epub_core.HarnessError, "unsupported top-level fields|version"):
            self._build()

    def test_failed_rebuild_preserves_previous_valid_epub(self) -> None:
        epub = self._build()
        before = hashlib.sha256(epub.read_bytes()).hexdigest()
        self.markdown = self.markdown.replace("### 제1장: 첫 기록", "### 제1장: 잘못된 제목")
        self._write_project()

        with self.assertRaises(epub_core.HarnessError):
            self._build()

        self.assertEqual(before, hashlib.sha256(epub.read_bytes()).hexdigest())

    def test_nav_and_ncx_contain_the_same_nested_targets(self) -> None:
        epub = self._build()
        with zipfile.ZipFile(epub) as archive:
            nav = archive.read("EPUB/nav.xhtml").decode("utf-8")
            ncx = archive.read("EPUB/toc.ncx").decode("utf-8")

        for label in ("머리말", "제1부: 문을 열며", "제1장: 첫 기록", "1.1 작은 표식", "맺음말"):
            self.assertIn(label, nav)
            self.assertIn(label, ncx)


if __name__ == "__main__":
    unittest.main()
