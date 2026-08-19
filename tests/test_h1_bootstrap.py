"""H1(코드리뷰, 최우선) 회귀 — stale 판정이 sqlite 커넥션을 쥔 채 파일 glob 하던 결함(1)
+ 그 부트스트랩이 검색을 블로킹하던 결함(2).

두 결함은 같은 원인(`_get_staleness()` 가 호출자 커넥션을 쥔 채 동기로
`check_staleness()` 를 돌렸다)에서 나왔다 — 그래서 회귀 테스트도 이 파일 하나에 묶는다.
지시서 §1 "H1 회귀 테스트 (2개, 둘 다 필수)" 대응.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import config
import search

from tests import testutil

_CORPUS = {
    "docs/h1/doc_a.md": "# 문서 A\n\n## 섹션\n\nH1BOOTSTRAPTERM 내용이다.\n",
}


class _H1FixtureCase(unittest.TestCase):
    """인스턴스별 임시 코퍼스 — `config.source_paths` 를 몽키패치하는 시나리오라 클래스
    공유 코퍼스(`testutil.IsolatedIndexCase`)와는 안 맞는다(다른 테스트로 몽키패치가
    새면 안 된다).
    """

    def setUp(self) -> None:
        self._orig_docs_root = config.DOCS_ROOT
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_h1_")
        self.tmp_path = Path(self._tmpdir)
        self.docs_root = self.tmp_path / "docs_root"
        self.index_dir = self.tmp_path / "index"

        testutil.write_corpus(self.docs_root, _CORPUS)
        config.DOCS_ROOT = self.docs_root
        config.INDEX_DIR = self.index_dir
        testutil.reset_search_module_caches()

        exit_code, out = testutil.reindex_in_process()
        if exit_code != 0:
            raise RuntimeError(f"{type(self).__name__} 픽스처 색인 실패(exit={exit_code}):\n{out}")

    def tearDown(self) -> None:
        config.DOCS_ROOT = self._orig_docs_root
        config.INDEX_DIR = self._orig_index_dir
        testutil.reset_search_module_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class TestH1ScanHoldsNoSqliteHandle(_H1FixtureCase):
    """H1(1) — stale 스캔(파일시스템 glob) 중 sqlite 핸들이 0개여야 한다.

    `config.source_paths` 를 몽키패치해 "스캔이 도는 동안" `index/chunks.sqlite` 를
    `os.rename` 으로 옆으로 옮겼다 되돌린다 — 그 시점에 커넥션이 열려 있으면 win32 에서
    `PermissionError` 가 난다(이게 H1 의 실제 실패 모드에 대한 정직한 오라클이다). 이
    예외는 `_compute_staleness_and_fingerprint()` 의 catch-all 이 삼켜
    `StalenessCheck(ok=False, ...)` 로 바꾸므로, "예외가 전파되는지"가 아니라 "check.ok
    가 True 로 남는지"로 판정해야 한다.
    """

    def _rename_probe(self, orig_source_paths):
        sqlite_path = self.index_dir / "chunks.sqlite"
        backup = self.index_dir / "chunks.sqlite.h1_probe_bak"

        def probe():
            os.rename(sqlite_path, backup)  # 커넥션이 열려 있으면(누수) 여기서 PermissionError
            os.rename(backup, sqlite_path)
            return orig_source_paths()

        return probe

    @unittest.skipUnless(
        os.name == "nt",
        "win32 전용 오라클 — POSIX 는 열린 파일도 rename 을 막지 않아 이 테스트가 무정보다",
    )
    def test_compute_staleness_scan_holds_no_handle(self) -> None:
        orig = config.source_paths
        config.source_paths = self._rename_probe(orig)
        try:
            check = search.compute_staleness()
        finally:
            config.source_paths = orig

        self.assertTrue(
            check.ok,
            f"probe 중 rename 이 실패해 판정이 무산됐다(sqlite 핸들 누수 의심): {check.detail}",
        )
        self.assertFalse(check.stale)

    @unittest.skipUnless(
        os.name == "nt",
        "win32 전용 오라클 — POSIX 는 열린 파일도 rename 을 막지 않아 이 테스트가 무정보다",
    )
    def test_refresh_staleness_bg_scan_holds_no_handle(self) -> None:
        orig = config.source_paths
        config.source_paths = self._rename_probe(orig)
        try:
            search._refresh_staleness_bg()  # 스레드 없이 동기 직접 호출(결정적 테스트)
        finally:
            config.source_paths = orig

        snap = search._stale_snapshot
        self.assertIsNotNone(snap, "_refresh_staleness_bg() 가 스냅샷을 발행하지 않았다")
        self.assertTrue(
            snap.check.ok,
            f"probe 중 rename 이 실패해 판정이 무산됐다(sqlite 핸들 누수 의심): {snap.check.detail}",
        )
        self.assertFalse(snap.check.stale)


class TestH1BootstrapNonBlocking(_H1FixtureCase):
    """H1(2) — 프로세스 첫 `search()` 호출이 stale 판정 계산(느릴 수 있음)을 기다리면
    안 된다. `config.source_paths` 를 0.5초 지연시켜 "느린 파일시스템"을 흉내 낸다 —
    수정 전이라면 부트스트랩이 이 호출을 동기로 기다려 전체 지연이 0.5초를 넘었다.
    """

    def test_first_search_does_not_block_on_slow_staleness_scan(self) -> None:
        orig = config.source_paths

        def slow_source_paths():
            time.sleep(0.5)
            return orig()

        config.source_paths = slow_source_paths
        try:
            t0 = time.monotonic()
            r = search.search("H1BOOTSTRAPTERM", category="all", k=5, log=False, source="test")
            elapsed = time.monotonic() - t0
            self.assertLess(
                elapsed, 0.3,
                f"첫 search() 가 {elapsed:.3f}s 걸렸다 — stale 부트스트랩이 여전히 동기 "
                f"블로킹 중일 수 있다(H1 회귀, 수정 전이면 >0.5s)",
            )
            # 첫 호출은 pending(index_stale_ok=False) 이어야 한다 — 이 자체가 "동기 계산을
            # 안 기다렸다"는 증거다(계산을 기다렸다면 이미 True 로 나왔을 것).
            self.assertFalse(
                r.index_stale_ok,
                "pending 이 아니라 이미 판정이 실려 있다 — 이 테스트의 전제(부트스트랩이 "
                "비동기여야 함)가 깨졌을 수 있다",
            )

            # 명시적으로 동기화하면 판정이 실제로 실린다는 것도 같이 확인한다(비블로킹이
            # "영원히 판정 안 됨"으로 변질되지 않았는지).
            search.refresh_staleness_now()
            r2 = search.search("H1BOOTSTRAPTERM", category="all", k=5, log=False, source="test")
            self.assertTrue(r2.index_stale_ok, "refresh_staleness_now() 이후에도 판정이 안 실렸다")
            self.assertFalse(r2.index_stale)
        finally:
            config.source_paths = orig
            # 첫 search() 가 fire-and-forget 로 띄운 백그라운드 스레드가 아직 slow_source_paths
            # 를 돌고 있을 수 있다 — 완전히 끝날 때까지 대기해 다음 테스트로 상태(및 tearDown
            # 이 지우는 tmpdir 를 스레드가 읽는 경합)가 새지 않게 한다.
            deadline = time.monotonic() + 2.0
            while search._stale_revalidating and time.monotonic() < deadline:
                time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
