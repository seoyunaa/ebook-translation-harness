from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import extract_epub_outline as extractor  # noqa: E402
from epub_agent_translation_harness import _xhtml_to_markdown  # noqa: E402


class SourceOutlineTests(unittest.TestCase):
    def test_nested_epub_nav_is_preserved_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            epub = Path(temp_dir) / "fictional.epub"
            with zipfile.ZipFile(epub, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                archive.writestr(
                    "META-INF/container.xml",
                    """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
                )
                archive.writestr(
                    "EPUB/package.opf",
                    """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">fictional-id</dc:identifier><dc:title>가상 원문</dc:title>
    <dc:creator>가상 저자</dc:creator><dc:language>ko</dc:language>
  </metadata>
  <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>
  <spine/>
</package>""",
                )
                archive.writestr(
                    "EPUB/nav.xhtml",
                    """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <body><nav epub:type="toc"><ol>
    <li><a href="part.xhtml">제1부</a><ol>
      <li><a href="chapter.xhtml">제1장</a></li>
    </ol></li>
  </ol></nav></body>
</html>""",
                )

            outline = extractor.extract_outline(epub)

        self.assertEqual("nav.xhtml", outline["outline_source"])
        self.assertEqual(2, outline["item_count"])
        self.assertEqual(2, outline["max_depth"])
        self.assertEqual("제1부", outline["items"][0]["label"])
        self.assertEqual("제1장", outline["items"][0]["children"][0]["label"])

    def test_xhtml_table_cells_are_preserved_as_markdown(self) -> None:
        root = ET.fromstring(
            """<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <h2>Chapter</h2>
  <table>
    <caption>Measurements</caption>
    <tr><th>Name</th><th>Value</th></tr>
    <tr><td>Alpha</td><td>42</td></tr>
  </table>
  <p>After the table.</p>
</body></html>"""
        )

        markdown = _xhtml_to_markdown(root)

        self.assertIn("## Chapter", markdown)
        self.assertIn("Measurements", markdown)
        self.assertIn("| Name | Value |", markdown)
        self.assertIn("| Alpha | 42 |", markdown)
        self.assertIn("After the table.", markdown)


if __name__ == "__main__":
    unittest.main()
