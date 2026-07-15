---
name: ebook-translation-harness
description: Prepare user-supplied EPUB, PDF, TXT, or Markdown books for Korean translation, run a faithful draft-to-polished workflow, and publish validated Korean Markdown or EPUB outputs. Use for book translation, polishing, output-format selection, TOC repair, or resuming a prepared book in Codex or Claude Code.
---

# Ebook Translation Harness

Run commands from the repository root. Treat original books, extracted text, translations, combined
Markdown, reports, and generated EPUBs as private working files; never add them to this repository.

## Select the subscription environment

This skill runs inside the user's subscribed AI environment. It does not call a model API or manage API
keys. Before translating, select and record one of these minimum quality profiles:

- GPT/Codex: `gpt-5.6-terra` with effort `high`; allow `xhigh`, `max`, or a stronger model.
- Claude Code: `claude-opus-4-8` with effort `high`; allow `xhigh`, `max`, or a stronger model.

Pass the selected platform, model, and effort to `prepare*`. When the user has not chosen, use the
platform default above and record it without stopping an autonomous run. Treat the stored profile as a
requested session policy, not proof of the model that actually ran. If the active model cannot be
observed, report that limitation. Never claim that this Python harness changed the active model or that
an arbitrary custom model is at least as strong as the recorded minimum.

## Select the final format

Ask for `md` or `epub` when the user has not chosen. Use `combine --output-format md` for an editable
Markdown deliverable and `combine --output-format epub` for a reader-ready EPUB. EPUB packaging still
retains combined Markdown as private validation evidence; do not describe it as an extra requested
deliverable. Keep legacy `--build` only for existing workflows.

## Safe sequence

1. Inspect the supplied source and any existing project folder before writing files. Never overwrite a
   previous translation chunk.
2. Prepare the source with `scripts/epub_agent_translation_harness.py`. For EPUB input, preserve the
   source `nav.xhtml` or `toc.ncx` hierarchy as `source_outline.json`.
3. Translate each distinct chunk with a paragraph-level `translate -> self-review -> refine` pass,
   then polish only from each `draft_ko` file into its matching
   `polished_ko` file. Read [references/polishing-principles-ko.md](references/polishing-principles-ko.md)
   before the polished pass. Preserve headings, tables, quotations, notes, names, dates, units, and note
   numbers. If the separate `humanize-korean` skill is available, use it as an optional polishing worker
   under these stricter book-preservation rules; never claim it ran when it is unavailable.
4. Put the exact reader notice `이 전자책은 AI 윤문 번역본입니다.` once in front matter as prose or
   a block quote. Never make it a heading and never repeat it per chunk or chapter.
5. Review the source outline, printed contents, and actually available translation scope. Write an exact
   `toc_contract.json` for the intended Korean navigation. Record missing source chapters as
   `SOURCE GAP`; do not invent content to fill them.
6. Run translation QC, combine to private Markdown, and then run structure QC in that order. If Markdown
   is the chosen final format, this passing combined file is the deliverable and no second combine is
   needed. For new
   work, polished QC compares `draft_ko` with `polished_ko`; do not treat a file polished directly from
   the source as valid. Legacy manifests without the new polish policy remain readable, but report that
   draft comparison was unavailable. Fix derived assembly rules rather than rewriting immutable source
   or polished chunks.
7. If EPUB is the chosen format, run coordinator `combine --output-format epub` after structure QC.
   Both `--output-format md` and `--output-format epub` require `book_meta.json` and a reviewed
   `toc_contract.json`; EPUB also runs the atomic builder and package validator. Keep the lower-level
   builder's `--only` option for advanced compatibility work.
8. Validate ZIP/XML/OPF, manifest and spine targets, navigation-versus-spine order, internal links,
   Korean metadata, the single AI notice, `nav.xhtml`, `toc.ncx`, and the exact nested contract.
   Publish by atomic replacement only after every check passes.

## Parallel work

Use parallel workers by default for draft and polished passes. Ask the coordinator for the next batch
with `next --workers auto`; it recommends a conservative batch from available memory and remaining
tasks. Use no more workers than the current AI environment actually permits. Assign one distinct task
to each worker and never let two workers write the same target. Use `--workers N` to lower or raise the
requested batch explicitly, and keep legacy `--limit N` only for existing automation.

The coordinator calculates and lists a batch; it does not launch model sessions. Its automatic plan is
capped at four because it cannot inspect Codex or Claude subscription slots. The active agent must apply
the actual platform limit before spawning workers.

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

Report completed/total chunk counts, selected model policy, worker count, requested final format,
the corresponding Markdown or EPUB path, retained validation evidence, structure and final validation
results, and any deliberately excluded or unavailable source scope.
