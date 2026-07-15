# Ebook Translation Harness (Markdown / EPUB)

사용자가 제공한 EPUB, PDF, Markdown, TXT 책을 **작업 조각으로 준비하고, 한국어 번역·윤문 결과를 검사한 뒤, 검증된 Markdown 또는 EPUB으로 만드는 도구 모음**입니다.

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
- **TOC contract**: 최종 Markdown과 EPUB에 반드시 나타나야 할 제목, 순서, 부모·자식 관계를 사람이 검토해 적은 JSON 파일입니다.
- **combined Markdown**: 번역·윤문 조각을 순서대로 합친 파생 파일입니다.

## 최종 산출물 고르기

최종 합본 명령을 실행할 때 형식을 고릅니다. 장기 작업이라면 시작할 때 원하는 형식을 작업 메모에도 적어 두되, 실제 실행 선택은 마지막 `combine --output-format`에서 이루어집니다.

| 선택 | 명령 옵션 | 독자에게 전달할 파일 | 설명 |
|---|---|---|---|
| Markdown | `--output-format md` | `03_outputs/translations/combined/<book_key>.md` | 편집·검색·인용이 쉬운 최종 원고 |
| EPUB | `--output-format epub` | `03_outputs/translations/epub/<book_key>.epub` | 전자책 앱에서 읽는 검증된 전자책 |

EPUB을 선택해도 combined Markdown은 검증 근거로 비공개 작업 공간에 남습니다. 이것은 EPUB의 제목·AI 고지·본문 순서를 다시 검사하는 중간 근거이며, 사용자가 요청한 추가 최종 산출물이라는 뜻은 아닙니다. `--build`는 기존 자동화의 호환용 별칭이고 새 작업에서는 `--output-format`을 사용합니다.

## 누가 번역하고 윤문하는가

이 저장소의 Python 하네스는 번역 모델을 직접 호출하지 않으며 API 키도 받지 않습니다. Claude 또는 GPT/Codex 구독 환경 안에서 실행된 에이전트가 `draft_ko`와 `polished_ko`를 작성하고, 하네스는 작업 분배·진행 추적·보존 검사·합본·출판을 담당합니다.

권장 최소 품질 기준은 다음과 같습니다.

| 실행 환경 | 기본 모델 | 최소 effort | 선택 가능한 상향 설정 |
|---|---|---|---|
| GPT/Codex | `gpt-5.6-terra` | `high` | `xhigh`, `max` 또는 더 강한 모델 |
| Claude Code | `claude-opus-4-8` | `high` | `xhigh`, `max` 또는 더 강한 모델 |

모델 선택은 구독 앱이나 Claude Code/Codex 세션에서 이루어집니다. 모델 이름을 생략하면 표의 기본 모델이 기록됩니다. `--translation-model`로 다른 모델도 선택할 수 있지만, 하네스가 모델의 우열을 자동 판정하지는 않으므로 기본값보다 약하지 않은지는 실행 환경에서 확인해야 합니다. 하네스는 요청한 플랫폼·모델·effort를 private manifest에 기록할 뿐, 실제 세션 모델을 바꾸거나 검증했다고 주장하지 않습니다.

모델 이름과 effort 범위는 [OpenAI 모델 목록](https://developers.openai.com/api/docs/models), [Claude Opus 4.8 안내](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-8), [Claude effort 안내](https://platform.claude.com/docs/en/build-with-claude/effort)를 기준으로 적었습니다.

### 윤문 단계

현재 하네스에는 모델을 직접 실행하는 독립 윤문 엔진이 내장되어 있지 않습니다. 대신 [`translate-book-to-korean`](https://github.com/seoyunaa/translate-book-to-korean)의 번역·번역투 교정과 [`im-not-ai`](https://github.com/seoyunaa/im-not-ai)의 `humanize-korean`을 전자책의 학술 인용·각주·구조 보존 요건과 결합한 [통합 윤문 원칙](references/polishing-principles-ko.md)을 정식 작업 단계로 사용합니다.

새 `prepare*` 작업의 윤문은 반드시 `draft_ko/<task-id>.md`를 읽어 같은 ID의 `polished_ko/<task-id>.md`에 새로 씁니다. 원문에서 곧바로 윤문본을 만들거나 원문·초벌 번역을 덮어쓰면 안 됩니다. 별도 `humanize-korean` 스킬이 설치된 환경에서는 선택형 윤문 작업자로 사용할 수 있지만, 고유명사·수치·인용·각주·표·제목을 보존하는 이 하네스의 원칙이 우선합니다. 예전 manifest에 윤문본만 있으면 하위호환을 위해 읽되, 초벌 대조를 할 수 없었다는 경고를 냅니다. 아직 윤문하지 않은 예전 조각에 초벌본이 없으면 `MIGRATION_REQUIRED`로 표시하고 먼저 `next --stage draft` 흐름으로 초벌 근거를 만들게 합니다.

Python 자동 검사는 제목 순서, 수치·단위의 순서, 통화, 각주 표식, URL과 링크 대상, 직접 인용, 블록 인용, 표 행·열 형태, 50% 초과 수정률을 대조합니다. 고유명사 의미, 논증의 확신도, 문장 누락과 30% 초과 수정의 Strict 재검토는 번역 에이전트나 사람이 확인합니다. 즉 자동 검사가 모든 의미 보존을 판정한다고 가정하면 안 됩니다.

## Codex와 Claude Code에서 사용하기

하네스의 Python 코드는 특정 모델 API에 의존하지 않고, 루트의 `SKILL.md`는 여러 AI 도구가 공유하는 Agent Skills 형식을 사용합니다. [Claude Code 공식 문서](https://code.claude.com/docs/en/slash-commands)도 이 형식을 여러 AI 도구에서 작동하는 공개 표준으로 설명합니다. 따라서 Python 3.11 이상, 로컬 파일 접근, 명령 실행 기능이 있는 **Codex 앱/CLI와 Claude Code**에서 사용할 수 있습니다.

- Codex 개인 스킬 위치 예: `~/.codex/skills/ebook-translation-harness/`
- Claude Code 개인 스킬 위치 예: `~/.claude/skills/ebook-translation-harness/`

일반 웹 채팅처럼 로컬 파일과 명령 실행 기능이 없는 화면에서는 이 하네스를 끝까지 실행할 수 없습니다. Claude Code에서는 시작 전에 `/model claude-opus-4-8`처럼 모델을 선택할 수 있고, Codex에서는 모델 선택 화면이나 세션 설정에서 Terra와 `high` 이상을 선택합니다.

## 예상 작업 시간

다음 시간은 보장 시간이 아니라 정상적인 텍스트 상태와 구독 사용량을 가정한 대략적인 범위입니다.

| 작업 | 보통 걸리는 시간 |
|---|---|
| 100~150쪽, 텍스트 상태가 좋은 책 | 약 3~7시간 |
| 250~400쪽 학술서 | 약 8~20시간 |
| 표·각주·다단 편집 또는 OCR 문제가 많은 PDF | 1~3일 이상 |
| 이미 번역된 Markdown을 검수해 EPUB으로 정리 | 약 30분~2시간 |
| 목차가 심하게 망가진 기존 EPUB 수리 | 약 1~4시간 |

전체 시간은 대체로 원문 준비 10~20%, 초벌 번역 40~50%, 윤문 25~35%, 목차·출판 검증 10~20%로 나뉩니다. 기본적으로 서로 다른 조각을 병렬 처리합니다. coordinator는 남은 조각과 메모리로 추천 수만 계산하며, 구독 사용량 제한과 실제 동시 에이전트 슬롯을 반영해 작업자를 시작하는 일은 현재 Codex/Claude 에이전트가 담당합니다.

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
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" prepare --epub "..\book-workspace\input\source.epub" --book-key sample_book_ko --book-id sample_book --title-ko "가상 한국어 제목" --author-ko "가상 저자" --translation-platform gpt
~~~

PDF:

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" prepare-pdf --pdf "..\book-workspace\input\source.pdf" --book-key sample_book_ko --book-id sample_book --title-ko "가상 한국어 제목" --author-ko "가상 저자" --translation-platform gpt
~~~

Markdown 또는 TXT:

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" prepare-md --markdown "..\book-workspace\input\source.md" --book-key sample_book_ko --book-id sample_book --title-ko "가상 한국어 제목" --author-ko "가상 저자" --translation-platform gpt
~~~

EPUB 원본이면 준비 단계가 원본 목차를 source_outline.json으로 보존합니다. 이것은 **근거 자료**이지 자동으로 승인된 최종 목차가 아닙니다. 새 작업은 품질을 위해 기본 8,000자 단위로 나누며 `--max-chars`로 조정할 수 있습니다. 또한 `03_outputs/translations/assets/<book_id>/glossary_ko.md` 용어집·문체표를 만들며, 병렬 작업자는 배치 중 이를 읽기 전용으로 사용합니다.

Claude Code에서 작업하면 `--translation-platform claude`를 사용합니다. 모델을 직접 지정하려면 `--translation-model`, 기본 `high`보다 높이려면 `--translation-effort xhigh` 또는 `max`를 추가합니다.

### 2. 진행 상태와 다음 조각을 확인합니다

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" status --book-key sample_book_ko --book-id sample_book
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" next --book-key sample_book_ko --book-id sample_book --stage draft --workers auto
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" next --book-key sample_book_ko --book-id sample_book --stage polished --workers auto
~~~

`--workers auto`는 남은 조각 수와 사용 가능한 메모리로 보수적인 병렬 작업 수를 권장하며, 실행 환경의 동시 작업 한도를 직접 확인할 수 없으므로 자동값은 최대 4개로 제한합니다. 이 명령은 작업 목록만 만들고 모델을 직접 시작하지 않습니다. 실제 작업자 수는 현재 Claude/Codex 환경이 허용하는 동시 에이전트 수보다 커서는 안 됩니다. 필요하면 `--workers 2`처럼 직접 낮추거나 높일 수 있습니다. 기존 `--limit`은 호환용입니다.

하네스가 만든 source, draft_ko, polished_ko 조각의 역할을 섞지 마세요. 원문 조각은 초벌 번역의 근거이고, 윤문은 `draft_ko → polished_ko` 순서로만 진행합니다. polished_ko가 최종 합본의 입력입니다.

### 3. 목차 계약을 검토해 작성합니다

원본 목차, 인쇄 목차, 실제로 확보된 번역 범위를 함께 확인한 뒤 `--project-root`로 지정한 비공개 작업 공간의 다음 위치에 계약을 둡니다.

    03_outputs/translations/assets/sample_book/toc_contract.json

형식은 examples/sample_book/toc_contract.json과 schemas/toc_contract.schema.json을 참고하세요. 원본에 장이 적혀 있어도 본문 파일이 없다면 새 내용을 만들지 말고 계약의 `source_scope`에 SOURCE GAP을 기록합니다.

~~~json
"source_scope": {
  "status": "SOURCE GAP",
  "missing": ["제4장 원문 본문 없음"],
  "evidence": "인쇄 목차에는 있으나 제공된 원문 범위에는 없음"
}
~~~

### 4. 번역 조각의 품질을 먼저 검사합니다

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" qc --book-key sample_book_ko --book-id sample_book --stage polished
~~~

### 5. 합본을 만들고 구조를 검사합니다

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" combine --book-key sample_book_ko --book-id sample_book --stage polished --output-format md
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" structure-qc --book-key sample_book_ko --book-id sample_book --label before_build
~~~

Markdown을 최종 산출물로 골랐다면 구조 검사가 통과한 뒤 여기서 끝납니다.

### 6. EPUB을 선택한 경우에만 전자책을 만듭니다

~~~powershell
python scripts/epub_agent_translation_harness.py --project-root "..\book-workspace" combine --book-key sample_book_ko --book-id sample_book --stage polished --output-format epub
~~~

EPUB 선택도 내부 검증 근거인 combined Markdown을 보존합니다. 이 명령은 검토된 `toc_contract.json`이 없거나 실제 목차가 계약과 다르면 실패합니다. `--output-format md`를 실행해도 과거에 만든 EPUB을 자동 삭제하지 않으므로, 상태 보고에서 “이번 실행에서 EPUB을 다시 만들지 않았다”고 구분해야 합니다.

독립 builder는 고급·호환 작업에서만 사용합니다. 일상 작업에서는 coordinator의 `--output-format`을 권장합니다.

저장소에 포함된 완전한 가상 예제를 바로 빌드하려면 다음 명령을 사용합니다.

~~~powershell
python scripts/build_epubs_from_combined.py --combined-dir examples/sample_book --config-dir examples --output-dir dist --only sample_book_ko
python scripts/validate_epub_toc_contract.py --config-dir examples --output-dir dist --combined-dir examples/sample_book --only sample_book_ko
~~~

### 7. 최종 EPUB을 다시 검증합니다

~~~powershell
python scripts/validate_epub_toc_contract.py --project-root "..\book-workspace" --book-key sample_book_ko --label sample_book_final
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

This repository provides a model-neutral, evidence-first Agent Skill and Python workflow for preparing user-supplied books, translating and polishing them inside a subscribed Codex or Claude Code session, and publishing structurally validated Korean Markdown or EPUB deliverables. A reviewed TOC contract is mandatory for either final format; generated EPUB navigation must match it exactly. The harness records the requested model profile but does not call or verify the active model. No copyrighted books, translations, or generated EPUBs belong in this public repository.

## License

이 저장소 자체는 MIT입니다. [LICENSE](LICENSE)를 보세요. 윤문 원칙의 공개 참고 자료에 대한 저작권·허가 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 보존합니다.
