"""2-1(qa 독립검증, 2026-08-14) 회귀 — confidence 지표를 raw_max 단일축(임계 6.5)에서
raw_max×coverage 2축 AND 규칙(RAW_MAX_FLOOR=1.5, COVERAGE_FLOOR=0.75)으로 교체한 변경의
회귀 테스트.

여기서 확인하는 것:
  1. 높은 raw_max(옛 임계 6.5를 넘김)라도 coverage 가 낮으면 "ok" 가 아니다(수정 전이라면
     이 테스트는 실패했을 것 — `test_high_raw_max_low_coverage_is_not_ok`).
  2. `_confidence()` 경계값 동작(raw_max<=0, 정확히 임계에 걸치는 경우).
  3. 희소열 coverage 경로(`_coverage_sparse`)와 전체배열 경로(`_coverage_full_array`)가
     같은 값을 낸다(비용 최적화가 정답을 안 바꾼다는 회귀 — 브리프 명시 요구).
  4. 희소열 구조가 깨지면(`retriever.scores` 파손) 예외 없이 전체배열 경로로 폴백한다.
  5. `SearchResult.coverage`/`n_query_tokens`/`n_matched_tokens` 가 실제 검색 결과에
     노출된다(신규 공개 필드 계약).
"""
from __future__ import annotations

import unittest

import config
import search
import tokenizer

from tests import testutil


def _doc(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class TestConfidenceUnitBoundaries(unittest.TestCase):
    """색인 없이 `_confidence()` 자체의 경계값만 확인한다(순수 함수)."""

    def test_raw_max_zero_or_negative_is_none(self) -> None:
        self.assertEqual(search._confidence(0.0, 0.0), "none")
        self.assertEqual(search._confidence(-1.0, 1.0), "none")  # 방어 코드 — L4 주석 참고

    def test_exact_floor_boundary_is_ok(self) -> None:
        """경계는 포함(>=) — 정확히 RAW_MAX_FLOOR·COVERAGE_FLOOR 에 걸치면 'ok'."""
        self.assertEqual(search._confidence(search.RAW_MAX_FLOOR, search.COVERAGE_FLOOR), "ok")

    def test_just_below_raw_max_floor_is_low(self) -> None:
        self.assertEqual(
            search._confidence(search.RAW_MAX_FLOOR - 0.01, 1.0), "low",
            "raw_max 가 임계 바로 아래인데 coverage=1.0(만점)이어도 low 여야 한다(AND 규칙)",
        )

    def test_just_below_coverage_floor_is_low(self) -> None:
        self.assertEqual(
            search._confidence(999.0, search.COVERAGE_FLOOR - 0.01), "low",
            "raw_max 가 아무리 커도 coverage 미달이면 low 여야 한다(AND 규칙 — 2-1 핵심 변경)",
        )

    def test_high_raw_max_alone_is_not_sufficient(self) -> None:
        """수정 전 코드(raw_max>=6.5 단일축)라면 raw_max=20 은 무조건 'ok' 였다 — 수정 후는
        coverage 미달이면 'low' 여야 한다. 이게 이번 배치가 고치는 핵심 결함(§2-1 '라면
        끓이는 법' 사례)의 최소 재현이다.
        """
        self.assertEqual(search._confidence(20.0, 0.1), "low")

    def test_deficient_axis_labels(self) -> None:
        self.assertEqual(search._deficient_axis(0.0, 0.0), "raw_max<=0(무매칭)")
        self.assertIn("raw_max<", search._deficient_axis(1.0, 1.0))
        self.assertIn("coverage<", search._deficient_axis(999.0, 0.1))
        self.assertEqual(search._deficient_axis(999.0, 1.0), "")


class TestConfidenceEndToEnd(testutil.IsolatedIndexCase):
    """실제 search() 호출로 2축 규칙이 최종 confidence 에 반영되는지 확인한다.

    H1(dev-lead 재판정, 2026-08-14) — `RAW_MAX_FLOOR` 가 1.5→6.2 로 재조정되면서
    원래 픽스처(단일 한글 단어 `CONFRARETERMXYZ` 40회 반복 + 채움 문서 17개, raw_max=
    2.035)가 더 이상 raw_max 축을 통과 못 해 픽스처를 재설계했다(수정 전이라면 이
    테스트들이 실패한다 — 그게 이 배치가 왜 바뀌는 게 옳은지의 근거다).

    **재설계 근거(실측)**: 반복 횟수를 단순히 늘리는 것만으로는 부족했다 — BM25 는
    문서 길이 정규화가 있어 반복 횟수가 늘면 문서 길이도 같이 늘어 tf/문서장 비율이
    포화·역전되고(200회 반복 실측: raw_max 가 오히려 2.035→1.676 로 **하락**), 코퍼스
    크기(문서 수)만 늘리는 것도 idf 증가가 로그형이라 비효율적이다(300 채움 문서까지
    늘려도 raw_max=3.747 로 6.2 에 못 미침). 대신 **식별자 형태 토큰**(`CONF-999`,
    tokenizer 가 `["CONF-999", "conf999"]` 2토큰으로 확장 — `D-52`/`API-084`/`REQ-701`
    같은 실제 프로덕션 식별자 질의와 동일한 패턴)을 쓰면 두 토큰이 각각 raw_max 에
    가법적으로 기여해 훨씬 적은 반복(50회)·채움 문서(200개)로도 raw_max=6.900(>=6.2)에
    안정적으로 도달한다(실측, 마진 +0.7) — 실제 짧은 정답 식별자 질의(REQ-701=6.453·
    API-046=6.563·FN-041=6.946·OP-38=7.360, 전부 README H1 절 표 참고)와 같은 구조라
    프로덕션 국면을 더 잘 대표하기도 한다.
    """

    CORPUS = {
        "docs/conf/rare_term_doc.md": _doc(
            "# 희귀 식별자 문서", "", "## 섹션", "",
            *(["CONF-999 은 이 문서에서만 반복되는 표식이다."] * 50),
        ),
        **{
            f"docs/conf/filler_{i:03d}.md": _doc(
                f"# 채움 문서 {i:03d}", "", "## 섹션", "",
                f"이 문서 {i:03d} 는 잡다한 서술로 코퍼스 크기를 채운다 서로 다른 주제 {i:03d} 를 다룬다.",
            )
            for i in range(1, 201)
        },
    }

    def test_high_raw_max_low_coverage_is_not_ok(self) -> None:
        """질의에 매칭 토큰(`CONF-999`→2개 하위토큰) + 코퍼스 어디에도 없는 ASCII 잡음
        토큰 5개를 섞어 raw_max(=raw_max 를 낸 hit 문서, 여전히 rare_term_doc)는 그대로인데
        coverage 만 낮아지게 만든다(잡음 토큰은 전부 OOV라 raw_max 자체엔 기여하지 않음
        — `test_high_coverage_and_raw_max_is_ok` 와 raw_max 를 직접 대조한다).

        수정 전 코드(raw_max 단일축)라면 raw_max 값이 임계(당시 6.5)를 넘기만 하면
        coverage 와 무관하게 'ok' 였다 — 이 구체적 수치 조합에서 실제로 그런 회귀가
        생기는지는 아래 `test_high_raw_max_alone_is_not_sufficient`(직접 함수 호출,
        raw_max=20 처럼 옛 임계를 확실히 넘는 값)가 결정적으로 확인한다. 이 테스트는
        "같은 raw_max·다른 coverage"가 실제 search() 파이프라인에서 confidence 를
        갈라놓는지(엔드투엔드로 축이 실제로 작동하는지)를 확인한다.
        """
        query = "CONF-999 zzzznoisetoken1 zzzznoisetoken2 zzzznoisetoken3 zzzznoisetoken4 zzzznoisetoken5"
        r = search.search(query, category="all", k=5, log=False, source="test")
        self.assertGreater(len(r.hits), 0, "매칭 문서가 없다 — 픽스처 재설계 필요")
        self.assertEqual(r.hits[0].rel, "docs/conf/rare_term_doc.md")

        # 사전조건 — raw_max 축은 통과(>=RAW_MAX_FLOOR)하는데 coverage 축만 미달인지 확인.
        self.assertGreaterEqual(
            r.raw_max, search.RAW_MAX_FLOOR,
            f"이 테스트가 전제하는 raw_max>=RAW_MAX_FLOOR 국면을 못 밟았다(실제={r.raw_max}) "
            f"— 픽스처 재설계 필요",
        )
        self.assertLess(
            r.coverage, search.COVERAGE_FLOOR,
            f"coverage={r.coverage} 가 낮은 커버리지 국면을 재현하지 못했다 — 픽스처 재설계 필요",
        )
        # CONF-999 가 2토큰("CONF-999"+"conf999")으로 확장되므로 2(매칭) + 5(잡음) = 7.
        self.assertEqual(r.n_query_tokens, 7, f"실제: {r.n_query_tokens}")
        self.assertEqual(r.n_matched_tokens, 2, f"실제: {r.n_matched_tokens}")

        self.assertEqual(
            r.confidence, "low",
            f"raw_max={r.raw_max}(축 통과)인데 coverage={r.coverage}(미달)임에도 'ok' 로 "
            f"판정됐다 — 2축 AND 규칙이 적용 안 된 것(2-1 회귀)",
        )

    def test_high_coverage_and_raw_max_is_ok(self) -> None:
        """대조군 — 잡음 토큰 없이 `CONF-999` 단독 질의는 coverage=1.0 이고 raw_max 도
        같은 문서에서 나오므로(위 테스트와 raw_max 동일해야 함) 'ok' 여야 한다.
        """
        r = search.search("CONF-999", category="all", k=5, log=False, source="test")
        self.assertGreater(len(r.hits), 0)
        self.assertEqual(r.coverage, 1.0)
        self.assertGreaterEqual(r.raw_max, search.RAW_MAX_FLOOR)
        self.assertEqual(r.confidence, "ok")

    def test_search_result_exposes_coverage_fields(self) -> None:
        """SearchResult 신규 필드 3종(coverage/n_query_tokens/n_matched_tokens)이 실제
        검색 결과에 노출되는지 확인한다(공개 계약 추가분).
        """
        r = search.search("CONF-999", category="all", k=5, log=False, source="test")
        self.assertIsInstance(r.coverage, float)
        self.assertIsInstance(r.n_query_tokens, int)
        self.assertIsInstance(r.n_matched_tokens, int)
        self.assertEqual(r.n_query_tokens, len(tokenizer.tokenize_query("CONF-999")))
        self.assertLessEqual(r.n_matched_tokens, r.n_query_tokens)

    def test_zero_token_query_has_zero_coverage_fields(self) -> None:
        """토큰 0개 조기 반환 경로 — coverage 관련 필드가 전부 기본값(0)이어야 한다."""
        r = search.search("!!!???", category="all", k=5, log=False, source="test")
        # "!!!???" 는 tokenizer 가 토큰을 하나도 못 뽑는 입력(식별자·라틴·한글 패턴 전부
        # 불일치) — 사전조건으로 실제로 토큰 0개인지 확인한다.
        self.assertEqual(tokenizer.tokenize_query("!!!???"), [])
        self.assertEqual(r.coverage, 0.0)
        self.assertEqual(r.n_query_tokens, 0)
        self.assertEqual(r.n_matched_tokens, 0)
        self.assertEqual(r.confidence, "none")

    def test_sparse_and_full_array_coverage_agree(self) -> None:
        """희소열 경로(`_coverage_sparse`)와 전체배열 경로(`_coverage_full_array`)가
        코퍼스 전 문서·여러 질의에 대해 정확히 같은 값을 낸다 — 비용 최적화(희소열
        경로 채택)가 판정을 바꾸지 않는다는 회귀(브리프 §2-1 명시 요구).
        """
        conn, retriever, n_chunks, _fp = search._open_index()
        try:
            queries = [
                "CONF-999 전혀 무관한 잡음 토큰들 채워넣기",
                "채움 문서 잡다한 서술",
                "CONF-999",
            ]
            checked_any = False
            for q in queries:
                tokens = tokenizer.tokenize_query(q)
                token_ids = search._token_ids_in_vocab(retriever, tokens)
                if not token_ids:
                    continue
                for doc_id in range(n_chunks):
                    sparse = search._coverage_sparse(retriever, token_ids, doc_id)
                    full = search._coverage_full_array(retriever, token_ids, doc_id)
                    self.assertEqual(
                        sparse, full,
                        f"doc_id={doc_id} q={q!r} — 희소열={sparse} vs 전체배열={full}",
                    )
                    checked_any = True
            self.assertTrue(checked_any, "이 테스트가 실제로 비교를 한 건도 못 했다(픽스처 재설계 필요)")
        finally:
            conn.close()

    def test_coverage_falls_back_when_sparse_structure_broken(self) -> None:
        """희소열 경로(`_coverage_sparse`)가 예외를 던지면 `_compute_coverage()`가 예외로
        죽지 않고 전체배열 경로(`_coverage_full_array`)로 폴백하며, **같은 정답 값**을
        낸다 — 폴백 경로 자체가 실제로 동작하고 결과도 맞는지 확인한다(브리프 §2-1 명시
        요구: "폴백이 동작하는지도 테스트로 남겨라").

        `retriever.scores` 자체를 깨는 방식은 안 쓴다 — 그 dict 는 bm25s 의
        `get_scores_from_ids()`(전체배열 경로가 내부적으로 쓰는 공식 API)도 동일하게
        읽으므로, 구조를 깨면 두 경로가 동시에 죽어 "폴백만 성공"을 검증할 수 없다
        (실측 확인). 대신 `_coverage_sparse` 함수 자체를 일시적으로 예외를 던지는
        가짜로 바꿔치기해 "sparse 경로만 실패 → compute_coverage 가 fallback 을 태워
        여전히 정답을 낸다"는 제어 흐름을 정확히 겨냥한다.
        """
        conn, retriever, n_chunks, _fp = search._open_index()
        try:
            tokens = tokenizer.tokenize_query("CONF-999")
            token_ids = search._token_ids_in_vocab(retriever, tokens)
            self.assertTrue(token_ids, "사전조건 실패 — 어휘에 없는 토큰뿐이다")

            doc_id = 0
            expected = search._coverage_full_array(retriever, token_ids, doc_id)

            orig_sparse = search._coverage_sparse

            def _broken_sparse(*_a, **_kw):
                raise RuntimeError("2-1 테스트 — 희소열 구조 가정 이탈 시뮬레이션")

            search._coverage_sparse = _broken_sparse
            try:
                coverage, n_q, n_m = search._compute_coverage(retriever, tokens, doc_id)
                self.assertEqual(n_q, len(tokens))
                self.assertEqual(
                    n_m, expected,
                    "폴백(전체배열) 경로가 정상 경로와 다른 값을 냈다 — 폴백이 오답을 낸다",
                )
            finally:
                search._coverage_sparse = orig_sparse  # 다음 테스트에 영향 없게 원복
        finally:
            conn.close()

    def test_token_ids_in_vocab_filters_out_of_bounds_sentinel(self) -> None:
        """M1(qa 독립검증 후속, 2026-08-14) — bm25s 미지토큰 센티널(`vocab_dict` 에는
        있지만 점수 열(indptr) 범위 밖인 id)이 섞여도 `_token_ids_in_vocab()` 가 조용히
        제외해야 한다(수정 전이라면 이 id 가 그대로 흘러가 `_coverage_sparse`=IndexError·
        `_coverage_full_array`=ValueError 로 죽었을 것 — 브리프 M1 실측 그대로 재현).
        """
        conn, retriever, n_chunks, _fp = search._open_index()
        try:
            real_token = next(iter(retriever.vocab_dict))
            n_cols = len(retriever.scores["indptr"]) - 1
            orig_vocab = retriever.vocab_dict
            patched = dict(orig_vocab)
            # 유효 열 범위는 0..n_cols-1 뿐이다 — n_cols 자체가 bm25s 센티널과 같은 모양의
            # "한 칸 밖" id 다.
            patched["__M1_SENTINEL_OOB__"] = n_cols
            retriever.vocab_dict = patched
            try:
                ids = search._token_ids_in_vocab(retriever, [real_token, "__M1_SENTINEL_OOB__"])
                self.assertEqual(
                    ids, [orig_vocab[real_token]],
                    "범위 밖 센티널 id 가 걸러지지 않았다 — M1 회귀",
                )
                # 걸러진 뒤의 token_ids 로 실제 coverage 함수 두 경로 모두 안전하게 도는지도 확인.
                search._coverage_sparse(retriever, ids, 0)
                search._coverage_full_array(retriever, ids, 0)
            finally:
                retriever.vocab_dict = orig_vocab
        finally:
            conn.close()

    def test_coverage_full_array_warns_on_nonoccurrence_array(self) -> None:
        """M2(qa 독립검증 후속, 2026-08-14) — `retriever.nonoccurrence_array` 가 설정된
        상태(다른 idf_method 시뮬레이션)에서 `_coverage_full_array()` 를 타면 stderr 로
        1회성 경고를 내야 한다(무음 오답 방지 — 값 자체를 보정하지는 않는다, 함수
        docstring 참고).
        """
        import contextlib
        import io

        conn, retriever, n_chunks, _fp = search._open_index()
        try:
            tokens = tokenizer.tokenize_query("CONF-999")
            token_ids = search._token_ids_in_vocab(retriever, tokens)
            self.assertTrue(token_ids, "사전조건 실패 — 어휘에 없는 토큰뿐이다")

            import numpy as np

            had_attr = hasattr(retriever, "nonoccurrence_array")
            orig_val = getattr(retriever, "nonoccurrence_array", None)
            orig_warned = search._nonoccurrence_array_warned
            # None 이 아니기만 하면 경고 조건은 성립하지만, bm25s.get_scores_from_ids() 가
            # 내부에서 이 배열을 실제로 인덱싱(`self.nonoccurrence_array[query_tokens_ids]`)
            # 하므로 진짜 subscriptable 한 배열(전부 0 — 값 자체는 안 바뀌게)을 넣는다.
            retriever.nonoccurrence_array = np.zeros(len(retriever.vocab_dict) + 10)
            search._nonoccurrence_array_warned = False
            buf = io.StringIO()
            try:
                with contextlib.redirect_stderr(buf):
                    search._coverage_full_array(retriever, token_ids, 0)
            finally:
                if had_attr:
                    retriever.nonoccurrence_array = orig_val
                else:
                    del retriever.nonoccurrence_array
                search._nonoccurrence_array_warned = orig_warned
            self.assertIn(
                "nonoccurrence_array", buf.getvalue(),
                f"nonoccurrence_array 경고가 안 났다 — M2 회귀. stderr={buf.getvalue()!r}",
            )
        finally:
            conn.close()

    def test_coverage_full_array_no_warning_when_nonoccurrence_array_none(self) -> None:
        """대조군 — 이 색인의 실제 idf_method(lucene)는 `nonoccurrence_array` 가 없으므로
        (`None`) 정상 경로에서는 경고가 나지 않아야 한다(오탐 방지 확인).
        """
        import contextlib
        import io

        conn, retriever, n_chunks, _fp = search._open_index()
        try:
            self.assertIsNone(
                getattr(retriever, "nonoccurrence_array", None),
                "사전조건 실패 — 이 테스트는 nonoccurrence_array 가 None 인 색인을 전제한다",
            )
            tokens = tokenizer.tokenize_query("CONF-999")
            token_ids = search._token_ids_in_vocab(retriever, tokens)
            orig_warned = search._nonoccurrence_array_warned
            search._nonoccurrence_array_warned = False
            buf = io.StringIO()
            try:
                with contextlib.redirect_stderr(buf):
                    search._coverage_full_array(retriever, token_ids, 0)
            finally:
                search._nonoccurrence_array_warned = orig_warned
            self.assertNotIn("nonoccurrence_array", buf.getvalue())
        finally:
            conn.close()


# (공개판 주석) 원 프로젝트에는 여기 `TestH1ExpandedLabelsetRegression` 이 있었다 —
# 실 코퍼스에서 측정한 특정 질의들의 raw_max/coverage 분포(예: 도메인 밖 장문 질의가
# raw_max 12.04 인데 coverage 미달로 low 가 되는 것)를 고정하는 캘리브레이션 회귀였다.
# 그 수치는 그 코퍼스의 속성이라 다른 코퍼스에서는 재현되지 않으므로 공개판에서 제거했다.
# 자기 코퍼스에 붙인 뒤 `python eval.py --floor` 로 도메인 안/밖 분포를 측정하고, 지키고
# 싶은 경계 사례(오탐으로 잡혀야 하는 질의 · ok 여야 하는 식별자 질의)를 같은 형태의
# skipUnless(_INDEX_OK) 테스트로 몇 건 고정해 두기를 권한다.


if __name__ == "__main__":
    unittest.main()
