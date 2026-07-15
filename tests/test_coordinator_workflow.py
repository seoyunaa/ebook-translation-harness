from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import epub_agent_translation_harness as harness  # noqa: E402


def epub_utf8_text(path: Path) -> str:
    """Return all UTF-8-readable EPUB members for reader-facing assertions."""
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            try:
                chunks.append(archive.read(name).decode("utf-8"))
            except (UnicodeDecodeError, IsADirectoryError):
                continue
    return "\n".join(chunks)


class CoordinatorWorkflowTests(unittest.TestCase):
    def test_prepare_polish_combine_build_and_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source = project / "fictional_source.md"
            source.write_text(
                "## 제1부: 가상 기록\n\n"
                "### 제1장: 첫 문\n\n"
                "본문은 테스트를 위해 새로 쓴 문장입니다.\n",
                encoding="utf-8",
            )
            prepare_args = [
                "--project-root",
                str(project),
                "prepare-md",
                "--markdown",
                str(source),
                "--book-key",
                "fictional_book_ko",
                "--book-id",
                "fictional_book",
                "--title-ko",
                "가상 기록집",
                "--author-ko",
                "예시 저자",
            ]

            self.assertEqual(0, harness.main(prepare_args))
            self.assertEqual(2, harness.main(prepare_args))

            translations = project / "03_outputs" / "translations"
            book_dir = translations / "fictional_book"
            manifest = json.loads(
                (book_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, manifest["task_count"])
            task = manifest["tasks"][0]
            polished = book_dir / task["polished"]
            polished.write_text(
                source.read_text(encoding="utf-8")
                + "\n<!-- PDF_PAGE: 9 -->\n"
                + "<!-- NORMAL-INTERNAL-COMMENT -->\n",
                encoding="utf-8",
            )
            contract = {
                "version": 1,
                "items": [
                    {
                        "label": "제1부: 가상 기록",
                        "children": [
                            {"label": "제1장: 첫 문", "children": []}
                        ],
                    }
                ],
                "forbidden_label_patterns": [r"계속\s+\d+$"],
            }
            (translations / "assets" / "fictional_book" / "toc_contract.json").write_text(
                json.dumps(contract, ensure_ascii=False), encoding="utf-8"
            )

            result = harness.main(
                [
                    "--project-root",
                    str(project),
                    "combine",
                    "--book-key",
                    "fictional_book_ko",
                    "--book-id",
                    "fictional_book",
                    "--stage",
                    "polished",
                    "--build",
                ]
            )

            self.assertEqual(0, result)
            combined = translations / "combined" / "fictional_book_ko.md"
            epub = translations / "epub" / "fictional_book_ko.epub"
            self.assertTrue(combined.is_file())
            self.assertTrue(epub.is_file())
            self.assertEqual(1, combined.read_text(encoding="utf-8").count(harness.AI_NOTICE))
            epub_text = epub_utf8_text(epub)
            self.assertNotIn("PDF_PAGE", epub_text)
            self.assertNotIn("NORMAL-INTERNAL-COMMENT", epub_text)

    def test_manifest_identity_blocks_cross_book_progress_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source = project / "source.md"
            source.write_text("## Chapter\n\nFictional text.\n", encoding="utf-8")
            prepare = [
                "--project-root",
                str(project),
                "prepare-md",
                "--markdown",
                str(source),
                "--book-key",
                "alpha_ko",
                "--book-id",
                "alpha",
                "--title-ko",
                "Alpha",
                "--author-ko",
                "Example Author",
            ]
            self.assertEqual(0, harness.main(prepare))

            for command in ("status", "next", "qc", "combine"):
                with self.subTest(command=command):
                    self.assertEqual(
                        2,
                        harness.main(
                            [
                                "--project-root",
                                str(project),
                                command,
                                "--book-key",
                                "beta_ko",
                                "--book-id",
                                "alpha",
                            ]
                        ),
                    )

            manifest_path = (
                project
                / "03_outputs"
                / "translations"
                / "alpha"
                / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["book_id"] = "different_id"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            for command in ("status", "next", "qc", "combine"):
                with self.subTest(command=command, mismatch="book_id"):
                    self.assertEqual(
                        2,
                        harness.main(
                            [
                                "--project-root",
                                str(project),
                                command,
                                "--book-key",
                                "alpha_ko",
                                "--book-id",
                                "alpha",
                            ]
                        ),
                    )

    def test_custom_book_id_builds_removes_pdf_comments_and_blocks_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source = project / "source.md"
            source.write_text("## Chapter\n\nSource text.\n", encoding="utf-8")
            common = ["--project-root", str(project)]
            self.assertEqual(
                0,
                harness.main(
                    common
                    + [
                        "prepare-md",
                        "--markdown",
                        str(source),
                        "--book-key",
                        "alpha_ko",
                        "--book-id",
                        "custom_assets",
                        "--title-ko",
                        "Alpha",
                        "--author-ko",
                        "Example Author",
                    ]
                ),
            )
            translations = project / "03_outputs" / "translations"
            manifest = json.loads(
                (translations / "custom_assets" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            polished = translations / "custom_assets" / manifest["tasks"][0]["polished"]
            polished.write_text(
                "## Chapter\n\n"
                "<!-- PDF_PAGE: 17 -->\n\n"
                "Fictional body.\n\n"
                "<!-- CUSTOM-INTERNAL-COMMENT -->\n",
                encoding="utf-8",
            )
            contract = {
                "version": 1,
                "items": [{"label": "Chapter", "children": []}],
                "forbidden_label_patterns": [r"^TASK-"],
            }
            (translations / "assets" / "custom_assets" / "toc_contract.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
            self.assertEqual(
                0,
                harness.main(
                    common
                    + [
                        "combine",
                        "--book-key",
                        "alpha_ko",
                        "--book-id",
                        "custom_assets",
                        "--stage",
                        "polished",
                        "--build",
                    ]
                ),
            )
            combined = translations / "combined" / "alpha_ko.md"
            self.assertNotIn("PDF_PAGE", combined.read_text(encoding="utf-8"))
            epub = translations / "epub" / "alpha_ko.epub"
            self.assertTrue(epub.is_file())
            epub_text = epub_utf8_text(epub)
            self.assertNotIn("PDF_PAGE", epub_text)
            self.assertNotIn("CUSTOM-INTERNAL-COMMENT", epub_text)
            self.assertEqual(
                0,
                harness.main(
                    common
                    + [
                        "structure-qc",
                        "--book-key",
                        "alpha_ko",
                        "--book-id",
                        "custom_assets",
                    ]
                ),
            )

            (translations / "assets" / "alpha").mkdir()
            self.assertEqual(
                2,
                harness.main(
                    common
                    + [
                        "status",
                        "--book-key",
                        "alpha_ko",
                        "--book-id",
                        "custom_assets",
                    ]
                ),
            )


if __name__ == "__main__":
    unittest.main()
