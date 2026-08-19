# docs-rag-mcp

**로컬 BM25 문서 검색 MCP 서버** — Claude Code 서브에이전트가 팀 문서(마크다운)를
문서 전문 통독 대신 **조각(청크) + `파일:줄범위`** 로 검색하게 한다.

> **TL;DR (EN)** — A fully local, zero-API-key BM25 search MCP server for markdown
> document corpora, built for Claude Code subagents. Korean+English tokenization
> (2-gram), chunk-level results with file:line ranges, confidence signals,
> staleness warnings, query logging with a measured path to hybrid (vector) search.
> No document ever leaves your machine.

## 왜 쓰나

- **컨텍스트 절감** — 에이전트가 스펙 확인을 할 때 문서 전문을 Read 하는 대신 관련 조각만 받는다.
  원 프로젝트(문서 211개 · 421만 자) 스팟 실측(n=1): 질의 1건의 top-3 반환 ≈ 8.5천 자 vs
  해당 문서 전문 2.8만~10.9만 자 — **약 3~13배 절감**. 핵심은 조각이 아니라 `파일:줄범위`다 —
  빗나가도 그 지점부터 부분 Read 로 확대하면 되므로 통독으로 돌아가지 않는다.
- **키 0개 · 반출 0** — 외부 API·임베딩·LLM 호출이 없다. 색인·검색 전부 로컬 계산이라
  법무·계약 문서가 섞인 코퍼스에도 안전하고, 팀원 수만큼 키·설정이 늘지 않는다.
- **한국어+영문 혼합 코퍼스 대응** — 한글 2-gram + 영문/숫자 토큰화. `D-52` · `API-084` 같은
  식별자, 표·코드블록이 많은 개발 문서에서 임베딩보다 정확하다(근거·실측: [DECISIONS.md](DECISIONS.md)).

## 기능

| 기능 | 설명 |
|---|---|
| BM25 검색 | `bm25s` 기반, 점수 0~1 정규화(RRF 대비 이음매) |
| 조각 반환 | 목표 1,800자·최대 3,000자 청크 + 원본 `파일:줄범위` |
| confidence 신호 | `raw_max ∧ coverage` 2축 판정 — `ok`/`low`/`none` 을 결과에 명시(침묵 없음) |
| 색인 낡음 경고 | 원본이 색인보다 새로우면 결과에 경고 자동 표시, 재색인 후엔 색인 세대 자동 재로드 |
| status 강등 | 머리말 마커(`status: stale` 등)로 구버전·폐기 문서 점수 강등(0.35 / 0.15) |
| 용어집 질의 확장 | `glossary.tsv` — 한↔영 동의어를 임베딩 없이 흡수 |
| 질의 로그 | `logs/queries.jsonl` + `log_reader.py` — 하이브리드 전환 조건(재질의율 등)을 **측정**으로 판정 |
| 검증 게이트 | `eval.py` 골든 질의 hit@3(80% 게이트) + 회귀 테스트 141건 |
| 서빙 2종 | MCP stdio 서버(`mcp_server.py`, 서브에이전트용) + 로컬 HTTP UI(`server.py`, 사람용, 루프백 전용) |

## 빠른 시작 (동봉 예제 코퍼스)

Windows (PowerShell) — venv activate 없이 python.exe 직접 호출을 권장:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:DOCS_RAG_ROOT = "$PWD\examples"          # 예제 코퍼스(examples/docs/)로 시작
.\.venv\Scripts\python.exe indexer.py --stats  # 색인 생성 + 왕복 검증
.\.venv\Scripts\python.exe search.py "요청 한도 정책" --no-log
.\.venv\Scripts\python.exe eval.py             # 골든 게이트
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

macOS / Linux:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

export DOCS_RAG_ROOT="$PWD/examples"
./.venv/bin/python indexer.py --stats
./.venv/bin/python search.py "요청 한도 정책" --no-log
./.venv/bin/python eval.py
./.venv/bin/python -m unittest discover -s tests -t .
```

사람이 브라우저로 보려면: `python server.py` → http://127.0.0.1:8765 (루프백 전용 바인딩).

## Claude Code 에 연결

`.mcp.json.example` 을 프로젝트의 `.mcp.json` 으로 복사하고 **절대경로 3곳**을 자기 머신에
맞게 바꾼다(`.mcp.json` 은 머신 로컬 — 커밋하지 않는다):

```json
{
  "mcpServers": {
    "docs-rag": {
      "command": "C:\\path\\to\\docs-rag-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\docs-rag-mcp\\mcp_server.py"],
      "env": { "DOCS_RAG_ROOT": "C:\\path\\to\\your-docs-root" }
    }
  }
}
```

툴 2개가 노출된다: `search_docs(query, category, k, caller_id)` · `list_doc_categories()`.

**서브에이전트에 붙이기** — 에이전트 정의(`.claude/agents/*.md`) frontmatter 의 `tools` 에
추가하고, 본문에 발동 트리거를 한 줄 넣는다:

```yaml
tools: Read, Grep, Glob, mcp__docs-rag__search_docs, mcp__docs-rag__list_doc_categories
```

> 스펙·결정·문서 근거가 필요하면 Grep/Read 전에 `search_docs` 를 먼저 호출한다.
> 결과의 `파일:줄범위` 로 부족하면 그 부분만 Read 로 확대한다(전문 통독 금지).

재질의율 측정을 살리려면 에이전트가 `caller_id` 에 세션/워커별로 안정적인 값을 넘기게 한다.

## 자기 코퍼스에 붙이기 (4단계)

1. **`DOCS_RAG_ROOT`** 를 문서 루트로 지정하고 `config.py` 의 `SOURCE_DIRS` 를 실제 하위 폴더로 바꾼다.
2. **`config.py` `CATEGORY_RULES`** 를 자기 경로 명명에 맞춘다 — 어느 규칙에도 안 걸린 문서는
   `etc` 로 떨어져 카테고리 필터로 못 찾는다(색인 후 `list_doc_categories` 로 분포 확인).
3. **`glossary.tsv`** 를 자기 도메인 동의어로 교체한다(등재 기준은 파일 머리말 — grep 실측 후 등재).
4. **`eval.py` `GOLDEN`** 을 자기 코퍼스의 실제 질의·기대 문서로 교체한다(동봉분은 예제용.
   결과를 보기 전에 기대값을 먼저 고정할 것 — 파일 머리말의 무결성 조건 참고).

문서를 고친 뒤에는 재색인한다: `python indexer.py --stats` (검증 절차 전체는
[.claude/skills/rag-reindex/SKILL.md](.claude/skills/rag-reindex/SKILL.md) — Claude Code 스킬로도 동작).

## 하이브리드(벡터) 전환은 언제?

**체감이 아니라 측정으로.** `log_reader.py` 가 질의 로그에서 재질의율·용어 불일치율을 집계하고,
[DECISIONS.md](DECISIONS.md) 의 전환 조건(재질의 30% · 용어 불일치 20% 초과)이 발화할 때만
R-002 를 연다. 인터페이스·점수 정규화·청크·로그 4개 이음매가 미리 심겨 있어 전환 시
`search.py` 내부만 바뀐다(호출 에이전트 무수정). 임베딩은 반환 토큰량을 줄여주지 않는다는
점도 같은 문서에 정리돼 있다.

## 구조

```
mcp_server.py   MCP stdio 서버 (Claude Code 서브에이전트용, stdout 오염 방어 내장)
server.py       로컬 HTTP UI + JSON API (127.0.0.1 전용, Host 헤더 검증)
search.py       검색 코어 — confidence 2축 · staleness · 로그 · CLI
indexer.py      색인 생성 (bm25s + SQLite, 왕복 검증 포함)
chunker.py      헤딩 경계 청킹 + status(stale/폐기) 판정
tokenizer.py    한글 2-gram + 영문/숫자 토큰화 + 용어집 질의 확장
config.py       공개 설정 표면 (문서 루트·카테고리·청킹·가중치)
glossary.tsv    질의 확장 동의어 (탭 구분)
eval.py         골든 질의 검증 게이트 + confidence 하한 측정(--floor)
log_reader.py   질의 로그 판독 — 하이브리드 전환 조건 측정
tests/          회귀 테스트 141건 (합성 코퍼스 기반, 실 색인 불요)
examples/docs/  예제 코퍼스 3문서 (골든·용어집 데모와 연동)
```

## 한계

- BM25 단독이라 **용어가 완전히 다른 의미 검색**은 약하다 — 1차 방어는 용어집, 2차는 호출
  에이전트의 재질의 루프, 최종형은 측정 후 하이브리드(위 참조).
- 전체 재색인 방식(증분 없음) — 수백 문서·수백만 자 규모에서 수 초 수준이라 실용상 문제없다.
- 한국어 형태소 분석 없이 2-gram 휴리스틱 — 개발 문서(식별자·영문 혼합)에 최적화된 트레이드오프다.

## 라이선스

[MIT](LICENSE)
