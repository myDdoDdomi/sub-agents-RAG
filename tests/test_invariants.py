"""불변식 회귀(지시서 §5) — I1·I2·I3·I4·I6·I7·I8·I9·I10.

I5(예산 텍스트/JSON 일치)는 tests/test_budget.py 로 분리했다 — 색인 없이 SearchResult 를
직접 구성해 검증할 수 있어 이 클래스의 공유 코퍼스에 묶을 이유가 없다.

공유 코퍼스 1개(클래스당 1회만 색인, testutil.IsolatedIndexCase)를 여러 불변식이
나눠 쓴다 — 문서마다 서로 다른 고유 표식 문자열을 써서 질의가 섞이지 않게 한다.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import config
import search

from tests import testutil


def _doc(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class TestInvariants(testutil.IsolatedIndexCase):
    CORPUS = {
        # I1/I7 — status 가중. current(x2 반복, raw 낮음) vs stale(x6, raw 높음) vs
        # deprecated(x4). 반복 횟수는 실측(scratchpad probe_status_weight.py, 2026-08-13)
        # 으로 확정 — current 가 raw 로는 stale 보다 낮은데 가중 후에는 위로 와야 한다.
        "docs/status/sw_current.md": _doc(
            "# 현재 문서", "", "## 섹션", "",
            *(["STATUSWEIGHTTERM는 이 문서의 핵심 개념이다."] * 2),
        ),
        "docs/status/sw_stale.md": _doc(
            "status: stale", "", "# 낡은 문서", "", "## 섹션", "",
            *(["STATUSWEIGHTTERM는 이 문서의 핵심 개념이다."] * 6),
        ),
        "docs/status/sw_deprecated.md": _doc(
            "status: deprecated", "", "# 폐기 문서", "", "## 섹션", "",
            *(["STATUSWEIGHTTERM는 이 문서의 핵심 개념이다."] * 4),
        ),
        # I3 — 줄범위. 짧은 단일 조각이라 병합돼 start_line=1 이 된다(실측 확인, chunker.py
        # 는 읽기전용이라 이 병합 동작 자체를 assert 하지 않고 "원문 슬라이스와 일치"만 본다).
        "docs/line_range/lr_doc.md": _doc(
            "# 줄범위 문서", "", "## 대상 섹션", "",
            "LINERANGEMARKER 은 이 섹션에서만 등장하는 유일한 표식이다.",
            "이 줄도 같은 조각에 포함되어야 한다.",
        ),
        # I4 — 카테고리 필터. 같은 용어를 api/db 두 카테고리 문서에 심어 필터가 실제로
        # 후보를 걸러내는지(둘 다 매칭되는데 하나만 반환되는지) 확인한다.
        "docs/category/api-spec/cat_api.md": _doc(
            "# API 카테고리 문서", "", "## 섹션", "",
            "CATFILTERTERM 이 이 API 문서의 핵심이다.",
        ),
        "docs/category/db-schema/cat_db.md": _doc(
            "# DB 카테고리 문서", "", "## 섹션", "",
            "CATFILTERTERM 이 이 DB 문서에도 등장한다.",
        ),
        # I10 — 용어집 확장. glossary.tsv 의 실제 등재 쌍(검색엔진 <-> search_engine)을
        # 그대로 쓴다(glossary.tsv 는 수정 금지 — 새 항목을 추가하지 않고 기존 항목만 쓴다).
        # 문서는 순수 영문으로만 채워 한글 2-gram 우연 매칭 가능성을 없앤다.
        "docs/glossary/gloss_doc.md": _doc(
            "# Glossary Doc", "", "## Section", "",
            "This document describes the search_engine module design in detail.",
        ),
        # I2 — 본문 비어있지 않음.
        "docs/body/body_doc.md": _doc(
            "# 본문 문서", "", "## 섹션", "",
            "BODYCONTENTMARKER_UNIQ_9f21 이 조각의 본문에 실제로 담겨야 하는 고유 표식이다.",
            "본문이 통째로 비면 이 문자열도 함께 사라진다.",
        ),
    }

    # ── I1 ───────────────────────────────────────────────────────
    def test_i1_status_weight_moves_ranking(self) -> None:
        """I1 — status 가중이 실제로 순위를 움직인다.

        잡는 변이: `STATUS_WEIGHT` 전부 1.0 / status 를 랭킹에서 배제(raw 로 정렬).
        둘 다 "가중 없이 raw 로만 정렬"과 동치 결과를 내므로 이 테스트 하나로 둘 다 잡는다.

        current 문서는 STATUSWEIGHTTERM 을 2회, stale 문서는 6회 반복해 **raw 점수는
        stale 이 더 높게** 만들었다(실측 확인) — 그런데도 가중(stale=0.35) 적용 후에는
        current 가 1위여야 한다. raw 로만 정렬하면 이 순서가 뒤집힌다.
        """
        r = search.search("STATUSWEIGHTTERM", category="all", k=5, log=False, source="test")
        rels = [h.rel for h in r.hits]
        self.assertIn("docs/status/sw_current.md", rels)
        self.assertIn("docs/status/sw_stale.md", rels)

        cur_idx = rels.index("docs/status/sw_current.md")
        stale_idx = rels.index("docs/status/sw_stale.md")

        # 가중 후 순위: current 가 stale 보다 앞(작은 인덱스)이어야 한다.
        self.assertLess(
            cur_idx, stale_idx,
            f"가중 후 순위가 뒤집히지 않았다 — current={cur_idx}위, stale={stale_idx}위 "
            f"(STATUS_WEIGHT 가 랭킹에 반영 안 됐을 수 있다)",
        )
        # raw 점수 자체는 stale 이 더 높아야(=이 테스트가 실제로 가중 기전을 밟는다는
        # 증거) 한다 — 그렇지 않으면 애초에 가중과 무관하게 current 가 이겼을 수 있어
        # 이 테스트가 무정보 green 이 된다(eval.py F4 재검증과 같은 우려).
        self.assertGreater(
            r.raw_scores[stale_idx], r.raw_scores[cur_idx],
            "stale 문서의 raw 점수가 current 보다 높지 않다 — 이 코퍼스로는 가중 기전이 "
            "실제로 순위를 바꿨는지 검증할 수 없다(픽스처 재설계 필요)",
        )

        # search.diagnose_status_weight_rank() 로도 교차 확인(가중 전/후 순위가 달라야 함).
        diag = search.diagnose_status_weight_rank("STATUSWEIGHTTERM", "sw_stale")
        self.assertTrue(diag["found_raw"] and diag["found_weighted"])
        self.assertNotEqual(
            diag["raw_rank"], diag["weighted_rank"],
            "diagnose_status_weight_rank 로 봐도 가중이 순위를 안 옮겼다",
        )

    # ── I2 ───────────────────────────────────────────────────────
    def test_i2_body_not_empty(self) -> None:
        """I2 — 조각 본문이 비어 있지 않다. 잡는 변이: 조각 본문 전멸(body='').

        Hit.text 필드가 아니라 **format_result() 가 실제로 내는 출력**을 검사한다 —
        body_empty 변이는 `_hit_body_text()` 내부 지역변수만 비우므로 Hit.text 자체는
        멀쩡히 남는다(그래서 Hit.text 를 직접 보면 이 변이를 못 잡는다).
        """
        r = search.search("BODYCONTENTMARKER_UNIQ_9f21", category="all", k=5, log=False, source="test")
        self.assertGreaterEqual(len(r.hits), 1)

        rendered = search.format_result(r, "BODYCONTENTMARKER_UNIQ_9f21")
        self.assertIn(
            "BODYCONTENTMARKER_UNIQ_9f21", rendered,
            "format_result() 출력에 본문 고유 표식이 없다 — 본문이 비어서 나갔을 수 있다",
        )

        # _hit_body_text() 는 MCP/HTTP 두 표면이 공유하는 단일 근거다 — 직접도 확인한다.
        body, _truncated = search._hit_body_text(r.hits[0])
        self.assertTrue(body.strip(), "_hit_body_text() 가 빈 문자열을 반환했다")

    # ── I3 ───────────────────────────────────────────────────────
    def test_i3_line_range_matches_source_file(self) -> None:
        """I3 — 줄범위가 원문 파일의 실제 줄과 일치한다. 잡는 변이: 줄범위 0(Read 에스컬레이션 파손).

        원문 .md 를 실제로 읽어 [start_line, end_line] 구간을 슬라이스하고, 조각 본문과
        바이트 단위로 대조한다(합성 코퍼스라 가능 — 지시서 §5 명시 요구).
        """
        r = search.search("LINERANGEMARKER", category="all", k=5, log=False, source="test")
        self.assertEqual(len(r.hits), 1, f"고유 표식 질의인데 히트가 {len(r.hits)}건이다")
        hit = r.hits[0]
        self.assertEqual(hit.rel, "docs/line_range/lr_doc.md")

        src_path = self.docs_root / hit.rel
        src_lines = src_path.read_text(encoding="utf-8").splitlines()

        self.assertGreater(hit.start_line, 0, "start_line 이 0 이하다(파손 의심)")
        self.assertGreaterEqual(hit.end_line, hit.start_line, "end_line < start_line")

        expected_slice = "\n".join(src_lines[hit.start_line - 1 : hit.end_line])
        self.assertTrue(expected_slice.strip(), "슬라이스가 비어 있다 — 줄범위가 파손됐을 수 있다")

        body, _truncated = search._hit_body_text(hit)
        self.assertEqual(
            body, expected_slice,
            f"조각 본문이 원문 {hit.rel} 의 [{hit.start_line}:{hit.end_line}] 슬라이스와 다르다",
        )
        self.assertIn("LINERANGEMARKER", expected_slice)

    # ── I4 ───────────────────────────────────────────────────────
    def test_i4_category_filter_effective(self) -> None:
        """I4 — 카테고리 필터가 실효한다(요청 카테고리 결과는 전부 그 카테고리).
        잡는 변이: 카테고리 필터 무력화(filtered = list(candidates)).
        """
        # 사전조건 — category="all" 이면 api·db 양쪽 문서가 다 걸려야 한다(그래야 아래
        # category="api" 필터링이 "원래 0건"이 아니라 "걸러서 줄인" 것임을 보증한다).
        r_all = search.search("CATFILTERTERM", category="all", k=10, log=False, source="test")
        cats_all = {h.category for h in r_all.hits}
        self.assertEqual(
            cats_all, {"api", "db"},
            f"사전조건 실패 — category=all 인데 api/db 양쪽이 다 안 걸렸다(실제: {cats_all})",
        )

        r_api = search.search("CATFILTERTERM", category="api", k=10, log=False, source="test")
        self.assertGreaterEqual(len(r_api.hits), 1)
        self.assertTrue(
            all(h.category == "api" for h in r_api.hits),
            f"category='api' 요청인데 다른 카테고리가 섞였다: {[h.category for h in r_api.hits]}",
        )
        self.assertFalse(r_api.fallback_used)
        self.assertEqual(r_api.category_used, "api")

        r_db = search.search("CATFILTERTERM", category="db", k=10, log=False, source="test")
        self.assertTrue(all(h.category == "db" for h in r_db.hits))

    # ── I6 ───────────────────────────────────────────────────────
    def test_i6_score_normalized_0_to_1(self) -> None:
        """I6 — 점수가 0~1 로 정규화된다(1위=1.0, 전건 0<=s<=1).
        잡는 변이: 점수 정규화 제거(norm = weighted, R-001 이음매 2 파손).
        """
        r = search.search("STATUSWEIGHTTERM", category="all", k=5, log=False, source="test")
        self.assertGreaterEqual(len(r.hits), 1)
        self.assertEqual(
            r.hits[0].score, 1.0,
            f"1위 항목의 점수가 1.0 이 아니다(실제: {r.hits[0].score}) — 정규화가 빠졌을 수 있다",
        )
        for h in r.hits:
            self.assertGreaterEqual(h.score, 0.0)
            self.assertLessEqual(h.score, 1.0)

    # ── I7 ───────────────────────────────────────────────────────
    def test_i7_hit_status_matches_sqlite(self) -> None:
        """I7 — 결과의 status 가 sqlite 실제 값과 일치한다.
        잡는 변이: status 표시를 전부 current 로 위조.

        search.py 가 만든 Hit.status 를 다시 search.py 로 확인하는 자기순환을 피하려고
        sqlite 를 **직접** 열어 대조한다(오라클을 피검증 코드 밖에 둔다).
        """
        r = search.search("STATUSWEIGHTTERM", category="all", k=5, log=False, source="test")
        statuses = {h.rel: h.status for h in r.hits}
        # 사전조건 — 이 질의는 current/stale/deprecated 세 상태를 전부 걸어야 한다
        # (그래야 "전부 current 로 위조" 변이가 뭔가를 실제로 바꾼다).
        self.assertEqual(
            set(statuses.values()), {"current", "stale", "deprecated"},
            f"사전조건 실패 — 세 상태가 다 안 걸렸다(실제: {statuses})",
        )

        conn = sqlite3.connect(str(self.index_dir / "chunks.sqlite"))
        conn.row_factory = sqlite3.Row
        try:
            for h in r.hits:
                row = conn.execute("SELECT status FROM chunks WHERE idx = ?", (h.idx,)).fetchone()
                self.assertIsNotNone(row, f"idx={h.idx} 가 sqlite 에 없다")
                self.assertEqual(
                    h.status, row["status"],
                    f"{h.rel} — Hit.status={h.status!r} 인데 sqlite 실제값={row['status']!r}",
                )
        finally:
            conn.close()

    # ── I8 ───────────────────────────────────────────────────────
    def test_i8_zero_score_padding_excluded(self) -> None:
        """I8 — 0점 패딩이 결과에 안 실린다(완전 무관 질의 -> hits 0).
        잡는 변이: 0점 패딩 필터 제거(B3 무력화).

        코퍼스 어디에도 없는 gibberish 토큰 — bm25s 가 k 를 채우려 raw=0 인 후보로
        패딩해도, search() 는 이걸 걸러 hits=[] 를 내야 한다.
        """
        gibberish = "zzqzz9182xnvbnqwiopxyz"
        # 전제 확인 — 이 토큰이 코퍼스 어디에도 실제로 없는지(다른 이유로 우연히 걸리면
        # 이 테스트가 무의미해진다).
        for content in self.CORPUS.values():
            self.assertNotIn(gibberish, content.lower())

        r = search.search(gibberish, category="all", k=5, log=False, source="test")
        self.assertEqual(
            r.hits, [],
            f"완전 무관 질의인데 hits 가 {len(r.hits)}건 나왔다 — 0점 패딩이 샜을 수 있다",
        )
        self.assertEqual(r.confidence, "none")

    # ── I9 ───────────────────────────────────────────────────────
    def test_i9_overfetch_maintained(self) -> None:
        """I9 — 오버페치가 유지된다(fetch = min(max(50, k*5), n_chunks)).
        잡는 변이: 오버페치 제거(fetch = min(k, n_chunks)).

        코퍼스가 작아도(< 50 조각) 공식 자체를 그대로 재현해 대조한다 — 작은 코퍼스에서도
        k*5 < n_chunks 인 한 두 공식은 다른 값을 낸다(실측 확인).
        """
        meta = self.sqlite_meta()
        n_chunks = int(meta["n_chunks"])
        k = 1
        expected_fetch = min(max(50, k * 5), n_chunks)

        r = search.search("STATUSWEIGHTTERM", category="all", k=k, log=False, source="test")
        self.assertEqual(
            r.fetch, expected_fetch,
            f"fetch={r.fetch} 인데 기대값(min(max(50,k*5),n_chunks))={expected_fetch}",
        )

    # ── I10 ──────────────────────────────────────────────────────
    def test_i10_glossary_expansion_works(self) -> None:
        """I10 — 용어집 질의확장이 동작한다. 잡는 변이: 용어집 확장 제거(no_glossary).

        문서는 순수 영문("search_engine")만 담고, 질의는 한글("검색엔진")만 쓴다 —
        용어집(glossary.tsv 기존 등재 항목, 수정하지 않음) 확장이 없으면 이 질의는
        이 문서를 원리적으로 찾을 수 없다(한글/영문 토큰이 겹치지 않는다).
        """
        r = search.search("검색엔진", category="all", k=5, log=False, source="test")
        rels = [h.rel for h in r.hits]
        self.assertIn(
            "docs/glossary/gloss_doc.md", rels,
            f"'검색엔진'(한글) 질의로 'search_engine'(영문) 전용 문서를 못 찾았다 — "
            f"용어집 확장이 꺼졌을 수 있다. 실제 히트: {rels}",
        )


if __name__ == "__main__":
    unittest.main()
