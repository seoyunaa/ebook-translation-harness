# EPUB Translation Harness

사용자가 제공한 EPUB, PDF, Markdown, TXT 책을 **작업 조각으로 준비하고, 한국어 번역·윤문 결과를 검사한 뒤, 구조가 검증된 EPUB으로 묶는 도구 모음**입니다.

이 저장소는 번역 모델이나 책 파일을 제공하지 않습니다. 원문과 번역문은 사용자가 합법적으로 준비해야 하며, 공개 저장소에는 올리지 않아야 합니다.

## 왜 이 하네스가 필요한가

EPUB 파일이 열리기만 한다고 올바른 책은 아닙니다. 부·장·절 순서가 뒤섞이거나, 각주 제목과 PDF 페이지 번호가 목차에 들어가도 일반 형식 검사만으로는 놓칠 수 있습니다.

이 하네스는 두 가지를 함께 확인합니다.

1. ZIP, XML, OPF, 링크, 메타데이터가 올바른지 확인합니다.
2. 독자에게 보이는 목차가 사전에 검토한 목차 계약과 정확히 같은지 확인합니다.

## 초보자를 위한 핵심 개념

- **book key**: 최종 합본과 EPUB의 파일 이름입니다. 예: sample_book_ko
- **book id**: 한 책의 작업 폴더 이름입니다. 예: sample_book
- **source outline**: 원본 EPUB의 내비게이션에서 추출한 목차 근거입니다.
- **TOC contract**: 최종 한국어 EPUB에 반드시 나타나야 할 제목, 순서, 부모·자식 관계를 사람이 검토해 적은 JSON 파일입니다.
- **combined Markdown**: 번역·윤문 조각을 순서대로 합친 파생 파일입니다.

## 코드와 책 작업 파일을 분리하기

하네스 코드는 공개 가능한 전용 폴더에, 원문·번역·EPUB은 비공개 작업 폴더에 두는 구조를 권장합니다.

~~~text
research/
├─ ebook-translation-harness/   # 이 저장소: 코드·테스트·가상 예제만
└─ book-workspace/             # 원문·번역 조각·합본·완성 EPUB
~~~

코드 폴더에서 비공개 작업 공간을 지정할 때는 `--project-root`를 사용합니다.

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" status --book-key sample_book_ko --book-id sample_book
~~~

이렇게 두면 Git에 코드를 올릴 때 실제 책 파일이 섞일 위험이 크게 줄어듭니다.

## 설치

Python 3.11 이상을 권장합니다. Windows PowerShell에서는 저장소 루트에서 다음 명령을 차례로 실행합니다.

~~~powershell
python -m venv .venv
./.venv/Scripts/Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
~~~

## 안전한 작업 순서

### 1. 원본을 작업 조각으로 준비합니다

EPUB:

~~~powershell
python scripts/epub_agent_translation_harness.py prepare --epub "input/source.epub" --book-key sample_book_ko --book-id sample_book --title-ko "가상 한국어 제목" --author-ko "가상 저자"
~~~

PDF:

~~~powershell
python scripts/epub_agent_translation_harness.py prepare-pdf --pdf "input/source.pdf" --book-key sample_book_ko --book-id sample_book --title-ko "가상 한국어 제목" --author-ko "가상 저자"
~~~

Markdown 또는 TXT:

~~~powershell
python scripts/epub_agent_translation_harness.py prepare-md --markdown "input/source.md" --book-key sample_book_ko --book-id sample_book --title-ko "가상 한국어 제목" --author-ko "가상 저자"
~~~

EPUB 원본이면 준비 단계가 원본 목차를 source_outline.json으로 보존합니다. 이것은 **근거 자료**이지 자동으로 승인된 최종 목차가 아닙니다.

### 2. 진행 상태와 다음 조각을 확인합니다

~~~powershell
python scripts/epub_agent_translation_harness.py status --book-key sample_book_ko --book-id sample_book
python scripts/epub_agent_translation_harness.py next --book-key sample_book_ko --book-id sample_book --stage polished --limit 6
~~~

하네스가 만든 source, draft_ko, polished_ko 조각의 역할을 섞지 마세요. 원문 조각은 근거이고, polished_ko가 최종 합본의 입력입니다.

### 3. 목차 계약을 검토해 작성합니다

원본 목차, 인쇄 목차, 실제로 확보된 번역 범위를 함께 확인한 뒤 다음 위치에 계약을 둡니다.

    03_outputs/translations/assets/sample_book/toc_contract.json

형식은 examples/sample_book/toc_contract.json과 schemas/toc_contract.schema.json을 참고하세요. 원본에 장이 적혀 있어도 본문 파일이 없다면 새 내용을 만들지 말고 SOURCE GAP으로 기록합니다.

### 4. 번역 조각의 품질을 먼저 검사합니다

~~~powershell
python scripts/epub_agent_translation_harness.py qc --book-key sample_book_ko --book-id sample_book --stage polished
~~~

### 5. 합본을 만들고 구조를 검사합니다

~~~powershell
python scripts/epub_agent_translation_harness.py combine --book-key sample_book_ko --book-id sample_book --stage polished
python scripts/epub_agent_translation_harness.py structure-qc --book-key sample_book_ko --book-id sample_book --label before_build
~~~

### 6. 한 권만 지정해 EPUB을 만듭니다

~~~powershell
python scripts/build_epubs_from_combined.py --only sample_book_ko
~~~

일상 작업에서는 --only를 생략하지 마세요. 지정 빌드는 검토된 toc_contract.json이 없거나 실제 목차가 계약과 다르면 실패합니다.

저장소에 포함된 완전한 가상 예제를 바로 빌드하려면 다음 명령을 사용합니다.

~~~powershell
python scripts/build_epubs_from_combined.py --combined-dir examples/sample_book --config-dir examples --output-dir dist --only sample_book_ko
python scripts/validate_epub_toc_contract.py --config-dir examples --output-dir dist --combined-dir examples/sample_book --only sample_book_ko
~~~

### 7. 최종 EPUB을 다시 검증합니다

~~~powershell
python scripts/validate_epub_toc_contract.py --book-key sample_book_ko --label sample_book_final
~~~

테스트 전체 실행:

~~~powershell
python -m unittest discover -s tests -p "test_*.py"
~~~

## 안전 보장

- **계약 우선**: 지정 빌드는 검토된 목차 계약 없이는 출판하지 않습니다.
- **근거 보존**: EPUB 원본의 nav.xhtml 또는 toc.ncx 목차를 source_outline.json에 보존합니다.
- **정확한 계층 비교**: 제목뿐 아니라 순서와 부모·자식 관계까지 계약과 비교합니다.
- **AI 고지 한 번**: “이 전자책은 AI 윤문 번역본입니다.”를 일반 문단 또는 인용문으로 정확히 한 번 두고 목차 제목으로 만들지 않습니다.
- **읽기 순서 대조**: 목차가 가리키는 모든 문서가 OPF spine에 있고, 목차 순서와 실제 읽기 순서가 일치해야 합니다.
- **표를 버리지 않음**: 원본 EPUB을 준비할 때 XHTML 표의 셀 내용을 Markdown 표로 보존합니다.
- **숨은 표식과 위험한 SVG 차단**: HTML 주석은 독자 본문에서 제거하고, 스크립트나 외부 추적 자원을 포함한 SVG는 빌드를 중단합니다.
- **작업 표식 차단**: PDF 페이지 표식, 내부 작업 ID, 자동 분할의 “계속”, 색인 A–Z, 그림 번호 같은 항목을 독자 목차에서 막습니다.
- **원자적 출판**: 임시 EPUB이 모든 검사를 통과한 뒤에만 최종 파일을 교체합니다. 실패한 빌드는 기존 정상본을 덮어쓰지 않습니다.
- **내용을 꾸며내지 않음**: 원문이 없는 장은 SOURCE GAP으로 남깁니다.
- **공개 자료 분리**: .gitignore가 원문, 번역 조각, 합본, EPUB, PDF, 이미지와 작업 산출물을 기본적으로 제외합니다. 커밋 전에는 반드시 git status와 git diff --cached를 직접 확인하세요.
- **자동 회귀 검사**: GitHub Actions가 매 푸시에 기능 테스트, 가상 예제 빌드, 공개 자료 위생 검사를 다시 실행합니다.

## 저장소에 포함하면 안 되는 것

- 저작권이 있는 EPUB, PDF, 이미지
- 원문 추출 파일과 번역·윤문 조각
- combined Markdown과 완성 EPUB
- 실제 책의 목차 계약, source outline, 메타데이터
- 사용자 이름이나 로컬 절대 경로가 들어간 로그·보고서

## 더 자세한 설명

- 구조 정책: docs/EPUB_STRUCTURE_HARNESS.md
- 계약 스키마: schemas/toc_contract.schema.json
- 가상 예시: examples/sample_book/

## English summary

This repository provides an evidence-first workflow for preparing user-supplied books, assembling Korean translations, and publishing structurally validated EPUBs. A reviewed TOC contract is mandatory for targeted builds; the generated navigation must match it exactly. Source outlines are retained as evidence, the AI-polished translation notice appears exactly once outside the TOC, and the final EPUB replaces the previous file only after all checks pass. No copyrighted books, translations, or generated EPUBs belong in this public repository.

## License

MIT. See LICENSE.
