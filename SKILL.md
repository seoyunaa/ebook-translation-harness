---
name: ebook-translation-harness
description: Prepare user-supplied EPUB, PDF, TXT, or Markdown books for Korean translation, verify reader-facing hierarchy with an exact TOC contract, and build validated EPUBs without publishing source books or translation artifacts.
---

# Ebook Translation Harness

Run commands from the repository root. Treat original books, extracted text, translations, combined
Markdown, reports, and generated EPUBs as private working files; never add them to this repository.

## Safe sequence

1. Inspect the supplied source and any existing project folder before writing files. Never overwrite a
   previous translation chunk.
2. Prepare the source with `scripts/epub_agent_translation_harness.py`. For EPUB input, preserve the
   source `nav.xhtml` or `toc.ncx` hierarchy as `source_outline.json`.
3. Translate and polish distinct chunks without changing their recorded order. Preserve headings,
   tables, quotations, notes, names, dates, units, and note numbers.
4. Put the exact reader notice `이 전자책은 AI 윤문 번역본입니다.` once in front matter as prose or
   a block quote. Never make it a heading and never repeat it per chunk or chapter.
5. Review the source outline, printed contents, and actually available translation scope. Write an exact
   `toc_contract.json` for the intended Korean navigation. Record missing source chapters as
   `SOURCE GAP`; do not invent content to fill them.
6. Run translation QC and structure QC before combining. Fix derived assembly rules rather than
   rewriting immutable source or polished chunks.
7. Build one named book with `--only`. A targeted build without `book_meta.json` and a reviewed
   `toc_contract.json` must stop.
8. Validate ZIP/XML/OPF, manifest and spine targets, navigation-versus-spine order, internal links,
   Korean metadata, the single AI notice, `nav.xhtml`, `toc.ncx`, and the exact nested contract.
   Publish by atomic replacement only after every check passes.

## Reader-facing hierarchy

- H1: book title, once
- H2: top-level divisions or independent front/back matter
- H3: chapters under a division
- H4: numbered sections under a chapter
- H5 or prose: notes, figure labels, index letters, and worker-only detail that must not enter navigation

Use each book's evidence and contract rather than forcing this example mechanically.

## Never bypass

Do not bypass a failed structure or TOC-contract report by calling a lower-level builder. Do not allow
PDF page markers, worker IDs, generated `계속 N` labels, repeated notices, note headings, index A–Z,
or figure labels to leak into the navigation. Strip non-reader HTML comments, preserve table cells, and
block active or externally loading SVG content. Do not replace a known-good EPUB with an unvalidated file.

## Report

Report completed/total chunk counts, combined and EPUB paths, structure and final validation results,
and any deliberately excluded or unavailable source scope.
