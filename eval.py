"""검증 게이트 — 골든 쿼리(hit@3) + confidence 하한 실측(raw_max ∧ coverage 2축).

    python eval.py             # 골든 평가, 상위 3건만 표시(실패 질의는 top-DIAG_K 까지 진단)
    python eval.py --verbose   # 위 + 상위 k(substr 는 DIAG_K, relative 는 REL_CHECK_K) 전체 표시
    python eval.py --floor     # confidence 하한 측정 모드(확대 표본 + 오탐/미탐 자동 집계)

**동봉된 GOLDEN 은 examples/ 예제 코퍼스용이다** — 자기 코퍼스에 붙일 때는 GOLDEN ·
IN_DOMAIN_EXTRA 를 그 코퍼스의 실제 질의·기대 문서로 교체한다(아래 무결성 조건 준수).

무결성 조건 — 이 게이트가 자기순환이 되지 않게 하는 규칙:
  · 통과시키려고 glossary.tsv 에 항목을 끼워 넣거나, 골든 질의·기대값을 바꾸거나,
    가중치를 이 질의들에만 맞춰 튜닝하지 않는다. **이 파일은 config.py/tokenizer.py/
    glossary.tsv 를 읽기만 하고 절대 쓰지 않는다.**
  · 결과를 보고 나서 기대값을 맞추는 것은 부정행위다 — 표(GOLDEN)를 먼저 정의하고
    그 다음 돌린다. 실패는 숨기지 않고 원인과 함께 그대로 보고한다.
  · search()·format_result() 는 전부 `log=False, source="eval"` 로 호출한다(이중
    방어 — search.py DECISIONS 참고: 애초에 로그를 안 남기고, 혹시 나중에 log=True 로
    잘못 켜져도 source="eval" 로 질의 로그 분석 시 걸러진다).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import config
import tokenizer
from search import COVERAGE_FLOOR, RAW_MAX_FLOOR, diagnose_status_weight_rank, search

HIT_AT = 3          # substr 판정 질의의 hit@N — 판정 기준(변경 안 함)
# 게이트: 골든의 80% 이상 통과(원 프로젝트의 8/10 과 같은 비율) — 미달이면 exit code != 0.
# len(GOLDEN) 에 따라 자동 산출되므로 골든을 교체해도 이 줄은 손대지 않는다.

# F3(dev-lead 재검증, 2026-08-13): 판정은 HIT_AT(top-3) 그대로 두되, 실패 시 "기대
# 문서의 실제 순위"를 보여주기 위한 진단 전용 깊이. HIT_AT=3·DIAG_K=10 모두
# max(50, k*5)=50 이라 fetch 가 동일해 top-3 판정 결과에는 영향이 없다(같은 후보 풀).
DIAG_K = 10

# relative 질의(상대 순위 판정) 전용 — "넉넉히"를 구체화한 값. hit@3 보다 넓게 봐서
# "stale 강등이 순위를 밀어내되 top-3 밖으로 아예 사라지진 않았는지"까지 관찰 가능하게
# 한다. config.MAX_K(20)보다는 작게 잡아 통상적인 호출 규모를 벗어나지 않게 했다.
# L11/L12(code-reviewer, 2026-08-13): DIAG_K 와 값이 우연히 같다(둘 다 10) — 관계는
# 없다. DIAG_K 는 substr 질의(kind="substr")의 진단 깊이, REL_CHECK_K 는 #3(상대 순위,
# kind="relative")의 판정 깊이 자체다(진단이 아니라 판정 기준의 일부). 같은 숫자를
# 쓴 건 "둘 다 hit@3 보다 넉넉히"라는 같은 취지일 뿐, 하나를 바꿔도 다른 하나는
# 안 바뀐다 — 나중에 둘 중 하나만 조정할 사람을 위해 이 독립성을 명시한다.
REL_CHECK_K = 10

# ── 골든 질의 (결과를 보기 전에 먼저 고정한다) ────────────────────────────────
# 아래는 **동봉 예제 코퍼스(examples/docs/)용 데모 골든**이다. 자기 코퍼스에 붙일 때는
# 이 표를 그 코퍼스의 실제 질의로 교체한다. 항목 형식:
#   · kind="substr"   — expect 가 top-HIT_AT 결과의 rel(상대경로)에 부분문자열로 있으면 통과
#   · kind="relative" — expect_above(정본)가 expect_below(stale)보다 위 순위면 통과
#                       (stale 강등 기전 검증 — 두 값은 상호 부분문자열이면 안 된다.
#                        _selfcheck_substr_disjoint 가 자동 확인한다)
# 권장 구성: ID 검색 · 한글 질의 · 영문 질의 · 용어집 확장 경유 · stale 강등, 각 1건 이상.
GOLDEN: list[dict] = [
    dict(n=1, query="API-002", kind="substr",
         expect="api-spec", note="ID"),
    dict(n=2, query="요청 한도 정책", kind="relative",
         expect_above="api-spec_v1", expect_below="api-spec_v0",
         note="stale 강등 — v1(current) 이 v0(stale) 보다 위여야 함"),
    dict(n=3, query="온보딩 설치 절차", kind="substr",
         expect="guide-getting-started", note="한글 질의"),
    dict(n=4, query="검색엔진", kind="substr",
         expect="guide-getting-started", note="용어집 확장(검색엔진→search_engine, 문서엔 영문만 존재)"),
    dict(n=5, query="rate limit exceeded", kind="substr",
         expect="api-spec", note="영문 질의"),
]

PASS_THRESHOLD = -(-len(GOLDEN) * 8 // 10)  # ceil(0.8·N) — 원 프로젝트의 8/10 과 같은 비율

# ── RAW_MAX_FLOOR 측정용 질의 집합 (--floor 전용, GOLDEN 과 별개) ──────────────
# 도메인 안: GOLDEN + 아래 추가분. **짧은 질의(토큰 1~4개)를 표본에 넣어야 raw_max 의
# 토큰수 비례 편향이 드러난다** — 원 프로젝트 실측에서 토큰 3개 이상만으로 구성한 표본이
# 이 편향을 완전히 놓쳤다(도메인 안 최소를 6.946 으로 오판 — 실제는 1.880). 자기
# 코퍼스로 교체할 때도 짧은 질의를 반드시 섞을 것.
IN_DOMAIN_EXTRA: list[str] = [
    "설치 요구사항",
    "에러 코드",
    "설치",        # 짧은 질의(토큰 1~2개) — 토큰수 비례 편향 관측용
    "API-001",
    "endpoint",
]

# 도메인 밖: glossary.tsv 등재 토큰을 의도적으로 피했다 — 섞이면 질의확장으로 도메인 안
# 문서가 걸려 "도메인 밖" 표본이 오염된다. 자기 코퍼스로 교체할 때도 같은 규칙을 지킬 것.
OUT_DOMAIN: list[str] = [
    "블록체인 채굴 난이도 조정",
    "라면 끓이는 법 황금 레시피",
    "quantum chromodynamics lattice",
    "qqzzxxvv",
    "축구 국가대표 명단 발표",
    "달의 뒷면 탐사 계획",
    "electric guitar amplifier settings",
    "김치찌개",
    "손흥민",
    "photosynthesis",
    "베토벤 교향곡",
]


def _eval_substr(item: dict) -> dict:
    # F3(dev-lead 재검증): k=DIAG_K 로 더 깊이 받아오되 판정은 상위 HIT_AT(3)만 본다.
    # HIT_AT·DIAG_K 모두 fetch=max(50,k*5)=50 이라(위 상수 주석 참고) 두 깊이의 후보 풀은
    # 완전히 동일하다 — 즉 r.hits[:HIT_AT] 는 k=HIT_AT 로 직접 불렀을 때와 100% 같은
    # 결과다(판정 기준을 바꾸지 않았다는 근거).
    r = search(item["query"], category="all", k=DIAG_K, log=False, source="eval")

    hit_rank = None
    for i, h in enumerate(r.hits[:HIT_AT], start=1):
        if item["expect"] in h.rel:
            hit_rank = i
            break

    # 실패 시 진단: top-HIT_AT 밖이라도 top-DIAG_K 안에서 실제 순위를 찾는다.
    diag_rank = None
    if hit_rank is None:
        for i, h in enumerate(r.hits, start=1):
            if item["expect"] in h.rel:
                diag_rank = i
                break

    return dict(item=item, passed=hit_rank is not None, result=r, hit_rank=hit_rank, diag_rank=diag_rank)


def _eval_relative(item: dict) -> dict:
    r = search(item["query"], category="all", k=REL_CHECK_K, log=False, source="eval")

    rank_above = None  # expect_above (정본, 예: api-spec_v1)
    rank_below = None  # expect_below (stale, 예: api-spec_v0)
    for i, h in enumerate(r.hits, start=1):
        if rank_above is None and item["expect_above"] in h.rel:
            rank_above = i
        if rank_below is None and item["expect_below"] in h.rel:
            rank_below = i

    # 판정 규칙 (명시적으로 정함 — 부분문자열은 상호 배타 확인됨, 아래 self-check 참고):
    #   둘 다 있음         → 정본 순위가 stale 순위보다 낮은 숫자(더 위)여야 통과
    #   정본만 top-K 안    → stale 이 top-K 밖으로 완전히 밀려난 것 = 강등의 강화판 → 통과
    #   stale 만 top-K 안  → 바로 이 테스트가 잡으려는 실패(정본이 강등판에 가려짐) → 실패
    #   둘 다 top-K 밖     → 이 질의로는 정본을 못 찾음 → 실패
    if rank_above is not None and rank_below is not None:
        passed = rank_above < rank_below
        detail = f"{item['expect_above']}={rank_above}위, {item['expect_below']}={rank_below}위"
    elif rank_above is not None:
        passed = True
        detail = f"{item['expect_above']}={rank_above}위, {item['expect_below']}=top-{REL_CHECK_K} 밖(강한 강등, 통과 처리)"
    elif rank_below is not None:
        passed = False
        detail = f"{item['expect_above']}=top-{REL_CHECK_K} 밖, {item['expect_below']}={rank_below}위 — 정본이 stale 에 가려짐"
    else:
        passed = False
        detail = f"둘 다 top-{REL_CHECK_K} 밖"

    # F4(dev-lead 재검증, 2026-08-13): 이 판정이 STATUS_WEIGHT 가중 기전을 실제로
    # 밟았는지 관측한다(전체 코퍼스 스캔, search() 의 판정용 오버페치와 무관 — 진단
    # 전용). 가중 전/후 순위가 같으면(또는 대상이 가중 없이도 이미 위였으면) 이 판정은
    # 가중치가 0 이어도 통과했을 무정보 green 이라는 뜻이다 — #3 판정 기준 자체는
    # 바꾸지 않고 이 사실을 별도 관측 줄로만 드러낸다.
    diag = diagnose_status_weight_rank(item["query"], item["expect_below"])

    return dict(item=item, passed=passed, result=r, detail=detail, diag=diag)


def _selfcheck_substr_disjoint() -> None:
    """relative 질의의 expect 쌍이 실제로 상호 배타적인지 직접 확인한다(추측 금지).

    L9(code-reviewer, 2026-08-13): bare assert 는 `python -O` 로 실행하면 통째로
    사라진다 — 이 게이트가 지키려는 불변식이 최적화 플래그 하나로 무음 스킵되면 안
    되므로 명시적 예외로 바꿨다. GOLDEN 을 교체해도 자동으로 새 쌍을 검사한다.
    """
    for item in GOLDEN:
        if item["kind"] != "relative":
            continue
        above, below = item["expect_above"], item["expect_below"]
        if above in below:
            raise SystemExit(f"[무결성 오류] {above!r} 이 {below!r} 안에 포함됨 — 판정 로직 재검토 필요")
        if below in above:
            raise SystemExit(f"[무결성 오류] {below!r} 이 {above!r} 안에 포함됨 — 판정 로직 재검토 필요")


def _print_hits(result, limit: int) -> None:
    shown = result.hits[:limit]
    for i, h in enumerate(shown, start=1):
        print(f"       {i}. {h.rel} · score={h.score:.3f} · status={h.status}")
    if len(result.hits) > limit:
        print(f"       ... (총 {len(result.hits)}건 중 {limit}건 표시)")


def _print_index_meta() -> None:
    """B7(code-reviewer, 2026-08-13): 이 게이트가 어떤 색인을 검증했는지 남긴다(재현성) —
    indexer.py 가 이미 `meta` 테이블에 적어둔 값을 그대로 읽어 찍는다(별도 계산 없음)."""
    try:
        conn = sqlite3.connect(str(config.INDEX_DIR / "chunks.sqlite"))
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        print(
            f"[색인] created_at={meta.get('created_at', '?')} · "
            f"n_docs={meta.get('n_docs', '?')} · n_chunks={meta.get('n_chunks', '?')} · "
            f"docs_root={meta.get('docs_root', '?')}\n"
        )
    except Exception as e:
        print(f"[색인] meta 조회 실패({type(e).__name__}: {e}) — 색인이 없거나 손상됐을 수 있다.\n")


def run_golden(verbose: bool) -> int:
    _print_index_meta()
    _selfcheck_substr_disjoint()

    results = []
    for item in GOLDEN:
        fn = _eval_relative if item["kind"] == "relative" else _eval_substr
        results.append(fn(item))

    n_pass = sum(1 for r in results if r["passed"])

    print("=== 골든 쿼리 평가 (hit@3, relative 항목은 상대 순위 판정) ===\n")
    for r in results:
        item, res = r["item"], r["result"]
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{item['n']:>2}] {status}  질의: {item['query']!r}  ({item['note']})")

        if item["kind"] == "substr":
            if r["hit_rank"] is not None:
                print(f"     기대 '{item['expect']}' 실제 순위: {r['hit_rank']}위 / top-{HIT_AT}")
            elif r["diag_rank"] is not None:
                # F3(dev-lead 재검증): 실패해도 "기대 문서의 실제 순위"가 보이게 한다.
                print(f"     기대 '{item['expect']}' — top-{HIT_AT} 밖 (실제 {r['diag_rank']}위 / top-{DIAG_K} 안에서 발견)")
            else:
                print(f"     기대 '{item['expect']}' — top-{DIAG_K} 밖")
        else:
            print(f"     {r['detail']}")
            diag = r["diag"]
            if diag["found_raw"] and diag["found_weighted"]:
                shifted = diag["raw_rank"] != diag["weighted_rank"]
                print(
                    f"     stale 강등 관측(전체 코퍼스, 청크 단위): 가중 없음 {diag['raw_rank']}위 → "
                    f"가중 적용 {diag['weighted_rank']}위 (status={diag['status']} ×{diag['weight']})"
                )
                if not shifted:
                    print("     [주의] 가중이 순위를 바꾸지 않음 — 이 판정은 강등 기전을 검증하지 못한다")
            else:
                print(
                    f"     stale 강등 관측: 대상 조각을 전체 코퍼스({diag['n_candidates']}개) 스캔에서도 "
                    f"찾지 못함(raw 발견={diag['found_raw']}, weighted 발견={diag['found_weighted']})"
                )

        limit = len(res.hits) if verbose else min(3, len(res.hits))
        print(f"     상위 {limit}건:")
        _print_hits(res, limit)
        print(f"     raw_max={res.raw_max:.3f} confidence={res.confidence}")
        print()

    print(f"=== {n_pass}/{len(GOLDEN)} 통과 (게이트 기준 {PASS_THRESHOLD}/{len(GOLDEN)} 이상) ===")
    if n_pass < PASS_THRESHOLD:
        print("[게이트 미달] 원인 분석 후 진행 판단이 필요하다 — 숨기지 말고 그대로 보고할 것.")
    return 0 if n_pass >= PASS_THRESHOLD else 1


def run_floor() -> None:
    print("=== RAW_MAX_FLOOR 측정 (도메인 안/밖 raw_max 분포, 확대 표본) ===\n")
    _print_index_meta()

    # rows: (label, query, raw_max, weighted_max, confidence, n_tokens)
    rows: list[tuple[str, str, float, float, str, int]] = []

    def _measure(label: str, queries: list[str]) -> None:
        for q in queries:
            r = search(q, category="all", k=5, log=False, source="eval")
            weighted = [
                raw * config.STATUS_WEIGHT.get(h.status, 1.0)
                for raw, h in zip(r.raw_scores, r.hits)
            ]
            wmax = max(weighted, default=0.0)
            n_tokens = len(tokenizer.tokenize_query(q))
            # r.confidence 는 search 의 현행 채택 하한(raw_max ∧ coverage)으로 이미 판정된 값이다 —
            # 여기서 별도로 임계 로직을 재구현하지 않고 그대로 재사용한다(중복 방지).
            rows.append((label, q, r.raw_max, wmax, r.confidence, n_tokens))

    _measure("도메인 안", [g["query"] for g in GOLDEN])
    _measure("도메인 안", IN_DOMAIN_EXTRA)
    _measure("도메인 밖", OUT_DOMAIN)

    print(f"{'구분':<10} {'질의':<34} {'raw_max':>10} {'weighted_max':>14} {'토큰':>4} {'판정':<6}")
    print("-" * 84)
    for label, q, raw_max, wmax, conf, n_tokens in rows:
        print(f"{label:<10} {q:<34} {raw_max:>10.3f} {wmax:>14.3f} {n_tokens:>4} {conf:<6}")

    in_rows = [r for r in rows if r[0] == "도메인 안"]
    out_rows = [r for r in rows if r[0] == "도메인 밖"]
    in_vals = [r[2] for r in in_rows]
    out_vals = [r[2] for r in out_rows]

    print()
    print(f"도메인 안 raw_max: 최소 {min(in_vals):.3f} · 최대 {max(in_vals):.3f}  (n={len(in_vals)})")
    print(f"도메인 밖 raw_max: 최소 {min(out_vals):.3f} · 최대 {max(out_vals):.3f}  (n={len(out_vals)})")
    print()
    if max(out_vals) < min(in_vals):
        print(
            f"분포 분리됨 — 겹치는 구간 없음. "
            f"도메인 밖 최대 {max(out_vals):.3f} < 도메인 안 최소 {min(in_vals):.3f} "
            f"(간격 폭 {min(in_vals) - max(out_vals):.3f})"
        )
    else:
        print(
            f"[주의] 분포가 겹친다 — 도메인 밖 최대 {max(out_vals):.3f} >= "
            f"도메인 안 최소 {min(in_vals):.3f}. 오탐(도메인 안을 low 로 찍음) 이 "
            f"미탐보다 싸다는 전제로 마진을 판단할 것."
        )

    # F2(dev-lead 재검증, 2026-08-13): 현재 RAW_MAX_FLOOR 기준 오탐/미탐을 눈으로 세지
    # 않고 여기서 자동 집계한다.
    in_false = [r for r in in_rows if r[4] in ("low", "none")]      # 도메인 안인데 저신뢰로 오분류
    out_missed = [r for r in out_rows if r[4] == "ok"]              # 도메인 밖인데 ok 로 놓침
    out_caught = [r for r in out_rows if r[4] in ("low", "none")]   # 도메인 밖을 정확히 잡음
    # B8(code-reviewer, 2026-08-13): 도메인 밖 표본 중 상당수는 raw_max 가 정확히 0.000 이라
    # 임계값과 무관하게 _confidence() 의 "raw_max<=0 → none" 분기가 잡는 자유 정답이다
    # — 이걸 "정탐"에 섞어 세면 임계 자체의 변별력이 부풀려 보인다. 그래서 **임계가 실제로
    # 개입하는 표본(raw_max>0)만 뗀 수치**를 따로 병기한다.
    out_rows_gated = [r for r in out_rows if r[2] > 0]
    out_caught_gated = [r for r in out_rows_gated if r[4] in ("low", "none")]

    # 2026-08-14: confidence 는 단일축이 아니라 **2축 연접**이다 — raw_max 하한만 적으면
    # 아래 오탐/미탐이 어느 축에서 났는지 오독된다. 두 축을 함께 표기한다.
    print(
        f"\n--- 오탐/미탐 요약 (기준: search.RAW_MAX_FLOOR = {RAW_MAX_FLOOR} "
        f"∧ search.COVERAGE_FLOOR = {COVERAGE_FLOOR}) ---"
    )
    print(
        f"도메인 안 오탐(사실 안인데 low/none): {len(in_false)}/{len(in_rows)}건"
        + (" — " + ", ".join(f"{r[1]!r}({r[2]:.3f})" for r in in_false) if in_false else "")
    )
    print(
        f"도메인 밖 미탐(사실 밖인데 ok): {len(out_missed)}/{len(out_rows)}건"
        + (" — " + ", ".join(f"{r[1]!r}({r[2]:.3f})" for r in out_missed) if out_missed else "")
    )
    print(f"도메인 밖 정탐(전체, low/none 으로 정확히 잡음): {len(out_caught)}/{len(out_rows)}건")
    print(
        f"  ↳ 임계 판별 표본만(raw_max>0 — <=0 자동분기 제외): "
        f"{len(out_caught_gated)}/{len(out_rows_gated)}건. "
        f"나머지 {len(out_rows) - len(out_rows_gated)}건은 raw_max=0.000 이라 임계와 무관하게 "
        f"항상 잡힌다(두 하한을 어떤 값으로 바꿔도 결과가 같다)."
    )
    print(
        "\n※ 이 하한은 하드 드롭이 아니라 조언(soft advisory)이다 — 오탐 비용은 경고 한 줄,"
        " 미탐 비용은 경고 누락 한 줄뿐이라 위 오탐/미탐이 0이 아니어도 즉시 위험은 아니다"
        "(search.py RAW_MAX_FLOOR 주석 참고)."
    )
    print(
        "※ 한계(B8): 도메인 밖 표본은 glossary.tsv 등재어를 의도적으로 피해 구성했다(질의"
        "확장으로 도메인 안 문서가 걸려 표본이 오염되는 것을 막기 위함) — 그 결과 **등재어와"
        " 어휘가 인접한 무관 질의**는 이 표본에 없다. 실사용에서 가장 흔할 실패 모드(용어는"
        " 비슷한데 의도는 다른 질의)는 아직 측정되지 않았다 — 표본 확대는 후속 과제로 남긴다."
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    # L10(code-reviewer, 2026-08-13): 이전엔 "인자 문자열에 포함되는지"로만 판정해서
    # 오타(예: `--flor`)가 조용히 골든 게이트를 정상 실행하고 종료 코드도 정상으로
    # 끝냈다 — 사용자는 --floor 를 돌린 줄 알지만 실제로는 다른 걸 돌린 것이다. argparse
    # 로 바꿔 미지 인자는 에러로 걸러지고 `--help` 도 생긴다.
    parser = argparse.ArgumentParser(description="docs-rag-mcp 검증 게이트")
    parser.add_argument("--verbose", action="store_true", help="상위 k 전체 표시(F3 이후: 실패한 substr 질의도 top-DIAG_K 까지)")
    parser.add_argument("--floor", action="store_true", help="confidence 하한 측정 모드(도메인 안/밖 raw_max 분포 + 2축 기준 오탐/미탐 집계)")
    args = parser.parse_args()

    if args.floor:
        run_floor()
        return 0

    return run_golden(verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
