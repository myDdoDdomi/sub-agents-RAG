"""표면 계약(지시서 §6) — MCP<->HTTP 조각집합 동치성 · k/category/q 매트릭스 ·
stdio 왕복 통합 티어.

인프로세스로 한다(지시서 §6 명시): HTTP 는 `fastapi.testclient.TestClient`, MCP 는
`mcp_server.search_docs()` **직접 호출**(`@app.tool()` 이 평범한 함수를 그대로 반환함 —
실측 확인됨). 이 직접 호출은 MCP SDK 의 프로토콜 계층(JSON 스키마 검증)을 **우회**한다
— SDK 가 막았을 케이스와 우리 함수 자체의 방어 코드가 막는 케이스는 다르다(아래
TestKCategoryQMatrix 클래스 docstring 에 그 경계를 명시한다). 그 차이가 실제로
관찰 가능한 지점은 stdio 왕복(별도 통합 티어)뿐이다.
"""
from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import mcp_server
import server

from tests import testutil

REPO_ROOT = Path(__file__).resolve().parent.parent

# search._hit_header_text() 의 실제 포맷과 정확히 맞춘 정규식(search.py 읽기전용 —
# 소스를 바꾸지 않고 출력 포맷만 파싱한다).
# 2-1(qa 독립검증, 2026-08-14) — 헤더가 "점수 X.XXX(...설명...) · raw Y.YY · status=..."
# 로 바뀌었다(raw 가 confidence 와 무관하게 항상 병기됨) — 그룹 7이 raw, 8이 status로
# 밀렸다(이전엔 그룹 7이 status였다).
_HDR_RE = re.compile(
    r"^\[(\d+)\] (.+?) \((.*?)\) 줄 (\d+)~(\d+) · 점수 ([\d.]+)\([^)]*\) · "
    r"raw ([\d.]+|\?) · status=(\w+)",
    re.M,
)


def _parse_mcp_hits(text: str) -> list[tuple[str, int, int, str]]:
    """MCP 텍스트 출력에서 (rel, start_line, end_line, status) 튜플 리스트를 뽑는다."""
    return [(m.group(2), int(m.group(4)), int(m.group(5)), m.group(8)) for m in _HDR_RE.finditer(text)]


def _http_hit_tuples(hits: list[dict]) -> list[tuple[str, int, int, str]]:
    return [(h["rel"], h["start_line"], h["end_line"], h["status"]) for h in hits]


def _surface_doc(n: int, reps: int) -> str:
    body = " ".join([f"SURFACETERM 항목{n:02d}" for _ in range(reps)])
    return f"# 표면계약 문서 {n:02d}\n\n## 섹션\n\n{body}\n"


class TestMcpHttpEquivalence(testutil.IsolatedIndexCase):
    """MCP <-> HTTP 조각집합 동치성(§6 첫 항목) — qa_f_equiv.py 이관.

    12개 문서 모두 공통 용어 SURFACETERM 을 서로 다른 반복 횟수(12~1회)로 담아 순위가
    타이 없이 갈리게 한다 — MCP 상한 k=10 / HTTP 상한 k=20 경계를 여러 k 로 실제로
    넘나들며 "MCP 결과가 HTTP 결과의 접두(prefix)"인지 검증한다.
    """

    CORPUS = {f"docs/surface/doc{n:02d}.md": _surface_doc(n, 13 - n) for n in range(1, 13)}

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.client = TestClient(server.app, base_url="http://127.0.0.1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        super().tearDownClass()

    def _mcp(self, query: str, category: str, k: int) -> tuple[list[tuple], str]:
        text = mcp_server.search_docs(query, category=category, k=k)
        return _parse_mcp_hits(text), text

    def _http(self, query: str, category: str, k: int) -> tuple[list[tuple], dict]:
        r = self.client.get("/search", params={"q": query, "category": category, "k": str(k)})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        return _http_hit_tuples(body["hits"]), body

    def test_mcp_hits_are_prefix_of_http_hits_across_k(self) -> None:
        for k in [1, 3, 5, 10, 12, 15, 20]:
            with self.subTest(k=k):
                mcp_hits, mcp_text = self._mcp("SURFACETERM", "all", k)
                http_hits, http_body = self._http("SURFACETERM", "all", k)

                self.assertGreaterEqual(len(http_hits), 1, "HTTP 결과가 0건이다(픽스처 이상)")
                mcp_cap = min(k, mcp_server._MCP_MAX_K)
                expected_prefix = http_hits[:mcp_cap]

                self.assertEqual(
                    mcp_hits, expected_prefix,
                    f"k={k}: MCP 결과가 HTTP 결과의 접두가 아니다\n"
                    f"  MCP({len(mcp_hits)}건)={mcp_hits}\n  HTTP접두({len(expected_prefix)}건)={expected_prefix}",
                )
                self.assertLessEqual(len(mcp_hits), mcp_server._MCP_MAX_K)

    def test_mcp_and_http_agree_on_category_filter(self) -> None:
        mcp_hits, _ = self._mcp("SURFACETERM", "all", 10)
        http_hits, http_body = self._http("SURFACETERM", "all", 10)
        self.assertEqual(mcp_hits, http_hits[:10])
        self.assertEqual(http_body["category_used"], "all")

    def test_list_doc_categories_mcp_matches_http_categories_endpoint(self) -> None:
        mcp_text = mcp_server.list_doc_categories()
        r = self.client.get("/categories")
        self.assertEqual(r.status_code, 200)
        http_cats = r.json()
        for cat in http_cats:
            self.assertIn(cat, mcp_text, f"카테고리 '{cat}' 가 MCP 목록 텍스트에 없다")


class TestKCategoryQMatrix(testutil.IsolatedIndexCase):
    """k/category/q 매트릭스(§6 둘째 항목) — qa_b_matrix.py 이관, 축소판.

    판정 기준(지시서 명시): **HTTP 500 = 결함.** 이 축은 절대 위반하면 안 된다.

    MCP 쪽은 `mcp_server.search_docs()` **직접 함수 호출**이라 실제 MCP SDK 프로토콜의
    JSON 스키마 검증을 거치지 않는다 — 그래서 여기서 관찰하는 MCP 동작은 "우리 함수
    자체의 방어 코드"(`_clamp_k` 등)이지 "SDK 가 잘못된 타입을 프로토콜 단에서 막는
    것"이 아니다. 후자는 stdio 왕복 통합 테스트(TestStdioRoundTrip)에서만 원리적으로
    관찰 가능하다 — 이 클래스 내 각 테스트가 그 경계를 docstring 에 명시한다.
    """

    CORPUS = {f"docs/surface/doc{n:02d}.md": _surface_doc(n, 13 - n) for n in range(1, 13)}
    Q = "SURFACETERM"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.client = TestClient(server.app, base_url="http://127.0.0.1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        super().tearDownClass()

    def _http_status(self, params: dict) -> tuple[int, object]:
        r = self.client.get("/search", params=params)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body

    # ── k 축 ─────────────────────────────────────────────────────
    def test_k_axis_http_never_500(self) -> None:
        cases = {
            "k-omit": {"q": self.Q},
            "k-0": {"q": self.Q, "k": "0"},
            "k-neg": {"q": self.Q, "k": "-3"},
            "k-99": {"q": self.Q, "k": "99"},
            "k-str-digit": {"q": self.Q, "k": "3"},
            "k-str-abc": {"q": self.Q, "k": "abc"},
            "k-float": {"q": self.Q, "k": "2.7"},
            "k-empty": {"q": self.Q, "k": ""},
            "k-huge": {"q": self.Q, "k": "1000000000"},
        }
        for cid, params in cases.items():
            with self.subTest(case=cid):
                status, body = self._http_status(params)
                self.assertNotEqual(status, 500, f"{cid}: HTTP 500(결함) — body={body}")
                self.assertIn(status, (200,), f"{cid}: 예상 밖 상태코드 {status} — body={body}")

    def test_k_axis_mcp_direct_call_never_raises(self) -> None:
        """직접 함수 호출 — SDK 스키마 검증 없음(위 클래스 docstring 참고). 우리 함수
        자체가 안 죽는지(예외를 [오류] 문자열로 흡수하는지)만 본다.
        """
        for k in [0, -3, 99, "abc", 2.7, None, 10**9, True]:
            with self.subTest(k=k):
                out = mcp_server.search_docs(self.Q, category="all", k=k)  # type: ignore[arg-type]
                self.assertIsInstance(out, str)
                self.assertFalse(out.startswith("[오류]"), f"k={k!r} 에서 내부 오류: {out[:120]}")

    # ── category 축 ──────────────────────────────────────────────
    def test_category_axis_http_never_500(self) -> None:
        cases = {
            "cat-valid": {"q": self.Q, "category": "etc"},
            "cat-upper": {"q": self.Q, "category": "ETC"},
            "cat-space": {"q": self.Q, "category": " etc "},
            "cat-bogus": {"q": self.Q, "category": "존재없음카테고리"},
            "cat-empty": {"q": self.Q, "category": ""},
            "cat-omit": {"q": self.Q},
            "cat-int-like": {"q": self.Q, "category": "123"},
        }
        for cid, params in cases.items():
            with self.subTest(case=cid):
                status, body = self._http_status(params)
                self.assertNotEqual(status, 500, f"{cid}: HTTP 500(결함) — body={body}")

    def test_category_axis_case_and_space_normalized_both_surfaces(self) -> None:
        """L7 계약 — 대소문자/공백 정규화가 MCP·HTTP 양쪽에서 동일해야 한다."""
        variants = ["etc", "ETC", " etc ", "Etc"]
        http_results = []
        for v in variants:
            status, body = self._http_status({"q": self.Q, "category": v})
            self.assertEqual(status, 200)
            self.assertTrue(body["category_valid"], f"category={v!r} 가 무효 처리됐다")
            http_results.append(body["category_used"])
        self.assertTrue(all(c == "etc" for c in http_results), http_results)

        for v in variants:
            out = mcp_server.search_docs(self.Q, category=v, k=3)
            self.assertNotIn("[안내] 요청한 카테고리", out, f"MCP 에서 category={v!r} 가 무효 처리됐다")

    # ── q 축 ─────────────────────────────────────────────────────
    def test_q_axis_http_never_500(self) -> None:
        cases = {
            "q-omit": {},
            "q-empty": {"q": ""},
            "q-spaces": {"q": "   "},
            "q-nonascii": {"q": "약관 동의 화면 문구"},
            "q-long": {"q": "코스 추천 " * 800},
            "q-special": {"q": "<script>alert(1)</script> & \" ' ` % # ?"},
            "q-sqlmeta": {"q": "'; DROP TABLE chunks; --"},
            "q-emoji": {"q": "코스 🚀 추천"},
        }
        for cid, params in cases.items():
            with self.subTest(case=cid):
                status, body = self._http_status(params)
                self.assertNotEqual(status, 500, f"{cid}: HTTP 500(결함) — body={body}")

    def test_empty_q_deficient_axis_matches_helper_not_hardcoded_empty_string(self) -> None:
        """L2(qa 독립검증 후속, 2026-08-14) — 빈 q 조기 반환의 `deficient_axis` 가 리터럴
        `""` 이 아니라 `search._deficient_axis(0.0, 0.0)` 과 같은 값이어야 한다(라벨
        드리프트 방지 — 수정 전이라면 이 응답만 다른 경로와 다른 값을 냈다)."""
        import search as search_mod

        status, body = self._http_status({"q": ""})
        self.assertEqual(status, 200)
        self.assertEqual(body["deficient_axis"], search_mod._deficient_axis(0.0, 0.0))
        self.assertNotEqual(body["deficient_axis"], "", "빈 q 응답이 여전히 하드코딩된 빈 문자열이다")

    def test_q_axis_nul_byte_raw_querystring_http_never_500(self) -> None:
        """NUL 바이트는 dict params 로 못 담아 원시 쿼리스트링으로 보낸다(qa_b_matrix.py
        와 동일한 이유 — urlencode 가 처리 못 하는 축)."""
        r = self.client.get("/search?q=a%00b")
        self.assertNotEqual(r.status_code, 500, f"NUL 바이트 질의에서 500 — body={r.text[:200]}")

    def test_q_axis_mcp_direct_call_handles_none_and_int(self) -> None:
        """q=None 직접 호출 — SDK 라면 프로토콜 단에서 막혔겠지만(타입 불일치), 직접
        호출은 그걸 우회한다(위 클래스 docstring) — 그래도 우리 함수 자체가 예외를
        [오류] 문자열로 흡수해 죽지 않아야 한다.
        """
        out_none = mcp_server.search_docs(None, category="all", k=3)  # type: ignore[arg-type]
        self.assertIsInstance(out_none, str)

        out_int = mcp_server.search_docs(42, category="all", k=3)  # type: ignore[arg-type]
        self.assertIsInstance(out_int, str)


class TestStdioRoundTrip(unittest.TestCase):
    """§6 마지막 항목 — stdio 왕복 1건, 별도 통합 티어(실 색인 필요).

    `config.INDEX_DIR` 는 환경변수로 오버라이드할 수 없다(config.py 수정 금지 —
    지시서 §2) — 그래서 이 테스트만은 **실제 서브프로세스**로 mcp_server.py 를 띄우고,
    그 서브프로세스는 항상 실 `docs-rag-mcp/index/` 를 읽는다(순수 조회만 하므로 실
    색인을 변경하지 않는다). 실 색인이 없는 환경(예: CI 최초 클론)에서는 스킵하되
    **스킵 사유가 -v 실행 시 출력되게** 한다(지시서 §6 명시 요구 — 무음 스킵 금지).
    """

    _INDEX_OK = testutil.real_index_available()
    if not _INDEX_OK:
        sys.stderr.write(
            "[tests] TestStdioRoundTrip 스킵 — 실 index/ 가 없다(python indexer.py 먼저 실행)\n"
        )

    @unittest.skipUnless(_INDEX_OK, "실 index/ 가 없다 — python indexer.py 를 먼저 실행하라")
    def test_stdio_initialize_and_call_tool_round_trip(self) -> None:
        async def _run() -> tuple[str, str]:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=sys.executable,
                args=[str(REPO_ROOT / "mcp_server.py")],
                cwd=str(REPO_ROOT),
                env={
                    "PYTHONUTF8": "1", "SystemRoot": r"C:\Windows", "PATH": r"C:\Windows\System32",
                    # 중요 — StdioServerParameters 에 env= 를 명시하면 **부모 환경을 상속하지
                    # 않고 전체 대체**한다(실측 확인: 이 줄이 없으면 이 서브프로세스는
                    # DOCS_RAG_NO_LOG 를 못 받아 실 logs/queries.jsonl 에 로그를 남긴다
                    # — qa_f_equiv.py/qa_g_mcp_stale.py 참고 스크립트도 이 함정에 걸려 있었다).
                    "DOCS_RAG_NO_LOG": "1",
                },
            )
            with open("nul", "w") as devnull:
                async with stdio_client(params, errlog=devnull) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        search_res = await session.call_tool("search_docs", {"query": "D-52 결정", "k": 3})
                        search_text = "".join(
                            getattr(c, "text", "") or "" for c in (search_res.content or [])
                        )
                        cat_res = await session.call_tool("list_doc_categories", {})
                        cat_text = "".join(getattr(c, "text", "") or "" for c in (cat_res.content or []))
            return search_text, cat_text

        search_text, cat_text = asyncio.run(_run())

        self.assertTrue(search_text, "search_docs stdio 왕복 결과가 비었다")
        self.assertFalse(search_text.startswith("[오류]"), f"stdio 왕복에서 오류: {search_text[:200]}")
        self.assertIn("D-52", search_text)

        self.assertIn("all — 전체", cat_text)


if __name__ == "__main__":
    unittest.main()
