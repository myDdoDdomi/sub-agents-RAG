"""I5 — 응답 총량 예산 판정(지시서 §5). 색인이 필요 없다 — SearchResult 를 직접
구성해 search.plan_response_budget()/response_prelude()/format_result() 만 검증한다
(텍스트·JSON 두 표면이 공유하는 단일 근거이므로 여기서 계약을 지키면 둘 다 지켜진다 —
server.py 의 JSON 경로도 이 두 함수를 그대로 호출한다).

잡는 변이: 응답 예산을 300자로 축소(RESPONSE_CHAR_BUDGET 상수 자체를 소스에서 바꾸는
변이) — `format_result()`/`plan_response_budget()` 의 `max_total_chars` 기본 인자값은
**함수 정의 시점**(=모듈 import 시점)에 한 번 바인딩된다. 소스 사본으로 상수를 바꾸는
변이(qa_e_mutation.py 방식)는 그 바인딩 자체를 바꾸므로, "기본 인자에 의존하는 호출"
(명시적으로 max_total_chars 를 넘기지 않는 호출)이 있어야 그 변이를 잡을 수 있다 —
아래 test_default_budget_no_omission_for_small_results 가 그 역할이다.
"""
from __future__ import annotations

import unittest

from search import Hit, SearchResult, format_result, plan_response_budget, response_prelude


def _make_hit(n: int, *, body_chars: int = 200) -> Hit:
    rel = f"docs/budget/item{n}.md"
    heading = f"항목{n}"
    body = f"본문{n} " * (body_chars // 4)
    text = f"[{rel}] {heading}\n{body.strip()}"
    return Hit(
        idx=n, score=1.0 - n * 0.01, rel=rel, heading_path=heading,
        start_line=1, end_line=5, category="etc", status="current", text=text,
    )


def _make_result(n_hits: int, *, body_chars: int = 200) -> SearchResult:
    hits = [_make_hit(i, body_chars=body_chars) for i in range(1, n_hits + 1)]
    return SearchResult(
        hits=hits, category_used="all", fallback_used=False,
        raw_max=10.0, raw_scores=[10.0 - i for i in range(n_hits)], confidence="ok",
    )


class TestBudgetInvariant(unittest.TestCase):
    def test_default_budget_no_omission_for_small_results(self) -> None:
        """작은 결과 3건(각 ~200자)은 **기본** 예산(RESPONSE_CHAR_BUDGET, 명시 오버라이드
        없음)에서 전부 포함돼야 한다 — 300자로 줄이는 변이가 있으면 2번째 항목부터
        생략된다.
        """
        r = _make_result(3, body_chars=200)
        # 기본 인자에 의존 — max_total_chars 를 넘기지 않는다(중요, 위 docstring 참고).
        text = format_result(r, "예산 기본값 테스트")
        for i in range(1, 4):
            self.assertIn(f"항목{i}", text)
            self.assertIn(f"본문{i}", text, f"항목{i} 본문이 기본 예산에서 생략됐다")
        self.assertNotIn("응답 총량 예산", text, "작은 결과인데 예산 초과 안내가 떴다")

        items = plan_response_budget(r)  # max_total_chars 기본값 사용
        self.assertTrue(all(it.include for it in items), "기본 예산에서 일부 항목이 생략됐다")

    def test_explicit_small_budget_triggers_omission_but_keeps_first(self) -> None:
        """명시적으로 작은 예산을 줘서 생략 경로 자체가 실제로 동작하는지 확인한다
        (한 번도 안 밟히면 이 불변식 자체가 무정보 green 이 될 수 있다). 1위 항목은
        예산과 무관하게 항상 포함된다(L14 계약).
        """
        r = _make_result(5, body_chars=800)
        prelude = response_prelude(r, "작은 예산 테스트")
        prelude_chars = sum(len(l) + 1 for l in prelude)

        items = plan_response_budget(r, max_total_chars=400, prelude_chars=prelude_chars)
        self.assertTrue(items[0].include, "1위 항목이 예산 부족으로 생략됐다(L14 위반)")
        self.assertTrue(
            any(not it.include for it in items[1:]),
            "예산을 400자로 줘도 생략이 한 건도 안 일어났다 — 이 테스트가 생략 경로를 "
            "실제로 밟지 못하고 있다(픽스처 재설계 필요)",
        )

    def test_text_and_json_paths_agree_on_same_budget(self) -> None:
        """텍스트(format_result)와 JSON(server._run_search 와 동일한 두 함수 조합)이
        **같은 예산**에서 생략 건수·생략 파일 집합이 일치해야 한다(3차 배치 결함②
        재발 방지 — 이전엔 헤더 크기 근사가 달라 텍스트/JSON 이 다른 건수를 생략했다).
        """
        r = _make_result(8, body_chars=700)
        budget = 1500

        # JSON 경로 재현 — server._run_search() 와 정확히 같은 두 함수 호출 순서.
        prelude_lines = response_prelude(r, "정합 테스트")
        prelude_chars = sum(len(line) + 1 for line in prelude_lines)
        items = plan_response_budget(r, max_total_chars=budget, prelude_chars=prelude_chars)
        json_omitted = {it.rel for it in items if not it.include}

        self.assertTrue(json_omitted, "이 테스트가 생략 자체를 못 밟았다(픽스처 재설계 필요)")

        # 텍스트 경로.
        text = format_result(r, "정합 테스트", max_total_chars=budget)
        for rel in json_omitted:
            self.assertIn(
                rel, text,
                f"{rel} 이 JSON 경로에서는 생략됐다고 판정됐는데 텍스트 안내 목록에 없다",
            )
        for it in items:
            if it.include:
                # 포함된 항목은 텍스트에도 본문 마커가 그대로 실려야 한다.
                marker = it.rel.split("item")[-1].split(".md")[0]
                self.assertIn(f"본문{marker}", text)


class TestH2HeaderBudgetRegression(unittest.TestCase):
    """H2(qa 독립검증 후속, 2026-08-14) — hit 헤더가 매번 "질의 내 상대값 — 1위는 항상
    1.000, 완벽 일치를 뜻하지 않음"을 반복해 예산을 갉아먹던 결함(항목당 +38~50자) 회귀.
    """

    def test_hit_header_no_longer_repeats_long_explanation(self) -> None:
        """수정 전이라면 각 hit 헤더 줄에 긴 설명이 그대로 있었다 — 이제 짧은
        '(상대값)' 표기만 남고, 긴 설명 문구 자체는 헤더에서 사라져야 한다."""
        from search import _hit_header_text

        r = _make_result(3, body_chars=200)
        for i, h in enumerate(r.hits, start=1):
            header = _hit_header_text(h, i, r)
            self.assertNotIn(
                "1위는 항상 1.000", header,
                f"헤더에 긴 설명이 여전히 반복되고 있다(H2 회귀): {header!r}",
            )
            self.assertIn("(상대값)", header)
            self.assertIn("raw ", header, "raw 병기가 빠졌다(2-1 계약 유지)")

    def test_explanation_appears_once_in_prelude_when_hits_present(self) -> None:
        """설명 문장은 hit 이 있을 때 response_prelude() 가 검색당 1회만 낸다."""
        r = _make_result(5, body_chars=200)
        prelude = response_prelude(r, "H2 테스트")
        occurrences = sum(1 for line in prelude if "1위는 항상 1.000" in line)
        self.assertEqual(occurrences, 1, f"설명 문장이 정확히 1회가 아니다: {prelude!r}")

    def test_no_prelude_explanation_when_no_hits(self) -> None:
        """hits 가 0건이면 설명할 점수가 없으므로 이 줄 자체가 없어야 한다."""
        r = SearchResult(hits=[], category_used="all", fallback_used=False, confidence="none")
        prelude = response_prelude(r, "빈 결과 테스트")
        self.assertFalse(any("1위는 항상 1.000" in line for line in prelude))

    def test_header_length_reduced_vs_old_wording(self) -> None:
        """헤더 1건당 절감량이 브리프 실측치(+38~50자)와 같은 자릿수인지 직접 잰다 —
        옛 설명문으로 되돌린 헤더와 현재 헤더의 길이 차를 비교한다."""
        from search import _hit_header_text

        r = _make_result(1, body_chars=200)
        new_header = _hit_header_text(r.hits[0], 1, r)
        old_style_header = new_header.replace(
            "(상대값)", "(질의 내 상대값 — 1위는 항상 1.000, 완벽 일치를 뜻하지 않음)"
        )
        saved = len(old_style_header) - len(new_header)
        self.assertGreater(saved, 0, "헤더가 옛 표기보다 짧아지지 않았다")
        self.assertGreaterEqual(saved, 30, f"절감량이 브리프 실측(38~50자)보다 훨씬 작다: {saved}자")

    def test_real_query_k20_omission_count_matches_measured_improvement(self) -> None:
        """실 색인 기준 — 리뷰가 실측한 회귀 사례("정합감사 리포트" k=20)를 그대로
        재현한다. 구현 보고서 실측: 수정 전 생략 10건 → 수정 후 9건. 코퍼스가 바뀌면
        절대값은 흔들릴 수 있으므로, **회귀 방지 목적에 필요한 만큼만**(수정 전 값
        10을 넘지 않아야 한다) 느슨하게 잠근다.
        """
        import search as search_mod
        from tests import testutil

        if not testutil.real_index_available():
            self.skipTest("실 index/ 가 없다 — python indexer.py 를 먼저 실행하라")

        r = search_mod.search("정합감사 리포트", category="all", k=20, log=False, source="test")
        items = plan_response_budget(r)
        omitted = sum(1 for it in items if not it.include)
        self.assertLessEqual(
            omitted, 10,
            f"'정합감사 리포트' k=20 생략 건수({omitted})가 H2 수정 전 실측치(10)를 넘었다 — "
            f"헤더 축약이 무효화됐을 수 있다",
        )


if __name__ == "__main__":
    unittest.main()
