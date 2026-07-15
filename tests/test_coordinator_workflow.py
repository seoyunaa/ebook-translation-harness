from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch


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
    def _prepare_simple(
        self,
        project: Path,
        *,
        book_key: str,
        book_id: str,
        text: str = "## Chapter\n\nFictional text.\n",
    ) -> tuple[Path, Path, Path, dict[str, Any]]:
        source = project / f"{book_id}_source.md"
        source.write_text(text, encoding="utf-8")
        self.assertEqual(
            0,
            harness.main(
                [
                    "--project-root",
                    str(project),
                    "prepare-md",
                    "--markdown",
                    str(source),
                    "--book-key",
                    book_key,
                    "--book-id",
                    book_id,
                    "--title-ko",
                    "Fictional Book",
                    "--author-ko",
                    "Example Author",
                    "--translation-platform",
                    "gpt",
                ]
            ),
        )
        translations = project / "03_outputs" / "translations"
        book_dir = translations / book_id
        manifest = json.loads((book_dir / "manifest.json").read_text(encoding="utf-8"))
        return source, translations, book_dir, manifest

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
                "--translation-platform",
                "gpt",
            ]

            self.assertEqual(0, harness.main(prepare_args))
            self.assertEqual(2, harness.main(prepare_args))

            translations = project / "03_outputs" / "translations"
            book_dir = translations / "fictional_book"
            manifest = json.loads(
                (book_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, manifest["task_count"])
            self.assertEqual(
                {
                    "platform": "gpt",
                    "requested_model": "gpt-5.6-terra",
                    "minimum_model": "gpt-5.6-terra",
                    "minimum_status": "recorded_default",
                    "requested_effort": "high",
                    "selection_surface": "external_subscription_session",
                    "runtime_verified": False,
                },
                manifest["translation_policy"],
            )
            task = manifest["tasks"][0]
            draft = book_dir / task["draft"]
            draft.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
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
                "--translation-platform",
                "gpt",
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
                        "--translation-platform",
                        "claude",
                        "--translation-model",
                        "claude-opus-4-9",
                        "--translation-effort",
                        "max",
                    ]
                ),
            )
            translations = project / "03_outputs" / "translations"
            manifest = json.loads(
                (translations / "custom_assets" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                {
                    "platform": "claude",
                    "requested_model": "claude-opus-4-9",
                    "minimum_model": "claude-opus-4-8",
                    "minimum_status": "custom_model_unverified",
                    "requested_effort": "max",
                    "selection_surface": "external_subscription_session",
                    "runtime_verified": False,
                },
                manifest["translation_policy"],
            )
            polished = translations / "custom_assets" / manifest["tasks"][0]["polished"]
            draft = translations / "custom_assets" / manifest["tasks"][0]["draft"]
            draft.write_text(
                "## Chapter\n\nFictional body.\n", encoding="utf-8"
            )
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
                        "--output-format",
                        "epub",
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

            status_output = StringIO()
            with redirect_stdout(status_output):
                self.assertEqual(
                    0,
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
            self.assertIn(
                "translation_policy=claude/claude-opus-4-9 "
                "minimum=claude-opus-4-8 minimum_status=custom_model_unverified effort=max "
                "surface=external_subscription_session "
                "runtime_verified=false",
                status_output.getvalue(),
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

    def test_default_and_explicit_md_output_do_not_build_epub(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source, translations, book_dir, manifest = self._prepare_simple(
                project,
                book_key="markdown_only_ko",
                book_id="markdown_only",
            )
            task = manifest["tasks"][0]
            (book_dir / task["draft"]).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (book_dir / task["polished"]).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            contract = {
                "version": 1,
                "items": [{"label": "Chapter", "children": []}],
                "forbidden_label_patterns": [],
            }
            (translations / "assets" / "markdown_only" / "toc_contract.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
            combine = [
                "--project-root",
                str(project),
                "combine",
                "--book-key",
                "markdown_only_ko",
                "--book-id",
                "markdown_only",
            ]

            self.assertEqual(0, harness.main(combine))
            combined = translations / "combined" / "markdown_only_ko.md"
            epub = translations / "epub" / "markdown_only_ko.epub"
            self.assertTrue(combined.is_file())
            self.assertFalse(epub.exists())

            self.assertEqual(0, harness.main(combine + ["--output-format", "md"]))
            self.assertTrue(combined.is_file())
            self.assertFalse(epub.exists())
            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    harness.main(combine + ["--build", "--output-format", "epub"])

    def test_next_polished_blocks_without_draft_and_uses_draft_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            _, _, book_dir, manifest = self._prepare_simple(
                project,
                book_key="next_paths_ko",
                book_id="next_paths",
            )
            task = manifest["tasks"][0]
            common = [
                "--project-root",
                str(project),
                "next",
                "--book-key",
                "next_paths_ko",
                "--book-id",
                "next_paths",
                "--stage",
                "polished",
            ]

            blocked_output = StringIO()
            with redirect_stdout(blocked_output):
                self.assertEqual(0, harness.main(common))
            blocked_text = blocked_output.getvalue()
            draft = book_dir / task["draft"]
            polished = book_dir / task["polished"]
            self.assertIn(f"missing_draft={draft}", blocked_text)
            self.assertIn("workers=0", blocked_text)
            self.assertIn("blocked_missing_draft=1", blocked_text)

            draft.write_text("## Chapter\n\nDraft text.\n", encoding="utf-8")
            ready_output = StringIO()
            with redirect_stdout(ready_output):
                self.assertEqual(0, harness.main(common + ["--workers", "7"]))
            ready_text = ready_output.getvalue()
            self.assertIn(f"source={draft}", ready_text)
            self.assertIn(f"target={polished}", ready_text)
            self.assertIn("workers=1 mode=manual", ready_text)
            self.assertIn("blocked_missing_draft=0", ready_text)

            limit_output = StringIO()
            with redirect_stdout(limit_output):
                self.assertEqual(
                    0,
                    harness.main(common + ["--workers", "7", "--limit", "1"]),
                )
            self.assertIn("mode=legacy-limit", limit_output.getvalue())
            self.assertIn("explicit --limit=1 overrides --workers", limit_output.getvalue())

    def test_auto_worker_memory_boundaries_manual_workers_and_ready_cap(self) -> None:
        gib = 1024**3
        cases = [
            (None, 4),
            (1 * gib, 1),
            (2 * gib, 1),
            (4 * gib, 2),
            (8 * gib, 4),
            (64 * gib, 4),
        ]
        for available, expected in cases:
            with self.subTest(available=available):
                with patch.object(harness, "_available_memory_bytes", return_value=available):
                    planned, mode, _reason = harness._resolve_next_plan(
                        workers="auto", limit=None, remaining=10
                    )
                self.assertEqual(expected, planned)
                self.assertEqual("auto", mode)

        with patch.object(harness, "_available_memory_bytes", return_value=None):
            planned, mode, reason = harness._resolve_next_plan(
                workers="auto", limit=None, remaining=2
            )
        self.assertEqual(2, planned)
        self.assertEqual("auto", mode)
        self.assertIn("fallback=4", reason)
        self.assertIn("capped_by_ready_tasks=2", reason)

        planned, mode, reason = harness._resolve_next_plan(
            workers=6, limit=None, remaining=3
        )
        self.assertEqual(3, planned)
        self.assertEqual("manual", mode)
        self.assertIn("capped_by_ready_tasks=3", reason)

        planned, mode, reason = harness._resolve_next_plan(
            workers=1, limit=5, remaining=7
        )
        self.assertEqual(5, planned)
        self.assertEqual("legacy-limit", mode)
        self.assertIn("overrides --workers", reason)

    def test_polished_qc_compares_draft_structure_and_protected_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            _, _, book_dir, manifest = self._prepare_simple(
                project,
                book_key="polish_guard_ko",
                book_id="polish_guard",
            )
            task = manifest["tasks"][0]
            draft = book_dir / task["draft"]
            polished = book_dir / task["polished"]
            draft_text = (
                "## 제1장\n\n"
                "> 직접 인용 2024년 12월 3일.\n\n"
                "연구값은 42.5%였고 https://example.test/source 에 기록되었다. "
                "각주[^1]와 \"고정 인용\", 『학술 인용』을 보존한다. "
                "[내부 절](chapter-2.md#section)과 [참고][ref]를 확인한다. "
                "이 문장은 변경률 검사를 위한 충분한 길이를 확보하려고 반복되지 않는 설명을 덧붙인다.\n\n"
                "| 항목 | 값 |\n|---|---|\n| A | 42.5% |\n\n"
                "[ref]: notes.md#entry\n\n"
                "[^1]: 출처 17쪽.\n"
            )
            draft.write_text(draft_text, encoding="utf-8")
            polished.write_text(
                draft_text.replace("기록되었다", "기록됐다"), encoding="utf-8"
            )
            common = [
                "--project-root",
                str(project),
                "qc",
                "--book-key",
                "polish_guard_ko",
                "--book-id",
                "polish_guard",
                "--stage",
                "polished",
            ]
            self.assertEqual(0, harness.main(common))

            polished.write_text(
                draft_text.replace("## 제1장", "## 제2장")
                .replace("42.5%", "41.0%")
                .replace("[^1]", "[^2]")
                .replace("『학술 인용』", "『변경된 인용』")
                .replace("chapter-2.md#section", "chapter-3.md#section"),
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(2, harness.main(common))
            result = output.getvalue()
            self.assertIn("polished headings differ", result)
            self.assertIn("number values differ", result)
            self.assertIn("footnote markers differ", result)
            self.assertIn("Markdown link targets differ", result)
            self.assertIn("inline quotations differ", result)

    def test_polished_qc_preserves_token_order_units_and_short_text(self) -> None:
        self.assertTrue(
            harness._polish_integrity_errors(
                "ordered", "값은 10% 뒤에 20%다.", "값은 20% 뒤에 10%다."
            )
        )
        unit_errors = harness._polish_integrity_errors(
            "units", "거리는 12 km다.", "거리는 12 mile이다."
        )
        self.assertTrue(any("number/unit tokens" in item for item in unit_errors))
        self.assertEqual(
            [],
            harness._polish_integrity_errors(
                "particle", "2024년에 출간됐다.", "2024년에는 출간됐다."
            ),
        )
        short_errors = harness._polish_integrity_errors(
            "short", "짧지만 중요한 원문이다.", "전혀 다른 내용으로 바뀌었다."
        )
        self.assertTrue(any("50% rollback gate" in item for item in short_errors))
        ordinal_errors = harness._polish_integrity_errors(
            "ordinal", "제1차 실험은 1.2e5회다.", "제2차 실험은 1.2e6회다."
        )
        self.assertTrue(any("number values" in item for item in ordinal_errors))
        currency_errors = harness._polish_integrity_errors(
            "currency", "가격은 USD 12다.", "가격은 EUR 12다."
        )
        self.assertTrue(any("currency tokens" in item for item in currency_errors))

    def test_polished_qc_handles_unbordered_tables_and_quote_reflow(self) -> None:
        draft_quote = "> 같은 인용문을 두 줄로\n> 나누어 적었다.\n"
        polished_quote = "> 같은 인용문을 두 줄로 나누어 적었다.\n"
        self.assertEqual(
            [],
            harness._polish_integrity_errors("quote", draft_quote, polished_quote),
        )

        draft_table = "항목 | 값\n---|---\nA | 1\nB | 2\n"
        polished_table = "항목 | 값\n---|---\nA | 1\n"
        table_errors = harness._polish_integrity_errors(
            "table", draft_table, polished_table
        )
        self.assertTrue(any("table row/column shape" in item for item in table_errors))

    def test_legacy_missing_draft_reports_migration_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            _, _, _, manifest = self._prepare_simple(
                project,
                book_key="legacy_migration_ko",
                book_id="legacy_migration",
            )
            manifest.pop("polish_policy")
            manifest_path = (
                project
                / "03_outputs"
                / "translations"
                / "legacy_migration"
                / "manifest.json"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    harness.main(
                        [
                            "--project-root",
                            str(project),
                            "next",
                            "--book-key",
                            "legacy_migration_ko",
                            "--book-id",
                            "legacy_migration",
                            "--stage",
                            "polished",
                        ]
                    ),
                )
            result = output.getvalue()
            self.assertIn("MIGRATION_REQUIRED", result)
            self.assertIn("create_draft=", result)
            self.assertIn("from_source=", result)

    def test_legacy_polished_only_manifest_remains_qc_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source, _, book_dir, manifest = self._prepare_simple(
                project,
                book_key="legacy_polished_ko",
                book_id="legacy_polished",
            )
            manifest.pop("polish_policy")
            (book_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            task = manifest["tasks"][0]
            (book_dir / task["polished"]).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    harness.main(
                        [
                            "--project-root",
                            str(project),
                            "qc",
                            "--book-key",
                            "legacy_polished_ko",
                            "--book-id",
                            "legacy_polished",
                            "--stage",
                            "polished",
                        ]
                    ),
                )
            self.assertIn("WARNING legacy_polished_ko polished", output.getvalue())
            self.assertIn("comparison was unavailable", output.getvalue())

    def test_prepare_defaults_to_gpt_and_rejects_obvious_provider_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            source = project / "source.md"
            source.write_text("## Chapter\n\nFictional text.\n", encoding="utf-8")
            common = [
                "--project-root",
                str(project),
                "prepare-md",
                "--markdown",
                str(source),
                "--book-key",
                "default_policy_ko",
                "--book-id",
                "default_policy",
                "--title-ko",
                "Default Policy",
                "--author-ko",
                "Example Author",
            ]
            self.assertEqual(0, harness.main(common))
            manifest = json.loads(
                (
                    project
                    / "03_outputs"
                    / "translations"
                    / "default_policy"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual("gpt", manifest["translation_policy"]["platform"])
            self.assertEqual(
                "recorded_default",
                manifest["translation_policy"]["minimum_status"],
            )

            mismatch = common.copy()
            mismatch[mismatch.index("default_policy_ko")] = "mismatch_ko"
            mismatch[mismatch.index("default_policy")] = "mismatch"
            mismatch.extend(["--translation-model", "claude-opus-4-8"])
            self.assertEqual(2, harness.main(mismatch))


if __name__ == "__main__":
    unittest.main()
