"""원본 .md stale 감지(4차 배치 B) + `indexer.py --check` 종료코드 계약.

지시서 §8 이 요구하는 "stale 감지 변이(감지 무력화)"의 표적이 되는 테스트다 — 이게
없으면 `check_staleness()` 의 핵심 판정(`stale = n_changed > 0 or count_changed`)이
`stale = False` 로 하드코딩돼도 어떤 테스트도 못 잡는다.

각 테스트가 자기만의 임시 코퍼스를 갖는다(인스턴스 레벨 setUp/tearDown) — 파일 mtime 을
테스트 중간에 바꾸는 시나리오라 클래스 공유 코퍼스(testutil.IsolatedIndexCase 의
setUpClass 1회 색인) 패턴과는 안 맞는다(mtime 변경이 다른 테스트 메서드에 새면 안 된다).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import config
import search
import server

from tests import testutil


class TestStaleDetectionAndCheck(unittest.TestCase):
    CORPUS = {
        "docs/stale_test/doc_a.md": (
            "# 문서 A\n\n## 섹션\n\nSTALECHECKTERM 이 이 문서의 핵심 표식이다.\n"
        ),
        "docs/stale_test/doc_b.md": (
            "# 문서 B\n\n## 섹션\n\n무관한 내용의 문서다.\n"
        ),
    }

    def setUp(self) -> None:
        self._orig_docs_root = config.DOCS_ROOT
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_stale_")
        self.tmp_path = Path(self._tmpdir)
        self.docs_root = self.tmp_path / "docs_root"
        self.index_dir = self.tmp_path / "index"

        testutil.write_corpus(self.docs_root, self.CORPUS)
        config.DOCS_ROOT = self.docs_root
        config.INDEX_DIR = self.index_dir
        testutil.reset_search_module_caches()

        exit_code, out = testutil.reindex_in_process()
        if exit_code != 0:
            raise RuntimeError(f"픽스처 색인 실패(exit={exit_code}):\n{out}")

    def tearDown(self) -> None:
        config.DOCS_ROOT = self._orig_docs_root
        config.INDEX_DIR = self._orig_index_dir
        testutil.reset_search_module_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _check_staleness_direct(self) -> search.StalenessCheck:
        conn = search._get_conn()
        try:
            return search.check_staleness(conn)
        finally:
            conn.close()

    def test_fresh_index_not_stale(self) -> None:
        """정상 대조군 — 방금 색인했으면 stale 이 아니어야 한다(§B-5 "stale 아님으로
        단정 안 함"과는 별개로, 실제로 신선한 상태에서는 stale=False 가 맞아야 한다).
        """
        check = self._check_staleness_direct()
        self.assertTrue(check.ok)
        self.assertFalse(check.stale)

        exit_code, out = testutil.reindex_in_process(["--check"])
        self.assertEqual(exit_code, 0, f"신선한 색인인데 --check 종료코드가 0 이 아니다:\n{out}")
        self.assertIn("신선함", out)

        # H1(코드리뷰) — _get_staleness() 가 SWR 부트스트랩이 되면서 "이 색인 세대에 대해
        # 아직 계산된 적 없음"인 첫 호출은 즉시 _STALENESS_PENDING(index_stale_ok=False)
        # 을 반환하고 계산은 백그라운드로 넘긴다(더 이상 호출자 커넥션을 쥔 채 동기 계산
        # 하지 않는다) — 그래서 이 단언 전에 refresh_staleness_now() 로 명시 동기화해야
        # 판정이 실제로 실린다(수정 전에는 첫 search() 호출이 곧바로 판정을 실었다).
        search.refresh_staleness_now()
        r = search.search("STALECHECKTERM", category="all", k=5, log=False, source="test")
        self.assertFalse(r.index_stale)
        self.assertTrue(r.index_stale_ok)

    def test_modified_source_detected_as_stale(self) -> None:
        """I(stale) — 원본이 색인보다 새로워지면 감지돼야 한다.
        잡는 변이: `check_staleness()` 의 stale 판정 무력화(항상 False).
        """
        target = self.docs_root / "docs" / "stale_test" / "doc_a.md"
        future = time.time() + 3600  # 1시간 뒤 — 지문 정밀도/시계 오차와 무관하게 확실히 미래
        os.utime(target, (future, future))

        check = self._check_staleness_direct()
        self.assertTrue(check.ok)
        self.assertTrue(check.stale, "원본을 미래 mtime 으로 만들었는데 stale=False 다")
        self.assertGreaterEqual(check.n_changed_docs, 1)

        # end-to-end: search() 결과에도 같은 신호가 실려야 한다(요건 B-1/B-6). H1(코드
        # 리뷰) — 위 test_fresh_index_not_stale 과 같은 이유로 동기 워밍이 필요하다: SWR
        # 부트스트랩이 비동기가 된 뒤로는 이 프로세스의 첫 판정이 아직 계산 전이면 pending
        # 을 낸다 — refresh_staleness_now() 로 동기화한 뒤에야 stale=True 가 실제로 실린다.
        search.refresh_staleness_now()
        r = search.search("STALECHECKTERM", category="all", k=5, log=False, source="test")
        self.assertTrue(r.index_stale)
        self.assertGreaterEqual(r.index_stale_docs, 1)
        self.assertTrue(r.index_stale_ok)

        rendered = search.format_result(r, "STALECHECKTERM")
        self.assertIn("[경고]", rendered)
        self.assertIn("낡았다", rendered)

        exit_code, out = testutil.reindex_in_process(["--check"])
        self.assertEqual(exit_code, 1, f"stale 상태인데 --check 종료코드가 1 이 아니다:\n{out}")
        self.assertIn("낡음", out)

    def test_stale_snapshot_invalidated_after_reindex_m3(self) -> None:
        """M3(코드리뷰) — 재색인 직후 최대 60초 "재색인하라" 오경고가 나면 안 된다.

        H1(2) 의 `_StaleSnapshot.fingerprint` 로 구조적으로 닫는다: `_get_staleness()` 가
        스냅샷의 색인 세대(fingerprint)와 지금 검색이 실제로 쓰는 색인 세대가 다르면
        그 스냅샷을 무효로 보고 pending + 백그라운드 재계산으로 간다 — TTL(경과시간)
        만으로 신선도를 판정하면 재색인 직후에도 최대 60초 동안 구세대 stale=True 를
        그대로 돌려준다(수정 전 결함). 잡는 변이: `_get_staleness()` 를 지문 비교가 아닌
        경과시간만으로 되돌리는 것.
        """
        target = self.docs_root / "docs" / "stale_test" / "doc_a.md"
        now = time.time()
        os.utime(target, (now, now))  # ② 원본 문서를 "현재 시각"으로 touch

        check = search.refresh_staleness_now()
        self.assertTrue(check.ok)
        self.assertTrue(check.stale, "touch 직후인데 stale=True 가 아니다(사전조건 실패)")

        # ③ 재색인 — 색인의 created_at 이 touch 시각보다 뒤로 갱신되므로 원리적으로
        # stale 이 해소돼야 한다.
        exit_code, out = testutil.reindex_in_process()
        self.assertEqual(exit_code, 0, f"재색인 실패:\n{out}")

        # ④ 재색인 직후 첫 search() 는 새 색인 세대의 지문을 들고 오지만, 방금 캐시된
        # 스냅샷은 구세대 지문이다(H1(2) fingerprint 비교) — pending(index_stale_ok=False)
        # 을 내야 한다. 이 assert 는 이 테스트가 "TTL 미만료라 구세대 값을 그대로 씀"이
        # 아니라 "세대가 달라 무효화됨" 경로를 실제로 밟고 있다는 사전조건 확인이다.
        r_pending = search.search("STALECHECKTERM", category="all", k=5, log=False, source="test")
        self.assertFalse(
            r_pending.index_stale_ok,
            "재색인 직후 첫 검색이 pending(index_stale_ok=False) 이 아니다 — 이 테스트의 "
            "전제(세대 무효화 경로)가 이 환경에서 성립하지 않는다",
        )

        # refresh_staleness_now() 로 새 세대 기준 판정을 명시 동기화한 뒤에는 stale 이
        # 확실히 해소돼야 한다 — 수정 전이라면(TTL 만 보는 구현) 아직 60초가 안 지나
        # stale=True 를 계속 돌려줘 아래 두 단언이 실패한다.
        search.refresh_staleness_now()
        r = search.search("STALECHECKTERM", category="all", k=5, log=False, source="test")
        self.assertIs(
            r.index_stale, False,
            f"재색인 후에도 index_stale=True 다(M3 회귀) — detail={r.index_stale_detail!r}",
        )
        self.assertIs(r.index_stale_ok, True)

    def test_check_returns_2_when_index_missing(self) -> None:
        """`indexer.py --check` 종료코드 계약 — 색인 자체가 없으면 2(판정 불가)여야
        한다(0/1 과 구분되는 세 번째 상태, O3 요건).
        """
        shutil.rmtree(self.index_dir, ignore_errors=True)
        testutil.reset_search_module_caches()

        exit_code, out = testutil.reindex_in_process(["--check"])
        self.assertEqual(exit_code, 2, f"색인이 없는데 --check 종료코드가 2 가 아니다:\n{out}")


class TestStaleCheckPendingVsFailedDistinguishable(unittest.TestCase):
    """2-4(qa 독립검증, 2026-08-14) — `stale_check_ok:false` 가 "판정 불가(failed)"와
    "판정 보류(pending, 백그라운드 계산 중)"를 겸해 두 상태를 구분할 수 없던 결함.

    직전 배치는 "detail·stderr 로 구분 가능"이라 문서화했지만 실측 결과 그 완화책이
    실재하지 않았다(`search.py` 가 ok=False 일 때 detail 을 "" 로 덮었고, server.py 는
    detail 을 응답에 싣지도 않았고, pending 은 stderr 에도 아무것도 안 썼다 —
    `$SC\\a1_stale.py` 재현). 잡는 변이: `StalenessCheck.pending`/
    `SearchResult.index_stale_pending` 을 다시 지우거나 항상 `False` 로 고정하는 것.
    """

    CORPUS = {
        "docs/pending_test/doc_a.md": (
            "# 문서 A\n\n## 섹션\n\nPENDINGDISTINCTTERM 이 이 문서의 핵심 표식이다.\n"
        ),
    }

    def setUp(self) -> None:
        self._orig_docs_root = config.DOCS_ROOT
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_pending_")
        self.tmp_path = Path(self._tmpdir)
        self.docs_root = self.tmp_path / "docs_root"
        self.index_dir = self.tmp_path / "index"

        testutil.write_corpus(self.docs_root, self.CORPUS)
        config.DOCS_ROOT = self.docs_root
        config.INDEX_DIR = self.index_dir
        testutil.reset_search_module_caches()

        exit_code, out = testutil.reindex_in_process()
        if exit_code != 0:
            raise RuntimeError(f"픽스처 색인 실패(exit={exit_code}):\n{out}")

    def tearDown(self) -> None:
        config.DOCS_ROOT = self._orig_docs_root
        config.INDEX_DIR = self._orig_index_dir
        testutil.reset_search_module_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_internal_staleness_check_objects_are_distinguishable(self) -> None:
        """StalenessCheck 객체 레벨 — pending 과 failed 는 `.pending` 값으로 구분되고,
        `.detail` 도 서로 다른 실제 문구를 담는다(더 이상 ""로 안 뭉개짐).
        """
        # 상태 P: 판정 보류 — 이 색인 세대에 대해 아직 계산 전(캐시가 전부 빈 상태).
        pending = search._get_staleness(("없는지문", "0"))
        self.assertFalse(pending.ok)
        self.assertTrue(pending.pending, "판정 보류 상태인데 pending=True 가 아니다(2-4 회귀)")
        self.assertTrue(pending.detail, "판정 보류 상태인데 detail 이 비어 있다")

        # 상태 F: 판정 불가 — meta.created_at 결측(실제 실패를 강제 발생시킴).
        conn = sqlite3.connect(str(self.index_dir / "chunks.sqlite"))
        conn.execute("DELETE FROM meta WHERE key='created_at'")
        conn.commit()
        conn.close()
        testutil.reset_search_module_caches()
        failed = search.compute_staleness()  # 동기 계산 — 즉시 판정 불가를 확정시킨다
        self.assertFalse(failed.ok)
        self.assertFalse(failed.pending, "판정 불가 상태인데 pending=True 로 잘못 표시됐다(2-4 회귀)")
        self.assertTrue(failed.detail, "판정 불가 상태인데 detail 이 비어 있다")

        self.assertNotEqual(
            pending.pending, failed.pending,
            "판정 보류와 판정 불가가 pending 필드로 구분되지 않는다(2-4 회귀)",
        )
        self.assertNotEqual(
            pending.detail, failed.detail,
            "판정 보류와 판정 불가의 detail 문구가 같다 — 두 상태가 텍스트로도 안 갈린다",
        )

    def test_search_result_pending_and_failed_distinguishable_via_http(self) -> None:
        """end-to-end — `/search` HTTP JSON 응답에서 두 상태가 실제로 구분되는지.

        수정 전에는 두 상태의 `/search` 응답이 **키·값 완전 동일**이었다(qa 실측,
        `a1_stale.py`). 이 테스트는 `stale_check_pending`(신규 필드)·`stale_detail`
        (신규 필드로 노출) 이 두 상태에서 서로 달라야 통과한다.
        """
        client = TestClient(server.app, base_url="http://127.0.0.1")
        try:
            # 상태 P: 판정 보류 — 프로세스 첫 호출(캐시 전부 빈 상태), refresh 없이 바로 검색.
            testutil.reset_search_module_caches()
            r_pending = client.get(
                "/search", params={"q": "PENDINGDISTINCTTERM", "k": 2}
            ).json()
            self.assertFalse(r_pending["stale_check_ok"], "사전조건 — 첫 호출은 pending(ok=False) 이어야 한다")

            # L10(qa 독립검증 후속, 2026-08-14) — 위 요청이 백그라운드 재계산 스레드를
            # 하나 띄워 뒀다(_get_staleness() 가 pending 을 즉시 반환하면서 스레드도
            # 하나 건다). 그 스레드에 참조가 없어(daemon, join 불가) 아래 meta 삭제 +
            # 동기 refresh_staleness_now() 보다 "늦게" 끝나면, 그 스레드가 (삭제 전
            # meta 를 이미 읽어 계산한) 구세대 "ok=True" 스냅샷으로 우리가 방금 발행한
            # "판정 불가" 스냅샷을 덮어써 버리는 경합이 있었다(잠재 flake). `join()` 은
            # 못 하지만 `search._stale_revalidating` 플래그가 스레드 종료 시 False 로
            # 내려가므로 짧게 폴링해 실질적으로 기다린다(비용 저렴 — 정상적으로는
            # 수 ms 안에 끝난다).
            deadline = time.monotonic() + 5.0
            while search._stale_revalidating and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(
                search._stale_revalidating,
                "판정 보류 단계의 백그라운드 재계산 스레드가 5초 안에 끝나지 않았다",
            )

            # 상태 F: 판정 불가 — meta.created_at 결측을 만들고 동기 계산으로 확정시킨다.
            conn = sqlite3.connect(str(self.index_dir / "chunks.sqlite"))
            conn.execute("DELETE FROM meta WHERE key='created_at'")
            conn.commit()
            conn.close()
            testutil.reset_search_module_caches()
            search.refresh_staleness_now()  # 동기 계산 — 판정 불가 스냅샷을 즉시 발행
            r_failed = client.get(
                "/search", params={"q": "PENDINGDISTINCTTERM", "k": 2}
            ).json()
            self.assertFalse(r_failed["stale_check_ok"], "사전조건 — meta 결측은 실패(ok=False) 여야 한다")

            # 수정 전이면 이 두 assertIn 이 이미 KeyError 급으로 실패한다(신규 필드 자체가 없었다).
            self.assertIn("stale_check_pending", r_pending, "HTTP 응답에 stale_check_pending 필드가 없다(2-4 회귀)")
            self.assertIn("stale_check_pending", r_failed)
            self.assertIn("stale_detail", r_pending, "HTTP 응답에 stale_detail 필드가 없다(2-4 회귀)")
            self.assertIn("stale_detail", r_failed)

            self.assertTrue(r_pending["stale_check_pending"], "판정 보류 응답인데 stale_check_pending=True 가 아니다")
            self.assertFalse(r_failed["stale_check_pending"], "판정 불가 응답인데 stale_check_pending=True 로 잘못 표시됐다")

            self.assertNotEqual(
                r_pending["stale_check_pending"], r_failed["stale_check_pending"],
                "두 상태가 stale_check_pending 값으로 구분되지 않는다(2-4 회귀)",
            )
            self.assertTrue(r_pending["stale_detail"], "판정 보류 응답의 stale_detail 이 비어 있다(2-4 회귀 — 예전엔 '' 로 덮였다)")
            self.assertTrue(r_failed["stale_detail"], "판정 불가 응답의 stale_detail 이 비어 있다(2-4 회귀 — 예전엔 '' 로 덮였다)")
            self.assertNotEqual(
                r_pending["stale_detail"], r_failed["stale_detail"],
                "두 상태의 stale_detail 문구가 같다 — 텍스트로도 구분 안 됨",
            )
        finally:
            client.close()

    def test_empty_q_early_return_key_set_matches_normal_response(self) -> None:
        """M5 키 집합 규약(server.py 기존 주석 — 빈 q 조기 반환도 정상 응답과 키 집합을
        맞춘다) — 2-4 신규 필드(stale_check_pending·stale_detail)도 양쪽에 있어야 한다.
        """
        client = TestClient(server.app, base_url="http://127.0.0.1")
        try:
            testutil.reset_search_module_caches()
            search.refresh_staleness_now()
            r_empty = client.get("/search", params={"q": ""}).json()
            r_normal = client.get(
                "/search", params={"q": "PENDINGDISTINCTTERM", "k": 2}
            ).json()
            missing_in_empty = set(r_normal) - set(r_empty)
            self.assertFalse(
                missing_in_empty,
                f"빈 q 응답에 정상 응답에 있는 키가 빠져 있다: {missing_in_empty}",
            )
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
