from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ebook_structure_qc as structure_qc  # noqa: E402


NOTICE = "> 이 전자책은 AI 윤문 번역본입니다."


class StructureQcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "books"
        self.book_key = "fictional_book_ko"
        book_dir = self.config / self.book_key
        book_dir.mkdir(parents=True)
        (book_dir / "book_meta.json").write_text(
            json.dumps(
                {
                    "book_key": self.book_key,
                    "title": "달빛 문서고",
                    "author": "예시 저자",
                    "language": "ko",
                    "toc_heading_levels": [2, 3],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (book_dir / "toc_contract.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "label": "제1부",
                            "children": [{"label": "제1장: 시작", "children": []}],
                        }
                    ],
                    "forbidden_label_patterns": [r"계속\s+\d+$", r"^TASK-"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.markdown = self.root / "fictional_book_ko.md"

    def _audit(self, body: str) -> structure_qc.Report:
        self.markdown.write_text(body, encoding="utf-8")
        return structure_qc.audit(
            book_key=self.book_key,
            markdown_path=self.markdown,
            config_dir=self.config,
        )

    def test_one_front_matter_notice_passes(self) -> None:
        report = self._audit(
            "# 달빛 문서고\n\n"
            + NOTICE
            + "\n\n## 제1부\n\n### 제1장: 시작\n\n가상 본문입니다.\n"
        )
        self.assertEqual("PASS", report.status)

    def test_repeated_notice_is_blocked(self) -> None:
        report = self._audit(
            "# 달빛 문서고\n\n"
            + NOTICE
            + "\n\n## 제1부\n\n### 제1장: 시작\n\n"
            + NOTICE
            + "\n"
        )
        self.assertEqual("BLOCKED", report.status)
        self.assertTrue(any("AI" in issue.code or "AI" in issue.message for issue in report.issues))

    def test_notice_inside_body_is_blocked(self) -> None:
        report = self._audit(
            "# 달빛 문서고\n\n## 제1부\n\n### 제1장: 시작\n\n"
            + NOTICE
            + "\n"
        )
        self.assertEqual("BLOCKED", report.status)
        self.assertTrue(any("AI" in issue.code or "front" in issue.message.lower() for issue in report.issues))

    def test_pdf_marker_and_worker_heading_are_blocked(self) -> None:
        report = self._audit(
            "# 달빛 문서고\n\n"
            + NOTICE
            + "\n\n## 제1부\n\n### 제1장: 시작\n\nPDF PAGE 17\n\n## 윤문 번역\n"
        )
        codes = {issue.code for issue in report.issues}
        self.assertIn("PDF_MARKER", codes)
        self.assertIn("WORK_HEADING", codes)


if __name__ == "__main__":
    unittest.main()
