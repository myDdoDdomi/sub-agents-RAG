"""질의 로그(logs/queries.jsonl) 판독기 — v1/v2 두 세대를 모두 읽어 R-001(DECISIONS.md)
하이브리드 전환 판정 근거를 정리한다.

**read-only** — 이 모듈은 로그 파일을 절대 쓰지 않는다(open() 호출이 전부 "r" 모드다).
실 색인(index/chunks.sqlite)도 읽기 전용(mode=ro URI)으로만 연다.

사용:
    python log_reader.py                       # logs/queries.jsonl 요약 리포트
    python log_reader.py --log <경로>          # 다른 로그 파일 지정(테스트·격리 검증용)
    python log_reader.py --log <경로> --index-dir <경로>  # L5 — 그 로그와 같은 세대의
                                                # 색인을 명시(안 맞추면 기준선 불순이 오탐된다)
    python log_reader.py --json                # 리포트를 JSON 으로 출력(자동화용)

2-6(qa 독립검증, 2026-08-14) — DECISIONS.md R-001 이 이 로그를 하이브리드 전환 판정의
**유일한 근거**로 지정했는데, 두 전환 조건 모두 기존 스키마(v1)로는 측정 불가였다(성패
라벨·세션 상관 ID 부재). search.py 가 스키마 v2(schema_version·run_id/pid/seq·
caller_id·coverage/n_query_tokens/n_matched_tokens/deficient_axis)로 그 공백 일부를
메웠지만, **여전히 측정 불가능한 부분이 남는다** — 이 판독기는 그 경계를 "측정
가능/불가능 + 이유"로 정직하게 보고한다(측정 불가를 측정 가능으로 포장하지 않는다).

v1 줄은 절대 변조하지 않는다 — `schema_version` 키의 **부재**가 v1 판정 기준이다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import config

DEFAULT_LOG_PATH = config.RAG_ROOT / "logs" / "queries.jsonl"


def load_entries(log_path: Path) -> tuple[list[dict], list[str]]:
    """JSONL 을 줄 단위로 읽는다. 손상된 줄은 건너뛰고 경고 문자열로 모은다(전체 로드가
    한 줄 때문에 멈추지 않는다 — read-only 진단 도구의 태도, best-effort).

    각 항목에 내부용 `_schema_version`(int, 1 또는 2)·`_lineno`(1-based)를 계산해
    붙인다 — `schema_version` 키의 **부재**가 v1(기존 줄을 변조하지 않고 판독기가
    세대를 구분한다는 §2-6 제약).
    """
    entries: list[dict] = []
    warnings: list[str] = []
    if not log_path.exists():
        return entries, warnings
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                warnings.append(f"{lineno}행 JSON 파싱 실패(건너뜀): {type(e).__name__}: {e}")
                continue
            if not isinstance(obj, dict):
                warnings.append(f"{lineno}행이 JSON 객체가 아니다(건너뜀): {type(obj).__name__}")
                continue
            obj["_schema_version"] = 2 if "schema_version" in obj else 1
            obj["_lineno"] = lineno
            entries.append(obj)
    return entries, warnings


def known_corpus_rels(index_dir: Path | None = None) -> set[str] | None:
    """실 색인 sqlite 에서 현재 코퍼스의 rel(문서 상대경로) 전체 집합을 읽기전용으로
    가져온다. 색인이 없거나 조회에 실패하면 `None`(=판정 불가) — 호출자가 이걸로
    "확인 불가"와 "확인했더니 0건"을 구분해야 한다.
    """
    idx_dir = index_dir or config.INDEX_DIR
    sqlite_path = Path(idx_dir) / "chunks.sqlite"
    if not sqlite_path.exists():
        return None
    try:
        uri = sqlite_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("SELECT DISTINCT rel FROM chunks").fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()
    except Exception:
        return None


def find_baseline_impurity(entries: list[dict], known_rels: set[str] | None) -> list[dict]:
    """`top[].rel` 이 현재 코퍼스에 없는 줄을 찾는다(**탐지만 — 삭제하지 않는다**, §2-6
    명시 요구 — 삭제 판단은 사용자 몫). `known_rels=None`(색인 미접근)이면 빈 리스트를
    반환한다 — 호출자는 `known_rels is None` 으로 "판정 불가"를 구분해야 한다.
    """
    if known_rels is None:
        return []
    impure: list[dict] = []
    for e in entries:
        # L6(qa 독립검증 후속, 2026-08-14) — `top[]` 원소에 `rel` 키가 없는 손상 줄이면
        # `h.get("rel")` 이 `None` 이라 이 집합에 `None` 과 `str` 이 섞일 수 있다 —
        # Python 3 는 `None`·`str` 비교를 못 해 `sorted()` 가 `TypeError` 로 죽는다
        # (판독기 전체 로드가 그 한 줄 때문에 멈추면 안 된다는 이 모듈의 원칙 위반).
        # `key=str` 로 어떤 타입이 섞여도 정렬 가능하게 한다(None 은 그대로 남겨 손상
        # 사실 자체는 보여준다 — 조용히 걸러내지 않는다).
        bad_rels = sorted(
            {h.get("rel") for h in e.get("top", []) if isinstance(h, dict) and h.get("rel") not in known_rels},
            key=str,
        )
        if bad_rels:
            impure.append({
                "lineno": e["_lineno"], "ts": e.get("ts"), "query": e.get("query"),
                "bad_rels": bad_rels,
            })
    return impure


def find_missing_index_created_at(entries: list[dict]) -> list[dict]:
    """`index_created_at` 이 결측인 줄을 찾는다(v1 전체 + v2 라도 unavailable/None 인
    경우 — index_created_at_source 로 그 이유를 함께 낸다, v1 은 그 필드 자체가 없어
    None 으로 뜬다).
    """
    out: list[dict] = []
    for e in entries:
        if e.get("index_created_at") is None:
            out.append({
                "lineno": e["_lineno"], "ts": e.get("ts"),
                "schema_version": e["_schema_version"],
                "index_created_at_source": e.get("index_created_at_source"),
            })
    return out


def confidence_axis_breakdown(entries: list[dict]) -> dict:
    """2-1 이 남긴 `deficient_axis`(v2 전용)로 저신뢰(low/none) 원인을 coverage 미달
    (용어 불일치로 추정) vs raw_max 미달(증거량 부족) vs 무매칭으로 나눈다.

    **주의** — 이건 R-001 조건 1("문서에 있는데 용어가 달라 못 찾음")의 정확한 측정이
    아니라 근사 대리 지표다: 정답이 애초에 코퍼스에 없는 질의(2-1 라벨셋의 B/C 그룹처럼)
    도 coverage 가 낮게 나오므로, "coverage 미달"과 "용어가 달라 못 찾음"은 동치가 아니다.
    """
    v2 = [e for e in entries if e["_schema_version"] == 2]
    low_or_none = [e for e in v2 if e.get("confidence") in ("low", "none")]
    buckets: dict[str, int] = defaultdict(int)
    for e in low_or_none:
        axis = e.get("deficient_axis") or "(미상 — v2 인데 deficient_axis 결측)"
        buckets[axis] += 1
    return {
        "v2_total": len(v2),
        "v2_low_or_none": len(low_or_none),
        "axis_buckets": dict(buckets),
    }


def r001_condition1_report(entries: list[dict]) -> dict:
    """R-001 조건 1 — "문서에 있는데 용어가 달라 못 찾음" > 20%. 정직한 판정."""
    axis = confidence_axis_breakdown(entries)
    coverage_deficient = sum(n for a, n in axis["axis_buckets"].items() if "coverage" in a)
    share = (coverage_deficient / axis["v2_low_or_none"]) if axis["v2_low_or_none"] else None
    return {
        "measurable": False,
        "reason": (
            "정답 존재 여부(성패) 라벨이 로그에 없다 — coverage/deficient_axis(v2)로 "
            "'저신뢰 중 용어 불일치로 추정되는 비율'은 대리 지표로 낼 수 있지만, 그게 "
            "곧 '정답이 있는데 못 찾음'의 정확한 비율은 아니다(정답이 애초에 코퍼스에 "
            "없는 질의도 coverage 가 낮게 나온다 — 채택 지표 2-1 라벨셋의 B/C 그룹이 "
            "정확히 이 경우다). 실제 측정에는 클릭/채택 등 성패 신호 계측이 별도로 필요하다."
        ),
        "proxy_v2_low_or_none": axis["v2_low_or_none"],
        "proxy_v2_total": axis["v2_total"],
        "proxy_coverage_deficient_share_among_low_or_none": share,
    }


def r001_condition2_report(entries: list[dict]) -> dict:
    """R-001 조건 2 — 재질의(같은 의도로 2회 이상 검색) > 30%. `caller_id` 채택률과
    함께 정직하게 보고한다("사실상 불가"가 기본값 — 브리프 §2-6 명시).
    """
    v2 = [e for e in entries if e["_schema_version"] == 2]
    with_caller = [e for e in v2 if e.get("caller_id")]
    if not with_caller:
        return {
            "measurable": False,
            "reason": (
                "caller_id 를 넘긴 호출이 아직 0건이다(선택적 신규 필드, §2-6) — 호출자가 "
                "채택하기 전까지는 run_id/pid 만으로는 세션·서브에이전트를 못 가른다(한 "
                "MCP 서버 프로세스를 여러 서브에이전트가 공유할 수 있어서다). 재질의율은 "
                "여전히 측정 불가 — '해결'이 아니라 '선택적으로 가능해진 것'뿐이다."
            ),
            "v2_total": len(v2), "with_caller_id": 0,
            "requery_rate_among_caller_id": None,
        }
    # caller_id 가 있는 호출만 대상으로 "같은 caller_id 안에서 정규화 질의 문자열이
    # 재등장"을 재질의로 근사한다 — 의도 동일성까지는 판정 못 하는 best-effort 근사다.
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in with_caller:
        groups[e["caller_id"]].append(e)
    n_requery = 0
    n_total_in_group = 0
    for _caller, es in groups.items():
        es_sorted = sorted(es, key=lambda x: x.get("seq", 0))
        n_total_in_group += len(es_sorted)
        seen_q: set[str] = set()
        for e in es_sorted:
            qn = (e.get("query") or "").strip().lower()
            if qn in seen_q:
                n_requery += 1
            seen_q.add(qn)
    rate = (n_requery / n_total_in_group) if n_total_in_group else None
    return {
        "measurable": True,
        "reason": (
            "caller_id 가 있는 호출에 한해 근사 측정한다(질의 문자열 재등장 기준 — 의도 "
            "동일성까지는 판정 못 한다, best-effort)."
        ),
        "v2_total": len(v2), "with_caller_id": len(with_caller),
        "requery_rate_among_caller_id": rate,
        "caveat": "caller_id 미채택 트래픽(대다수)은 이 비율에서 통째로 빠진다 — 전사 재질의율이 아니다.",
    }


def summarize(log_path: Path, index_dir: Path | None = None) -> dict:
    """모든 하위 리포트를 묶는다. read-only — 여기서 로그·색인 어느 쪽도 쓰지 않는다.

    L5(qa 독립검증 후속, 2026-08-14) — `index_dir` 은 기준선 불순 판정(코퍼스 rel 목록)에
    쓸 색인 위치를 명시적으로 고른다(기본값 None 이면 기존과 동일하게 `config.INDEX_DIR`).
    `--log` 로 다른 세대의 로그(예: 테스트·격리 검증용 로그)를 지정해도 이 함수가 항상
    현재 `config.INDEX_DIR` 만 보면, 그 로그가 가리키던 옛 코퍼스의 rel 이 "코퍼스에 없음"
    으로 잘못 잡혀 "기준선 불순" 오탐이 난다 — `--log`·`--index-dir` 를 같은 세대로
    맞춰 지정할 수 있게 열어 둔다.
    """
    entries, warnings = load_entries(log_path)
    known_rels = known_corpus_rels(index_dir)
    return {
        "log_path": str(log_path),
        "n_lines": len(entries),
        "n_v1": sum(1 for e in entries if e["_schema_version"] == 1),
        "n_v2": sum(1 for e in entries if e["_schema_version"] == 2),
        "parse_warnings": warnings,
        "baseline_impurity": {
            "measurable": known_rels is not None,
            "reason": None if known_rels is not None else "실 색인(index/chunks.sqlite)에 접근할 수 없다.",
            "impure_lines": find_baseline_impurity(entries, known_rels),
        },
        "missing_index_created_at": find_missing_index_created_at(entries),
        "confidence_axis_breakdown": confidence_axis_breakdown(entries),
        "r001_condition1": r001_condition1_report(entries),
        "r001_condition2": r001_condition2_report(entries),
    }


def _print_report(report: dict) -> None:
    print(f"=== 질의 로그 판독 — {report['log_path']} ===\n")
    print(f"총 {report['n_lines']}줄 (v1={report['n_v1']} · v2={report['n_v2']})")
    if report["parse_warnings"]:
        print(f"[경고] 파싱 실패 {len(report['parse_warnings'])}줄:")
        for w in report["parse_warnings"][:10]:
            print(f"  - {w}")

    bi = report["baseline_impurity"]
    print("\n-- 기준선 불순(코퍼스에 없는 rel, 탐지만 — 삭제 안 함) --")
    if not bi["measurable"]:
        print(f"  판정 불가: {bi['reason']}")
    else:
        print(f"  {len(bi['impure_lines'])}줄 탐지")
        for row in bi["impure_lines"][:20]:
            print(f"    {row['lineno']}행 ts={row['ts']} query={row['query']!r} bad_rels={row['bad_rels']}")

    mi = report["missing_index_created_at"]
    print(f"\n-- index_created_at 결측 -- {len(mi)}줄")

    ax = report["confidence_axis_breakdown"]
    print(f"\n-- confidence 미달 축 분포(v2, n={ax['v2_total']}) --")
    print(f"  low/none: {ax['v2_low_or_none']}줄")
    for axis_name, n in sorted(ax["axis_buckets"].items(), key=lambda kv: -kv[1]):
        print(f"    {axis_name}: {n}")

    print("\n-- R-001 조건 1(용어 불일치로 못 찾음 > 20%) --")
    c1 = report["r001_condition1"]
    print(f"  측정 가능: {c1['measurable']}")
    print(f"  이유: {c1['reason']}")
    if c1["proxy_coverage_deficient_share_among_low_or_none"] is not None:
        print(
            f"  참고(대리 지표, 정답 판정 아님): 저신뢰 중 coverage 미달 비중 = "
            f"{c1['proxy_coverage_deficient_share_among_low_or_none']:.1%} "
            f"({ax['v2_low_or_none']}건 중)"
        )

    print("\n-- R-001 조건 2(재질의 > 30%) --")
    c2 = report["r001_condition2"]
    print(f"  측정 가능: {c2['measurable']}")
    print(f"  이유: {c2['reason']}")
    if c2.get("requery_rate_among_caller_id") is not None:
        print(
            f"  참고(caller_id 채택분만, n={c2['with_caller_id']}): 재질의율 근사 = "
            f"{c2['requery_rate_among_caller_id']:.1%} — {c2.get('caveat', '')}"
        )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="sub-agents-RAG 질의 로그 판독기(read-only)")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH), help="로그 파일 경로(기본: logs/queries.jsonl)")
    parser.add_argument(
        "--index-dir", default=None,
        help="기준선 불순 판정에 쓸 색인 디렉터리(기본: config.INDEX_DIR). "
             "L5(qa 독립검증 후속) — 다른 세대 로그(--log)를 볼 때 그 로그와 같은 세대의 "
             "색인을 명시적으로 지정할 수 있다(안 맞추면 다른 세대 코퍼스 rel 이 '없음'으로 오탐된다).",
    )
    parser.add_argument("--json", action="store_true", help="리포트를 JSON 으로 출력한다(자동화용)")
    args = parser.parse_args()

    index_dir = Path(args.index_dir) if args.index_dir else None
    report = summarize(Path(args.log), index_dir=index_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
