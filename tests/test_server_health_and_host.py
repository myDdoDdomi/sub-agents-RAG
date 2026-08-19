"""M4(코드리뷰) — `/health` 가 0바이트 유령 DB 를 생성할 수 있던 결함.
M5(코드리뷰) — `--host 0.0.0.0`(와일드카드) 로 기동하면 모든 요청이 400 이 되는 결함
(기동 시점에 거절하도록 수정).

지시서 §5·§6 대응.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import config
import server

from tests import testutil


class TestHealthNoGhostDb(unittest.TestCase):
    """M4 — `_index_files()` 가 존재를 확인한 뒤와 실제 connect 사이의 좁은 경합 창을
    흉내 낸다: `chunks.sqlite` 는 없지만 `index/` 디렉터리는 있는 상태에서 `/health` 를
    호출해도 그 경로에 파일이 새로 생기면 안 된다.
    """

    def setUp(self) -> None:
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_m4_")
        self.index_dir = Path(self._tmpdir) / "index"
        self.index_dir.mkdir(parents=True, exist_ok=True)  # 디렉터리는 존재 — sqlite 파일만 없음
        config.INDEX_DIR = self.index_dir
        testutil.reset_search_module_caches()
        self.client = TestClient(server.app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        self.client.close()
        config.INDEX_DIR = self._orig_index_dir
        testutil.reset_search_module_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_health_does_not_create_ghost_db_file(self) -> None:
        target = self.index_dir / "chunks.sqlite"
        self.assertFalse(target.exists())  # 사전조건

        # server._index_files() 를 (True, <존재하지 않는 경로>) 로 몽키패치해 "존재 확인은
        # 통과했지만 실제로는 파일이 없는" 경합 상태를 재현한다.
        with mock.patch.object(server, "_index_files", return_value=(True, target)):
            r = self.client.get("/health")

        self.assertEqual(r.status_code, 200, "헬스체크는 500 을 내면 안 된다(계약)")
        body = r.json()
        self.assertEqual(body["status"], "error")
        self.assertFalse(
            target.exists(),
            "M4 회귀 — /health 호출로 0바이트 유령 DB 가 생겼다(mode=ro 가 아니었을 가능성)",
        )


class TestValidateBindHost(unittest.TestCase):
    """M5 — `_validate_bind_host()` 순수 함수 단위 테스트."""

    def test_rejects_wildcard_hosts(self) -> None:
        for host in ["0.0.0.0", "::", "[::]", "*", ""]:
            with self.subTest(host=host):
                msg = server._validate_bind_host(host)
                self.assertIsNotNone(msg, f"{host!r} 가 와일드카드인데 거절되지 않았다")

    def test_allows_specific_addresses(self) -> None:
        for host in ["127.0.0.1", "localhost", "::1", "192.168.0.5"]:
            with self.subTest(host=host):
                msg = server._validate_bind_host(host)
                self.assertIsNone(msg, f"{host!r} 는 구체 주소인데 거절됐다({msg})")

    def test_rejects_ipv6_wildcard_equivalents(self) -> None:
        """2-2(qa 독립검증, 2026-08-14) — 리터럴 문자열 집합 `{"0.0.0.0", "::", "[::]",
        "*", ""}` 만 보던 수정 전 구현은 IPv6 동치 표기를 전부 통과시켰다(qa 실측:
        `--host ::0` 기동 시 `netstat` 상 `TCP [::]:8791 LISTENING`). 압축형·전개형·
        완전표기·IPv4-매핑·브래킷·대소문자·공백 변형을 전부 거절해야 한다.
        """
        for host in [
            "::0",  # qa 원 재현 표기
            "0:0:0:0:0:0:0:0",  # 완전 전개 표기
            "0000:0000:0000:0000:0000:0000:0000:0000",  # 0-패딩 전개 표기
            "::ffff:0.0.0.0",  # IPv4-매핑 IPv6 와일드카드
            "::ffff:0:0",  # IPv4-매핑 IPv6 와일드카드(16진 표기)
            "::FFFF:0:0",  # 위와 동일 주소의 대문자 표기
            "[::0]",  # 브래킷 표기
            "  ::0  ",  # 앞뒤 공백
            "\t::\t",  # 탭 공백
        ]:
            with self.subTest(host=host):
                msg = server._validate_bind_host(host)
                self.assertIsNotNone(
                    msg, f"{host!r} 는 IPv6 와일드카드 동치 표기인데 거절되지 않았다(2-2 회귀)"
                )

    def test_rejects_abbreviated_ipv4_wildcard_forms(self) -> None:
        """2-2 — `ipaddress` 표준 파서는 거부하지만 BSD 레거시 `inet_aton` 파서(OS 소켓
        경로가 실제로 받아들일 수 있는 축약형)는 전부 `0.0.0.0` 으로 해석하는 표기.
        실측(2026-08-14): `socket.inet_aton()` 이 이 다섯 표기를 모두
        `b"\\x00\\x00\\x00\\x00"` 로 해석한다.
        """
        for host in ["0", "0x0", "00000000", "0.0", "0.0.0"]:
            with self.subTest(host=host):
                msg = server._validate_bind_host(host)
                self.assertIsNotNone(
                    msg, f"{host!r} 는 inet_aton 기준 0.0.0.0 동치인데 거절되지 않았다(2-2 회귀)"
                )

    def test_does_not_reject_non_wildcard_ipv6(self) -> None:
        """회귀 방지 — 와일드카드가 아닌 구체 IPv6 주소는 여전히 통과해야 한다."""
        for host in ["2001:db8::1", "fe80::1"]:
            with self.subTest(host=host):
                msg = server._validate_bind_host(host)
                self.assertIsNone(msg, f"{host!r} 는 구체 IPv6 주소인데 거절됐다({msg})")


class TestMainRejectsWildcardHost(unittest.TestCase):
    """M5 — `server.main()` 이 실제로 `sys.exit(2)` 하고 uvicorn 을 기동하지 않는지."""

    def setUp(self) -> None:
        self._orig_argv = sys.argv

    def tearDown(self) -> None:
        sys.argv = self._orig_argv

    def test_main_exits_2_on_wildcard_host_and_never_starts_uvicorn(self) -> None:
        sys.argv = ["server.py", "--host", "0.0.0.0"]
        with mock.patch("uvicorn.run") as mock_run:
            with self.assertRaises(SystemExit) as cm:
                server.main()
            self.assertEqual(cm.exception.code, 2)
            mock_run.assert_not_called()


class TestHealthDetectsCorruptBm25(unittest.TestCase):
    """2-3(qa 독립검증, 2026-08-14) — `/health` 가 sqlite 만 보고 bm25 아티팩트의
    로드 가능성은 확인하지 않아, bm25 가 깨져도 `status:"ok"` 를 내던 결함(위양성).

    잡는 변이: `health()` 가 `search_mod._open_index()`(또는 동등한 bm25 로드 확인)를
    빼고 다시 sqlite 전용 조회로 되돌아가는 것 — 이 테스트는 그 상태에서 반드시 실패한다
    (수정 전 코드로 실측 확인: `$SC\\d3_health_vuln_demo.py`, `/health`=ok · `/search`=error).

    격리 임시 코퍼스를 쓴다(testutil.IsolatedIndexCase 패턴과 동일 원칙 — 실 index/ 는
    전혀 건드리지 않는다). 손상 주입이 테스트마다 다르므로 인스턴스 setUp/tearDown 을
    쓴다(test_stale_and_check.py 와 동일한 이유).
    """

    CORPUS = {
        "docs/health_test/doc_a.md": "# 문서 A\n\n## 섹션\n\nHEALTHCHECKTERM 이 핵심 표식이다.\n",
        "docs/health_test/doc_b.md": "# 문서 B\n\n## 섹션\n\n무관한 내용의 문서다.\n",
    }

    def setUp(self) -> None:
        self._orig_docs_root = config.DOCS_ROOT
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_health23_")
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

        self.client = TestClient(server.app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        self.client.close()
        config.DOCS_ROOT = self._orig_docs_root
        config.INDEX_DIR = self._orig_index_dir
        testutil.reset_search_module_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_health_ok_before_corruption(self) -> None:
        """사전조건 — 정상 색인이면 status=ok 여야 한다(대조군)."""
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_health_reports_error_when_bm25_canary_corrupt(self) -> None:
        """bm25 canary(`params.index.json`)가 깨지면 `/health` 가 `status="error"` 를
        내야 한다 — 수정 전에는 sqlite 만 봐서 `status="ok"` 를 냈다(qa 실측).
        """
        canary = self.index_dir / "bm25" / "params.index.json"
        self.assertTrue(canary.exists(), "사전조건 — canary 파일이 있어야 한다")
        canary.write_text("{ 깨진 JSON", encoding="utf-8")
        testutil.reset_search_module_caches()

        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200, "헬스체크는 500 을 내면 안 된다(계약)")
        body = r.json()
        self.assertEqual(
            body["status"], "error",
            f"bm25 canary 가 손상됐는데 /health 가 여전히 ok 를 보고했다(2-3 회귀) — body={body}",
        )

        # /search 도 같은 손상을 실제로 겪는지 대조(위양성이 아님을 재확인).
        s = self.client.get("/search", params={"q": "HEALTHCHECKTERM", "k": 3}).json()
        self.assertIn("error", s)

    def test_health_warm_call_does_not_reload_bm25_again(self) -> None:
        """비용 설계 검증 — 지문이 안 바뀌면 두 번째 `/health` 호출은 bm25 를 다시
        로드하지 않는다(캐시 히트, `search.cache_status()["reload_count"]` 불변).
        """
        import search as search_mod

        r1 = self.client.get("/health")
        self.assertEqual(r1.json()["status"], "ok")
        reload_after_first = search_mod.cache_status()["reload_count"]
        self.assertGreaterEqual(reload_after_first, 1, "첫 호출은 최초 로드를 해야 한다")

        r2 = self.client.get("/health")
        self.assertEqual(r2.json()["status"], "ok")
        reload_after_second = search_mod.cache_status()["reload_count"]
        self.assertEqual(
            reload_after_first, reload_after_second,
            "웜 /health 호출이 불필요하게 bm25 를 재로드했다(비용 설계 위반)",
        )


if __name__ == "__main__":
    unittest.main()
