"""M1(코드리뷰) — `_open_index()` 예외 경로 커넥션 누수 + M2 — `_fingerprint_trustworthy`
방어가 `_reload_with_retry()` 의 더블체크에 막혀 무력화되는 결함.

지시서 §2·§3 대응. 둘 다 `_open_index()`/`_reload_with_retry()` 내부 메커니즘을 다뤄
같은 파일로 묶는다.
"""
from __future__ import annotations

import os
import sqlite3
import unittest

import search

from tests import testutil


class TestM1ConnLeakOnException(testutil.IsolatedIndexCase):
    """`_open_index()` 도중 예외가 나도 커넥션이 새지 않아야 한다. win32 rename 오라클로
    확인한다 — 커넥션이 열려 있으면 `chunks.sqlite` 를 옆으로 옮기는 것 자체가
    `PermissionError` 로 막힌다.
    """

    CORPUS = {
        "docs/m1/doc_a.md": "# 문서 A\n\n## 섹션\n\nM1CONNLEAKTERM 내용이다.\n",
    }

    def _assert_no_leaked_handle(self) -> None:
        sqlite_path = self.index_dir / "chunks.sqlite"
        backup = self.index_dir / "chunks.sqlite.m1_probe_bak"
        os.rename(sqlite_path, backup)  # 커넥션이 열려 있으면(누수) 여기서 PermissionError
        os.rename(backup, sqlite_path)

    @unittest.skipUnless(
        os.name == "nt",
        "win32 전용 오라클 — POSIX 는 열린 파일도 rename 을 막지 않아 이 테스트가 무정보다",
    )
    def test_read_fingerprint_exception_does_not_leak_conn(self) -> None:
        """`_read_fingerprint(conn)` 이 던지면(예: 지문 조회 자체가 실패) `_open_index()`
        가 그 conn 을 닫고 나서 예외를 다시 던져야 한다.
        """
        testutil.reset_search_module_caches()
        orig = search._read_fingerprint

        def boom(conn):
            raise RuntimeError("M1 테스트 — _read_fingerprint 의도된 예외")

        search._read_fingerprint = boom
        try:
            with self.assertRaises(RuntimeError):
                search._open_index()
        finally:
            search._read_fingerprint = orig

        self._assert_no_leaked_handle()

    @unittest.skipUnless(
        os.name == "nt",
        "win32 전용 오라클 — POSIX 는 열린 파일도 rename 을 막지 않아 이 테스트가 무정보다",
    )
    def test_reload_with_retry_exception_does_not_leak_conn(self) -> None:
        """`_reload_with_retry(...)` 가 던지는 경로(재로드 자체가 실패)도 같은 방식으로
        확인한다. `_snapshot` 을 비워 반드시 재로드 분기를 타게 한다.
        """
        testutil.reset_search_module_caches()
        orig = search._reload_with_retry

        def boom(fingerprint, **kwargs):
            raise RuntimeError("M1 테스트 — _reload_with_retry 의도된 예외")

        search._reload_with_retry = boom
        try:
            with self.assertRaises(RuntimeError):
                search._open_index()
        finally:
            search._reload_with_retry = orig

        self._assert_no_leaked_handle()


class TestM2FingerprintTrustworthyForcesReload(testutil.IsolatedIndexCase):
    """`meta.created_at` 이 결측(신뢰 불가 지문)이면 `_open_index()` 는 매 호출 실제로
    재로드해야 한다 — 수정 전에는 `_reload_with_retry()` 의 더블체크가 "지문이 같으면
    캐시 반환"을 하고 있어, 신뢰 불가 지문이 캐시와 우연히(또는 필연히, 색인이 안
    바뀌었으므로) 같으면 그 캐시를 그대로 돌려줘 "항상 재로드"라는 문서화된 방어가
    무력화됐다.
    """

    CORPUS = {
        "docs/m2/doc_a.md": "# 문서 A\n\n## 섹션\n\nM2TRUSTTERM 내용이다.\n",
    }

    def test_untrustworthy_fingerprint_forces_reload_every_call(self) -> None:
        # 합성 색인의 meta 에서 created_at 행을 삭제 — "신뢰 불가 지문"(created_at 결측)
        # 상태를 인위적으로 만든다. search.py 의 검색 경로는 읽기 전용(mode=ro)이라 이
        # 조작엔 자체 read-write 커넥션이 필요하다.
        conn = sqlite3.connect(str(self.index_dir / "chunks.sqlite"))
        try:
            conn.execute("DELETE FROM meta WHERE key = 'created_at'")
            conn.commit()
        finally:
            conn.close()

        testutil.reset_search_module_caches()

        r1 = search.search("M2TRUSTTERM", category="all", k=5, log=False, source="test")
        self.assertEqual(search._reload_count, 1, "1회차 호출 후 reload_count 가 1이 아니다")
        self.assertGreaterEqual(len(r1.hits), 1, "M2TRUSTTERM 검색 결과가 0건이다(픽스처 이상)")

        r2 = search.search("M2TRUSTTERM", category="all", k=5, log=False, source="test")
        self.assertEqual(
            search._reload_count, 2,
            "2회차 호출 후에도 reload_count 가 안 늘었다(M2 회귀 — 신뢰 불가 지문인데 "
            "_reload_with_retry() 의 더블체크가 캐시를 그대로 재사용했다)",
        )
        self.assertGreaterEqual(len(r2.hits), 1, "2회차 호출 결과가 0건이다 — 결과 자체는 정상이어야 한다")


if __name__ == "__main__":
    unittest.main()
