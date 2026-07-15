from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SUFFIXES = {
    ".epub",
    ".pdf",
    ".mobi",
    ".azw",
    ".azw3",
    ".djvu",
    ".doc",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".mp3",
    ".mp4",
    ".zip",
    ".7z",
    ".rar",
}
FORBIDDEN_ROOTS = {
    "03_outputs",
    "input",
    "inputs",
    "original",
    "originals",
    "private",
    "source",
    "sources",
    "translations",
}
PRIVATE_STATE_NAMES = {"manifest.json", "source_outline.json"}
PUBLIC_BOOK_FILES = {
    "examples/sample_book/book_meta.json",
    "examples/sample_book/toc_contract.json",
}
WINDOWS_USER_PATH = re.compile(
    r"(?i)[a-z]:[\\/]+(?:users|documents[ ]and[ ]settings)[\\/]+[^\\/\s]+[\\/]"
)
POSIX_USER_PATH = re.compile(r"/(?:home|Users)/[^/\s]+/")


def tracked_files() -> list[PurePosixPath]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        PurePosixPath(value)
        for value in completed.stdout.decode("utf-8").split("\0")
        if value
    ]


class PublicationSafetyTests(unittest.TestCase):
    def test_tracked_tree_contains_only_public_harness_material(self) -> None:
        problems: list[str] = []
        for relative in tracked_files():
            relative_text = relative.as_posix()
            if relative.suffix.casefold() in FORBIDDEN_SUFFIXES:
                problems.append(f"private or binary publication format is tracked: {relative_text}")
            if relative.parts and relative.parts[0].casefold() in FORBIDDEN_ROOTS:
                problems.append(f"private workspace directory is tracked: {relative_text}")
            if relative.name.casefold() in PRIVATE_STATE_NAMES:
                problems.append(f"private harness state is tracked: {relative_text}")
            if relative.name in {"book_meta.json", "toc_contract.json"}:
                if relative_text not in PUBLIC_BOOK_FILES:
                    problems.append(f"non-sample book configuration is tracked: {relative_text}")

            path = ROOT.joinpath(*relative.parts)
            if not path.is_file() or path.stat().st_size > 1_000_000:
                if path.is_file() and path.stat().st_size > 1_000_000:
                    problems.append(f"unexpected file larger than 1 MB: {relative_text}")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"unexpected non-UTF-8 tracked file: {relative_text}")
                continue
            if WINDOWS_USER_PATH.search(text) or POSIX_USER_PATH.search(text):
                problems.append(f"local user path is embedded in: {relative_text}")

        self.assertEqual([], problems, "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
