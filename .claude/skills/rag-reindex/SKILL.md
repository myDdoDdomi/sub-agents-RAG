---
name: rag-reindex
description: sub-agents-RAG 문서 색인을 재생성하고 검증한다. 코퍼스 문서를 추가·수정·삭제한 뒤, 또는 search_docs 결과가 낡아 보일 때 호출한다. 색인 신선도 확인 → 재색인 → 왕복 검증 → 회귀 게이트 → 새 문서 자기검색 스모크까지 한 흐름으로 돌리고 결과를 요약한다. 문서 작성 자체는 하지 않는다(운영 전용).
---

# rag-reindex — 색인 재생성 · 검증

문서를 고친 뒤 색인을 맞추는 운영 절차다. 문서를 쓰거나 고치지 않는다.

작업 폴더: 이 레포 루트 · python: `.venv\Scripts\python.exe` (macOS/Linux: `.venv/bin/python`)

## 절차

### 1. 색인 신선도 확인

```powershell
.\.venv\Scripts\python.exe indexer.py --check
```

재색인하지 않고 "원본 `.md` 중 색인 이후 변경된 것"만 센다. 0건이면 2~3단계를 건너뛰고 4단계로 간다.

### 2. 재색인

```powershell
.\.venv\Scripts\python.exe indexer.py --stats
```

전체 재색인이다(증분 없음). 실패하면 기존 색인은 그대로 남는다 — 출력의 실패 사유를 그대로 보고하고 멈춘다.

`PermissionError [WinError 5]` 가 나면 `chunks.sqlite` 커넥션을 연 프로세스(MCP 서버 등)가 있다는 뜻이다. 그 프로세스를 끄고 다시 돌린다.

### 3. 왕복 검증

2단계 출력에 포함된다(`[검증] 통과 — bm25s idx ↔ sqlite 정합 확인됨`). 실패하면 색인은 교체되지 않는다 — **기준을 낮추지 말고** 실패 표본을 그대로 보고한다.

### 4. 회귀 게이트

```powershell
.\.venv\Scripts\python.exe eval.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

`eval.py` 는 골든 질의 hit@3(게이트 80%), `tests` 는 계약·불변식 회귀다. 둘 중 하나라도 실패하면 통과로 보고하지 않는다.
(골든이 아직 동봉 예제용 그대로면 자기 코퍼스에선 실패가 정상이다 — eval.py 머리말대로 GOLDEN 을 교체한다.)

### 5. 새/변경 문서 자기검색 스모크

2단계 `--stats` 출력에서 문서 수·조각 수 변화를 확인하고, **추가·수정된 문서마다** 그 문서의 고유어로 검색해 실제로 잡히는지 본다.

```powershell
.\.venv\Scripts\python.exe search.py "<그 문서에만 나오는 어구>" --k 5 --no-log
```

`--no-log` 는 반드시 붙인다 — 이 단계는 검증용 합성 질의라 `logs/queries.jsonl`(R-001 하이브리드 전환 판정의 근거, DECISIONS.md)에 섞이면 안 된다.

두 가지를 반드시 본다.

- **카테고리** — 결과의 `category` 가 `etc` 면 경로가 `config.CATEGORY_RULES` 의 어느 규약에도 안 걸렸다는 뜻이다. `etc` 로 떨어지면 카테고리 필터로 영영 안 잡힌다. 경로를 규약에 맞추거나, 규약 추가를 사람에게 제안한다.
- **status** — 폐기·구버전 문서인데 `status=current` 로 나오면 머리말 마커가 규약과 안 맞는 것이다. 강등(`stale` 0.35 · `deprecated` 0.15)이 안 걸리면 폐기 문서가 정본과 같은 무게로 검색된다.

### 6. 요약 보고

표로 낸다.

| 항목 | 값 |
|---|---|
| 문서 수 / 조각 수 | 변화 전 → 후 |
| 왕복 검증 | 통과 / 실패(사유) |
| `eval.py` | N/M |
| `tests` | N passed / 실패 목록 |
| 자기검색 스모크 | 문서별 잡힘 여부 · 카테고리 · status |
| 이상 | `etc` 로 떨어진 문서 · status 오판 문서 |

## 하지 않는 것

- 문서 작성·수정 (이 스킬 범위 밖)
- `eval.py` 판정 기준·골든 질의 변경 — 게이트가 실패하면 게이트를 고치지 말고 원인을 보고한다
- `config.py`·`glossary.tsv` 수정 — 카테고리 규약·용어집 변경은 사람에게 제안만 한다
- 커밋·푸시
