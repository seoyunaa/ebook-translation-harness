from __future__ import annotations

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
from epub_core import HarnessError  # noqa: E402


NOTICE = "> 이 전자책은 AI 윤문 번역본입니다."


class BuilderSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.combined = self.root / "combined"
        self.config = self.root / "config"
        self.output = self.root / "output"
        self.book_key = "safe_sample_ko"
        self.book_dir = self.config / self.book_key
        self.combined.mkdir()
        self.book_dir.mkdir(parents=True)
        self.image_dir = self.combined / "images"
        self.image_dir.mkdir()
        self.svg_path = self.image_dir / "figure.svg"

        meta = {
            "book_key": self.book_key,
            "title": "달빛 기록실",
            "author": "가상 작가",
            "language": "ko",
            "toc_heading_levels": [2],
        }
        contract = {
            "version": 1,
            "items": [{"label": "본문", "children": []}],
            "forbidden_label_patterns": [r"^TASK-", r"PDF_PAGE"],
        }
        (self.book_dir / "book_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        (self.book_dir / "toc_contract.json").write_text(
            json.dumps(contract, ensure_ascii=False), encoding="utf-8"
        )

    def _write_markdown(self, body: str) -> None:
        (self.combined / f"{self.book_key}.md").write_text(body, encoding="utf-8")

    def _write_svg(self, svg: str) -> None:
        self.svg_path.write_text(svg, encoding="utf-8")
        self._write_markdown(
            f"""# 달빛 기록실

{NOTICE}

## 본문

안전성 검사를 위한 가상 본문이다.

![가상 도형](images/figure.svg)
"""
        )

    def _build(self) -> Path:
        return builder.build_one(
            self.book_key,
            combined_dir=self.combined,
            config_dir=self.config,
            output_dir=self.output,
        )

    def test_html_comments_are_absent_from_epub_body_and_structure(self) -> None:
        self._write_markdown(
            f"""# 달빛 기록실

{NOTICE}

<!-- PDF_PAGE: 7 -->

## 본문

보이는 앞부분 <!-- 한 줄 비밀 --> 보이는 뒷부분.

앞 문장
<!--
## 숨은 장
PDF_PAGE: 999
여러 줄 비밀
![주석 속 외부 그림](https://example.invalid/hidden.png)
-->
뒤 문장
"""
        )

        epub = self._build()
        with zipfile.ZipFile(epub) as archive:
            xhtml = "\n".join(
                archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.endswith(".xhtml")
            )

        self.assertIn("보이는 앞부분", xhtml)
        self.assertIn("보이는 뒷부분", xhtml)
        for hidden in (
            "PDF_PAGE",
            "한 줄 비밀",
            "숨은 장",
            "여러 줄 비밀",
            "example.invalid",
            "&lt;!--",
            "<!--",
        ):
            self.assertNotIn(hidden, xhtml)

    def test_unclosed_html_comment_blocks_build(self) -> None:
        self._write_markdown(
            f"""# 달빛 기록실

{NOTICE}

## 본문

보이는 문장.

<!-- 닫히지 않은 주석
"""
        )

        with self.assertRaisesRegex(HarnessError, "Unclosed HTML comment"):
            self._build()
        self.assertFalse((self.output / f"{self.book_key}.epub").exists())

    def test_direct_xhtml_render_also_strips_html_comments(self) -> None:
        body = builder.markdown_to_xhtml_body(
            """## 본문

보이는 앞부분 <!-- 직접 렌더 비밀 --> 보이는 뒷부분.

<!--
PDF_PAGE: 321
여러 줄 직접 렌더 비밀
-->
""",
            first_heading_id="body",
            image_map={},
        )

        self.assertIn("보이는 앞부분", body)
        self.assertIn("보이는 뒷부분", body)
        for hidden in ("직접 렌더 비밀", "PDF_PAGE", "&lt;!--", "<!--"):
            self.assertNotIn(hidden, body)

    def test_self_contained_local_svg_is_allowed(self) -> None:
        self._write_svg(
            """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">
  <defs>
    <linearGradient id="paint"><stop offset="0" stop-color="#fff"/></linearGradient>
    <path id="shape" d="M1 1h18v18H1z"/>
  </defs>
  <style>.tone { fill: url(#paint); }</style>
  <use href="#shape" class="tone"/>
</svg>
"""
        )

        epub = self._build()
        with zipfile.ZipFile(epub) as archive:
            self.assertIn("EPUB/images/image-0001.svg", archive.namelist())

    def test_active_or_external_svg_content_blocks_build(self) -> None:
        cases = {
            "script": """<svg xmlns="http://www.w3.org/2000/svg">
  <script>alert(1)</script>
</svg>""",
            "foreign_object": """<svg xmlns="http://www.w3.org/2000/svg">
  <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>
</svg>""",
            "event_attribute": """<svg xmlns="http://www.w3.org/2000/svg" onLoad="alert(1)"/>""",
            "javascript_href": """<svg xmlns="http://www.w3.org/2000/svg">
  <a href="javascript:alert(1)"><text>x</text></a>
</svg>""",
            "data_href": """<svg xmlns="http://www.w3.org/2000/svg">
  <image href="data:image/png;base64,AAAA"/>
</svg>""",
            "http_href": """<svg xmlns="http://www.w3.org/2000/svg">
  <image href="http://example.invalid/image.png"/>
</svg>""",
            "https_href": """<svg xmlns="http://www.w3.org/2000/svg">
  <image href="https://example.invalid/image.png"/>
</svg>""",
            "xlink_href": """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:xlink="http://www.w3.org/1999/xlink">
  <image xlink:href="https://example.invalid/image.png"/>
</svg>""",
            "css_import": """<svg xmlns="http://www.w3.org/2000/svg">
  <style>@import "theme.css";</style>
</svg>""",
            "css_external_url": """<svg xmlns="http://www.w3.org/2000/svg">
  <rect style="fill:url(https://example.invalid/paint.svg)"/>
</svg>""",
            "css_comment_obfuscation": """<svg xmlns="http://www.w3.org/2000/svg">
  <style>@im/**/port "theme.css";</style>
</svg>""",
            "css_url_comment_obfuscation": """<svg xmlns="http://www.w3.org/2000/svg">
  <style>.tone { fill: u/**/rl(https://example.invalid/paint.svg); }</style>
</svg>""",
        }

        for label, svg in cases.items():
            with self.subTest(label=label):
                self._write_svg(svg)
                with self.assertRaisesRegex(HarnessError, "Unsafe SVG"):
                    self._build()
                self.assertFalse((self.output / f"{self.book_key}.epub").exists())


if __name__ == "__main__":
    unittest.main()
