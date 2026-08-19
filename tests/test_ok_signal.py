"""OK-1(dev-lead, 2026-08-18) 회귀 — `confidence="ok"` 일 때 아무 줄도 안 내던
fail-silent("침묵=ok")를 제거한 변경의 회귀 테스트.

무엇을 지키려는 테스트인가:
  1. **ok 는 이제 명시 신호를 낸다** — 출력에 `confidence=ok` 리터럴이 실제로 있다.
     수정 전 코드라면 ok 질의 출력에는 confidence 라는 글자 자체가 없었다(그래서
     호출 에이전트가 보고서에 채울 값을 *추론*해야 했고, 경고줄이 다른 이유로
     유실돼도 "ok 였다"로 읽혔다).
  2. **기존 low/none 경고줄·색인 stale 경고줄 문안은 한 글자도 안 바뀐다** — 하위
     소비자(server.py HTML 경로·에이전트 보고 템플릿)가 이미 이 문안을 파싱·인용한다.
     그래서 부분 문자열 포함이 아니라 **완전 일치**로 얼려둔다(임계 상수만 상수에서
     렌더 — 캘리브레이션 재조정은 문안 변경이 아니다).
  3. **신호줄은 문자 예산 절단의 대상이 아니다** — prelude(헤더부)에 있으므로 예산이
     아무리 빠듯해도 hit 본문보다 먼저 잘리지 않는다.
  4. **세 상태(ok/low/none) 어디에도 무신호 구간이 없다** — 어떤 상태든 출력에
     `confidence=<값>` 이 정확히 1줄 나온다(대칭성).
  5. **빈 결과(hits=0건)에 ok 신호가 붙는 경로는 없다** — 구조적으로 raw_max=0.0 →
     confidence="none" 이고, no-hits 분기는 애초에 prelude 를 부르지 않는다.
"""
from __future__ import annotations

import unittest

import search
from search import (
    COVERAGE_FLOOR,
    RAW_MAX_FLOOR,
    Hit,
    SearchResult,
    format_result,
    response_prelude,
)

from tests import testutil

# 엔드투엔드 티어가 쓰는 합성 코퍼스는 `tests/test_confidence_metric.py` 의 것을 그대로
# 재사용한다 — "무엇이 실제로 confidence=ok 를 만드는가"의 픽스처를 두 벌 두면 임계
# 재조정(2026-08-14 에 RAW_MAX_FLOOR 1.5→6.2 실제로 발생) 때 한쪽만 고쳐져 조용히
# 무정보 green 이 된다. **모듈 객체로 참조**한다 — TestCase 서브클래스를 이 모듈
# 네임스페이스에 바인딩하면 unittest 로더가 그 클래스를 여기서 한 번 더 수집해
# 중복 실행된다(`dir(module)` 순회 기반이라 이름이 `_` 로 시작해도 수집된다).
from tests import test_confidence_metric as _conf_fixture


def _hit(n: int = 1, *, body_chars: int = 200, status: str = "current") -> Hit:
    rel = f"docs/ok/item{n}.md"
    body = f"본문{n} " * (body_chars // 4)
    return Hit(
        idx=n, score=1.0, rel=rel, heading_path=f"항목{n}",
        start_line=1, end_line=5, category="etc", status=status,
        text=f"[{rel}] 항목{n}\n{body.strip()}",
    )


def _result(
    *, confidence: str, raw_max: float, coverage: float,
    n_matched: int, n_query: int, n_hits: int = 1, body_chars: int = 200,
) -> SearchResult:
    """prelude 만 검증하는 합성 결과 — 색인을 열지 않는다(순수 포맷 계약 테스트)."""
    return SearchResult(
        hits=[_hit(i, body_chars=body_chars) for i in range(1, n_hits + 1)],
        category_used="all", fallback_used=False,
        raw_max=raw_max, raw_scores=[raw_max] * n_hits, confidence=confidence,
        coverage=coverage, n_query_tokens=n_query, n_matched_tokens=n_matched,
    )


def _confidence_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if "confidence=" in l]


class TestOkSignalWording(unittest.TestCase):
    """신규 `[신호]` 줄 자체의 계약."""

    def test_ok_result_emits_confidence_ok_literal(self) -> None:
        """수정 전이라면 ok 질의 출력에는 'confidence' 라는 글자가 아예 없었다 —
        이 테스트가 그 fail-silent 를 직접 밟는다."""
        r = _result(confidence="ok", raw_max=7.88, coverage=1.0, n_matched=5, n_query=5)
        prelude = response_prelude(r, "ok 신호 테스트")
        joined = "\n".join(prelude)

        self.assertIn(
            "confidence=ok", joined,
            "confidence=ok 리터럴이 출력에 없다 — 호출 에이전트가 보고서 기입값을 "
            "추론해야 하는 상태로 되돌아갔다(OK-1 회귀)",
        )
        self.assertIn("[신호]", joined)

    def test_ok_signal_carries_same_numeric_fields_as_warning(self) -> None:
        """세 상태의 출력 형태가 대칭이어야 한다 — 경고줄과 같은 수치 필드
        (raw_max·coverage·매칭 토큰 수)를 신호줄도 싣는다."""
        r = _result(confidence="ok", raw_max=7.88, coverage=1.0, n_matched=5, n_query=5)
        signal = _confidence_lines(response_prelude(r, "필드 대칭 테스트"))[0]

        self.assertIn("raw_max=7.88", signal)
        self.assertIn("coverage=1.00", signal)
        self.assertIn("[5/5 토큰 매칭]", signal)

    def test_ok_signal_renders_thresholds_from_constants(self) -> None:
        """임계는 문안에 리터럴로 박지 않고 상수에서 렌더한다 — 캘리브레이션이 바뀌면
        (실제로 2026-08-14 에 1.5→6.2 로 바뀌었다) 문구가 조용히 stale 해지는 경로를
        만들지 않는다."""
        r = _result(confidence="ok", raw_max=9.0, coverage=0.9, n_matched=9, n_query=10)
        signal = _confidence_lines(response_prelude(r, "임계 렌더 테스트"))[0]

        self.assertIn(f"raw_max>={RAW_MAX_FLOOR}", signal)
        self.assertIn(f"coverage>={COVERAGE_FLOOR}", signal)

    def test_exact_ok_signal_wording(self) -> None:
        """신규 문안 자체도 얼려둔다 — 이 줄도 곧 하위 소비자가 파싱할 대상이 된다."""
        r = _result(confidence="ok", raw_max=7.88, coverage=1.0, n_matched=5, n_query=5)
        signal = _confidence_lines(response_prelude(r, "문안 고정 테스트"))[0]

        expected = (
            "[신호] confidence=ok (raw_max=7.88, coverage=1.00 [5/5 토큰 매칭]) "
            f"— 2축(raw_max>={RAW_MAX_FLOOR} 그리고 coverage>={COVERAGE_FLOOR}) 충족."
        )
        self.assertEqual(signal, expected)

    def test_ok_signal_absent_for_low_and_none(self) -> None:
        """ok 신호가 저신뢰 결과에 잘못 붙지 않는다."""
        for conf, raw_max, cov in (("low", 3.0, 0.5), ("none", 0.0, 0.0)):
            with self.subTest(confidence=conf):
                r = _result(
                    confidence=conf, raw_max=raw_max, coverage=cov,
                    n_matched=1, n_query=2,
                )
                joined = "\n".join(response_prelude(r, "오부착 테스트"))
                self.assertNotIn("[신호]", joined)
                self.assertNotIn("confidence=ok", joined)


class TestExistingWarningWordingFrozen(unittest.TestCase):
    """기존 문안 불변 — 하위 소비자가 이미 파싱·인용하는 줄들이다(브리프 §1-1 요건).

    부분 문자열 포함이 아니라 **완전 일치**로 얼려둔다(포함 검사는 문구가 늘어나도
    통과해 버려 '변경 금지'를 지키지 못한다).
    """

    def test_low_warning_wording_unchanged(self) -> None:
        r = _result(confidence="low", raw_max=3.0, coverage=0.5, n_matched=1, n_query=2)
        warning = _confidence_lines(response_prelude(r, "low 문안 테스트"))[0]

        expected = (
            "[경고] 이 질의는 매칭 신뢰도가 낮다(raw_max=3.00, coverage=0.50 "
            "[1/2 토큰 매칭], "
            f"미달축=raw_max<{RAW_MAX_FLOOR}+coverage<{COVERAGE_FLOOR}, confidence=low) "
            "— 용어를 바꾸거나 파일을 직접 Read 하라. "
            "(아래 결과는 참고용으로 낮은 확신도로 제공한다)"
        )
        self.assertEqual(warning, expected)

    def test_none_warning_wording_unchanged(self) -> None:
        r = _result(confidence="none", raw_max=0.0, coverage=0.0, n_matched=0, n_query=2)
        warning = _confidence_lines(response_prelude(r, "none 문안 테스트"))[0]

        expected = (
            "[경고] 이 질의는 매칭 신뢰도가 낮다(raw_max=0.00, coverage=0.00 "
            "[0/2 토큰 매칭], 미달축=raw_max<=0(무매칭), confidence=none) "
            "— 용어를 바꾸거나 파일을 직접 Read 하라. "
            "(아래 결과는 참고용으로 낮은 확신도로 제공한다)"
        )
        self.assertEqual(warning, expected)

    def test_index_stale_warning_wording_unchanged(self) -> None:
        """색인 stale 경고줄 2분기 문안도 그대로다(브리프 §1-1 — 변경 금지 대상)."""
        r_docs = _result(confidence="ok", raw_max=9.0, coverage=1.0, n_matched=3, n_query=3)
        r_docs.index_stale = True
        r_docs.index_stale_docs = 4
        self.assertEqual(
            search._index_stale_warning_lines(r_docs),
            ["[경고] 색인이 원본보다 낡았다(4개 문서가 색인 이후 변경됨). "
             "`python indexer.py` 를 실행하라."],
        )

        r_detail = _result(confidence="ok", raw_max=9.0, coverage=1.0, n_matched=3, n_query=3)
        r_detail.index_stale = True
        r_detail.index_stale_docs = 0
        r_detail.index_stale_detail = ""
        self.assertEqual(
            search._index_stale_warning_lines(r_detail),
            ["[경고] 색인이 원본보다 낡았을 수 있다(원본 디렉터리가 색인 이후 변경됨). "
             "`python indexer.py` 를 실행하라."],
        )


class TestNoSilentConfidenceState(unittest.TestCase):
    """대칭성 — 어떤 상태든 confidence 값이 정확히 1줄 나온다(무신호 구간 없음)."""

    def test_every_confidence_state_emits_exactly_one_line(self) -> None:
        cases = {
            "ok": (7.88, 1.0, 5, 5),
            "low": (3.0, 0.5, 1, 2),
            "none": (0.0, 0.0, 0, 2),
        }
        for conf, (raw_max, cov, n_matched, n_query) in cases.items():
            with self.subTest(confidence=conf):
                r = _result(
                    confidence=conf, raw_max=raw_max, coverage=cov,
                    n_matched=n_matched, n_query=n_query,
                )
                lines = _confidence_lines(response_prelude(r, "대칭성 테스트"))
                self.assertEqual(
                    len(lines), 1,
                    f"confidence={conf} 에서 confidence 줄이 {len(lines)}줄이다 "
                    f"(1줄이어야 한다 — 0줄이면 침묵=ok 결함 재발): {lines!r}",
                )
                self.assertIn(f"confidence={conf}", lines[0])


class TestSignalSurvivesCharBudget(unittest.TestCase):
    """신호줄은 prelude(헤더부)라 hit 본문 예산 절단의 대상이 아니다(요건 ③)."""

    def test_signal_present_under_tiny_budget(self) -> None:
        r = _result(
            confidence="ok", raw_max=7.88, coverage=1.0, n_matched=5, n_query=5,
            n_hits=6, body_chars=1200,
        )
        text = format_result(r, "예산 절단 테스트", max_total_chars=300)

        self.assertIn(
            "응답 총량 예산", text,
            "이 테스트가 절단 경로를 실제로 밟지 못했다(픽스처 재설계 필요)",
        )
        self.assertIn("confidence=ok", text, "예산 절단이 신호줄까지 삼켰다(OK-1 회귀)")

    def test_signal_precedes_first_hit_block(self) -> None:
        """위치 확인 — 신호줄이 첫 hit 헤더보다 앞(헤더부)에 있다."""
        r = _result(
            confidence="ok", raw_max=7.88, coverage=1.0, n_matched=5, n_query=5, n_hits=3,
        )
        lines = format_result(r, "위치 테스트").splitlines()
        signal_idx = next(i for i, l in enumerate(lines) if "[신호]" in l)
        first_hit_idx = next(i for i, l in enumerate(lines) if "docs/ok/item1.md" in l)
        self.assertLess(signal_idx, first_hit_idx)


class TestNoHitsNeverGetsOkSignal(unittest.TestCase):
    """빈 결과에 ok 신호가 붙는 경로가 있는지 — 브리프 §1-1 마지막 요건.

    구조적 근거: `raw_max = max(raw_scores_out, default=0.0)` 이라 hits 가 비면
    raw_max 는 0.0 이고, `_confidence()` 는 raw_max<=0 을 무조건 "none" 으로 낸다.
    게다가 raw<=0 후보(bm25s 의 0점 패딩)는 `candidates` 단계에서 이미 걸러지므로
    "hits 는 있는데 raw_max 가 0" 도 성립하지 않는다.
    """

    def test_empty_hits_confidence_is_structurally_none(self) -> None:
        self.assertEqual(search._confidence(0.0, 1.0), "none")
        self.assertEqual(search._confidence(0.0, 0.0), "none")

    def test_no_hits_format_has_no_signal_line(self) -> None:
        r = SearchResult(hits=[], category_used="all", fallback_used=False, confidence="none")
        text = format_result(r, "빈 결과 테스트")
        self.assertIn("결과 없음", text)
        self.assertNotIn("[신호]", text)
        self.assertNotIn("confidence=ok", text)


class TestOkSignalEndToEnd(testutil.IsolatedIndexCase):
    """합성 색인 위에서 실제 search()→format_result() 를 통과시킨다 — 합성
    SearchResult 만으로는 "실 파이프라인이 정말 ok 를 만들고 그게 텍스트에 실리는가"를
    못 밟는다.
    """

    CORPUS = _conf_fixture.TestConfidenceEndToEnd.CORPUS

    def test_real_ok_query_output_carries_literal(self) -> None:
        r = search.search("CONF-999", category="all", k=5, log=False, source="test")
        # 사전조건 — 이 픽스처가 실제로 ok 국면을 밟는지(임계 재조정 시 여기서 실패한다).
        self.assertEqual(
            r.confidence, "ok",
            f"픽스처가 ok 국면을 못 밟았다(raw_max={r.raw_max}, coverage={r.coverage}) "
            f"— 픽스처 재설계 필요",
        )
        text = format_result(r, "CONF-999")
        self.assertIn("confidence=ok", text)
        self.assertIn(f"raw_max={r.raw_max:.2f}", text)

    def test_real_low_query_keeps_warning_and_no_signal(self) -> None:
        query = (
            "CONF-999 zzzznoisetoken1 zzzznoisetoken2 zzzznoisetoken3 "
            "zzzznoisetoken4 zzzznoisetoken5"
        )
        r = search.search(query, category="all", k=5, log=False, source="test")
        self.assertEqual(r.confidence, "low", "픽스처가 low 국면을 못 밟았다 — 재설계 필요")

        text = format_result(r, query)
        self.assertIn("[경고] 이 질의는 매칭 신뢰도가 낮다", text)
        self.assertIn("confidence=low", text)
        self.assertNotIn("[신호]", text)

    def test_real_unmatched_query_yields_no_hits_and_no_signal(self) -> None:
        """코퍼스 어디에도 없는 토큰만으로 된 질의 — bm25s 0점 패딩이 걸러져 hits=0건이
        되고, confidence 는 "none" 이며, ok 신호는 붙지 않는다(빈 결과 경로 실측)."""
        r = search.search("zzzznoisetoken9 zzzznoisetoken8", category="all", k=5,
                          log=False, source="test")
        self.assertEqual(len(r.hits), 0, "이 테스트가 전제하는 0건 국면을 못 밟았다")
        self.assertEqual(r.confidence, "none")

        text = format_result(r, "zzzznoisetoken9 zzzznoisetoken8")
        self.assertNotIn("[신호]", text)
        self.assertNotIn("confidence=ok", text)


if __name__ == "__main__":
    unittest.main()
