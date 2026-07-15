from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extract_pdf_pages import (
    PageRecord,
    PositionedLine,
    detect_repeated_edge_lines,
    is_isolated_page_number,
    normalize_running_head,
    render_markdown_pages,
)


def line(text: str, top: float, bottom: float) -> PositionedLine:
    return PositionedLine(text=text, top=top, bottom=bottom)


class PdfEdgeCleanupTests(unittest.TestCase):
    def make_pages(self) -> list[PageRecord]:
        pages: list[PageRecord] = []
        for page_number in range(1, 5):
            pages.append(
                PageRecord(
                    page_number=page_number,
                    width=600,
                    height=800,
                    lines=(
                        line(f"CHAPTER ONE {page_number}", 24, 36),
                        line("This repeated sentence is legitimate body text.", 300, 312),
                        line(f"Unique body text on page {page_number}.", 330, 342),
                        line(str(page_number), 770, 782),
                    ),
                )
            )
        return pages

    def test_repeated_edge_lines_and_page_numbers_are_removed(self) -> None:
        pages = self.make_pages()
        removals = detect_repeated_edge_lines(pages)

        for page_number in range(1, 5):
            self.assertEqual(
                removals[(page_number, 0)]["reason"], "repeated_running_header"
            )
            self.assertEqual(removals[(page_number, 3)]["reason"], "page_number")

    def test_repeated_body_text_is_not_removed(self) -> None:
        removals = detect_repeated_edge_lines(self.make_pages())
        self.assertNotIn((1, 1), removals)
        self.assertNotIn((2, 1), removals)

    def test_repeated_figure_text_near_but_outside_margin_is_kept(self) -> None:
        pages = [
            PageRecord(
                page_number=page_number,
                width=600,
                height=800,
                lines=(line("Repeated figure-axis label", 77, 88),),
            )
            for page_number in range(1, 5)
        ]
        self.assertEqual(detect_repeated_edge_lines(pages), {})

    def test_markdown_uses_invisible_page_comments(self) -> None:
        pages = self.make_pages()
        markdown = render_markdown_pages(pages, detect_repeated_edge_lines(pages))

        self.assertIn("<!-- PDF_PAGE: 1 -->", markdown)
        self.assertIn("<!-- PDF_PAGE: 4 -->", markdown)
        self.assertNotIn("===== PDF PAGE", markdown)
        self.assertNotIn("CHAPTER ONE 1", markdown)
        self.assertIn("Unique body text on page 1.", markdown)

    def test_running_head_normalization_ignores_changing_page_number(self) -> None:
        self.assertEqual(
            normalize_running_head("12 CHAPTER ONE"),
            normalize_running_head("CHAPTER ONE 13"),
        )

    def test_isolated_arabic_and_roman_page_numbers(self) -> None:
        self.assertTrue(is_isolated_page_number("- 123 -"))
        self.assertTrue(is_isolated_page_number("xii"))
        self.assertFalse(is_isolated_page_number("Chapter 12"))

    def test_page_number_can_use_wider_margin_than_running_head(self) -> None:
        page = PageRecord(
            page_number=1,
            width=600,
            height=800,
            lines=(line("42", 710, 722),),
        )
        self.assertEqual(
            detect_repeated_edge_lines([page])[(1, 0)]["reason"], "page_number"
        )


if __name__ == "__main__":
    unittest.main()
