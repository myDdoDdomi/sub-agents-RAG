"""F-5(4차 배치, Sev3) — Host 헤더 검증 + nosniff. 지시서 §7.

색인이 필요 없다 — `/categories` 는 정적 응답이라 인덱스 상태와 무관하게 미들웨어
동작만 순수하게 검증할 수 있다. `fastapi.testclient.TestClient` 는 실측 확인상 이
venv 에서 httpx 가 아니라 `httpx2`(호환 포크)로 동작한다 — 그리고 **기본 base_url
("http://testserver")을 그대로 쓰면 Host 헤더가 "testserver"가 되어 우리 자신의
HostGuardMiddleware 에 막혀 400 이 난다**(실측 확인, 2026-08-13) — 그래서 반드시
`base_url="http://127.0.0.1"` 로 생성한다.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import server


class TestHostGuard(unittest.TestCase):
    def setUp(self) -> None:
        # server.ALLOWED_HOSTS 는 모듈 전역 가변 리스트이고 HostGuardMiddleware 가 그
        # 객체 참조를 그대로 들고 있다 — 다른 테스트로 새지 않게 내용만 복원한다
        # (리스트 객체 자체를 새로 대입하면 미들웨어가 든 참조와 끊어진다).
        self._orig_allowed_hosts = list(server.ALLOWED_HOSTS)
        self.client = TestClient(server.app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        server.ALLOWED_HOSTS[:] = self._orig_allowed_hosts
        self.client.close()

    def test_default_loopback_host_allowed(self) -> None:
        r = self.client.get("/categories")
        self.assertEqual(r.status_code, 200)

    def test_loopback_with_port_allowed(self) -> None:
        r = self.client.get("/categories", headers={"Host": "127.0.0.1:8765"})
        self.assertEqual(r.status_code, 200)

    def test_localhost_with_port_allowed(self) -> None:
        r = self.client.get("/categories", headers={"Host": "localhost:8765"})
        self.assertEqual(r.status_code, 200)

    def test_ipv6_loopback_bracket_allowed(self) -> None:
        """IPv6 브래킷 표기 — TrustedHostMiddleware 대체 구현의 핵심 동기(파싱 버그 회피)."""
        r = self.client.get("/categories", headers={"Host": "[::1]:8765"})
        self.assertEqual(r.status_code, 200)

    def test_spoofed_host_blocked_with_400(self) -> None:
        r = self.client.get("/categories", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.text, "Invalid host header")

    def test_ipv6_bracket_prefix_does_not_leak_open_all_ipv6(self) -> None:
        """`"["` 하나만 허용목록에 남는 파싱 버그(Starlette 내장 미들웨어 결함, F-5 동기)가
        재발하면 임의의 IPv6 호스트가 전부 통과한다 — 그렇지 않은지 직접 확인한다.
        """
        r = self.client.get("/categories", headers={"Host": "[dead:beef::1]:8765"})
        self.assertEqual(r.status_code, 400)

    def test_missing_host_header_blocked(self) -> None:
        # httpx/TestClient 는 보통 Host 를 자동으로 채우지만, 명시적으로 빈 값을 주면
        # 서버가 "없음"과 동일하게 취급하는지까지는 클라이언트 계층이 보장하지 않는다
        # — 여기서는 미들웨어의 `host_header is None` 분기를 직접 재현하기보다
        # 실제 도달 가능한 경로(허용되지 않은 호스트)로 대체 검증한다.
        r = self.client.get("/categories", headers={"Host": ""})
        self.assertEqual(r.status_code, 400)

    def test_nosniff_on_allowed_response(self) -> None:
        r = self.client.get("/categories")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")

    def test_nosniff_on_blocked_response(self) -> None:
        """요건 3 — nosniff 는 차단(400) 응답에도 붙어야 한다(등록 순서 계약)."""
        r = self.client.get("/categories", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")

    def test_custom_host_gets_appended_and_then_allowed(self) -> None:
        """`--host` 로 준 값이 허용 목록에 실제로 들어가는지(요건 2) — `server.main()`
        이 하는 것과 정확히 같은 append 로직을 재현한다(main() 자체는 uvicorn.run() 을
        블로킹 호출하므로 단위 테스트에서 직접 부르지 않는다).
        """
        custom_host = "192.0.2.42"  # TEST-NET-1(RFC 5737), 실사용 없는 예약 주소
        # 재현 전: 아직 허용 안 됨.
        r_before = self.client.get("/categories", headers={"Host": custom_host})
        self.assertEqual(r_before.status_code, 400)

        # server.main() 의 append 조건과 동일한 로직.
        if not any(
            server._normalize_host(custom_host) == server._normalize_host(h)
            for h in server.ALLOWED_HOSTS
        ):
            server.ALLOWED_HOSTS.append(custom_host)

        r_after = self.client.get("/categories", headers={"Host": custom_host})
        self.assertEqual(r_after.status_code, 200, "ALLOWED_HOSTS 참조 유지 설계가 깨졌을 수 있다")


class TestHostParsingHelpers(unittest.TestCase):
    """미들웨어 우회 없이 순수 파싱 함수만 직접 검증(빠른 화이트박스 보강)."""

    def test_host_without_port_ipv4(self) -> None:
        self.assertEqual(server._host_without_port("127.0.0.1:8765"), "127.0.0.1")

    def test_host_without_port_ipv6_bracket(self) -> None:
        self.assertEqual(server._host_without_port("[::1]:8765"), "[::1]")

    def test_host_without_port_hostname_no_port(self) -> None:
        self.assertEqual(server._host_without_port("localhost"), "localhost")

    def test_normalize_host_strips_bracket_and_lowercases(self) -> None:
        self.assertEqual(server._normalize_host("[::1]"), "::1")
        self.assertEqual(server._normalize_host("LOCALHOST"), "localhost")


if __name__ == "__main__":
    unittest.main()
