"""BM25 검색 — 조각 조회 + status 가중 + 카테고리 필터 + 신뢰도 판정.

핵심 문제(2026-08-13 실측, 1차 배치가 인계): 이 코퍼스는 한글 2-gram 토크나이저를
쓰기 때문에 **완전히 무관한 질의도 BM25 raw 점수가 0 이 아니다**. "블록체인 채굴
난이도 조정" 같은 도메인 밖 한글 질의도 2-gram 조각(블록/록체/체인/채굴/...) 이
코퍼스 어딘가와 우연히 겹쳐 raw 3~5점대가 나온다. 순수 라틴 미등장 토큰만 0.0 이다.

그래서 `score > 0` 필터로는 노이즈를 못 거르고, 단순 max 정규화(그 질의 안에서
최고점을 1.0 으로 만드는 것)는 노이즈만 있는 질의도 1.0 으로 보이게 만들어 호출
에이전트가 쓰레기를 만점으로 오인하게 한다. 그래서 여기서는:
  · `Hit.score` — 정규화된(0~1) 값. 질의 *안에서의* 상대 순위용. RRF 융합 대비 표면은
    그대로 유지한다(DECISIONS.md R-001 이음매 2).
  · `SearchResult.raw_max`/`raw_scores` — status 가중 **전** 원점수. 질의 *간* 신뢰도
    판정용(하한 임계 RAW_MAX_FLOOR, 실측 근거는 아래 주석). status 가중은 랭킹 선호지
    "이게 진짜 매칭이냐"의 증거가 아니므로 하한 판정에서 뺀다.
  · **M6(qa 독립검증 후속, 2026-08-14) — `confidence` 는 raw_max 단일축이 아니다.**
    `raw_max >= RAW_MAX_FLOOR` **그리고** `coverage >= COVERAGE_FLOOR`(질의 토큰 중
    raw_max 를 낸 hit 문서에 실제 기여한 토큰 비율, `_compute_coverage()` 참고) 2축
    AND 판정이다(근거는 아래 RAW_MAX_FLOOR 주석의 H1 절 — 1축만으로는 짧은 질의에서
    coverage 가 원리적으로 판별력을 잃어 규칙이 raw_max 단일축으로 조용히 퇴화한다).
    `coverage` 도 raw_max 와 마찬가지로 **반환된 hits 기준**이라 위 B2 의 k·category
    의존성을 그대로 상속한다 — 카테고리를 좁히거나 k 를 바꾸면 raw_max 를 내는 hit
    자체가 바뀌어 coverage 도 함께 바뀔 수 있다.

B2(code-reviewer, 2026-08-13) — `raw_max`/`confidence` 는 **반환된 hits 기준**이라 `k`·
`category` 에 의존한다(동작은 바꾸지 않는다 — 아래가 왜 이게 맞는지):
  · **category 의존**: 카테고리를 좁히면 그 카테고리 안에서 가장 약한 매칭이 raw_max 가
    된다 — 실측 "D-52 결정" `category="all"` raw_max=7.631(ok) 인데 `category="spec"`
    로 좁히면 0.542(low). 필터 전 풀 기준으로 계산하면 "약한 필터 결과에 ok 를 붙이는"
    거꾸로 된 상황이 되어 이번 배치가 막으려던 실패로 되돌아간다 — 그래서 반환된 hits
    기준이 맞다.
  · **k 의존**: k 가 커지면 약한 후보까지 hits 에 들어와 raw_max 가 오히려 커질 수 있다
    (weighted 정렬이라 stale 조각이 큰 k 에서 진입하는 경우 등) — 실측 "HiFi 대기목록"
    k=5 → 11.351, k=10 → 15.162.
  · 그래서 **질의 간 raw_max/confidence 비교는 같은 k·category 안에서만 유효**하다.
    `RAW_MAX_FLOOR`(아래) 는 `eval.py --floor` 의 캘리브레이션 조건인 `category="all",
    k=5` 에서만 실측됐다 — 다른 축(좁은 카테고리·큰 k)에서는 경고("low"/"none")가
    이 문서의 실측치보다 더 자주 뜰 수 있다.

Windows/MCP 함정:
  · 이 모듈은 stdout 에 절대 print 하지 않는다 — MCP stdio 서버가 이 함수들을 부를
    때 stdout 은 프로토콜 채널이다. 라이브러리 코드의 진단 출력은 전부
    `sys.stderr.write(...)` 로만 한다(print() 자체를 쓰지 않아 실수로도 stdout 에
    안 걸리게). CLI 진입점(`if __name__`)만 print 를 쓰고 거기서 stdout/stderr 를
    utf-8 로 재설정한다.

스레드 안전:
  · bm25s 인덱스는 모듈 수준에서 **필요 시(지문 불일치 시)마다 재로드 후 캐시**한다
    (N5-4: "1회 지연 로드 후 캐시"였던 이전 문장은 2차 배치 이후 **거짓**이 됐다 —
    프로세스 생애주기 동안 여러 번 재로드될 수 있는 설계로 바뀌었다. 이 문장을 이번
    배치에서 개정한다). retrieve() 자체는 웜 상태에서 호출당 0.1~0.3ms 수준으로 빠르다.
    L5(code-reviewer 재검증): README 의 "콜드 39.014ms"는 **로드만이 아니라 이
    프로세스의 첫 search() 호출 전체**(import 이후 load+retrieve+sqlite)다 — 분해
    실측(2026-08-13, 3회) 결과 그중 load() 자체가 89~92%(30~53ms 왕복 범위)를 차지해
    대부분이긴 하나, "40ms=load 단독"으로 읽지 않도록 명시한다.
  · **F-1(4차 배치, Sev2) — 캐시 무효화.** 위 캐시는 원래 프로세스 수명 내내 무효화
    경로가 없었다: sqlite 는 호출마다 새로 열어 **신 색인**을 읽는데 bm25s 캐시는
    **구 색인**에 머물러 있으면, bm25s(구 idx 공간)로 순위를 매기고 sqlite(신 idx
    공간)에서 본문을 꺼내는 조합이 어긋난다. 신 조각 수가 구 조각 수보다 **작을 때만**
    `n_missing` 가드가 이 어긋남을 감지했고(A2), 조각 수가 늘거나 같으면 `n_missing=0`
    이라 전 채널 무음으로 틀린 결과가 나갔다 — 문서를 고치면 조각 수는 보통 늘거나
    비슷하므로 이게 실사용 경로에서 정확히 밟히는 국면이었다(재현: 2026-08-13, dev-lead).
  · **해소책 — 신선도 지문 + 단일 스냅샷 발행(`_RetrieverSnapshot`, N5).** 호출마다
    sqlite 에서 `meta.created_at`+`meta.n_chunks` 지문을 읽는다(`_read_fingerprint`).
    이 지문이 캐시된 `_snapshot.fingerprint` 와 **다르면**(M3: `!=` 비교 — `>` 를 쓰면
    "백업에서 색인을 되돌렸을 때"(지문이 시간상 과거로 되돌아가는 경우) 무효화가 안
    걸린다. `n_chunks` 단독 지문으로 단순화하지도 않는다 — "2371→2371"처럼 조각 수는
    그대로인 채 문구만 고친 재색인을 `n_chunks` 만으로는 못 잡는다) bm25s 를 재로드한다.
    `_retriever`/`_n_chunks` 를 별개 전역으로 두지 않고 `_RetrieverSnapshot`
    (retriever+n_chunks+fingerprint) 불변 객체 하나로 묶어 전역 `_snapshot` 하나에만
    발행한다. **이 설계가 성립하는 근거 4가지(N5, 명문화):**
      1. **CPython 의 전역 이름 1개 읽기/쓰기는 GIL 하에서 원자적**이다 — 이게 "락 없는
         단일 전역 읽기 1회"가 안전한 유일한 근거다. 이 레포 `.venv` 는 cp312(GIL 있는
         표준 빌드)다. **free-threaded 빌드(PEP 703, `--disable-gil`)로 옮기면 이
         원자성 가정 자체가 깨지므로 반드시 재검토한다 `(확인 필요)`.**
      2. 스냅샷 객체는 **불변**(`frozen=True`)이고 **완전히 구성된 뒤에만** 전역에
         대입된다(`_snapshot = new_snap` 한 줄 — 부분 초기화 객체를 먼저 발행하고
         필드를 나중에 채우는 패턴은 절대 쓰지 않는다).
      3. 리더는 **로컬 변수로 받은 스냅샷 하나만** 쓰고, 그 호출 안에서 전역을 다시
         읽지 않는다 — 재읽기 1회가 바로 찢어진 조합을 부활시키는 지점이므로(다시
         읽는 순간 다른 스레드의 재발행과 경합), `_open_index()`(아래) 를 포함해 이
         모듈의 모든 리더는 이 규율을 지킨다.
      4. (구 A1 의 "n_chunks 먼저, retriever 마지막" 2단 발행 순서 규약은 이 단일
         객체 발행이 대체하는 **일반형(강화)**이다 — 두 개의 별도 전역을 유지하는
         설계였다면 그 순서 규약을 그대로 지켜야 했다.)
    재로드는 락 안에서 하고 진입 후 지문을 재확인해(더블체크) 중복 로드를 막으며,
    **재로드 전에 `_snapshot = None` 으로 캐시를 먼저 비운다.**
  · **M1(2차 배치) — 지문↔bm25 아티팩트 TOCTOU 교차검증.** 지문은 sqlite `meta` 에서
    읽지만 재로드 대상은 `INDEX_DIR/bm25` **디렉터리**다 — 서로 다른 두 파일시스템
    개체다. "지문 읽기 → (그 사이 재색인 스왑 발생) → bm25 로드"로 인터리빙되면, 로드된
    bm25 아티팩트는 **N+1세대**인데 캐시에는 **N세대 지문**이 찍히는 조합이 생길 수
    있다 — 다음 호출은 지문이 다시 안 맞아 자가 치유하지만, **그 한 번은 조용히
    틀린다**(F-1 과 같은 모양의 결함). 그래서 `_load_and_verify()` 가 로드 직후
    `int(retriever.scores["num_docs"]) == int(지문의 n_chunks)` 를 교차검증한다 —
    스냅샷 안의 "지문"과 "실제 로드된 아티팩트"라는 두 출처를 실제로 묶는 유일한
    오라클이다. 불일치하면 그 스냅샷을 폐기하고 1회 재시도, 그래도 다르면 예외.
  · **M2 — 지문 읽기에 무음 폴백 금지.** `meta` 조회 실패를 삼키고 "일단 캐시 사용"
    으로 폴백하면 meta 가 깨진 색인에서 무효화가 **영구히** 꺼진다. 그래서
    `_read_fingerprint()` 의 실패는 전파한다(호출자의 예외 처리 경로로 흡수). 또한
    `created_at` 키가 결측이라 지문이 `(None, ...)` 로 나오는 경우 — 이건 예외는
    아니지만 "신뢰할 수 없는 지문"이다 — 캐시가 우연히 같은 `(None, ...)` 을 들고
    있어도 **캐시를 신뢰하지 않고 항상 재로드를 시도**한다(`_fingerprint_trustworthy`,
    안전한 방향 폴백).
  · **N1(2차 배치, 최우선) — 재로드를 커넥션 밖으로.** 1차 배치는 "커넥션을 먼저 열고
    재로드 내내 그대로 쥐고 있는" 설계였다 — sqlite 핸들을 30~53ms 동안 쥔 채 bm25s
    를 로드했다. win32 에서 열린 커넥션은 `indexer.py` 3단 rename 을 막으므로(아래
    "재색인 잠금"), 이는 **"재색인 직후"라는 가장 흔한 시점에 재색인 실패 확률을
    올리는** 구조였다. `_open_index()`(아래) 는 순서를 바꿨다:
      1) conn 개방(읽기전용, N3) → 지문 조회 → 스냅샷 1회 원자 읽기
      2) 지문 일치 → **그 커넥션 그대로 반환**(핫패스, 커넥션 1개 — 기존 설계 의도 유지)
      3) 지문 불일치 → **커넥션을 먼저 닫고**(재로드 중 sqlite 핸들 0개) 락 안에서
         로드+M1 교차검증 → **커넥션을 다시 열어 지문 재확인** → 새 스냅샷 지문과
         같으면 진행 / 다르면 1회 재시도 / 그래도 다르면 예외
    핫패스 커넥션 수는 그대로 1개고, 추가 connect 는 재색인당 최대 2회(재확인용)뿐이다.
    **불변식은 "`_fetch_rows` 를 수행하는 그 커넥션에서 지문이 확인됐을 것"**이다 —
    지문을 읽은 커넥션과 본문을 읽는 커넥션이 다르면 그 사이 스왑이 끼어들 여지가
    다시 생긴다. **이 보장이 SQLite 트랜잭션 격리가 아니라 OS 파일 핸들이 rename 을
    가로질러 옛 파일을 계속 가리키게(또는 win32 에서 rename 자체를 막게) 하는 데서
    온다는 점이 중요하다 — `indexer.py` 가 나중에 in-place 쓰기로 바뀌면 이 가정이
    깨지므로 그때는 명시적 read 트랜잭션이 필요해진다(N1).**
  · **커넥션 생존 구간 — N1 이후 재로드 중엔 0개.** N1 채택 전(1차 배치)에는 재로드가
    걸리는 호출에서 그 커넥션이 열린 채로 bm25s 로드(30~53ms)를 거쳤다. win32 에서
    열린 sqlite 커넥션은 `indexer.py` 3단 rename 을 `PermissionError [WinError 5]`
    로 막는다(README "재색인 잠금" 실측) — 이건 1차 배치가 감수한 **의도된 트레이드
    오프**였다(안전한 실패: 기존 색인 보존 + indexer 비정상 종료). N1 채택 후에는
    재로드 자체가 커넥션을 안 쥐고 있으므로(2단계에서 close 후 로드) 이 창이 구조적
    으로 사라진다 — 재로드 중 win32 rename 충돌 가능성이 1차 설계 대비 원리적으로
    줄어든다(정량 비교는 4차 배치 2차 보고 참고).
  · **N6 — 로드 실패 짧은 재시도 + 실패 캐시.** N1 설계는 "재색인 직후 = 부분 스왑이
    가장 흔한 시점"에 로드를 걸리게 만든다. `_reload_with_retry()` 는 로드 실패
    (`bm25s.BM25.load()` 예외 또는 M1 교차검증 실패) 시 100~200ms 대기 후 1회만 더
    시도한다. 그래도 실패하면 stderr 에 "재색인 중일 수 있다 — 잠시 후 재시도" 안내와
    함께 예외를 전파하고, **같은 지문에 대한 실패를 아주 짧게(0.2초) 캐시**해 대기
    스레드 N개가 락을 순차로 얻어 각자 또 실패해 트레이스백이 N번 쌓이는 것을
    완화한다. **실패 캐시는 지문이 같을 때만 적용된다** — 색인이 그 사이 또 바뀌면
    (지문이 다르면) 캐시를 무시하고 새로 시도한다(실패 캐시가 성공을 가리면 안 된다).
  · **N7 — 재로드 실패(하드) vs 원본 stale(소프트) 비대칭은 의도적이다.** 재로드 실패는
    예외로 전파하고(하드), 원본이 색인보다 새로운 것은 경고만 붙인다(소프트) — 둘 다
    옳지만 이유가 다르다: 전자는 **증명 가능하게 정합이 깨진 상태**(bm25 아티팩트를
    못 읽었거나 M1 교차검증에 실패 — 내용 자체가 틀렸거나 못 믿을 상태)이고, 후자는
    **정합은 맞고 다만 최신이 아닐 뿐**(색인 내부는 자기 정합적이고 내용도 맞지만
    원본이 그 뒤에 바뀌었을 뿐)이다. 이 구분이 사라지면 다음 배치가 "일관성 없다"며
    한쪽을 다른 쪽에 맞추려 들 수 있어 여기 명시한다.
  · SQLite 는 **호출마다 새 커넥션을 열고 finally 로 닫는다** — 커넥션은 **읽기 전용
    모드**(N3, `_get_conn` 참고)로 연다. FastAPI/MCP 는 다른 스레드에서 이 함수를
    부를 수 있는데, sqlite3 커넥션은 기본적으로 만든 스레드에서만 쓸 수 있다
    (`check_same_thread`). B5(code-reviewer 재검증, 2026-08-13): 이 구조를 유지하는
    진짜 근거는 "커넥션이 저렴해서"가 아니다 — 웜 지연을 구간별로 분해해보면(2026-08-13
    실측) `retrieve` 0.2ms · `tokenize_query` 0.1ms · **sqlite connect+조회+close
    2.0ms** 로 **웜 지연의 대부분(약 85%)이 커넥션 쪽**이다. 그런데도 유지하는 이유는
    둘: ① 절대값이 2ms 라 이 코퍼스 규모(2,371 조각)에서는 최적화가 급하지 않다
    ② **Windows 재색인 3단 rename 안전성** — `indexer.py` 의 재색인이 `index`
    디렉터리를 통째로 rename·rmtree 하는데, 스레드에 커넥션을 장기 캐시하면 열린 파일
    핸들이 win32 에서 그 삭제를 막을 수 있다. `threading.local()` 캐시로 바꾸는 건 이
    재색인 스왑과의 상호작용을 먼저 검증해야 하는 후속 과제로 남긴다.
  · 후보 조각의 메타데이터 조회는 `idx IN (...)` 한 번으로 끝낸다(N+1 금지) — `search()`
    의 사용자용 오버페치는 최대 100개(MAX_K=20 기준)라 SQLite 바인드 파라미터 상한과
    무관하다. L1(code-reviewer, 2026-08-13): 단, 진단 전용 `diagnose_status_weight_rank`
    는 전체 코퍼스를 한 번에 넘기므로 `_fetch_rows` 자체가 900개 단위로 나눠 쿼리한다
    (SQLite 기본 변수 상한 보수적 가정 — 아래 `_fetch_rows` 참고).
  · **N4 — `search()`와 `diagnose_status_weight_rank()`는 `_open_index()` 하나를
    공유한다.** 예전에는 두 함수가 각자 `_get_retriever()`+별도 `_get_conn()` 조합을
    따로 구현해 같은 버그 클래스를 두 벌로 안고 있었다(한쪽만 고쳐지고 한쪽은 남는
    위험). `diagnose_status_weight_rank` 는 `eval.py` 회귀 게이트 #3(stale 강등
    판정)의 근거이기도 해서, 캐시가 낡으면 게이트가 엉뚱한 색인으로 PASS 를 낼 수
    있었다 — 지금은 한 곳(`_open_index`)만 고치면 둘 다 같이 고쳐진다.
  · **원본 .md stale 감지(4차 배치 B, M0 로 glob 기반 재설계)는 별개 축이다** — F-1 이
    "메모리 캐시 vs 디스크 색인" 정합이라면, 이건 "디스크 색인 vs 원본 파일" 신선도다.
    `check_staleness`/`_get_staleness` 절 참고(아래).
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bm25s

import config
import tokenizer

# ── 신뢰도 하한 (실측값 — eval.py --floor, 2026-08-13 / 2026-08-13 dev-lead 재검증 반영) ──
# [1차 측정, 15건 표본] 도메인 안 15건(골든10+임의5) 최소 6.946("FN-041") vs 도메인 밖
# 7건 최대 6.103("축구 국가대표 명단 발표", 예외 1건 "라면 끓이는 법 황금 레시피" 12.041
# 은 2-gram 잡음이 아니라 인프라·배포 로드맵 문서 "5. 핵심 레시피 2종" 섹션과의 진짜
# 어휘 충돌 — DECISIONS.md R-001 인지 한계). 이 표본으로 6.5를 골랐었다.
#
# [dev-lead 재검증, 2026-08-13 — 표본을 도메인 안 20·밖 11로 확대해 반증]
# **"도메인 안 최소 6.946"은 15건 표본의 산물이었다 — 확대 표본의 실제 최소는 1.880
# ("SSE", 토큰 1개).** 근본 원인: raw_max 는 질의 **토큰 수에 비례**해 커진다(BM25 는
# 매칭 텀마다 점수를 누적하므로) — 그래서 짧은 ID/약어 질의는 정확히 맞아도 절대값이
# 낮고, 긴 도메인 밖 질의는 완전히 틀려도 절대값이 높게 나올 수 있다. 절대 임계 하나로는
# 원리적으로 완전 분리가 안 된다.
#   실측 오탐/미탐(임계 6.5 기준, 확대 표본 n=20/11):
#     · 도메인 안 오탐(사실 안인데 low/none): 2/20 — "SSE"(1.880) · "REQ-701"(6.453)
#     · 도메인 밖 미탐(사실 밖인데 ok): 1/11 — "라면 끓이는 법 황금 레시피"(12.041, 위 예외)
#     · 도메인 밖 정탐(low/none 으로 정확히 잡음): 10/11
#   **토큰수 정규화(raw_max/토큰수)도 대안으로 시험**했다 — 아래는 code-reviewer 재검증
#   (2026-08-13)이 사실로 정정한 결론이다(이전 버전은 "정규화가 절대값보다 분리력이
#   나쁘다"고 잘못 적었었다 — 인용한 지표(겹침구간 낀 개수)로는 오히려 반대였다):
#     방식          | 겹침구간(양 끝값)      | 그 구간에 낀 도메인 안 개수 | 최적임계 총오류
#     절대값 raw_max | 1.880 ~ 12.041        | 14건                       | 2 (t≈6.453 에서 오탐1·미탐1)
#     토큰수 정규화   | 0.870 ~ 1.720         | 9건                        | 2 (t≈0.870~0.900 에서 오탐0~1·미탐1~2)
#   겹침구간에 낀 개수만 보면 정규화가 더 좁아 보이지만, **분류 성능(최적 임계에서의
#   총 오류)은 둘 다 동률(2건)이다** — 정규화가 실제로 우세하지 않다. 그래서 전환할
#   이득이 없고, 오히려 질의 토큰 수라는 축에 결과가 커플링돼 구현·설명만 복잡해진다
#   → **채택하지 않는다**(raw_max 절대값 방식 유지 — 단순하고 토큰수에 안 얽힌다).
#
# **2-1(qa 독립검증, 2026-08-14) — 위 6.5 임계는 폐기됐다. 근본 원인은 임계값이 아니라
# 축이었다.** 위 캘리브레이션은 "도메인 안이냐 밖이냐" 축에서 측정됐는데, `confidence`
# 소비자가 실제로 기대하는 것은 "정답을 찾았나" 축이다 — 같은 raw_max 절대값 하나로는
# 이 두 축을 원리적으로 동시에 만족시킬 수 없다(실증: `라면 끓이는 법 황금 레시피`가
# raw_max=12.041 로 옛 임계 6.5를 가뿐히 넘겨 "ok" 를 받았지만 실제로는 인프라 로드맵
# 문서의 "5. 핵심 레시피 2종" 섹션과의 2-gram 우연 매칭이었다 — 라벨셋 38질의 중 오답
# 14건의 절반(7건)이 여전히 "ok" 로 판정됐다).
#
# **[폐기 — 아래 H1 절 참고] 2-1 이 채택했던 `RAW_MAX_FLOOR=1.5 ∧ COVERAGE_FLOOR=0.75`
# 는 42건 표본에서만 좋아 보였을 뿐, qa 독립검증(2026-08-14, 다음 배치)이 확대 라벨셋
# (56건)으로 재측정하자 **옛 6.5 단일축 규칙보다도 나쁜 규칙(총오류 13 > 11)**임이
# 드러나 폐기됐다.** 이유: `n_query_tokens==1` 이면 그 토큰이 raw_max 문서에 반드시
# 기여했으므로 `coverage ≡ 1.0` 이 되어 — 1토큰 질의에서 coverage 축은 **원리적으로
# 판별력이 0**이다(수학적 필연, 확률적 관찰이 아니다). 그 결과 1.5 라는 낮은 raw_max
# 하한 하나로 규칙이 조용히 퇴화해, "라면"·"고양이"·"날씨"·"주식" 같은 1토큰 도메인
# 밖 질의가 전부 raw_max 2.6~4.4·coverage=1.0 으로 "ok" 를 받았다(42건 표본엔 이런
# 1~2토큰 도메인 밖 질의가 아예 없어 이 결함이 안 보였다 — **표본 편향**, 아래 H1 참고).
#
# **H1(dev-lead 재판정, 2026-08-14, qa 6건 결함 후속) — 라벨셋을 56건으로 확대해
# (38+ADVERSARIAL4+짧은도메인밖12+짧은도메인안신규2) 6종 규칙을 재격자탐색 +
# LOO + plateau 로 재판정, 폐기된 1.5/0.75 를 대체한다:**
#     confidence = "none"  if raw_max <= 0
#                = "ok"    if raw_max >= RAW_MAX_FLOOR and coverage >= COVERAGE_FLOOR
#                = "low"   otherwise
#   규칙의 *형태*(2축 AND)는 안 바뀐다 — coverage 축 자체는 무효가 아니라 여전히 유용
#   하다(아래 표 참고, 옛 규칙 대비 오탐을 9→1로 줄인다). 문제는 축이 아니라 **raw_max
#   축의 하한값이 너무 낮게 잡혔던 것**이다.
#
#   실측 비교표(56건 확대 표본, 총오류=미탐+오탐 정의는 브리프 고정: 미탐=정답인데 ok
#   아님, 오탐=오답인데 ok):
#     규칙                                              총오류  미탐  오탐  LOO   plateau(축별)
#     옛 raw_max>=6.5 (production 정의)                    11     2     9    10~12   raw_max축 좁음
#     폐기 raw_max>=1.5 ∧ coverage>=0.75(2-1 채택값)        13     1    12    (고정값,재적합대상아님) —
#     참고 per_tok>=0.711 ∧ coverage>=0.764                12     1    11    —       —
#     참고 coverage>=0.764 단독                             14     1    13    —       —
#     참고 계단형(n<=2:raw>=5.441 / n>2:raw>=5.945) ∧ cov    3     2     1    (미실행,아래 사유)  —
#     참고 선형형 raw>=a+b·n_tok ∧ cov(격자탐색 최적 b=0)      3     2     1    (미실행,아래 사유)  —
#     **채택 raw_max>=6.2 ∧ coverage>=0.76**                 3     2     1     3~8    raw축±3.9%·cov축 0%(아래 설명)
#   길이조건부 계단형·선형형은 42/56건 격자탐색에서 **선형형의 최적 기울기 b 가 0 으로
#   수렴**했다 — 즉 "길이에 비례해 raw 하한을 키운다"는 가정 자체가 이 표본에서는 이득이
#   없다(raw_max 하한을 충분히 올리고 coverage 축을 병행하면 길이 조건 없이도 동일
#   최소총오류에 도달한다). 파라미터가 늘면(계단형·선형형 3개 vs 채택안 2개) 그만큼
#   과적합 위험도 커지므로, **in-sample 에서 더 나은 규칙이 아니면(동률로는 부족)
#   3파라미터 규칙을 채택하지 않는다**(브리프 §H1(3)-4 명시 기준) — 그래서 LOO 는 이
#   둘에 대해 별도로 안 돌렸다(동률인 채로는 채택 후보가 될 수 없어 재적합 비용을 쓸
#   이유가 없다).
#
#   **coverage 축 plateau 가 0%(단일점 0.764)인 이유** — 데이터 두 점이 정확히 그 경계를
#   만든다: `라면 레시피`(2토큰, coverage=0.750, 오답 — 반드시 배제) 와 `웨이브1 킬게이트
#   경계`(coverage=0.778, 정답 — 반드시 포함) 사이 midpoint 가 0.764다. 이는 우연한
#   단일점이 아니라 **두 실측값 사이의 진짜 경계**이므로(다른 임계 후보였다면 이 경계
#   자체가 이동했을 것) 사전선언값으로 0.76(반올림, `(0.750, 0.778]` 구간 안)을 택한다
#   — 0.75(기존값)는 `라면 레시피`(coverage 정확히 0.750)를 `>=` 비교로 통과시켜버려
#   그 자체가 리뷰가 지적한 결함 사례였다.
#   **raw_max 축 plateau [5.945, 6.425](±3.9%, 중심 6.185)** — 하한은 `스키마 명세
#   문서`(raw=5.786, 오답, coverage=1.0 이라 coverage 축으로도 못 거른다 — 반드시
#   배제) 직후, 상한은 `REQ-701`(raw=6.453, 정답 — 반드시 포함) 직전이다. 6.2(반올림,
#   plateau 안)를 사전선언값으로 택한다.
#
#   **LOO 3~8** — 옛 규칙 LOO(10~12)의 **최선(10)보다도 새 규칙의 최악(8)이 낮다** →
#   tie-break 정책과 무관하게 우위가 뒤집히지 않는다.
#
#   **남는 오류 3건, 전부 원리적으로 이 두 축(raw_max·coverage)만으로는 분리 불가**:
#     · 미탐 `SSE`(1토큰, raw=1.880, coverage=1.0, 정답) — **이 배치의 핵심 트레이드오프.**
#       `SSE`(정답)와 `라면`(raw=3.226)·`고양이`(2.754)·`날씨`(3.133)·`주식`(2.618)·
#       `test`(2.449)·`42`(3.284)·`커피`(4.429)·`산책`(2.592, 전부 오답) 는 **1토큰
#       질의라 coverage 가 전부 정확히 1.0 으로 동일**하고, raw_max 값도 서로 뒤섞여
#       있다(SSE=1.880 이 이 무리 중 가장 낮다 — 오답 8건 전부보다 낮다). raw_max 로도
#       coverage 로도 이 쌍을 원리적으로 분리할 수 없다 — **오답 8건을 막으려면 SSE
#       하나를 반드시 희생해야 한다.** 오탐(무관한데 "ok") 이 미탐(관련 있는데 경고)
#       보다 비용이 크다는 원칙(위 근거 그대로) 에 따라 **SSE 를 희생하고 오답 8건을
#       막는 쪽을 택했다** — `search_docs("SSE")` 는 이제 raw_max·coverage 는 여전히
#       hits 에 노출되므로(경고 문구에 두 값을 함께 보여준다) 호출 에이전트가 낮은
#       확신도 경고를 보고도 결과 자체(Phase2 문서, top-1 은 계속 정답)는 그대로 받는다
#       — confidence 는 하드 드롭이 아니므로 결과 자체를 잃는 것은 아니다.
#     · 미탐 `정합감사 리포트`(coverage=0.700 < 0.76, 폐기된 1.5/0.75 채택값에서도 이미
#       미탐이었다 — 새로 생긴 오류가 아니다, coverage 축 자체의 알려진 한계).
#     · 오탐 `테이블 설계 문서 명세`(raw=9.635·coverage=1.00, 짧고 흔한 어휘라 두 축을
#       모두 통과한다 — 폐기된 1.5/0.75 에서도 이미 오탐이었다).
#
#   **표본 한계(n=56)** — 여전히 순위 비교용이지 임계 확정용이 아니다. 실 트래픽에는
#   성패 라벨이 없어 실제 오탐률을 이 표본으로 재현할 수 없다(§2-6 참고). 오답 30건의
#   구성이 실 트래픽 오답 분포와 같다는 근거도 없다. **1~2토큰 도메인 밖 질의 12건은
#   전부 dev-lead 가 sqlite LIKE 사전 스캔으로 정답 부재를 확인한 것**이며(구현 보고서
#   §H1 방법 절 참고) 이 계층이 42건 표본에 전무했다는 사실 자체가, 표본을 늘릴 때마다
#   전에 안 보이던 축이 드러날 수 있음을 시사한다 — **이 56건도 종점이 아니라 재확대
#   가능한 중간 지점**으로 남긴다.
#
# **`eval.py --floor` 영향(수정 금지 파일이라 코드는 못 고친다, 보고만) — 값이 6.5→6.2 로
# 바뀌면서 그 스크립트의 "--- 오탐/미탐 요약 (기준: search.RAW_MAX_FLOOR = X) ---" 라벨은
# raw_max 축 하나만 말하게 되어 불완전해졌다(coverage 축은 그 라벨에 안 드러난다). 알려진
# 한계로 명시한다 — `eval.py --floor` 자체는 예외 없이 완주한다(구현 보고서에서 확인).**
RAW_MAX_FLOOR = 6.2

# H1(dev-lead 재판정, 2026-08-14) — coverage 축 하한. 정의·근거는 위 RAW_MAX_FLOOR
# 주석 참고. `_compute_coverage()` 가 산출하는 coverage(0~1)와 비교한다.
COVERAGE_FLOOR = 0.76

# ── 응답 총량 상한 (실측값 — dev-lead, 2026-08-13, 3차 배치 L14) ──────────────
# 질의 15건(골든10+임의5, category="all") 의 k=1..10/20 별 format_result() 출력
# 문자수 실측 표에서 도출한 확정값이다 — 새로 값을 만들지 않고 그대로 쓴다.
#   1) DEFAULT_K=5 경로에서는 상한이 발동하지 않아야 한다 — k=5 실측 최대 항목합
#      11,191자("meal_warning") → 하한 12,000 초과 조건을 만족해야 발동 안 함이 보장된다.
#   2) MCP 상한 k=10 의 실측 중앙값 15,567자 바로 위 — k=10 요청의 절반은 그대로 통과.
#   3) k=10 실측 최대 24,238자를 34% 억제(발동 시 평균 7.7건 유지).
#   4) 본부 상시 로드 컨텍스트 총예산 76,000자(scripts/context-budget.py TOTAL_BUDGET)
#      대비 검색 1회 약 21%.
#
# [주의 — code-reviewer 지적, dev-lead 반영 2026-08-13] 이 상수는 하드 상한이 아니다 —
# "본문을 실을지"만 판정하는 기준이고, 생략된 항목의 헤더 줄·말미 안내(예산 초과 안내·
# Read 에스컬레이션 문구)는 예산 판정과 무관하게 항상 실리므로 **실제 출력은 이 상한을
# 넘을 수 있다.** 실측(질의 15건, category="all"): k=10 최대 16,527자(+3.3%) · k=20
# 최대 19,502자(+21.9%). code-reviewer 가 헤딩경로가 가장 긴 조각들로 구성한 최악
# 시뮬레이션에서는 k=20 +35.4%까지 관측됐다 — "응답이 이 값을 절대 넘지 않는다"는
# 하드 상한으로 오인하지 말 것.
RESPONSE_CHAR_BUDGET = 16_000

LOG_PATH = config.RAG_ROOT / "logs" / "queries.jsonl"

# 2-6(qa 독립검증, 2026-08-14) — 로그 스키마 v2 + 상관 ID. 기존 339줄(v1)은 이 필드들이
# 아예 없다 — 판독기(log_reader.py)가 `schema_version` 키의 **부재**로 v1 을 식별한다
# (기존 줄을 변조하지 않는다는 제약, 브리프 §2-6-1). `_LOG_RUN_ID`/`_LOG_PID` 는
# **프로세스당 1회**만 만든다(모듈 import 시점 — 이 프로세스가 켜져 있는 동안 고정).
# 한계(브리프가 명시적으로 요구한 정직한 고지): MCP 는 stdio 라 클라이언트당 보통 1
# 프로세스지만, **한 Claude Code 세션의 서브에이전트 N개가 같은 MCP 서버 프로세스를
# 공유**하면 run_id 로도 서브에이전트가 안 갈린다 — 그래서 아래 `caller_id`(선택적,
# 호출자가 자발적으로 넘기는 값)를 별도로 열어둔다. 호출자 협조가 없으면 서브에이전트
# 귀속은 이 배치로도 여전히 불가능하다 — "해결됐다"로 쓰지 않는다.
_LOG_SCHEMA_VERSION = 2
_LOG_RUN_ID = uuid.uuid4().hex
_LOG_PID = os.getpid()
_log_seq_lock = threading.Lock()
# L9(qa 독립검증 후속, 2026-08-14 — 정정) — 이 카운터는 "실제로 쓴 줄 수"가 아니라
# **소비된(=`_next_log_seq()` 가 호출된) 횟수**다. `_log_query()` 안에서 이 함수는
# entry dict 를 만들 때(파일 쓰기 *전*) 호출되므로, 그 뒤 `LOG_PATH.open()`/`f.write()`
# 가 실패해도(권한 오류·디스크 가득 참 등, 아래 `_log_query` 의 `except Exception` 이
# 흡수하는 바로 그 경우) 카운터는 이미 증가한 뒤다 — 실패한 시도도 `seq` 를 소비한다.
_log_seq_counter = 0


def _next_log_seq() -> int:
    """2-6 — 프로세스 내 로그 줄 순서 카운터(비용 0 — 정수 증가 1회 + 락). 여러 스레드가
    동시에 `_log_query()` 를 불러도 각 줄이 서로 다른 `seq` 를 받는다(락은 이 짧은 증가
    구간에서만 잡는다 — 실제 파일 I/O 는 락 밖). L9 — 이 값은 "실제 기록 성공" 이 아니라
    "호출 순서"를 보장한다(위 모듈 상수 주석 참고 — 쓰기 실패 시에도 소비된다).
    """
    global _log_seq_counter
    with _log_seq_lock:
        _log_seq_counter += 1
        return _log_seq_counter


# M3(qa 독립검증 후속, 2026-08-14) — caller_id 길이·문자셋 상한. 자세한 근거는
# `_sanitize_caller_id()` docstring 참고.
_CALLER_ID_MAX_LEN = 128
_CALLER_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9._:-]*$")
_caller_id_warned = False  # 비적합 caller_id 경고를 프로세스당 1회로 제한(매 호출 도배 방지)


def _sanitize_caller_id(caller_id: str | None) -> str | None:
    """M3(qa 독립검증 후속, 2026-08-14) — `caller_id` 는 호출자(MCP 툴 인자·HTTP 쿼리
    파라미터)가 임의 문자열로 넘길 수 있는 선택적 필드라 길이 상한이 없었다.
    `caller_id="B"*200000` 이면 로그 한 줄이 **200,778바이트**가 된다(이 로그는 R-001
    판정의 유일한 근거이고 로테이션이 없다 — `json.dumps` 이스케이프가 줄 위조는
    막지만 줄 크기 폭주는 막지 않는다).

    길이를 `_CALLER_ID_MAX_LEN`(128자)로 캡해 초과분을 절단한다. 캡 후에도 허용
    문자셋(`[A-Za-z0-9._:-]`) 밖의 문자가 있거나 원래 길이가 캡을 넘었으면 stderr 로
    프로세스당 1회만 경고한다(무음 처리 금지, 매 호출 도배는 방지) — 검색 자체를
    막지는 않는다(하드 드롭 반대 원칙과 동일한 태도: caller_id 는 어디까지나 로그용
    부가 정보라 그 값이 이상하다고 검색을 거부할 이유는 없다). 문자셋 위반 문자를
    임의로 치환·제거하지는 않는다 — `json.dumps` 가 이스케이프를 책임지므로 그 값
    그대로(캡만 적용해) 남겨야 사후 분석 시 원인 추적이 쉽다.
    """
    if not caller_id:
        return caller_id
    truncated = caller_id[:_CALLER_ID_MAX_LEN]
    nonconforming = len(caller_id) > _CALLER_ID_MAX_LEN or not _CALLER_ID_CHARSET_RE.match(truncated)
    if nonconforming:
        global _caller_id_warned
        if not _caller_id_warned:
            _caller_id_warned = True
            sys.stderr.write(
                f"[search] 경고 — caller_id 가 길이 상한({_CALLER_ID_MAX_LEN}자) 또는 "
                f"허용 문자셋([A-Za-z0-9._:-])을 벗어났다(원래 길이={len(caller_id)}) — "
                f"로그에는 {_CALLER_ID_MAX_LEN}자로 절단해 남긴다.\n"
            )
    return truncated


def _cached_index_created_at() -> tuple[str | None, str]:
    """2-6 — 토큰 0개 조기 반환처럼 색인을 아예 안 여는 경로에서도 `index_created_at` 을
    최대한 채운다(브리프 요구: "그 마저도 없으면 null 로 두되 출처를 구분"). 색인을 새로
    열지 않고 **이미 캐시된 스냅샷**(F-1 `_snapshot`, 전역 1회 읽기 — 비용 0)의 지문만
    본다 — 그래서 이 값은 "지금 이 호출이 실제로 검색에 쓴 색인"이 아니라 "이 프로세스가
    마지막으로 확인한 색인"일 수 있다. 그 차이를 `index_created_at_source` 로 구분해
    로그에 함께 남긴다("live"=이번 호출이 `_open_index()` 로 직접 연 색인의 지문 /
    "cached_snapshot"=조기 반환이라 안 열고 캐시만 봄 / "unavailable"=캐시조차 없음,
    이 프로세스의 첫 호출이 토큰 0개인 경우).
    """
    snap = _snapshot  # 락 없는 단일 전역 읽기(N5 와 동일 원칙) — 이후 snap 로컬만 쓴다
    if snap is not None and snap.fingerprint[0] is not None:
        return snap.fingerprint[0], "cached_snapshot"
    return None, "unavailable"


@dataclass
class Hit:
    idx: int
    score: float  # 0~1 정규화 (질의 내 상대값 — raw 를 섞지 않는다)
    rel: str
    heading_path: str
    start_line: int
    end_line: int
    category: str
    status: str
    text: str


@dataclass
class SearchResult:
    hits: list[Hit]
    category_used: str
    fallback_used: bool
    category_requested: str = "all"
    category_valid: bool = True
    raw_max: float = 0.0
    raw_scores: list[float] = field(default_factory=list)  # hits 와 같은 순서·길이
    confidence: str = "none"  # "ok" | "low" | "none"
    # A2(code-reviewer, 2026-08-13): bm25s ↔ sqlite idx 정합이 깨져 후보 조각을 못 찾은
    # 건수. indexer.py 가 "가장 조용히 고장나는 방식"이라 부르며 왕복검증까지 넣어 막은
    # 실패 모드를 런타임에서 무음으로 재생산하지 않기 위한 필드다(기본값 0 — 정상 시 항상 0).
    n_missing: int = 0
    # B1(code-reviewer, 2026-08-13): 이번 호출이 실제로 오버페치한 후보 수(=fetch). 카테고리
    # 폴백 문구가 "이 안에서만 못 찾았다"를 정확히 말할 수 있게 노출한다. 토큰 0개 조기
    # 반환 경로는 bm25s 를 아예 안 불러서 fetch 개념이 없다 — 기본값 0 유지.
    fetch: int = 0
    # 4차 배치 B(원본 stale 감지) — R-001 이음매 1(기본값 있는 필드만 추가)을 지킨다.
    # index_stale=True 면 원본 .md 가 색인보다 새롭다(결과를 막지 않는다 — 경고만).
    # 이름을 hit 단위 status 값("stale"/"deprecated", 문서 자체 마커)과 구분하려고
    # "index_" 접두를 붙였다 — 이건 색인 전체의 신선도지 개별 문서 상태가 아니다.
    index_stale: bool = False
    index_stale_docs: int = 0  # 색인 이후 mtime 이 갱신된 것으로 잡힌 문서 수(알려진 rel 목록 기준)
    index_stale_detail: str = ""  # 빈 문자열=기본 문구 사용, 있으면 대체 설명(예: 파일 수 변경만)
    # 2차 배치 — StalenessCheck.ok 를 노출한다(O1). dev-lead 판정: "검색 표면에서
    # index_stale=False 로 조용히 수렴하는 건 유지해도 되지만(드문 예외에 매 검색
    # 노이즈를 띄우지 않는 게 맞다), '판정 불가' 자체는 어딘가에서 관측 가능해야
    # 한다"(그래야 무음 실패가 아니다) — 그 창구가 이 필드다. 기본값 True(=판정 시도가
    # 성공했다는 뜻 — 아직 stale 판정을 아예 안 한 조기 반환 경로에서도 "실패했다"는
    # 오신호를 내지 않기 위한 안전한 기본값).
    index_stale_ok: bool = True
    # 2-4(qa 독립검증, 2026-08-14, 신규 필드) — index_stale_ok=False 가 "판정 불가
    # (failed)"와 "판정 보류(pending, 백그라운드 계산 중)"를 겸해 두 상태를 구분할 수
    # 없었다(qa 실측: 두 상태의 /search 응답이 키·값 완전 동일했다). True 면 "판정
    # 보류"(아직 이 색인 세대에 대해 계산되지 않음 — StalenessCheck.pending 그대로
    # 전달), False(기본값)면 index_stale_ok 가 그대로 성공/실패를 뜻한다(기존 의미
    # 불변 — 이 필드는 False 안에서 한 축을 더 나눌 뿐이다). index_stale_ok=True 일 때는
    # 항상 False(펜딩은 ok=False 일 때만 발생할 수 있다).
    index_stale_pending: bool = False
    # 2-1(qa 독립검증, 2026-08-14, 신규 필드 3종) — confidence 판정에 실제로 쓰인 coverage
    # 신호를 호출자에게도 노출한다(질의 로그 §2-6 과 같은 값 — 단일 계산을 재사용해 로그와
    # 응답이 어긋나지 않게 한다, `_compute_coverage()` 참고). 토큰 0개 조기 반환·hits 0건
    # 경로에서는 전부 기본값(0.0/0/0)이다.
    coverage: float = 0.0  # 질의 토큰 중 raw_max 를 낸 hit 문서에 기여(raw>0)한 토큰의 비율
    n_query_tokens: int = 0  # tokenizer.tokenize_query() 가 낸 전체 토큰 수(중복 포함, 용어집 확장분 포함)
    n_matched_tokens: int = 0  # 그중 raw_max 문서에 실제로 기여한 토큰 수


# ── bm25s 인덱스 지연 로드 + 캐시 (F-1 수정 — 신선도 지문 + 단일 스냅샷 발행) ──────
_retriever_lock = threading.Lock()


@dataclass(frozen=True)
class _RetrieverSnapshot:
    """retriever+n_chunks+발행 시점 지문을 묶은 불변 객체. 전역 `_snapshot` 하나에만 발행해
    "단일 전역 읽기 1회"로 fast path 를 원자적으로 만든다(모듈 docstring 스레드 안전 절 참고
    — `_retriever`/`_n_chunks` 를 따로 읽고 비교하면 그 사이 다른 스레드의 재발행과 겹쳐
    찢어진 조합을 볼 수 있다).
    """

    retriever: bm25s.BM25
    n_chunks: int
    fingerprint: tuple[str | None, str | None]  # (meta.created_at, meta.n_chunks) 원문 그대로


_snapshot: _RetrieverSnapshot | None = None
_reload_count = 0  # O1 — 프로세스 생애주기 누적 재로드 횟수(관측용, /health 가 노출)
_last_load_failure: tuple[float, tuple, BaseException] | None = None  # N6: (실패시각, 지문, 예외)
_LOAD_FAILURE_CACHE_TTL = 0.2  # N6 — 초. 실패 캐시가 "성공을 가리면 안 되므로" 아주 짧게 잡았다.
_untrustworthy_fingerprint_warned = False  # M2 — 신뢰 불가 지문 경고를 프로세스당 1회로 제한
_nonoccurrence_array_warned = False  # M2(qa 독립검증 후속) — coverage 무음오답 경고를 프로세스당 1회로 제한


def _read_fingerprint(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """meta.created_at + meta.n_chunks 로 신선도 지문을 만든다. 호출자가 이미 연 커넥션을
    그대로 받는다(N1 — 커넥션 수를 늘리지 않는다). 이 값이 캐시된 스냅샷의 지문과
    **다르면**(M3: 동등 비교, `_open_index` 참고) bm25s 를 재로드한다.

    M2 — 조회 실패는 여기서 삼키지 않고 그대로 전파한다(무음 폴백 금지). 반환값 자체가
    `(None, ...)` 인 경우(meta.created_at 결측)는 예외는 아니지만 "신뢰할 수 없는 지문"
    이다 — 판정은 `_fingerprint_trustworthy()` 가 한다.
    """
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('created_at', 'n_chunks')"
    ).fetchall()
    d = {r["key"]: r["value"] for r in rows}
    return (d.get("created_at"), d.get("n_chunks"))


def _fingerprint_trustworthy(fingerprint: tuple[str | None, str | None]) -> bool:
    """M2 — `created_at` 이 없으면(meta 결측·깨진 색인) 지문을 신뢰하지 않는다. 캐시가
    우연히 같은 `(None, ...)` 을 들고 있어도 그걸 히트로 치지 않고 항상 재로드를
    시도한다 — "무효화가 영구히 꺼지는" 위험한 폴백 대신 안전한 방향(매 호출 재시도 +
    결국 로드가 실패하면 stderr 경고와 함께 예외)을 택한다.

    순수 판정 함수다(부작용 없음) — `_open_index()` 가 fast path 판정과
    `_reload_with_retry(..., allow_cache_hit=...)` 인자 계산 두 곳에서 각각 호출한다.
    신뢰 불가 상태의 1회성 stderr 경고는 `_warn_untrustworthy_fingerprint_once()` 가
    별도로 맡는다(이 함수를 부작용 없는 술어로 유지하기 위함).
    """
    return fingerprint[0] is not None


def _warn_untrustworthy_fingerprint_once() -> None:
    """M2 — `created_at` 결측(신뢰 불가 지문)을 프로세스당 1회만 stderr 에 경고한다(매
    호출 도배 방지 — `_open_index()` 가 신뢰 불가 지문을 만날 때마다 부르지만 실제
    출력은 처음 한 번뿐이다).
    """
    global _untrustworthy_fingerprint_warned
    if not _untrustworthy_fingerprint_warned:
        _untrustworthy_fingerprint_warned = True
        sys.stderr.write(
            "[search] 경고 — 색인 meta.created_at 이 결측이라 지문을 신뢰할 수 없다. "
            "안전한 방향으로 매 호출 재로드한다(캐시 히트를 안 씀 — 호출당 로드 비용 "
            "추가됨). python indexer.py 로 재색인을 권장한다.\n"
        )


def _warn_nonoccurrence_array_once() -> None:
    """M2(qa 독립검증 후속, 2026-08-14) — `retriever.nonoccurrence_array` 가 있는(=
    `idf_method` 가 robertson 계열 등) 상태에서 `_coverage_full_array()` 를 타면
    coverage 가 조용히 1.0 으로 무너진다(무음 오답, `_coverage_full_array` docstring
    참고) — 프로세스당 1회만 stderr 에 경고한다(매 호출 도배 방지).
    """
    global _nonoccurrence_array_warned
    if not _nonoccurrence_array_warned:
        _nonoccurrence_array_warned = True
        sys.stderr.write(
            "[search] 경고 — 이 색인의 bm25s idf_method 가 nonoccurrence_array 를 쓴다"
            "(robertson 계열 등으로 추정). _coverage_full_array() 폴백 경로가 이 조건에서"
            " coverage 를 조용히 1.0 으로 잘못 낼 수 있다(무음 오답) — 결과의 confidence"
            "/coverage 값을 신뢰하기 전에 idf_method 를 확인하라.\n"
        )


def _load_and_verify(fingerprint: tuple[str | None, str | None]) -> _RetrieverSnapshot:
    """bm25s 아티팩트를 로드하고 **M1 교차검증**(로드된 `num_docs` 가 지문의 `n_chunks`
    와 같은지)까지 통과해야 스냅샷을 반환한다. 락은 호출자(`_reload_with_retry`)가 쥔다.

    지문은 sqlite `meta` 에서, 로드 대상은 `INDEX_DIR/bm25` 디렉터리에서 온다 — 서로
    다른 개체라 "지문 읽기 → (그 사이 재색인 스왑) → bm25 로드"로 인터리빙되면 N+1세대
    아티팩트에 N세대 지문이 찍힌 스냅샷이 만들어질 수 있다(TOCTOU). 이 교차검증이 그
    찢어짐을 잡는 유일한 오라클이다 — 불일치하면 이 스냅샷은 폐기(예외)하고, 호출자가
    재시도 여부를 결정한다.
    """
    r = bm25s.BM25.load(str(config.INDEX_DIR / "bm25"))
    n_from_retriever = int(r.scores["num_docs"])
    expected_raw = fingerprint[1]
    if expected_raw is not None:
        try:
            expected_n = int(expected_raw)
        except (TypeError, ValueError):
            expected_n = None
        if expected_n is not None and n_from_retriever != expected_n:
            raise RuntimeError(
                f"M1 교차검증 실패 — 로드된 bm25 아티팩트의 num_docs={n_from_retriever} "
                f"가 sqlite meta.n_chunks={expected_n} 와 다르다(지문 읽기와 bm25 로드 "
                f"사이에 재색인 스왑이 끼어든 TOCTOU 로 추정). 안전하지 않은 스냅샷이라 "
                f"폐기한다."
            )
    return _RetrieverSnapshot(retriever=r, n_chunks=n_from_retriever, fingerprint=fingerprint)


def _reload_with_retry(
    fingerprint: tuple[str | None, str | None], *, allow_cache_hit: bool = True
) -> _RetrieverSnapshot:
    """락 안에서 로드+M1 교차검증. 실패 시 100~200ms 대기 후 1회만 더 시도한다(N6).
    그래도 실패하면 stderr 에 "재색인 중일 수 있다" 안내와 함께 예외를 전파한다.

    같은 지문에 대해 최근(`_LOAD_FAILURE_CACHE_TTL` 이내) 이미 실패했으면 재시도 없이
    그 예외를 즉시 재사용한다 — 대기 스레드 N개가 이 락을 순차로 얻어 각자 재시도해
    stderr 트레이스백이 N번 쌓이는 것을 완화한다(N6). **실패 캐시는 지문이 같을 때만
    적용된다** — 다른 지문(=색인이 그 사이 또 바뀜)이면 캐시를 무시하고 새로 시도한다
    (실패 캐시가 성공을 가리면 안 된다는 요건).

    M2(코드리뷰) — `allow_cache_hit=False` 면 아래 더블체크 조기 반환(락 대기 중 다른
    스레드가 이미 이 지문으로 로드해 뒀으면 재사용하는 최적화)을 건너뛰고 항상 재로드
    한다. `_open_index()` 가 `_fingerprint_trustworthy(fingerprint)` 가 False 일 때(=
    `created_at` 결측 — 비정상 색인) 이 값을 넘긴다: 신뢰 불가 지문은 캐시가 우연히
    같은 값을 들고 있어도 히트로 치면 안 되기 때문이다 — 안 그러면 `_open_index()` 의
    fast path 는 막혀도 이 더블체크가 사실상 같은 캐시를 그대로 돌려줘 "항상 재로드를
    시도한다"는 모듈 docstring 의 약속이 무력화된다(코드리뷰 지적).
    """
    global _snapshot, _last_load_failure, _reload_count
    with _retriever_lock:
        snap = _snapshot  # 더블체크 — 락 대기 중 다른 스레드가 이미 이 지문으로 로드했을 수 있음
        if allow_cache_hit and snap is not None and snap.fingerprint == fingerprint:
            return snap

        if _last_load_failure is not None:
            fail_time, fail_fp, fail_exc = _last_load_failure
            if fail_fp == fingerprint and (time.monotonic() - fail_time) < _LOAD_FAILURE_CACHE_TTL:
                raise fail_exc

        # 재로드 전에 캐시를 먼저 비운다 — load() 가 실패해도 구 retriever 가 살아남아
        # 조용히 어긋난 결과를 내는 일이 없게 한다.
        _snapshot = None

        try:
            new_snap = _load_and_verify(fingerprint)
        except Exception:
            time.sleep(0.15)  # N6: 100~200ms
            try:
                new_snap = _load_and_verify(fingerprint)
            except Exception as e2:
                sys.stderr.write(
                    f"[search] 색인 로드 실패(2회 시도) — 재색인 중일 수 있다, 잠시 후 "
                    f"재시도하라: {type(e2).__name__}: {e2}\n"
                )
                _last_load_failure = (time.monotonic(), fingerprint, e2)
                raise

        _last_load_failure = None  # 성공 — 실패 캐시 해제
        _snapshot = new_snap  # 단일 전역 재발행(원자적) — 불변 객체라 부분관측 불가
        _reload_count += 1
        sys.stderr.write("[search] 색인 세대 변경 감지 — 재로드\n")  # O1
        return new_snap


def _open_index() -> tuple[sqlite3.Connection, bm25s.BM25, int, tuple[str | None, str | None]]:
    """검색에 필요한 커넥션 하나 + retriever + n_chunks + 지문을 연다.

    N4 — `search()`와 `diagnose_status_weight_rank()`가 공유하는 단일 진입점(예전엔
    두 함수가 각자 구현해 같은 버그 클래스를 두 벌로 안고 있었다).

    호출자는 반환된 conn 을 **반드시 finally 에서 닫아야 한다.** N1 순서(모듈 docstring
    "스레드 안전" 절 참고): conn 개방 → 지문 조회 → 스냅샷 1회 원자 읽기 → 일치하면 그
    커넥션 그대로 반환(핫패스) → 불일치하면 커넥션을 먼저 닫고 락 안에서 재로드 →
    커넥션을 다시 열어 지문 재확인 → 일치하면 진행, 불일치하면 1회 재시도, 그래도
    불일치하면 예외.

    M1(코드리뷰) — 이 함수 어디서든 예외가 나면 그 시점에 "현재 소유 중인" conn(있다면)
    을 반드시 닫고 나서 예외를 다시 던진다(아래 전체를 감싸는 try/except). `conn` 은
    매 재개방 지점마다 재바인딩되므로 except 시점의 `conn` 은 항상 최신 소유물을
    가리킨다. 이미 닫힌 conn 에 `close()` 를 또 불러도 안전하다(no-op — sqlite3 명세 +
    `tests/test_conn_lifecycle.py` 가 1회 확인) — 그래서 "이미 닫아 둔 뒤 아직 다시 안
    연 윈도우"(`_reload_with_retry()` 호출 구간처럼)에서 예외가 나도 이 catch-all 이
    안전하게 동작한다.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _get_conn()
        fingerprint = _read_fingerprint(conn)  # 실패 시 예외 전파(M2)

        trustworthy = _fingerprint_trustworthy(fingerprint)
        if trustworthy:
            snap = _snapshot  # 락 없는 단일 전역 읽기(N5) — 이후 snap 로컬만 쓰고 전역을 다시 안 읽는다
            if snap is not None and snap.fingerprint == fingerprint:
                return conn, snap.retriever, snap.n_chunks, fingerprint
        else:
            _warn_untrustworthy_fingerprint_once()  # M2

        # 지문 불일치(또는 신뢰 불가, M2) → 재로드 필요. N1: 재로드 전에 커넥션을 닫는다
        # (재로드 중 sqlite 핸들을 0개로 유지 — win32 재색인 rename 과 충돌하지 않도록).
        conn.close()

        # M2 — allow_cache_hit=trustworthy: 신뢰 불가 지문이면 _reload_with_retry() 의
        # 더블체크 조기 반환도 건너뛰어 매 호출 실제로 재로드한다.
        new_snap = _reload_with_retry(fingerprint, allow_cache_hit=trustworthy)
        conn = _get_conn()
        fp_confirm = _read_fingerprint(conn)
        if fp_confirm == new_snap.fingerprint:
            return conn, new_snap.retriever, new_snap.n_chunks, fp_confirm

        # N1: 커넥션을 다시 열어 지문을 재확인했더니 또 바뀌어 있음 — 1회만 재시도.
        conn.close()
        new_snap2 = _reload_with_retry(fp_confirm, allow_cache_hit=_fingerprint_trustworthy(fp_confirm))
        conn = _get_conn()
        fp_confirm2 = _read_fingerprint(conn)
        if fp_confirm2 == new_snap2.fingerprint:
            return conn, new_snap2.retriever, new_snap2.n_chunks, fp_confirm2

        conn.close()
        raise RuntimeError(
            f"색인이 계속 교체되고 있어 안정된 스냅샷을 얻지 못했다(커넥션 재확인 2회 모두 "
            f"지문 불일치) — 마지막 관측 지문={fp_confirm2}. 재색인이 비정상적으로 빈번하거나 "
            f"진행 중일 수 있다 — 잠시 후 재시도하라."
        )
    except BaseException:
        if conn is not None:
            conn.close()
        raise


def cache_status() -> dict:
    """O1 — `/health` 등 관측 표면이 쓰는 인트로스펙션. 이번 결함(F-1)은 "전 채널
    무음"이었는데, 수정 후에도 캐시가 실제로 무효화됐는지 볼 창구가 없으면 같은 유형의
    문제가 재발해도 아무도 못 본다. 현재 캐시된 지문과 누적 재로드 횟수를 디스크(sqlite
    meta) 값과 나란히 노출해, 둘이 계속 어긋나 있으면(예: 재로드가 전혀 안 일어남)
    운영자가 알아챌 수 있게 한다. 락 없이 스냅샷을 1회 읽어 반환한다(다른 스레드가 막
    갱신 중이면 근소하게 부정확할 수 있으나 관측용이라 허용한다 — 판정용 fast path 와
    달리 여기는 원자성이 중요하지 않다).
    """
    snap = _snapshot
    return {
        "cached_fingerprint_created_at": snap.fingerprint[0] if snap is not None else None,
        "cached_fingerprint_n_chunks": snap.fingerprint[1] if snap is not None else None,
        "cached_n_chunks": snap.n_chunks if snap is not None else None,
        "reload_count": _reload_count,
    }


def index_files_status() -> tuple[bool, Path]:
    """색인 파일 3종(bm25 디렉터리·bm25 canary·sqlite)이 모두 있는지 확인한다.

    O5(2차 배치) — `mcp_server._index_missing_message()` 와 `server._index_files()`
    가 공유하는 단일 진실 공급원이다(부분 스왑 노출 판단을 두 표면이 다르게 하던 것을
    통일 — MCP 쪽은 원래 bm25 디렉터리·sqlite 존재만 봤고 HTTP 쪽만 canary 를 봤다).
    canary 는 `bm25s.BM25.save()` 가 가장 마지막에 쓰는 `params.index.json` 이다
    (server.py 기존 실측 근거 그대로 재사용 — data/indices/indptr → vocab → params
    순으로 저장하므로 부분 저장·부분 rename 스왑이든 가장 먼저 빠질 파일이다).
    """
    bm25_dir = config.INDEX_DIR / "bm25"
    bm25_params = bm25_dir / "params.index.json"
    sqlite_path = config.INDEX_DIR / "chunks.sqlite"
    ok = bm25_dir.exists() and bm25_params.exists() and sqlite_path.exists()
    return ok, sqlite_path


def _get_conn() -> sqlite3.Connection:
    """N3(2차 배치) — 검색 경로는 읽기 전용으로 연다. `sqlite3.connect()` 기본 모드는
    경로가 없으면 **빈 DB 를 새로 만든다** — `index/` 는 있고 `chunks.sqlite` 만 없는
    부분 실패 잔재 상태에서 이 기본 동작이 0바이트 유령 DB 를 만들면
    `index_files_status()`(위) 가 "색인 있음"으로 오판한다. 지문 조회가 매 호출
    최상단으로 올라간 이번 설계는 이 경로를 훨씬 자주 밟으므로 `mode=ro` URI 로 아예
    생성을 막는다 — 파일이 없으면 `sqlite3.OperationalError` 가 나고, 다른 로드 실패와
    동일하게 호출자에게 전파된다(검색 경로는 원래도 쓰기가 필요 없다).
    """
    uri = Path(config.INDEX_DIR / "chunks.sqlite").as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# L1(code-reviewer, 2026-08-13): search() 의 사용자용 오버페치는 최대 100개라 원래
# "한 번에 끝낸다"가 맞았지만, diagnose_status_weight_rank() 가 전체 코퍼스(2,371개,
# 앞으로 색인이 커지면 더)를 통째로 넘긴다 — SQLite 기본 바인드 변수 상한(오래된 빌드
# 기준 999)을 보수적으로 피하려고 900개씩 나눠 쿼리한다.
_FETCH_ROWS_BATCH = 900


def _fetch_rows(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    """idx 목록의 조각 메타데이터를 가져온다(바인드 변수 상한을 피해 배치 단위로 나눔)."""
    if not ids:
        return {}
    out: dict[int, sqlite3.Row] = {}
    for i in range(0, len(ids), _FETCH_ROWS_BATCH):
        batch = ids[i : i + _FETCH_ROWS_BATCH]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT idx, rel, chunk_no, heading_path, text, start_line, end_line, "
            f"category, status FROM chunks WHERE idx IN ({placeholders})",
            batch,
        ).fetchall()
        out.update({row["idx"]: row for row in rows})
    return out


# ── 원본 .md stale 감지 (4차 배치 B — F-1 과 별개 축, M0 로 glob 기반 재설계) ─────
# F-1 이 "메모리 캐시(bm25s) vs 디스크 색인(sqlite)" 정합이라면, 이건 "디스크 색인 vs
# 원본 .md 파일" 신선도다: 원본을 고치고 재색인을 깜빡하면 색인은 내부적으로 정합돼
# 있지만(F-1 대상 아님) 내용 자체가 낡아 있다 — 결과가 "틀린" 게 아니라 "구식"이므로
# 결과를 막지 않고 경고만 붙인다(요건 B-2).
#
# **M0(2차 배치, 최우선 머지 불가 조건) — glob 기반으로 되돌렸다.** 1차 배치는 "sqlite
# `docs.rel` 목록만 stat"(글롭 제거) 방식을 썼는데, 이는 **알려진 목록 밖의 파일을
# 원리적으로 볼 수 없다** — 이미 있던 중첩 폴더 "안"에만 새 문서가 추가되는 경우(실제
# 문서 코퍼스는 대부분 `docs/<영역>/<주제>/*.md` 류 중첩 경로라 이것이
# 가장 흔한 변화 패턴) 완전히 미탐지였다(1차 배치 실측 확인, NTFS 는 조상
# 디렉터리까지 mtime 을 전파하지 않는다). 이건 이 기능의 존재 이유를 무너뜨리는
# 결함이었다.
#
# glob 을 다시 쓸 수 있는 이유: dev-lead 가 애초에 glob 47ms 를 문제로 지목한 전제는
# "그 비용이 **요청 경로에 실린다**"는 것이었다. stale-while-revalidate(아래
# `_get_staleness`) 는 TTL 만료 재계산을 **백그라운드 스레드에서만** 돌려 그 전제를
# 없앤다(1차 배치 실측: TTL 만료 호출도 1.135ms 로 즉시 반환). **백그라운드 스레드에서는
# 57ms 를 써도 웜 지연에 안 실린다.**
#
# **정정(H1, 코드리뷰) — "어떤 검색 호출도 이 함수의 완료를 기다리지 않는다"는 원래 여기
# 적혀 있었지만 거짓이었다.** SWR 캐시가 아예 비어 있는 프로세스의 첫 호출(부트스트랩)은
# `_get_staleness()` 가 **호출자의 conn 을 쥔 채 동기로 `check_staleness()` 를 실행**해
# +57ms 를 블로킹했다 — 그 커넥션이 sqlite 핸들을 쥔 채였고, TTL 캐시 자체도 지문이 아닌
# 경과시간만 봐서 재색인 직후 최대 60초 동안 구세대 판정을 그대로 돌려줬다(M3). 지금은
# 구조가 다르다: `_get_staleness(fingerprint)` 는 스냅샷이 없거나 지문이 안 맞으면(=이
# 색인 세대에 대해 아직 계산된 적이 없음) **절대 기다리지 않고** `_STALENESS_PENDING` 을
# 즉시 반환하며 백그라운드 재계산만 걸어둔다 — 동기 계산 자체가 이 함수에서 사라졌다.
# 그 대신 "프로세스 첫 검색이 pending 만 보고 끝난다"는 새 회귀가 생기므로, `search.py`
# CLI·`server.py`·`mcp_server.py` 세 진입점이 검색을 받기 **전에** `refresh_staleness_now()`
# 로 명시적으로 워밍한다(H1(3), 각 파일 참고) — 이 세 표면은 그래서 기존과 동일한 경고를
# 낸다. 워밍이 아직 안 끝난 아주 좁은 창(스레드 시작 직후~완료 전)에 도착하는 요청만
# pending(`index_stale_ok=False`)을 볼 수 있다는 한계는 남는다.
#
# 실측 근거(dev-lead, 2026-08-13, n=30, 대상 214개 파일):
#   config.source_paths() 자체(glob+is_file 필터+정렬) — 중앙값 46.987ms · p95 68.935ms
#   그 결과 전체 stat() 스윕                              — 중앙값  9.745ms · p95 15.675ms
#
# **한계(O4, 숨기지 않고 명시 — glob 로 되돌려도 남는 축):**
#   · 삭제는 mtime 비교로 직접 못 잡는다(파일이 사라지면 그 파일의 mtime 자체가 없다)
#     — 그래서 **파일 수 비교**(현재 glob 개수 vs `meta.n_docs`) 를 별도 신호로 병행
#     한다. 단 "삭제 1건 + 추가 1건"처럼 개수가 우연히 같아지면 이 신호도 못 잡는다.
#   · mtime **보존** rename(예: git checkout 이 원래 mtime 을 복원하는 경우)은 내용이
#     바뀌어도 mtime 이 안 바뀌면 이 판정 방식 전체가 놓친다.
#   · `config.EXCLUDE_PATTERNS` 와 이 함수의 스캔 대상이 어긋나면(필터가 나중에
#     바뀌면) 오탐/누락 가능성이 있다 — `config.source_paths()` 를 그대로 써서 색인기와
#     항상 같은 필터를 쓰지만, 필터 자체가 의도와 다르면 이 함수도 같이 틀린다.
#   · `meta.created_at` 은 **sqlite 쓰기 시점**(청킹보다 뒤, `indexer._write_sqlite`)
#     이라, 색인이 진행되는 도중에 수정된 파일은 청킹 시점 내용과 다를 수 있는데도
#     "색인 이후 변경 아님"(mtime < created_at) 으로 판정될 수 있다 — 좁은 창이지만
#     원리적 한계다.
#   · 스캔 중 파일이 사라지는 경합(`OSError`)은 best-effort catch 가 정당한 유일한
#     자리다 — 무음으로 넘기지 않고 stderr 1줄은 남긴다(아래 구현).
STALE_CHECK_TTL_SECONDS = 60.0
# 근거: 재색인은 사람/에이전트가 수동으로 트리거하는 드문 이벤트지 연속 자동화 루프가
# 아니다 — 하드 게이트가 아닌 조언성 경고(B-2)라 60초 지연은 충분히 보수적이다.
# **이 값은 실측이 아니라 운영 가정이다**(dev-lead 2차 배치 판정 — SWR 이라 TTL 만료
# 비용이 사실상 0 이고, 재색인 빈도는 운영 패턴에 달려 실측 가능한 근거 자체가 없다).
# 테스트가 override 할 수 있게 모듈 상수로 둔다(값 자체를 정본처럼 인용하지 말 것 —
# `(확인 필요)` 취급 유지).


@dataclass(frozen=True)
class StalenessCheck:
    """원본 .md 파일이 색인(meta.created_at·meta.n_docs)보다 새로운지 1회성으로 판정한
    결과.

    `search.py` 내부 캐시 계층(`_get_staleness`)과 `indexer.py --check` 가 함께 쓰는
    재사용 진입점이다(요건 B-7/O3 — 로직을 두 곳에 복제하지 않는다).

    2-4(qa 독립검증, 2026-08-14) — `ok=False` 가 "판정 불가(failed)"와 "판정 보류
    (pending, 백그라운드 계산 중)"를 겸해 두 상태를 구분할 수 없다는 결함이 있었다
    (직전 배치는 "detail·stderr 로 구분 가능"이라 적었으나 실측 결과 `search()` 가
    `ok=False` 일 때 `detail` 을 `""` 로 덮어(아래 `search()` 참고) 그 완화책이 실재하지
    않았다). `pending` 필드를 추가해 세 상태를 값으로 명확히 분리한다:
      · `ok=True` → 판정 성공(성공)
      · `ok=False, pending=True` → 판정 보류(이 색인 세대에 대해 아직 계산 안 됨,
        백그라운드 재계산 진행 중 — `_STALENESS_PENDING` 이 유일한 생성처)
      · `ok=False, pending=False` → 판정 불가(실패 — meta 손상·예외 등,
        `_staleness_check_failed()` 가 생성)
    기본값 `False` 라 이 필드를 모르는 기존 호출자는 기존과 동일하게 "ok=False 는
    실패"로 읽어도 틀리지 않는다(펜딩도 `ok=False` 인 건 그대로다 — 이 필드는 그 안에서
    한 축을 더 나눌 뿐, 기존 필드의 의미는 바꾸지 않는다).
    """

    ok: bool  # False = 판정 불가(색인 없음·meta 손상 등) — 아래 필드는 무의미
    stale: bool
    n_changed_docs: int  # mtime 이 색인보다 새로운 것으로 잡힌 파일 수(수정 축)
    n_current_docs: int | None  # glob 으로 지금 실제로 센 파일 수(참고용)
    n_indexed_docs: int | None  # meta.n_docs — 색인 시점 파일 수(참고용)
    created_at: str | None  # 비교 기준이 된 meta.created_at 원문(참고용)
    detail: str | None  # ok=False 일 때 실패/보류 사유, ok=True 여도 부가 설명이 있으면 채움
    # 2-4(신규 필드, 기본값 있음 — 공개 계약 하위호환) — True 면 "판정 보류"(아직 계산
    # 안 됨), False(기본값)면 ok 필드가 그대로 성공/실패를 뜻한다.
    pending: bool = False


def _read_staleness_meta(
    conn: sqlite3.Connection,
) -> tuple[str | None, datetime | None, int | None, tuple[str | None, str | None]]:
    """H1(1) — `check_staleness()`의 sqlite 조회분만 떼어낸 것(파일시스템은 안 건드림).
    호출자가 이미 연 커넥션을 그대로 받는다.

    반환: `(created_at_raw, created_at(datetime|None), n_indexed(meta.n_docs), fingerprint)`.
    `fingerprint` 는 `_read_fingerprint()`가 만드는 것과 같은 표현(`meta.created_at`,
    `meta.n_chunks` 원문 문자열 그대로)이다 — `_StaleSnapshot.fingerprint`를 bm25 캐시
    지문과 직접 비교하기 위함(M3, 아래 `_get_staleness` 참고).

    `created_at` 이 결측이면 `created_at=None`(3번째 원소)만 채워 돌린다 — 이 함수는
    `StalenessCheck`를 만들지 않는다(그건 `_scan_staleness()`의 책임, "판정 불가" 조기
    반환 분기를 포함해서). `created_at` 이 있는데 파싱 실패하면(형식 오류) 여기서 삼키지
    않고 그대로 전파한다(M2 원칙과 동일 — 무음 폴백 금지, 호출자의 try/except 가 흡수).
    """
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('created_at', 'n_docs', 'n_chunks')"
    ).fetchall()
    d = {r["key"]: r["value"] for r in rows}
    created_at_raw = d.get("created_at")
    fingerprint = (created_at_raw, d.get("n_chunks"))
    n_docs_raw = d.get("n_docs")
    n_indexed = int(n_docs_raw) if n_docs_raw is not None else None
    created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else None
    return created_at_raw, created_at, n_indexed, fingerprint


def _scan_staleness(
    meta: tuple[str | None, datetime | None, int | None, tuple[str | None, str | None]],
) -> StalenessCheck:
    """H1(1) — `check_staleness()`의 파일시스템 스캔분만 떼어낸 것. **커넥션을 전혀 쥐지
    않는다**(인자 `meta` 는 `_read_staleness_meta()` 의 반환값 — sqlite 는 이미 다 읽고
    닫은 뒤라는 뜻).

    M0(2차 배치) — `config.source_paths()` 전체 glob 스캔 기반이다(1차 배치의
    rel-목록-only stat 방식은 삭제됨 — 위 모듈 주석 "M0" 절 참고). best-effort — 스캔
    중 개별 파일 stat 실패(경합으로 사라짐 등)는 건너뛰되 stderr 1줄은 남긴다(O4).
    `created_at` 이 결측이면(meta 비정상) 스캔 자체를 하지 않고 즉시 `ok=False`("판정
    불가")를 돌린다(요건 B-5 — "stale 아님"으로 단정하지 않는다).
    """
    created_at_raw, created_at, n_indexed, _fingerprint = meta
    if created_at is None:
        return StalenessCheck(
            ok=False, stale=False, n_changed_docs=0, n_current_docs=None,
            n_indexed_docs=None, created_at=None,
            detail="meta.created_at 없음 — 색인이 비정상이거나 구버전",
        )

    paths = config.source_paths()  # M0/O4: 전체 glob — 수정·추가 감지의 유일한 수단
    n_current = len(paths)

    n_changed = 0
    for p in paths:
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError as e:
            # O4: 스캔 중 파일 접근 실패(경합으로 사라짐 등) — best-effort catch 가
            # 정당한 유일한 자리지만 무음으로 넘기지 않고 stderr 1줄은 남긴다.
            sys.stderr.write(
                f"[search] stale 스캔 중 파일 접근 실패(무시, best-effort): {p} — "
                f"{type(e).__name__}: {e}\n"
            )
            continue
        if mtime > created_at:
            n_changed += 1

    n_count_diff = (n_current - n_indexed) if n_indexed is not None else None
    count_changed = n_count_diff is not None and n_count_diff != 0
    stale = n_changed > 0 or count_changed

    detail = None
    if count_changed:
        sign = "+" if n_count_diff > 0 else ""
        detail = (
            f"파일 수 변경 — 색인 시점 {n_indexed}개 -> 현재 {n_current}개"
            f"({sign}{n_count_diff}) — 추가 또는 삭제로 추정(개수만으론 어느 쪽인지, "
            f"또는 둘 다 섞였는지 구분 불가 — O4 한계)"
        )

    return StalenessCheck(
        ok=True, stale=stale, n_changed_docs=n_changed,
        n_current_docs=n_current, n_indexed_docs=n_indexed,
        created_at=created_at_raw, detail=detail,
    )


def _staleness_check_failed(e: Exception) -> StalenessCheck:
    """`check_staleness()`/`_compute_staleness_and_fingerprint()` 공용 실패 처리 —
    stderr 에 남긴 뒤 "판정 불가"(`ok=False`)로 정리한다(요건 B-5, 무음 실패 금지).
    """
    sys.stderr.write(
        f"[search] stale 판정 실패(best-effort) — '판정 불가'로 처리: "
        f"{type(e).__name__}: {e}\n"
    )
    return StalenessCheck(
        ok=False, stale=False, n_changed_docs=0, n_current_docs=None,
        n_indexed_docs=None, created_at=None, detail=f"{type(e).__name__}: {e}",
    )


def check_staleness(conn: sqlite3.Connection) -> StalenessCheck:
    """원본 .md 파일이 색인보다 새로운지(수정 축) 또는 파일 수가 다른지(추가/삭제 축)
    판정한다(재색인은 안 함).

    H1(1) — `_read_staleness_meta(conn)` + `_scan_staleness(meta)` 의 합성이다(시그니처·
    반환 의미는 그대로). **호출자의 커넥션은 스캔(글롭+stat 스윕) 동안에도 열려 있다**
    — 커넥션을 안 쥐는 변형은 `compute_staleness()`. 전체 판정 자체가 실패하면(예:
    meta.created_at 형식 오류) 침묵하지 않고 stderr 에 남긴 뒤 `ok=False`("판정 불가")로
    돌린다(요건 B-5). 지금은 `indexer.py --check`(H1 이후 `compute_staleness()` 를
    쓴다)가 아니라 `tests/test_stale_and_check.py` 의 화이트박스 테스트가 이 conn-보유
    변형을 직접 검증한다.
    """
    try:
        meta = _read_staleness_meta(conn)
        return _scan_staleness(meta)
    except Exception as e:
        return _staleness_check_failed(e)


def _compute_staleness_and_fingerprint() -> tuple[StalenessCheck, tuple[str | None, str | None]]:
    """H1(1)/(2) — `compute_staleness()`와 `refresh_staleness_now()`가 공유하는 단일
    구현. 자기 커넥션을 열어 meta 만 읽고 **즉시 닫은 뒤**(스캔 중 sqlite 핸들 0개) 스캔
    한다. meta 읽기 시점의 지문도 함께 반환한다 — `refresh_staleness_now()`가 스냅샷에
    찍는 지문은 반드시 "그 판정을 만든 meta 읽기와 같은 시점"의 것이어야 한다(따로 다시
    읽으면 그 사이 재색인이 끼어드는 TOCTOU 창이 생긴다).

    `_get_conn()`(N3, mode=ro) 자체가 실패해도(색인 없음 등) 여기서 흡수해 항상
    `(StalenessCheck(ok=False, ...), (None, None))` 형태로 돌린다 — 이 함수를 쓰는 두
    공개 함수 모두 "절대 예외를 던지지 않는다"는 계약을 지키게 하기 위함이다.
    """
    try:
        conn = _get_conn()
        try:
            meta = _read_staleness_meta(conn)
        finally:
            conn.close()
        return _scan_staleness(meta), meta[3]
    except Exception as e:
        return _staleness_check_failed(e), (None, None)


def compute_staleness() -> StalenessCheck:
    """H1(1) — `check_staleness(conn)`과 반환 의미는 같지만 **스캔 중 sqlite 핸들을 0개로
    유지한다**(자기 커넥션을 열어 meta 만 읽고 즉시 닫은 뒤 파일시스템을 스캔한다). win32
    에서 열린 sqlite 커넥션은 `indexer.py` 3단 rename 을 막으므로, "재색인 직후 신선도를
    확인"하는 `indexer.py --check`(`_run_check()`)가 이걸 쓴다 — 예전에는
    `search._get_conn()` 을 열고 그 커넥션을 쥔 채 `check_staleness(conn)` 을 불러 스캔
    내내(중앙값 47ms+9.7ms) 핸들을 쥐고 있었다.

    절대 예외를 던지지 않는다(`_compute_staleness_and_fingerprint()` 가 모든 실패를
    `StalenessCheck(ok=False, ...)` 로 흡수한다) — 색인이 아예 없어도 그냥 ok=False 를
    돌려준다.
    """
    check, _fingerprint = _compute_staleness_and_fingerprint()
    return check


@dataclass(frozen=True)
class _StaleSnapshot:
    computed_at: float  # time.monotonic()
    check: StalenessCheck
    # H1(2)/M3 — 이 스냅샷이 계산된 시점의 색인 지문(meta.created_at, meta.n_chunks 원문).
    # _read_fingerprint()/_open_index() 가 만드는 것과 같은 표현이라 _get_staleness(fingerprint)
    # 에서 "이 색인 세대에 대한 판정인가"를 직접 비교할 수 있다 — 재색인 직후 최대 60초
    # 동안 구세대 stale 판정을 그대로 돌려주던 오경고(M3)를 TTL 시간이 아니라 세대
    # 일치로 구조적으로 닫는다.
    fingerprint: tuple[str | None, str | None]


_stale_lock = threading.Lock()
_stale_snapshot: _StaleSnapshot | None = None
_stale_revalidating = False  # 백그라운드 재계산 스레드 중복 기동 방지

# H1(2) — 스냅샷이 없거나(프로세스 첫 호출) 캐시된 스냅샷이 다른 색인 세대의 것일 때
# _get_staleness() 가 절대 기다리지 않고 즉시 돌려주는 값. ok=False 를 쓰는 근거(리드
# 판정): ok=True, stale=False 는 "판정했고 안 낡았다"는 거짓 주장이고, pending 이 어느
# 채널에도 안 드러나 무음이 된다. ok=False 면 SearchResult.index_stale_ok=False / HTTP
# stale_check_ok:false 로 관측 가능하고, index_stale=False(위 기본값)라 오경고도 안 낸다.
# SearchResult 에 필드를 추가하지 않고 pending 을 관측 가능하게 만드는 유일한 선택지다.
#
# 2-4(qa 독립검증, 2026-08-14) — 위 "한계" 문단이 서술한 "detail/stderr 로 구분 가능"은
# 실재하지 않았다(실측: search() 가 ok=False 일 때 detail 을 "" 로 덮었고, server.py 는
# detail 을 응답에 싣지도 않았고, pending 은 stderr 에도 아무것도 안 썼다 — 세 경로 모두
# 막혀 있었다). 이제 `StalenessCheck.pending=True` 로 이 상태를 명시적으로 표시한다
# (아래) — `ok=False` 자체의 의미는 그대로 두고(여전히 "판정 성공 아님"), 그 안에서
# pending/failed 를 나누는 새 축을 추가했을 뿐이다.
_STALENESS_PENDING = StalenessCheck(
    ok=False, stale=False, n_changed_docs=0, n_current_docs=None,
    n_indexed_docs=None, created_at=None,
    detail="신선도 판정 보류 — 이 색인 세대에 대해 아직 계산되지 않았다(백그라운드 계산 중, 다음 호출부터 반영).",
    pending=True,  # 2-4 — "판정 불가(failed)"와 구분되는 유일한 pending 생성처
)


def refresh_staleness_now() -> StalenessCheck:
    """H1(2) — 동기적으로 원본 stale 판정을 재계산하고 새 스냅샷을 발행한다. 백그라운드
    스레드(`_refresh_staleness_bg`)·`search.py` CLI·`server.py`/`mcp_server.py` 기동 전
    워밍(H1(3))·테스트가 공유하는 단일 구현이다. `_compute_staleness_and_fingerprint()`
    를 써서 스캔 중 sqlite 핸들을 쥐지 않는다(H1 — win32 재색인 rename 충돌 회피).

    절대 예외를 던지지 않는다(색인이 없어도 StalenessCheck(ok=False, ...) 를 스냅샷에
    그대로 발행한다) — `server.py`/`mcp_server.py` 의 워밍 스레드가 그래도 best-effort
    try/except 로 감싸는 이유는 이 함수 자체가 아니라 스레드 기동 주변의 예상 밖 실패까지
    막기 위함이다.
    """
    check, fingerprint = _compute_staleness_and_fingerprint()
    global _stale_snapshot
    with _stale_lock:
        _stale_snapshot = _StaleSnapshot(computed_at=time.monotonic(), check=check, fingerprint=fingerprint)
    return check


def _refresh_staleness_bg() -> None:
    """백그라운드 재계산 스레드 진입점. `refresh_staleness_now()`를 호출하고 `finally`
    에서 재계산 플래그만 내리는 얇은 래퍼다(H1 — 동기 계산·스냅샷 발행 로직 자체는
    `refresh_staleness_now()` 하나로 스레드·CLI·서버 워밍·테스트가 공유한다).
    """
    global _stale_revalidating
    try:
        refresh_staleness_now()
    except Exception as e:
        # refresh_staleness_now() 자체는 절대 예외를 던지지 않지만(위 docstring), 스레드
        # 경계에서 예상 밖 실패까지 흡수해 데몬 스레드가 트레이스백 없이 조용히 죽는
        # 것을 막는다.
        sys.stderr.write(f"[search] stale 백그라운드 재계산 실패: {type(e).__name__}: {e}\n")
    finally:
        with _stale_lock:
            _stale_revalidating = False


def _get_staleness(fingerprint: tuple[str | None, str | None]) -> StalenessCheck:
    """stale-while-revalidate 캐시 조회. **conn 을 받지 않는다** — 대신 호출자(`search()`)
    가 `_open_index()` 로 이미 확인한 "지금 이 검색이 실제로 쓰는 색인 세대"의 지문을
    받는다(H1(2)).

    이 함수는 절대 블로킹하지 않는다(동기 부트스트랩 계산 경로 자체가 없다):
      · 스냅샷이 없거나(프로세스 첫 호출) `snap.fingerprint != fingerprint`(= 이 색인
        세대에 대해 아직 계산된 적이 없다 — 재색인 직후가 정확히 이 경우, M3) → 즉시
        `_STALENESS_PENDING` 을 반환하고 백그라운드 재계산만 걸어둔다(중복 기동 방지는
        `_stale_revalidating`).
      · 스냅샷이 있고 지문이 같으면 → 즉시 그 값을 반환한다. TTL 이 만료됐으면 그래도
        즉시 반환하되 백그라운드 재계산만 추가로 걸어둔다(SWR).
    락은 스냅샷 대입/플래그 조작 구간에서만 잡는다 — 계산(디스크 IO)은 언제나 락 밖.

    "프로세스 첫 검색은 pending 만 본다"는 회귀는 `search.py` CLI·`server.py`·
    `mcp_server.py` 세 진입점이 검색을 받기 전에 `refresh_staleness_now()` 로 명시적으로
    워밍해서 막는다(H1(3), 각 파일 참고) — 이 함수 자체의 책임이 아니다.

    2-4(qa 독립검증, 2026-08-14) — **pending 은 의도적으로 stderr 를 안 쓴다**(판단
    근거를 남긴다 — qa 가 "판단 근거를 보고하라"고 명시 요구). 이유 셋:
      1) H1(3) 워밍(위 문단) 덕에 실사용 경로에서 pending 이 보이는 창은 "스레드 기동
         ~ 완료" 사이의 아주 좁은 구간뿐이다(정상 운영에선 사실상 안 보임) — 드문 경고가
         아니라 **거의 안 일어나는 일**이라 로그로 지킬 이득이 작다.
      2) 재색인 직후에는 이 함수가 **요청마다** 호출될 수 있다(지문이 새 세대로 바뀐
         뒤 재계산이 끝나기 전까지 오는 모든 요청이 이 분기를 탄다) — 요청률에 비례해
         stderr 를 쓰면 정확히 "재색인 방금 끝났다"는, 운영자가 가장 소음을 원치 않을
         구간에서 로그가 도배된다(브리프가 미리 지적한 위험).
      3) N7(모듈 docstring) 의 설계 원칙과 정합한다 — "재로드 실패는 하드(로그+예외),
         원본 stale 은 소프트(경고만)". pending 은 실패가 아니라 "계산이 이미 걸려
         있다"는 정상 중간 상태이므로 소프트 취급이 옳다 — 실패(`_staleness_check_failed`)
         쪽은 이미 stderr 를 쓴다(N7 비대칭 유지, 위 함수 참고).
    관측이 필요하면 `StalenessCheck.pending`/`SearchResult.index_stale_pending`(2-4 신규
    필드) · HTTP `stale_check_pending` 필드로 **풀링 방식**으로 확인할 수 있다 — 굳이
    푸시(stderr) 방식을 겹칠 필요가 없다.
    """
    global _stale_snapshot, _stale_revalidating
    snap = _stale_snapshot  # 락 없는 단일 전역 읽기(N5 와 동일 원칙) — 이후 snap 로컬만 쓴다

    if snap is None or snap.fingerprint != fingerprint:
        with _stale_lock:
            if not _stale_revalidating:
                _stale_revalidating = True
                threading.Thread(target=_refresh_staleness_bg, daemon=True).start()
        return _STALENESS_PENDING

    if time.monotonic() - snap.computed_at >= STALE_CHECK_TTL_SECONDS:
        with _stale_lock:
            if not _stale_revalidating:
                _stale_revalidating = True
                threading.Thread(target=_refresh_staleness_bg, daemon=True).start()
    return snap.check  # 만료됐어도 최신 캐시를 즉시 반환한다(SWR) — 여기서 절대 안 기다린다


def _token_ids_in_vocab(retriever: bm25s.BM25, tokens: list[str]) -> list[int]:
    """2-1 — 질의 토큰(중복 포함, 원문 순서 그대로)을 bm25s 어휘 id 로 바꾼다. 어휘에
    없는 토큰(OOV)은 조용히 빠진다(bm25s 자체 색인에 아예 없으니 어차피 raw 기여가
    0 일 수밖에 없다 — coverage 분모에는 그대로 잡히고 분자에서만 빠진다).

    M1(qa 독립검증 후속, 2026-08-14) — bm25s 는 **미지토큰 센티널**(빈 문자열 `""`)을
    `max_id+1` 로 `vocab_dict` 에 등록해 둘 수 있다(실측: `vocab_dict` 원소 수가 점수
    열(희소행렬 유효 컬럼) 수보다 1 많다). 이 센티널 id 가 그대로 넘어가면 희소열 경로
    (`_coverage_sparse`)는 `IndexError`, 전체배열 경로(`_coverage_full_array`)는
    `ValueError` 로 **둘 다** 죽는다 — 그리고 `_compute_coverage()` 의 폴백 호출이
    try 블록 *안*이라(아래) 그 예외가 그대로 `search()` 전체를 죽인다. 지금은
    `tokenizer.py` 가 빈 문자열 토큰을 걸러 이 경로에 실제로는 도달하지 않지만, 그건
    `tokenizer.py` 쪽의 우연한 방어이지 이 함수 자신의 방어가 아니다 — 유효 열 개수
    (`indptr` 길이-1, CSC 표준)를 넘는 id 는 여기서 조용히 제외해 두 경로 모두를
    한 곳에서 방어한다(두 경로가 같은 `token_ids` 리스트를 공유하므로 이 필터 하나로
    양쪽 다 막힌다). 필터링된 토큰은 매칭 안 된 것과 동일하게 취급된다(분모=원본
    tokens 길이는 그대로, 분자에서만 빠짐 — 위 OOV 처리와 같은 태도).
    """
    vocab = retriever.vocab_dict
    n_cols = len(retriever.scores["indptr"]) - 1
    return [vocab[t] for t in tokens if t in vocab and vocab[t] < n_cols]


def _coverage_sparse(retriever: bm25s.BM25, token_ids: list[int], doc_id: int) -> int:
    """2-1 — 희소 열(`retriever.scores["indptr"/"indices"/"data"]`, 토큰 id 기준 CSC) +
    `bisect` 이진탐색으로 각 토큰이 `doc_id` 문서에 실제로 기여(raw>0)했는지 판정한다.
    토큰별 전체 배열(get_scores) 할당 없이 그 토큰의 열 슬라이스(indptr[t]:indptr[t+1])만
    본다 — 비용 요건(2-1 §"coverage 계산은 반드시 희소열 경로로")을 지키는 핵심 경로다.

    `indices` 슬라이스는 문서 id 오름차순 정렬이 전제다(bm25s 내부 구현 확인, 모듈 상단
    RAW_MAX_FLOOR 주석 인용 표 참고) — 정렬이 깨지면 `bisect` 결과가 조용히 틀릴 수
    있으므로, 이 전제가 깨지는 어떤 구조 이상(KeyError·IndexError·타입 불일치 등)도 여기서
    삼키지 않고 그대로 던진다 — 호출자(`_compute_coverage`)가 `_coverage_full_array()`
    로 폴백한다(표준 라이브러리(`bisect`)만 쓰고 신규 의존성을 추가하지 않는다 — numpy 는
    bm25s 가 이미 만들어 둔 배열을 슬라이싱만 할 뿐, 이 함수는 numpy 를 직접 import 하지
    않는다).
    """
    indices = retriever.scores["indices"]
    indptr = retriever.scores["indptr"]
    data = retriever.scores["data"]
    n_hit = 0
    for tid in token_ids:
        start = int(indptr[tid])
        end = int(indptr[tid + 1])
        col = indices[start:end]
        pos = bisect.bisect_left(col, doc_id)
        if pos < len(col) and int(col[pos]) == doc_id and float(data[start + pos]) > 0:
            n_hit += 1
    return n_hit


def _coverage_full_array(retriever: bm25s.BM25, token_ids: list[int], doc_id: int) -> int:
    """2-1 — 희소열 경로(`_coverage_sparse`)가 구조 가정 이탈로 실패했을 때의 안전한
    대체 경로. 토큰별 전체 배열(`get_scores_from_ids`, 코퍼스 크기만큼 할당)을 만들어
    `doc_id` 위치 값을 직접 본다 — 비용은 크지만(질의당 토큰수 × num_docs 배열 할당)
    희소행렬 내부 저장 구조(indptr/indices/data 배열 형태)가 바뀌어도 그 구조에 의존하지
    않는다.

    M2(qa 독립검증 후속, 2026-08-14) — **[정정] 위 "구조가 바뀌어도 항상 맞는 값을
    낸다"는 이전 문구는 과잉 단정이었다 — 삭제한다.** `get_scores_from_ids()` 는
    `nonoccurrence_array` 가 설정된 idf_method(robertson 계열 등)에서 **모든 문서
    점수에 상수를 더한다** — 그러면 매칭 여부와 무관하게 `arr[doc_id] > 0` 이 전건
    참이 되어 **coverage 가 조용히 1.0 으로 무너진다**(무음 오답 — 위 `_confidence()`
    docstring 이 "idf_method 를 robertson 으로 바꾸는 날"을 이미 상정하고 있었다).
    현재 색인이 실제로 쓰는 `idf_method="lucene"`(bm25s 기본)은 `nonoccurrence_array`
    가 없어(`None`) 이 결함을 안 밟지만, 그 전제가 깨지면(다른 idf_method 로 재색인)
    이 함수는 더 이상 "항상 맞는 값"이 아니다 — 그래서 진입 시 그 전제를 확인하고,
    깨져 있으면 stderr 로 1회성 경고를 남긴다(무음 오답 방지, 값 자체를 보정하지는
    않는다 — 보정하려면 idf_method 별 상수를 알아야 하는데 그건 이 함수의 책임 밖이다).
    """
    if getattr(retriever, "nonoccurrence_array", None) is not None:
        _warn_nonoccurrence_array_once()
    n_hit = 0
    for tid in token_ids:
        arr = retriever.get_scores_from_ids([tid])
        if float(arr[doc_id]) > 0:
            n_hit += 1
    return n_hit


def _compute_coverage(
    retriever: bm25s.BM25, tokens: list[str], doc_id: int | None
) -> tuple[float, int, int]:
    """2-1 — `(coverage, n_query_tokens, n_matched_tokens)` 를 계산한다.

    coverage = 질의 토큰(중복 포함) 중 `doc_id` 문서에 raw>0 로 기여한 토큰의 비율.
    `doc_id` 는 **`search()` 가 실제로 반환하는 결과 집합에서 raw 가 최대인 hit**
    (=raw_max 의 출처와 동일 문서)이어야 한다 — 코퍼스 전역 argmax 가 아니다(모듈 상단
    RAW_MAX_FLOOR 주석 참고 — 두 정의가 42건 중 5건에서 다른 문서를 가리켰다). 호출자
    (`search()`)가 이미 그 문서의 idx 를 골라 넘긴다는 전제다.

    `doc_id=None`(hits 가 비어 raw_max 를 낸 문서가 없음) 또는 토큰이 전부 OOV 이면
    coverage=0.0 을 낸다(fetch 비용도 0 — bm25s 를 더 안 부른다).
    """
    n_query_tokens = len(tokens)
    if n_query_tokens == 0 or doc_id is None:
        return 0.0, n_query_tokens, 0

    token_ids = _token_ids_in_vocab(retriever, tokens)
    if not token_ids:
        return 0.0, n_query_tokens, 0

    try:
        n_matched = _coverage_sparse(retriever, token_ids, doc_id)
    except Exception as e:
        # 희소열 구조 가정이 깨진 경우(bm25s 버전·백엔드 차이 등) — 예외로 죽지 않고
        # 전체배열 경로로 폴백한다(2-1 요건). stderr 로만 남긴다(MCP stdout 금지 계약).
        sys.stderr.write(
            f"[search] coverage 희소열 경로 실패 — 전체배열 경로로 폴백: "
            f"{type(e).__name__}: {e}\n"
        )
        n_matched = _coverage_full_array(retriever, token_ids, doc_id)

    coverage = n_matched / n_query_tokens
    return coverage, n_query_tokens, n_matched


def _deficient_axis(raw_max: float, coverage: float) -> str:
    """2-1/2-6 — confidence 가 "ok" 에 못 미칠 때 어느 축이 미달인지 짧은 문자열로
    남긴다(로그·경고줄이 공유하는 단일 근거). "ok" 면 빈 문자열.
    """
    if raw_max <= 0:
        return "raw_max<=0(무매칭)"
    parts = []
    if raw_max < RAW_MAX_FLOOR:
        parts.append(f"raw_max<{RAW_MAX_FLOOR}")
    if coverage < COVERAGE_FLOOR:
        parts.append(f"coverage<{COVERAGE_FLOOR}")
    return "+".join(parts)


def _confidence(raw_max: float, coverage: float) -> str:
    """2-1(qa 독립검증, 2026-08-14) — raw_max 단일축 임계를 raw_max×coverage 2축 AND
    규칙으로 교체했다(값·근거는 모듈 상단 RAW_MAX_FLOOR 주석 참고). 스펙은
    "raw_max == 0" 이지만 <= 0 으로 안전하게 넓혔다 — L4(code-reviewer 재검증,
    2026-08-13): 현행 idf_method="lucene"(bm25s 기본, 이 인덱스가 실제로 쓰는 방식)
    에서는 IDF 가 음수가 되지 않아 이 경로가 실무에서 발생하지 않는다 — 그래도 방어
    코드는 유지한다. bm25s 의 다른 idf_method(예: robertson 계열)는 고빈도 텀에서
    이론상 음수 IDF 가 가능하므로, 색인을 그쪽으로 바꾸는 날 이 줄이 의미를 갖는다.
    """
    if raw_max <= 0:
        return "none"
    if raw_max >= RAW_MAX_FLOOR and coverage >= COVERAGE_FLOOR:
        return "ok"
    return "low"


def _env_truthy(name: str) -> bool:
    """환경변수 진위 해석 규약(4차 배치 D4) — `1`·`true`·`yes`·`on`(대소문자 무시)을
    참으로, 그 외(미설정 포함)는 거짓으로 본다. 호출 시점에 `os.environ` 을 읽는다(모듈
    import 시점에 고정하지 않음 — 그래야 테스트가 `os.environ`/`monkeypatch` 로 값을
    바꿔가며 검증할 수 있다, 요건 D3).
    """
    val = os.environ.get(name)
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on")


def _log_query(
    *, query: str, category_requested: str, category_used: str, k: int,
    fallback: bool, raw_max: float, confidence: str,
    top: list[dict], source: str, index_created_at: str | None,
    # 2-6(qa 독립검증, 2026-08-14) — 전부 키워드 전용 + 기본값 있음(하위호환, 기존
    # 호출자·테스트는 이 인자들을 몰라도 그대로 동작한다).
    coverage: float = 0.0,
    n_query_tokens: int = 0,
    n_matched_tokens: int = 0,
    deficient_axis: str = "",
    caller_id: str | None = None,
    # L1(qa 독립검증 후속, 2026-08-14 — 정정) — 기본값을 "live"(가장 신뢰도 높은 값)
    # 에서 "unknown"으로 바꾼다. "live"는 `_open_index()`로 지금 이 호출이 직접 연
    # 색인의 지문이라는 뜻인데, 이 인자를 몰라도 되는(하위호환) 기본값이 하필 가장
    # 신뢰도 높은 값이면 새 호출부가 이 인자를 빠뜨렸을 때 조기반환이 "live"로
    # 오라벨된다 — 무음으로 근거 없는 신뢰를 붙이는 셈이다. "unknown"은 그런 호출부가
    # 있으면 로그에서 바로 드러나게 한다(기존 호출부 2곳은 전부 "live"/"cached_snapshot"
    # 을 명시적으로 넘기므로 이 기본값 변경으로 실제 동작이 바뀌지 않는다).
    index_created_at_source: str = "unknown",
) -> None:
    """질의 로그 append. 실패해도 검색 자체는 죽지 않는다(try/except, stderr 로만).

    O2(2차 배치) — `index_created_at`(이번 호출이 실제로 쓴 색인의 meta.created_at)을
    함께 남긴다. `_log_query` 는 R-001 전환 판정의 유일한 근거인데 지금까지는 재색인
    경계를 구분할 필드가 없었다(직전 커밋이 로그 기준선을 통째로 리셋해야 했던 것도
    같은 이유 — 재색인 전후 트래픽을 사후에 못 갈랐다). `search()` 가 이미 매 호출
    지문을 읽으므로 이 필드를 추가하는 데 드는 추가 비용은 0 이다(호출자가 이미 가진
    값을 그대로 넘길 뿐). `None` 은 토큰 0개 조기 반환처럼 색인을 아예 안 연 호출을
    뜻한다.

    R-001 하이브리드 전환 판정의 유일한 근거 데이터라 오염 방지가 핵심이다.
    eval.py 는 반드시 log=False 로 호출하고(1차 방어), 혹시 나중에 log=True 로
    잘못 켜지더라도 source="eval" 로 남아 분석 시 걸러낼 수 있다(2차 방어). 이 스위치와
    무관하게 eval.py 는 이 함수 자체를 호출하지 않는다(수정 금지, 요건 D5).

    B4(code-reviewer, 2026-08-13): "실패해도 안 죽는다"는 이 docstring 의 약속을
    `except OSError` 하나로는 못 지킨다 — 실측: 질의에 서로게이트 문자(짝 없는 하이 서로게이트
    등)가 섞이면 파일 쓰기가 `UnicodeEncodeError`(OSError 의 자손 아님)로 죽는다. 3차
    배치의 HTTP `/search?q=` 는 URL 쿼리스트링을 통해 이런 입력을 그대로 받을 수 있는
    경로라 실제로 밟힐 가능성이 있다. 그래서 ① 예외 폭을 Exception 전체로 넓히고
    ② 파일을 `errors="replace"` 로 열어 애초에 인코딩 실패 여지를 줄이고 ③ `allow_nan=False`
    로 NaN/Infinity 리터럴이 로그에 안 새게 막는다(기본값 allow_nan=True 인 채로 두면
    Python 은 문제없이 쓰지만, 그 줄은 RFC 8259 기준 유효한 JSON 이 아니라 엄격한 파서가
    그 줄에서 막힌다 — 로그는 여러 줄을 나중에 일괄 분석하므로 한 줄이라도 깨지면 안 된다).

    4차 배치 D(F-8, 질의 로그 오염 방지) — 환경변수 2종을 **호출 시점에** 읽는다:
      · `DOCS_RAG_NO_LOG` — 참이면(해석 규약은 `_env_truthy` 참고) 이 함수는 아무것도
        쓰지 않고 즉시 반환한다(로깅 자체를 끈다).
      · `DOCS_RAG_LOG_SOURCE` — 값이 있으면(빈 문자열 제외) `source` 인자를 그 값으로
        덮어쓴다(호출자가 cli/api/mcp/http 무엇을 넘겼든 최종 우선). 테스트·QA 트래픽을
        `"qa"` 로 통일 표시해 실사용 로그(R-001 판정 근거)와 사후에도 분리할 수 있게 한다.

    2-6(qa 독립검증, 2026-08-14) — 스키마 v2. **기존 339줄(v1)은 변조하지 않는다** — v2
    전용 필드(`schema_version` 이하)는 이 함수가 새로 쓰는 줄에만 실린다. 판독기
    (`log_reader.py`)는 `schema_version` 키의 **부재**로 v1 을 식별한다. 추가한 필드:
      · `schema_version` — 이 줄의 스키마 세대(현재 2). v1 줄엔 이 키 자체가 없다.
      · `run_id`/`pid`/`seq` — 프로세스 상관 ID(모듈 상단 `_LOG_RUN_ID`/`_LOG_PID`/
        `_next_log_seq()` 참고). **한계**: 한 프로세스를 공유하는 서브에이전트는 이
        값만으로 안 갈린다 — `caller_id`(아래)가 그 공백을 메우려는 선택적 보완이다.
      · `caller_id` — 호출자가 자발적으로 넘기는 선택적 식별자(MCP 툴 인자·HTTP 쿼리
        파라미터, 둘 다 기본값 None). **호출자 협조가 없으면 여전히 None** — 서브에이전트
        귀속이 "해결"된 게 아니라 "선택적으로 가능해진" 것이다.
      · `coverage`/`n_query_tokens`/`n_matched_tokens`/`deficient_axis` — 2-1 이 새로
        도입한 confidence 신호를 그대로 남긴다(§2-1 `_compute_coverage`/`_deficient_axis`
        와 같은 계산 — 로그와 응답이 어긋나지 않는다). R-001 조건 1("용어가 달라 못
        찾음")에 접근 가능한 유일한 개선이지만, 성패(정답 존재 여부) 라벨은 여전히 없어
        완전한 측정은 아니다(`log_reader.py` 가 이 한계를 정직하게 보고한다).
      · `index_created_at_source` — `index_created_at` 값의 출처("live"=이번 호출이
        직접 연 색인 / "cached_snapshot"=조기 반환이라 캐시만 봄 / "unavailable"=캐시도
        없음). O2 가 남기던 `index_created_at` 자체는 그대로 두고 출처만 구분해 추가했다.
    """
    if _env_truthy("DOCS_RAG_NO_LOG"):
        return
    source_override = os.environ.get("DOCS_RAG_LOG_SOURCE")
    if source_override:
        source = source_override
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "query": query,
            "category": category_requested,
            "category_used": category_used,
            "k": k,
            "fallback": fallback,
            "raw_max": raw_max,
            "confidence": confidence,
            "top": top,
            "index_created_at": index_created_at,  # O2
            # ── 2-6 v2 신규 필드 (아래부터) ──────────────────────────────
            "schema_version": _LOG_SCHEMA_VERSION,
            "run_id": _LOG_RUN_ID,
            "pid": _LOG_PID,
            "seq": _next_log_seq(),
            "caller_id": _sanitize_caller_id(caller_id),  # M3 — 길이·문자셋 캡(위 헬퍼 참고)
            "coverage": coverage,
            "n_query_tokens": n_query_tokens,
            "n_matched_tokens": n_matched_tokens,
            "deficient_axis": deficient_axis,
            "index_created_at_source": index_created_at_source,
        }
        with LOG_PATH.open("a", encoding="utf-8", errors="replace") as f:
            f.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
    except Exception as e:
        sys.stderr.write(f"[search] 질의 로그 쓰기 실패(검색 결과에는 영향 없음): {type(e).__name__}: {e}\n")


def search(
    query: str,
    category: str = "all",
    k: int = 5,
    *,
    log: bool = True,
    source: str = "api",
    caller_id: str | None = None,
) -> SearchResult:
    """BM25 로 조각을 검색한다.

    파이프라인: tokenize_query → (토큰 0개면 즉시 반환) → bm25s 오버페치 →
    status 가중 → 카테고리 필터(+0건이면 전체 폴백) → weighted 정렬 → 상위 k →
    max 정규화 → coverage 계산 → confidence 판정 → 질의 로그.

    `log`/`source`/`caller_id` 는 키워드 전용이라 기존 위치인자 호출(`search(q)`,
    `search(q, "api", 5)`)은 그대로 동작한다. eval.py 는 반드시 `log=False, source="eval"`
    로 불러 질의 로그를 오염시키지 않는다.

    `category` 는 앞뒤 공백·대소문자를 무시하고 판정한다("API"·" api " 도 유효, L6/L7).
    `k<=0` 은 잘못된 입력으로 보고 `config.DEFAULT_K` 로 보정한다(조용히 1건으로 깎지
    않는다). 상한 `config.MAX_K` 클램프는 그대로 유지된다.

    2-6(qa 독립검증, 2026-08-14, 신규 인자) — `caller_id` 는 호출자가 자발적으로 넘기는
    선택적 상관 ID다(기본값 None, 하위호환). 질의 로그에 그대로 실린다(`_log_query`
    참고) — R-001 재질의율 판정에서 프로세스 상관 ID(run_id/pid)만으로는 한 MCP 서버
    프로세스를 공유하는 서브에이전트들을 못 가르는 공백을 메우려는 보완책이다. 호출자가
    안 넘기면(기본값) 그 보완은 그냥 작동하지 않는다 — "해결"이 아니라 "선택적 개선"이다.
    """
    category_requested = category  # 로그·안내 메시지에는 호출자가 준 원문 그대로 남긴다
    # L7(code-reviewer, 2026-08-13): 판정·필터링은 정규화한 값으로 한다 — "API"·" api "
    # 처럼 대소문자·공백만 다른 값이 전부 무효로 떨어지는 게 MCP/HTTP 호출자의 최빈 실수다.
    _cat_norm = category.strip().lower() if isinstance(category, str) else category
    category_valid = _cat_norm == "all" or _cat_norm in config.CATEGORY_DESC
    eff_category = _cat_norm if category_valid else "all"
    # L6(code-reviewer, 2026-08-13): k<=0 은 "1건만 달라"가 아니라 잘못된 입력일 가능성이
    # 높다 — 조용히 1건으로 깎는 대신 기본 k 로 보정한다(기존 상한 클램프는 그대로 유지).
    k_used = config.DEFAULT_K if k <= 0 else max(1, min(k, config.MAX_K))

    tokens = tokenizer.tokenize_query(query)
    if not tokens:
        result = SearchResult(
            hits=[], category_used="all", fallback_used=False,
            category_requested=category_requested, category_valid=category_valid,
            raw_max=0.0, raw_scores=[], confidence="none",
            coverage=0.0, n_query_tokens=0, n_matched_tokens=0,  # 2-1 — 토큰 0개 조기 반환
        )
        if log:
            # 2-6 — 색인을 아예 안 열었으므로 index_created_at 은 "지금 이 호출이 실제로
            # 쓴 색인"을 알 수 없다 — 대신 캐시된 스냅샷 지문을 최선노력으로 채운다
            # (비용 0, `_cached_index_created_at()` 참고). 그마저 없으면 None 이고
            # source 필드로 그 구분이 남는다.
            cached_created_at, created_at_source = _cached_index_created_at()
            _log_query(
                query=query, category_requested=category_requested,
                category_used=result.category_used, k=k_used, fallback=False,
                raw_max=0.0, confidence="none", top=[], source=source,
                index_created_at=cached_created_at,  # O2/2-6
                index_created_at_source=created_at_source,
                coverage=0.0, n_query_tokens=0, n_matched_tokens=0,
                # L2(qa 독립검증 후속, 2026-08-14) — 문자열 리터럴 복제 대신 헬퍼를 호출한다
                # (server.py 의 빈 q 경로도 같은 이유로 이 헬퍼 호출로 맞췄다 — 라벨 드리프트
                # 방지, `_deficient_axis(0.0, 0.0)` 은 "raw_max<=0(무매칭)"과 동치).
                deficient_axis=_deficient_axis(0.0, 0.0), caller_id=caller_id,
            )
        return result

    # N1/N4(2차 배치) — _open_index() 가 conn+retriever+n_chunks+지문을 한 번에 연다
    # (search() 와 diagnose_status_weight_rank() 가 이 헬퍼 하나를 공유한다).
    conn, retriever, n_chunks, fingerprint = _open_index()
    try:
        # H1(2) — _get_staleness() 는 conn 이 아니라 "지금 이 검색이 실제로 쓰는 색인
        # 세대"의 지문을 받는다(더 이상 호출자 커넥션을 쥔 채 동기 계산하지 않는다).
        stale_check = _get_staleness(fingerprint)
        fetch = min(max(50, k_used * 5), n_chunks)

        ids_arr, scores_arr = retriever.retrieve([tokens], k=fetch, show_progress=False)
        cand_ids = [int(x) for x in ids_arr[0]]
        cand_raw = [float(x) for x in scores_arr[0]]

        rows = _fetch_rows(conn, cand_ids)
    finally:
        conn.close()

    candidates: list[tuple[int, float, float, sqlite3.Row]] = []
    n_missing = 0
    for cid, raw in zip(cand_ids, cand_raw):
        row = rows.get(cid)
        if row is None:
            n_missing += 1  # A2(code-reviewer): idx 정합 파손 — 무음으로 넘기지 않고 센다
            continue
        weight = config.STATUS_WEIGHT.get(row["status"], 1.0)
        candidates.append((cid, raw, raw * weight, row))

    if n_missing:
        # A2(code-reviewer, 2026-08-13): sqlite↔bm25 idx 정합이 깨지면 "색인 전체 파손"과
        # "이 질의는 그냥 결과가 없음"이 구분 불가해진다(indexer.py 가 "가장 조용히 고장나는
        # 방식"이라 부르며 왕복검증까지 넣어 막은 실패 모드) — 그래서 무음 스킵 대신 반드시
        # 흔적을 남긴다. stdout 금지 계약상 stderr 로만.
        sys.stderr.write(
            f"[search] idx 정합 경고: bm25 후보 {len(cand_ids)}건 중 {n_missing}건이 "
            f"sqlite chunks 테이블에 없다 — 색인이 어긋났을 수 있다(indexer.py 재실행 검토).\n"
        )

    # B3(code-reviewer, 2026-08-13): bm25s 는 k 개를 채우려고 raw=0 인 후보를 코퍼스 순서로
    # 패딩한다(완전 무관 질의도 fetch 만큼 0점 후보가 나온다 — 예: "qqzzxxvv" k=5 실측
    # idx=0,2370,2369,16,17 전부 raw=0.0, format_result 6,178자). 여기서 거르는 건 *결과*가
    # 아니라 *0점 패딩*이다: weighted<=0 은 raw<=0 과 동치(STATUS_WEIGHT 는 항상 양수)이고,
    # 현행 idf_method=lucene 에서 raw==0 은 "질의 텀이 하나도 안 걸렸다"의 수학적 증명이라
    # 진짜 매칭 후보가 실수로 같이 걸릴 일이 없다. "하한 미달 결과를 하드 드롭하지 마라"
    # (D5)와 충돌하지 않는다 — 그건 *신뢰도가 낮은 결과*를 버리지 말라는 것이지, 매칭이
    # 전무한 패딩까지 결과로 포장하라는 뜻이 아니다. 전부 걸러지면 hits=[] 가 되어 기존
    # "결과 없음" 경로로 자연히 합류한다.
    candidates = [c for c in candidates if c[2] > 0]

    fallback_used = False
    if eff_category != "all":
        filtered = [c for c in candidates if c[3]["category"] == eff_category]
        if not filtered:
            filtered = candidates
            fallback_used = True
            category_used = "all"
        else:
            category_used = eff_category
    else:
        filtered = candidates
        category_used = "all"

    filtered.sort(key=lambda c: c[2], reverse=True)
    top = filtered[:k_used]

    max_weighted = max((c[2] for c in top), default=0.0)
    hits: list[Hit] = []
    raw_scores_out: list[float] = []
    log_top: list[dict] = []
    for cid, raw, weighted, row in top:
        norm = (weighted / max_weighted) if max_weighted > 0 else 0.0
        hits.append(Hit(
            idx=cid, score=norm, rel=row["rel"], heading_path=row["heading_path"],
            start_line=row["start_line"], end_line=row["end_line"],
            category=row["category"], status=row["status"], text=row["text"],
        ))
        raw_scores_out.append(raw)
        # 2-6(qa 독립검증, 2026-08-14) — 기존 rel/chunk_no/score 는 그대로 유지하고
        # status·raw(가중 전)를 추가한다. status 가중 효과의 사후분석과 가중 전 순위
        # 복원이 이제 로그만으로 가능하다(브리프 §2-6 "hit별 status·raw 추가").
        log_top.append({
            "rel": row["rel"], "chunk_no": row["chunk_no"], "score": norm,
            "status": row["status"], "raw": raw,
        })

    raw_max = max(raw_scores_out, default=0.0)

    # 2-1(qa 독립검증, 2026-08-14) — coverage 는 "raw_max 를 낸 그 hit 문서"를 기준으로
    # 계산한다(코퍼스 전역 argmax 가 아니다 — 모듈 상단 RAW_MAX_FLOOR 주석 참고). raw_max
    # 는 top 리스트(카테고리 필터·폴백이 이미 반영된 최종 결과 집합) 안에서의 최댓값이라,
    # 그 최댓값을 낸 hit 의 idx 를 그대로 찾는다(weighted 정렬이라 raw 최댓값이 1위가
    # 아닐 수 있음 — 그래서 raw_scores_out.index() 로 실제 위치를 찾는다).
    raw_max_doc_id: int | None = None
    if raw_scores_out:
        raw_max_doc_id = hits[raw_scores_out.index(raw_max)].idx
    coverage, n_query_tokens, n_matched_tokens = _compute_coverage(retriever, tokens, raw_max_doc_id)
    confidence = _confidence(raw_max, coverage)
    deficient_axis = _deficient_axis(raw_max, coverage)

    result = SearchResult(
        hits=hits, category_used=category_used, fallback_used=fallback_used,
        category_requested=category_requested, category_valid=category_valid,
        raw_max=raw_max, raw_scores=raw_scores_out, confidence=confidence,
        coverage=coverage, n_query_tokens=n_query_tokens, n_matched_tokens=n_matched_tokens,
        n_missing=n_missing, fetch=fetch,
        index_stale=stale_check.ok and stale_check.stale,
        index_stale_docs=stale_check.n_changed_docs if stale_check.ok else 0,
        # 2-4(qa 독립검증, 2026-08-14) — 수정 전에는 `if stale_check.ok else ""` 로
        # ok=False 일 때 detail 을 무조건 빈 문자열로 덮었다("문서화된 완화책"이 실재하지
        # 않았던 근본 원인 하나). 이제 항상 실제 detail 을 그대로 흘린다 — ok=True 일
        # 때의 기존 의미("빈 문자열=기본 문구 사용, 있으면 대체 설명")는 그대로고,
        # ok=False 일 때는 "판정 보류"/"판정 불가" 사유 문구가 실린다(전에는 그 사유가
        # StalenessCheck 내부에만 있고 SearchResult 밖으로 한 번도 안 나갔다).
        index_stale_detail=stale_check.detail or "",
        index_stale_ok=stale_check.ok,  # O1 — "판정 불가"를 관측 가능하게 노출
        index_stale_pending=stale_check.pending,  # 2-4 — "판정 불가"와 "판정 보류"를 값으로 구분
    )

    if log:
        _log_query(
            query=query, category_requested=category_requested,
            category_used=category_used, k=k_used, fallback=fallback_used,
            raw_max=raw_max, confidence=confidence, top=log_top, source=source,
            index_created_at=fingerprint[0],  # O2
            index_created_at_source="live",  # 2-6 — 이번 호출이 _open_index() 로 직접 연 색인
            coverage=coverage, n_query_tokens=n_query_tokens, n_matched_tokens=n_matched_tokens,
            deficient_axis=deficient_axis, caller_id=caller_id,
        )

    return result


def _hit_header_text(h: Hit, n: int, r: SearchResult) -> str:
    """format_result 의 헤더 줄을 만든다 — 예산 판정(plan_response_budget)과 실제 텍스트
    출력이 같은 문자열을 쓰게 하는 단일 근거다. JSON 경로(server._run_search)는 이
    문자열을 응답에 싣지 않지만 예산 크기 계산에는 그대로 쓴다(3차 배치 결함② 수정:
    이전엔 JSON 이 헤더 크기를 200자 고정으로 근사해 텍스트와 다른 개수를 생략했다).

    2-1(qa 독립검증, 2026-08-14) — `norm = weighted/max_weighted` 라 **1위는 품질과
    무관하게 항상 1.000** 인데("점수 1.000"), LLM 소비자는 이걸 "완벽 일치"로 읽는다
    (B6(code-reviewer, 2026-08-13)이 confidence!="ok" 일 때만 raw 를 병기해 일부 완화
    했으나, confidence="ok" 여도 raw_max 가 낮을 수 있는 경우 — 예: 짧은 질의 — 는
    여전히 가려졌다). 이제 ① "점수" 라벨 자체에 "상대값"임을 괄호로 명시하고
    ② raw 는 **confidence 와 무관하게 항상** 병기한다. `Hit.score` 필드 자체는 안
    건드린다(계약 유지 — 표기만 바뀐다).

    H2(qa 독립검증 후속, 2026-08-14) — 위 설명 문장을 원래 **hit 마다** 반복해서
    넣었더니(`"(질의 내 상대값 — 1위는 항상 1.000, 완벽 일치를 뜻하지 않음)"`, 항목당
    +38~50자) 응답 예산(plan_response_budget)이 그만큼 더 빨리 소진돼 **본문 생략
    건수가 늘었다**(리뷰 실측: 10개 질의×k 조합 중 4개에서 생략 1건 증가 — 예:
    `추천 알고리즘 가중치` k=10 생략 1→2, k=20 생략 11→12). 게다가 HTML 표면
    (`server._render_results`)은 원래 짧은 `(상대값)` 만 썼어서 **표면 간 문구도
    불일치**했다. 그래서 설명 문장은 `response_prelude()` 가 검색당 **1회만** 내고
    (아래), 이 헤더는 HTML 과 동일하게 축약한다 — "raw 상시 병기 + 상대값임이
    드러난다"는 2-1 의 원래 목적은 그대로 유지된다.
    """
    raw_str = f"{r.raw_scores[n - 1]:.2f}" if n - 1 < len(r.raw_scores) else "?"
    header = (
        f"[{n}] {h.rel} ({h.heading_path}) 줄 {h.start_line}~{h.end_line} · "
        f"점수 {h.score:.3f}(상대값) · raw {raw_str} · status={h.status}"
    )
    return header


def _hit_body_text(h: Hit) -> tuple[str, bool]:
    """h.text 에서 청크 머리의 '[rel] heading_path' prefix 를 떼고 config.MAX_CHARS_PER_HIT
    로 컷한 본문과 truncated 여부를 낸다(코드펜스 보정 포함). 이것도 예산 판정과 실제
    출력이 같은 값을 쓰게 하는 단일 근거다(_hit_header_text 와 같은 목적).
    """
    body = h.text
    expected_prefix = f"[{h.rel}] {h.heading_path}"
    first_line, sep, rest = body.partition("\n")
    if first_line == expected_prefix:
        body = rest if sep else ""

    truncated = False
    if len(body) > config.MAX_CHARS_PER_HIT:
        body = body[: config.MAX_CHARS_PER_HIT]
        truncated = True
        # L13(code-reviewer, 2026-08-13): 강제 분할된 조각은 코드펜스를 닫고 넘겨준다
        # (chunker.py 의 계약)지만, 여기서 2,500자로 다시 자르면 그 안에 홀수 개의
        # ``` 가 남아 펜스가 안 닫힌 채로 보일 수 있다 — 렌더러가 이후 텍스트를 전부
        # 코드블록으로 삼켜버리는 것을 막기 위해 닫는 펜스를 보충한다.
        if body.count("```") % 2 == 1:
            body += "\n```"
    return body, truncated


@dataclass
class BudgetItem:
    """plan_response_budget() 이 hit 하나당 내는 판정 결과.

    텍스트(format_result)·JSON(server._run_search) 두 경로가 이 값만 보고 렌더링하게
    해서 예산 판정(생략 여부)이 어긋나지 않게 한다 — 3차 배치 결함②: 이전엔 텍스트가
    실제 헤더 길이를, JSON 이 200자 고정 근사를 따로 써서 같은 질의·k 에서도 생략
    건수가 갈렸다(예: "정합감사 리포트" k=10 은 텍스트에선 0건, JSON 은 1건 생략됐었다).
    """

    include: bool  # True 면 본문을 싣는다
    rel: str  # 생략 시 omitted_files 를 채우는 용도
    block: list[str]  # 포함이면 [header, *body_lines, ""], 생략이면 [header, 안내줄, ""]
    chars_added: int  # 이 항목이 예산 누적(running)에 실제로 더한 문자수


def plan_response_budget(
    r: SearchResult, *, max_total_chars: int = RESPONSE_CHAR_BUDGET, prelude_chars: int = 0
) -> list[BudgetItem]:
    """각 hit 의 본문을 실을지 판정한다(item.include=True 면 본문 포함). 텍스트·JSON
    경로가 같은 판정을 쓰게 하는 단일 근거 — 항목 크기는 두 경로 모두 "헤더 실제 길이
    + 컷된 본문 길이 + 구분자"로 계산한다(_hit_header_text/_hit_body_text 를 그대로
    호출해 구한다 — 근사가 아니라 실제로 만든 문자열의 길이다). 1위 항목은 예산과
    무관하게 항상 포함한다(L14 계약을 그대로 이어받음).

    `prelude_chars` 는 호출자가 이미 소비한 시작값이다 — format_result·server._run_search
    둘 다 response_prelude() 로 실제 검색어·안내/경고 줄들을 만들어 그 길이 합을
    넘긴다. "항목 크기" 근사 불일치(헤더 200자 고정 vs 실제 93~293자 가변, 10항목
    누적 +1,675자)가 원래 결함②의 주된 원인이었지만, 그것만 고치고 시작값을 각자
    다르게(텍스트=실측, JSON=300 고정) 두면 완전 일치가 보장되지 않는다 — 실측
    "정합감사 리포트" k=20 에서, 안내/경고 줄이 하나도 없어 텍스트 실제 prelude 가
    17자 안팎인데 JSON 은 300 을 썼고, 그 차이(약 283자)가 13번째 항목(355자)
    하나의 포함 여부를 갈랐다. 그래서 시작값도 같은 함수로 통일한다.
    """
    items: list[BudgetItem] = []
    running = prelude_chars
    for n, h in enumerate(r.hits, start=1):
        header = _hit_header_text(h, n, r)
        body, truncated = _hit_body_text(h)
        body_block = [body]
        if truncated:
            body_block.append(f"...(잘림 — 원문 {h.rel} 줄 {h.start_line}~{h.end_line} 참고)")
        full_block = [header, *body_block, ""]
        full_chars = sum(len(l) + 1 for l in full_block)

        # L14: 1위 항목은 예산과 무관하게 항상 싣는다(최소 1건 보장 — 항목별
        # MAX_CHARS_PER_HIT 컷이 이미 있어 최대 ~2.7KB 로 유계다). 그 외엔 이 항목을
        # 더했을 때 예산을 넘기는지로 판정한다.
        if n == 1 or running + full_chars <= max_total_chars:
            include = True
            block = full_block
            chars_added = full_chars
        else:
            include = False
            block = [header, "  (응답 총량 예산 초과로 본문 생략 — 말미 안내 참고)", ""]
            chars_added = sum(len(l) + 1 for l in block)

        running += chars_added
        items.append(BudgetItem(include=include, rel=h.rel, block=block, chars_added=chars_added))
    return items


def _index_stale_warning_lines(r: SearchResult) -> list[str]:
    """색인이 원본보다 낡았다는 경고줄(요건 B-1 문구). **M4(2차 배치, 최우선 소프트웨어
    보정) — 이 신호는 hits 유무와 무관하게 유효하다**(confidence·hit 단위 stale/
    deprecated·n_missing 과 다르다 — 그것들은 "이미 나온 결과"에 대한 신뢰도/경고라
    hits 가 있어야 의미가 있지만, "원본이 색인보다 새롭다"는 hits 가 0건일 때 오히려
    **가장 값이 크다** — 방금 쓴 문서를 찾는 중인데 그게 아직 색인에 없어서 결과가
    0건인 상황이 정확히 그 경우다). 그래서 `response_prelude()`(hits 있음 경로)와
    `format_result()` 의 no-hits 분기가 이 헬퍼 하나를 공유한다 — 중복 구현하면 한쪽만
    고쳐지는 사고가 난다.
    """
    if not r.index_stale:
        return []
    if r.index_stale_docs > 0:
        return [
            f"[경고] 색인이 원본보다 낡았다({r.index_stale_docs}개 문서가 색인 이후 "
            f"변경됨). `python indexer.py` 를 실행하라."
        ]
    return [
        f"[경고] 색인이 원본보다 낡았을 수 있다"
        f"({r.index_stale_detail or '원본 디렉터리가 색인 이후 변경됨'}). "
        f"`python indexer.py` 를 실행하라."
    ]


def response_prelude(r: SearchResult, query: str) -> list[str]:
    """format_result 가 hit 순회 전에 실제로 만드는 검색어·안내/경고 줄들(카테고리
    무효/폴백 안내, 색인 stale 경고, **confidence 줄 — 저신뢰면 `[경고]`, 그 외엔
    `[신호]`(OK-1, 2026-08-18: ok 일 때 무신호였던 fail-silent 제거)**,
    stale/deprecated 경고, idx 정합 경고, 마지막 빈 줄)을 그대로 재현한다 — hit 순회부 예산 판정의 시작값을 텍스트·JSON 두
    경로가 똑같이 계산하게 하는 단일 근거다(3차 배치 결함② 잔여 불일치 수정,
    plan_response_budget docstring 참고). 4차 배치 B(원본 stale 감지)의 경고줄도 여기
    넣어 텍스트·JSON 양 경로의 예산 시작값이 계속 일치하게 한다(요건 B-6).

    이 함수는 format_result 의 "hits 있음" 분기와 같은 줄만 만든다 — r.hits 가 비어
    있을 때 format_result 가 내는 "결과 없음" 문구는 포함하지 않는다(그건 조기 반환
    경로라 애초에 hit 순회 예산 판정 자체가 없다). r.hits 가 빈 채로 호출돼도 예외는
    나지 않는다(호출자 쪽에서 이 값을 쓸 데가 없어질 뿐 — plan_response_budget 의
    for 루프가 비어 있으면 그냥 안 돈다).
    """
    lines: list[str] = [f"검색어: {query!r}"]

    if r.hits:
        # H2(qa 독립검증 후속, 2026-08-14) — "점수"가 상대값(1위=1.000 고정)이란 설명을
        # hit 마다 반복하지 않고 여기서 검색당 1회만 낸다(_hit_header_text docstring
        # 참고 — 반복하면 항목당 +38~50자가 응답 예산을 갉아먹어 본문 생략이 늘었다).
        lines.append(
            "[안내] 아래 '점수'는 이 질의 결과 안에서의 상대값이다(1위는 항상 1.000 — "
            "완벽 일치를 뜻하지 않음). 질의 간 비교·신뢰도 판단에는 'raw'(가중 전 "
            "원점수)를 참고하라."
        )

    if not r.category_valid:
        lines.append(
            f"[안내] 요청한 카테고리 '{r.category_requested}' 는 유효하지 않다 — "
            f"전체(all)로 검색했다. (유효 카테고리는 list_doc_categories 참고)"
        )
    elif r.fallback_used:
        # B1(code-reviewer, 2026-08-13): 카테고리 필터는 전체 코퍼스가 아니라 오버페치
        # 상위 fetch 건에 대한 **사후 필터**다 — 그 창 밖에 해당 카테고리 문서가 있어도
        # "결과가 없다"고 말했었다(실측: category="legal" 는 창 안엔 0건이지만 전역
        # 205위에 실존). 그 구조를 정확히 알리는 문구로 고쳤다.
        lines.append(
            f"[안내] 상위 후보 {r.fetch}건 안에 카테고리 '{r.category_requested}' 결과가 "
            f"없어 전체(all)로 폴백했다(카테고리 필터는 오버페치 후보에 대한 사후 필터다 "
            f"— 더 깊은 순위에는 있을 수 있다)."
        )

    lines.extend(_index_stale_warning_lines(r))  # M4/M5 — hits 유무와 무관한 경고

    if r.confidence in ("low", "none"):
        # 2-1(qa 독립검증, 2026-08-14) — raw_max 단축만 보이던 경고줄에 새 축(coverage)과
        # 미달 축(deficient_axis, `_deficient_axis()` 와 같은 계산)을 함께 남긴다 — 어느
        # 축이 부족한지(증거량 부족 vs 용어 불일치로 추정) 호출 에이전트가 바로 읽을 수 있다.
        axis = _deficient_axis(r.raw_max, r.coverage)
        lines.append(
            f"[경고] 이 질의는 매칭 신뢰도가 낮다(raw_max={r.raw_max:.2f}, "
            f"coverage={r.coverage:.2f} [{r.n_matched_tokens}/{r.n_query_tokens} 토큰 매칭], "
            f"미달축={axis or '?'}, confidence={r.confidence}) — 용어를 바꾸거나 파일을 "
            f"직접 Read 하라. (아래 결과는 참고용으로 낮은 확신도로 제공한다)"
        )
    else:
        # OK-1(dev-lead, 2026-08-18) — **침묵=ok 를 제거한다.** 수정 전에는 low/none 일
        # 때만 줄이 나가고 ok 일 때는 아무 줄도 안 나갔다 — 호출 에이전트 입장에서
        # "신호 없음"과 "ok"가 구분 불가였다(fail-silent). 그래서 ① 포맷 변경·예산 절단·
        # 버그로 경고줄이 유실돼도 소비자는 그것을 "ok 였다"로 읽고(과신 편향),
        # ② dev 4종 에이전트의 보고 템플릿이 `confidence=<ok|low|none>` 기입을 강제하는데
        # 정작 ok 일 때 출력에 그 값이 없어 에이전트가 값을 **추론**해야 했다.
        #
        # 세 상태(ok/low/none)의 출력 형태를 대칭으로 맞춘다 — 위 경고줄과 같은 수치
        # 필드(raw_max·coverage·매칭 토큰 수)를 싣고, `confidence=<값>` 리터럴을 그대로
        # 노출해 소비자가 추론 없이 옮겨 적을 수 있게 한다. 임계는 리터럴로 박지 않고
        # 상수(RAW_MAX_FLOOR·COVERAGE_FLOOR)에서 렌더한다 — 캘리브레이션이 바뀌면 문구가
        # 조용히 stale 해지는 경로를 아예 만들지 않는다(`_deficient_axis()` 와 같은 규약).
        #
        # `if not in (low, none)` 형태(== "ok" 가 아니라)인 이유: 미래에 등급이 늘어도
        # 이 분기가 **무신호로 되돌아가지 않게** 하려는 것이다. 값 자체를 그대로 찍으므로
        # 없는 등급을 ok 라고 사칭하지도 않는다.
        #
        # 배치: 이 줄은 prelude(헤더부)라 hit 본문 예산 절단(plan_response_budget)의
        # 대상이 아니다 — 예산이 아무리 빠듯해도 신호줄이 먼저 잘리는 일은 없다.
        # hits 가 0건이면 `_confidence()` 가 구조적으로 "none" 을 내고(raw_max<=0),
        # format_result 의 no-hits 분기는 애초에 이 함수를 부르지 않는다 — 즉 빈 결과에
        # ok 신호가 붙는 경로는 없다(tests/test_ok_signal.py 가 이 불변식을 지킨다).
        lines.append(
            f"[신호] confidence={r.confidence} (raw_max={r.raw_max:.2f}, "
            f"coverage={r.coverage:.2f} [{r.n_matched_tokens}/{r.n_query_tokens} 토큰 매칭]) "
            f"— 2축(raw_max>={RAW_MAX_FLOOR} 그리고 coverage>={COVERAGE_FLOOR}) 충족."
        )

    if any(h.status in ("stale", "deprecated") for h in r.hits):
        lines.append(
            "[경고] 결과에 stale/deprecated 로 표시된(구버전일 수 있는) 조각이 섞여 "
            "있다 — 각 항목의 status 를 확인하라."
        )

    if r.n_missing:
        # A2(code-reviewer, 2026-08-13): idx 정합 파손을 사용자에게도 드러낸다(stderr 는
        # 운영자만 본다 — 호출 에이전트가 직접 보는 채널에도 남겨야 한다).
        lines.append(
            f"[경고] 색인 정합 이상 — 후보 {r.n_missing}건을 sqlite 에서 찾지 못해 "
            f"건너뛰었다. 결과가 비정상적으로 적을 수 있다(indexer.py 재실행 검토)."
        )

    lines.append("")
    return lines


def format_result(r: SearchResult, query: str, *, max_total_chars: int = RESPONSE_CHAR_BUDGET) -> str:
    """MCP/사람 공용 텍스트 포맷.

    결과를 조용히 버리지 않는다 — confidence 가 낮아도 하드 드롭하지 않고 경고만
    붙인다(오탐으로 검색이 통째로 죽는 것을 방지). 말미의 Read(offset,limit) 안내는
    검색이 빗나가도 최악이 "파일 통째 읽기"로 안 떨어지게 하는 에스컬레이션 경로라
    문구를 빼지 않는다.

    L14(3차 배치, dev-lead, 2026-08-13): `max_total_chars` 는 응답 총량 상한이다
    (기본값 RESPONSE_CHAR_BUDGET — 근거는 그 상수 정의부 주석). 예산을 넘기면
    *조용히* 자르지 않는다 — 헤더 줄(경로·줄범위·점수·status)은 항상 남기고 본문만
    생략하며, 몇 건이 어느 파일에서 생략됐는지 말미에 명시한다. **1위 항목은 예산과
    무관하게 항상 본문을 포함**한다(검색이 완전히 빗나가도 최소 1건은 사람이 읽을
    거리를 남긴다). 예산 계산은 정확한 바이트 회계가 아니라 누적 문자수 근사치다
    (헤더·경고 줄도 대략 포함). 키워드 전용 인자라 기존 위치인자 호출(search.py
    CLI — eval.py 는 애초에 이 함수를 호출하지 않는다)은 그대로 동작한다.

    실제 판정은 plan_response_budget() 에, 시작값은 response_prelude() 에 위임한다
    (3차 배치 결함② 수정 — 이 함수는 그 결과(prelude 줄들 + BudgetItem.block)를 그대로
    이어붙일 뿐이라 리팩터 전과 출력이 동일하다. server._run_search 의 JSON 경로도
    같은 두 함수로 판정해 텍스트와 생략 건수·시작값이 더 이상 어긋나지 않는다).
    """
    if not r.hits:
        # response_prelude() 는 "hits 있음" 분기와 같은 줄만 만들기 때문에(confidence/
        # stale/n_missing 경고 없음) 여기서는 재사용하지 않고 검색어줄+category안내만
        # 직접 만든다 — 원본 로직(hits 없으면 그 경고들을 아예 안 만듦)과 동일하게.
        # **단 M4(2차 배치) — index_stale 경고만은 예외다**: hits 유무와 무관한 신호라
        # _index_stale_warning_lines() 를 여기서도 호출한다("방금 쓴 문서가 아직
        # 색인에 없어 결과가 0건"이 정확히 이 신호가 가장 값이 큰 상황이다).
        lines: list[str] = [f"검색어: {query!r}"]
        if not r.category_valid:
            lines.append(
                f"[안내] 요청한 카테고리 '{r.category_requested}' 는 유효하지 않다 — "
                f"전체(all)로 검색했다. (유효 카테고리는 list_doc_categories 참고)"
            )
        elif r.fallback_used:
            lines.append(
                f"[안내] 상위 후보 {r.fetch}건 안에 카테고리 '{r.category_requested}' 결과가 "
                f"없어 전체(all)로 폴백했다(카테고리 필터는 오버페치 후보에 대한 사후 필터다 "
                f"— 더 깊은 순위에는 있을 수 있다)."
            )
        lines.extend(_index_stale_warning_lines(r))  # M4
        lines.append("")
        lines.append(
            "결과 없음 — 용어를 바꿔 다시 검색하거나, 관련 파일을 Grep/Read로 직접 확인하라."
        )
        return "\n".join(lines)

    # L14: response_prelude() 가 검색어·안내/경고 줄들을 실제로 만든다 — 이걸 쓴 양부터
    # 누적 문자수 예산을 잰다(정확한 바이트 회계가 아니라 "\n".join 결과 크기의 근사치:
    # 각 줄 + 개행 1로 계산 — join 은 구분자가 N-1개뿐이라 근사치가 실제보다 살짝 크게
    # 나오는 안전한 방향의 오차다). 이 값을 plan_response_budget() 의 시작값으로 그대로
    # 넘긴다.
    lines = response_prelude(r, query)
    running_chars = sum(len(l) + 1 for l in lines)
    omitted_files: list[str] = []

    for item in plan_response_budget(r, max_total_chars=max_total_chars, prelude_chars=running_chars):
        lines.extend(item.block)
        if not item.include:
            omitted_files.append(item.rel)

    if omitted_files:
        # 같은 파일에서 여러 조각이 생략될 수 있어 순서를 보존한 채 중복 제거한다.
        uniq_files = list(dict.fromkeys(omitted_files))
        lines.append(
            f"[안내] 응답 총량 예산({max_total_chars:,}자) 초과로 {len(omitted_files)}건의 "
            f"본문을 생략했다 — 생략된 파일: {', '.join(uniq_files)}"
        )
        lines.append("")

    lines.append(
        "부족하면 해당 파일을 Read(offset=시작줄, limit=범위) 로 확대해서 읽어라 "
        "— 검색이 빗나가도 최악이 파일 통째 읽기로 떨어지지 않게 하는 안전장치다."
    )
    return "\n".join(lines)


def diagnose_status_weight_rank(query: str, target_substr: str) -> dict:
    """진단 전용 — search() 의 공개 계약이 아니다. status 가중이 실제로 순위를 옮기는지 확인한다.

    F4(dev-lead 재검증, 2026-08-13): eval.py #3 류 "stale 강등" 판정은 대상 문서가
    가중 없이도 이미 정본보다 아래였다면 STATUS_WEIGHT 가중 기전을 한 번도 밟지 않은
    채 통과하는 무정보 green 일 수 있다. 이 함수는 search() 의 사용자용 오버페치
    (fetch=k*5, 최대 100 개)가 아니라 **전체 코퍼스**를 스캔해 target_substr 을 rel 에
    포함하는 조각(청크 단위 — search() 의 Hit 과 같은 단위)을 ① raw(가중 전) 정렬
    순위 ② weighted(가중 후) 정렬 순위 두 기준으로 각각 찾는다. 두 순위를 비교하면
    가중이 실제로 순위를 밀어냈는지(=기전이 검증됐는지) 알 수 있다.

    search() 의 기존 코드 경로(공개 계약 — 시그니처·Hit/SearchResult 필드)는 전혀
    건드리지 않는 순수 추가 함수다.

    주의: weighted 값이 같은 근방에 조각이 밀집(타이 클러스터)해 있으면 정확한 순위
    숫자는 집계 단위(청크 vs 문서)에 따라 갈릴 수 있다 — 이 함수는 청크 단위로만
    집계한다(search() 의 Hit 과 동일 단위로 맞추기 위함).

    N4(2차 배치) — 이 함수는 `search()` 와 `_open_index()` 하나를 공유한다(예전엔 각자
    `_get_retriever()`+별도 `_get_conn()` 조합을 따로 구현해 같은 버그 클래스를 두 벌로
    안고 있었다). 이건 `eval.py` 회귀 게이트 #3(stale 강등 판정)의 근거이기도 해서,
    캐시가 낡으면 게이트가 엉뚱한 색인으로 PASS 를 낼 수 있었다 — 공유 헬퍼로 그
    위험을 한 곳만 고치면 되게 만든다. 또한 `_open_index()` 가 반환하는 `n_chunks` 는
    **그 호출에서 실제로 로드·검증된 retriever 자신의 `num_docs`**(M1 교차검증까지
    통과한 값)이므로, 아래 `retrieve(k=n_chunks)` 가 `k > num_docs` 로 bm25s
    `ValueError` 를 내는 경우가 구조적으로 없다(재현 시도와 결론은 2차 배치 보고 참고).
    """
    tokens = tokenizer.tokenize_query(query)
    if not tokens:
        return {
            "found_raw": False, "found_weighted": False,
            "raw_rank": None, "weighted_rank": None,
            "n_candidates": 0, "status": None, "weight": None,
        }

    conn, retriever, n_chunks, _fingerprint = _open_index()
    try:
        ids_arr, scores_arr = retriever.retrieve([tokens], k=n_chunks, show_progress=False)
        cand_ids = [int(x) for x in ids_arr[0]]
        cand_raw = [float(x) for x in scores_arr[0]]
        rows = _fetch_rows(conn, cand_ids)
    finally:
        conn.close()

    candidates: list[tuple[float, float, sqlite3.Row]] = []
    for cid, raw in zip(cand_ids, cand_raw):
        row = rows.get(cid)
        if row is None:
            continue
        weight = config.STATUS_WEIGHT.get(row["status"], 1.0)
        candidates.append((raw, raw * weight, row))

    raw_sorted = sorted(candidates, key=lambda c: c[0], reverse=True)
    weighted_sorted = sorted(candidates, key=lambda c: c[1], reverse=True)

    raw_rank = next((i for i, c in enumerate(raw_sorted, start=1) if target_substr in c[2]["rel"]), None)
    weighted_rank = next(
        (i for i, c in enumerate(weighted_sorted, start=1) if target_substr in c[2]["rel"]), None
    )

    target = next((c for c in candidates if target_substr in c[2]["rel"]), None)
    status = target[2]["status"] if target is not None else None
    weight = config.STATUS_WEIGHT.get(status, 1.0) if status is not None else None

    return {
        "found_raw": raw_rank is not None,
        "found_weighted": weighted_rank is not None,
        "raw_rank": raw_rank,
        "weighted_rank": weighted_rank,
        "n_candidates": len(candidates),
        "status": status,
        "weight": weight,
    }


if __name__ == "__main__":
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="sub-agents-RAG BM25 검색 CLI")
    parser.add_argument("query", help="검색어")
    parser.add_argument("--category", default="all", help="카테고리 (list_doc_categories 참고, 기본 all)")
    parser.add_argument("--k", type=int, default=config.DEFAULT_K, help=f"반환 개수 (기본 {config.DEFAULT_K})")
    parser.add_argument(
        "--no-log", action="store_true",
        help="이 호출은 질의 로그(logs/queries.jsonl)에 기록하지 않는다(검증·스모크용, M7)",
    )
    args = parser.parse_args()

    # H1(3) — 검색 전 동기 워밍. pending 창(부트스트랩 SWR)이 CLI 단발 검색에마저 보이면
    # "재색인하라" stale 경고가 빠질 수 있다 — CLI 는 프로세스 기동 자체가 이미 ~1초라
    # 이 동기 호출의 비용(수십 ms)은 무시할 수 있다. 그래서 CLI 단발 검색은 기존과 100%
    # 동일한 stale 경고를 낸다.
    refresh_staleness_now()

    # CLI 수동 확인은 api/mcp 실사용과 다른 트래픽이라 source 를 분리해 로그 분석 시
    # 재질의율 등 실사용 통계에 안 섞이게 한다. M7 — --no-log 는 검증·스모크 반복 호출이
    # R-001 근거 로그(source="cli")에 섞이는 것을 막는 CLI 전용 스위치다(search() 공개
    # 계약은 그대로 — log=False 만 넘긴다).
    _result = search(args.query, category=args.category, k=args.k, log=not args.no_log, source="cli")
    print(format_result(_result, args.query))
