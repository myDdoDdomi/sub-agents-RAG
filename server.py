"""docs-rag-mcp 로컬 HTTP 서버 — FastAPI, 오프라인 자가완결 검색 UI + JSON API.

바인딩 기본값은 127.0.0.1(루프백)이다 — 이 코퍼스는 내부 기획·설계 문서라 바깥에
노출하지 않는다. --host 로 사용자가 명시적으로 다른 값을 줄 수는 있지만 **기본값은
절대 바꾸지 않는다**(비루프백 바인딩 시 stderr 에 경고 한 줄을 낸다).

이 서버에는 LLM 호출 엔드포인트가 없다 — 이 레포엔 LLM 이 없다(BM25 검색만 한다).

XSS 주의(3차 배치 위임 브리프 명시 사항): 코퍼스가 마크다운이라 문서 본문에
`<script>`·`<`·`&` 가 원문 그대로 들어 있는 조각이 실재한다(코드 예시 등). "/"
HTML 에 넣는 모든 동적 값(질의어 포함)은 반드시 `html.escape(..., quote=True)` 로
이스케이프한다 — 헬퍼 `_esc()` 하나로 강제해 실수로 빠뜨리지 않게 한다.

입력 방어: q/category/k 는 전부 문자열로만 선언한다(FastAPI 가 `int` 타입 애노테이션을
쓰면 Query 파싱 실패 시 우리 핸들러에 도달하기 전에 자동 422 를 던진다 — 그건 "500 이
아니다"는 만족하지만 "config.MAX_K 까지 클램프"라는 이 서버의 요구사항과는 안 맞는다.
그래서 q/category/k 모두 str 로 받고 이 파일 안에서 직접 파싱·클램프한다).
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

import config
import search as search_mod

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

app = FastAPI(
    title="docs-rag-mcp",
    description="팀 문서 로컬 검색(오프라인, LLM 호출 없음)",
    # ①(code-reviewer, 2026-08-13): FastAPI 기본 /docs(Swagger UI)·/redoc 페이지는
    # cdn.jsdelivr.net(swagger-ui-dist/redoc)·fonts.googleapis.com 을 부른다 — 이
    # 레포는 "문서가 밖으로 나가지 않는" 오프라인 도구다(모듈 docstring 7행,
    # DECISIONS.md R-001과의 계약) — 그래서 셋 다 끈다.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ── F-5(4차 배치, Sev3) — Host 헤더 검증 + nosniff ───────────────────────────
# `Host: evil.example.com` 으로 요청해도 200 + 문서 본문을 그대로 반환하던 문제(바인딩이
# 127.0.0.1 단독이고 CORS 도 없지만, DNS 리바인딩은 공격자 오리진을 127.0.0.1 로 만들어
# SOP 를 우회한다 — 서버가 떠 있는 동안 사용자가 악성 페이지를 열면 `/search` 로 내부
# 기획·법무·사업 문서를 읽어갈 수 있다). DECISIONS.md R-001 이 BM25 를 고른 운영 근거
# ("문서가 외부로 나가지 않는다")와 정면 충돌하는 결함이라 반드시 막는다.
#
# Starlette 내장 `TrustedHostMiddleware` 대신 자체 구현을 쓴다 — 그 미들웨어는 포트를
# `headers.get("host","").split(":")[0]` 로 떼는데, IPv6 브래킷 표기 `[::1]:8765` 는
# 콜론이 여러 개라 `"["` 하나만 남는다(dev-lead 실측). 허용목록에 `"["` 를 넣는 우회는
# 모든 IPv6 호스트를 통과시켜버리므로 금지 — 그래서 브래킷을 올바르게 파싱하는 동등
# 미들웨어를 직접 쓴다(요건 C-4).
def _host_without_port(host_header: str) -> str:
    """Host 헤더 원문에서 포트를 뗀다(브래킷 표기는 유지 — 정규화는 `_normalize_host` 가
    별도로 한다). 브래킷이 있으면(`[::1]:8765`) `]` 까지를 통째로 호스트로 보고, 없으면
    (IPv4·호스트명은 콜론이 있어도 하나뿐이라 안전) 첫 ':' 앞까지만 취한다.
    """
    h = host_header.strip()
    if h.startswith("["):
        end = h.find("]")
        return h[: end + 1] if end != -1 else h  # 닫는 브래킷이 없는 기형 입력은 그대로 반환(허용목록 미스로 자연 차단)
    return h.split(":", 1)[0]


def _normalize_host(h: str) -> str:
    """비교용 정규화 — 대소문자 무시 + IPv6 브래킷 제거. CLI `--host ::1`(브래킷 없음)과
    HTTP `Host: [::1]:8765`(브래킷 있음)가 같은 주소를 가리키므로 비교 전 통일한다.
    """
    h = h.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h.lower()


# M5(코드리뷰) — `--host 0.0.0.0`(와일드카드) 로 바인딩하면 이 서버는 뜨지만
# HostGuardMiddleware 가 모든 요청의 Host 헤더를 거절해(허용 목록에 미리 넣어줄 수 있는
# "그 주소"가 없다 — 클라이언트마다 Host 헤더가 다르다) 전 요청이 400 이 된다. "서버는
# 떴는데 아무것도 안 되는" 최악의 진단성이라 기동 시점에 거절한다(리드 판정).
#
# 2-2(qa 독립검증, 2026-08-14) — 위 원래 구현은 `{"0.0.0.0", "::", "[::]", "*", ""}` 리터럴
# 문자열 집합이었다. IPv6 는 같은 주소를 여러 표기로 쓸 수 있어(`::0`·`0:0:0:0:0:0:0:0`·
# 전개형 `0000:...:0000`·IPv4-매핑 `::ffff:0.0.0.0` 등) 리터럴 집합은 전부 우회됐다 —
# 실측(qa): `--host ::0` 기동 시 `netstat` 상 `TCP [::]:8791 LISTENING`(IPv6 와일드카드
# 바인딩 성립). 아래 `_is_wildcard_host()` 로 교체한다.
_WILDCARD_LITERALS = {"", "*"}  # ipaddress 로 판정할 수 없는 두 표기(빈 문자열·별표)


def _is_wildcard_host(host: str) -> bool:
    """2-2(qa 독립검증) — `host`(정규화 전 원문)가 와일드카드(모든 인터페이스) 바인딩과
    동치인 표기인지 표준 라이브러리만으로 판정한다. 표준 `ipaddress` 로 안 되는 값까지
    잡으려고 두 단계로 본다:

    1) `_normalize_host()`(공백 제거 + IPv6 브래킷 제거 + 소문자화, 기존 함수 재사용)
       후 `""`·`"*"` 는 여기서 직접 처리한다 — 유효한 IP 표기가 아니라 `ipaddress` 가
       못 다룬다.
    2) `ipaddress.ip_address(h).is_unspecified` — RFC 준수 IPv4/IPv6 리터럴을 전부
       잡는다. 압축형(`::`·`::0`)·전개형(`0000:0000:...:0000`)·완전표기(`0:0:0:0:0:0:0:0`)·
       IPv4-매핑 IPv6(`::ffff:0.0.0.0`·`::ffff:0:0`) 모두 `ipaddress` 가 정규화해서
       비교하므로 리터럴 집합보다 훨씬 넓게 잡힌다(대소문자·공백은 `_normalize_host`
       에서 이미 처리됨 — `ipaddress` 자체도 대소문자를 가리지 않는다).
    3) `ipaddress` 가 거부하는 **비표준 축약 IPv4**(`0`·`0x0`·`00000000`·`0.0`·`0.0.0`
       등, BSD 레거시 `inet_aton` 파서가 받아들이는 표기)는 `socket.inet_aton()` 으로
       재확인한다. 실측(2026-08-14, win32, 이 `.venv` 의 Python): 이 표기들로 실제
       `socket.bind()` 를 걸면 이 플랫폼에서는 `getaddrinfo` 단계가 먼저 실패해(엄격한
       `inet_pton`/`getaddrinfo` 파서만 쓰기 때문) 실제 바인딩까지는 못 간다 — 그래도
       `inet_aton`(느슨한 레거시 파서, 플랫폼에 따라 실제 바인딩 경로에서 쓰일 수 있다)
       은 이 표기들을 전부 `0.0.0.0` 으로 해석하므로 방어적으로 막는다(다른 플랫폼·다른
       바인딩 경로에서 실제로 와일드카드가 될 수 있는 표기를 조용히 통과시키지 않는다).

    구체 주소(특정 사설 IP 등)·호스트명(`localhost` 포함)은 여기 안 걸린다 — IP 로
    파싱되지 않고 `inet_aton` 도 `0.0.0.0` 이 아닌 값으로 해석하거나 애초에 거부하므로
    `False` 를 반환한다(예: `localhost` 는 `inet_aton` 이 `OSError` 를 던진다).
    """
    h = _normalize_host(host)
    if h in _WILDCARD_LITERALS:
        return True
    try:
        return ipaddress.ip_address(h).is_unspecified
    except ValueError:
        pass
    try:
        return socket.inet_aton(h) == b"\x00\x00\x00\x00"
    except OSError:
        return False


def _validate_bind_host(host: str) -> str | None:
    """와일드카드 바인딩 요청을 거절한다(M5, 2-2 에서 판정 로직을 `_is_wildcard_host()`
    로 강화). 통과하면 `None`, 거절하면 에러 메시지 문자열을 반환한다 — I/O 없는 순수
    함수로 분리해 `sys.exit` 없이도 단위 테스트할 수 있게 한다(`main()` 이 메시지가
    있으면 stderr 에 쓰고 `sys.exit(2)` 한다).

    판정 대상은 `_is_wildcard_host()` 참고(리터럴 `0.0.0.0`·`::`·`[::]`·`*`·빈 문자열
    뿐 아니라 IPv6 동치 표기·비표준 축약 IPv4까지 포함). **구체 주소(특정 사설 IP 등)는
    여기 안 걸린다** — `main()` 의 기존 비루프백 경고(ALLOWED_HOSTS append 직전)가 그
    경우를 맡는다. 와일드카드만 원리적으로 못 고친다: 특정 사설 IP 는 `main()` 이 이미
    `ALLOWED_HOSTS` 에 append 해서 정상 동작하지만, Host 헤더는 클라이언트마다 달라
    와일드카드 바인딩 자체에서 허용목록을 파생시킬 수 없다.
    """
    if _is_wildcard_host(host):
        return (
            f"[server] 거부 — --host {host!r} 는 와일드카드 바인딩(동치 표기 포함)이라 "
            f"지원하지 않는다. 이 서버는 로컬 전용 계약이라 루프백 바인딩만 지원한다. "
            f"원격 접근이 필요하면 SSH 터널이나 리버스 프록시를 쓰라. 특정 주소로 "
            f"바인딩하려면 그 주소를 --host 에 직접 지정하라(예: --host 192.168.0.5)."
        )
    return None


class HostGuardMiddleware(BaseHTTPMiddleware):
    """`TrustedHostMiddleware` 동등물 — IPv6 브래킷을 올바르게 다루는 자체 구현(F-5).

    허용 목록은 **참조를 그대로 보관**한다(리스트를 복사하지 않는다) — `main()` 이
    `--host` 파싱 후 이 리스트에 값을 append 해도(요건 C-2), 그 시점이 미들웨어 인스턴스
    생성 이후라도 항상 최신 내용을 본다(딕셔너리/리스트 복사본을 들고 있으면 그 시점
    스냅샷에 갇힌다 — dev-lead 가 실측한 "add_middleware 후 append 해도 반영된다"는
    Starlette 내장 미들웨어의 지연 인스턴스화 타이밍에 의존하는 동작인데, 참조를 직접
    들고 있으면 타이밍에 의존할 필요조차 없어진다).
    """

    def __init__(self, app, allowed_hosts: list[str]) -> None:
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host")
        if host_header is None:
            return PlainTextResponse("Invalid host header", status_code=400)
        host = _normalize_host(_host_without_port(host_header))
        if not any(host == _normalize_host(a) for a in self.allowed_hosts):
            # dev-lead 실측: 위조 Host 는 403 이 아니라 400 "Invalid host header" —
            # Starlette 내장 TrustedHostMiddleware 와 같은 문구·상태코드를 그대로 맞춘다.
            return PlainTextResponse("Invalid host header", status_code=400)
        return await call_next(request)


class NoSniffMiddleware(BaseHTTPMiddleware):
    """모든 응답에 `X-Content-Type-Options: nosniff` 를 붙인다(F-5 요건 3 — HostGuard 의
    400 차단 응답 포함). Starlette 미들웨어는 **나중에 add_middleware 한 쪽이 바깥쪽**을
    감싼다(`Starlette.add_middleware`가 `user_middleware.insert(0, ...)`, 빌드 시
    `reversed()` 로 안쪽부터 감싸는 실제 소스 확인, 2026-08-13) — 그래서 이 미들웨어를
    `HostGuardMiddleware` **다음에** 등록해야 HostGuard 의 400 응답까지 감싸 헤더를 붙일
    수 있다(아래 등록 순서 참고, 실 요청으로 재확인함).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


# 기본 허용 Host. `server.main()` 은 이미 `"::1"` 을 `"127.0.0.1"`/`"localhost"` 와 동급
# 루프백으로 취급해 비루프백 경고를 안 낸다(아래 main() 의 host 검사 참고) — 그 취급을
# Host 헤더 검증에도 그대로 반영해 `[::1]:8765` 가 파싱 버그로만이 아니라 정책으로도
# 정당하게 통과하게 한다(요건 C-4 "불일치를 반드시 해소하라").
ALLOWED_HOSTS: list[str] = ["127.0.0.1", "localhost", "::1"]

# 등록 순서 중요 — HostGuard 를 먼저(안쪽) 등록해야 NoSniff 가 나중(바깥쪽)에 등록되어
# HostGuard 의 400 응답도 감싼다(위 NoSniffMiddleware docstring 참고).
app.add_middleware(HostGuardMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(NoSniffMiddleware)


def _esc(s: object) -> str:
    """HTML 에 넣는 모든 동적 값은 이 함수를 거친다(속성값 컨텍스트 포함 quote=True)."""
    return html.escape(str(s), quote=True)


def _index_files() -> tuple[bool, Path]:
    """O5(2차 배치) — `search_mod.index_files_status()` 로 위임한다. 이전엔 이 함수가
    자체적으로 bm25 디렉터리·canary(`params.index.json`)·sqlite 존재를 확인했는데,
    `mcp_server._index_missing_message()` 는 canary 를 안 봐서 부분 스왑 상태를 두
    표면이 다르게 판단했다 — 이제 `search.py` 의 단일 진실 공급원 하나를 공유한다
    (canary 근거: `bm25s.BM25.save()` 가 data/indices/indptr → vocab → params 순으로
    저장하므로 params.index.json 이 부분 저장·부분 rename 스왑이든 가장 먼저 빠지는
    대표 파일이다 — 실측 근거는 `search.index_files_status()` 참고).
    """
    return search_mod.index_files_status()


def _clamp_k(raw: str | None) -> int:
    """어떤 문자열(빈 값·None·비정수·범위 밖)이 와도 안전한 정수로 만든다.

    search.search() 의 k<=0 처리(조용히 1건으로 깎지 않고 DEFAULT_K 로 보정)와
    같은 태도 — 파싱 실패·비양수는 DEFAULT_K, 그 외엔 1~config.MAX_K 로 클램프.
    """
    if raw is None or raw == "":
        return config.DEFAULT_K
    try:
        k_int = int(raw)
    except (TypeError, ValueError):
        return config.DEFAULT_K
    if k_int <= 0:
        return config.DEFAULT_K
    return max(1, min(config.MAX_K, k_int))


def _hit_to_dict(h: search_mod.Hit, raw_score: float) -> dict[str, Any]:
    # ④(code-reviewer, 2026-08-13): search._hit_body_text() 를 그대로 재사용한다 —
    # format_result(텍스트/MCP 경로)는 이미 이 함수로 조각 머리의 '[rel] heading_path'
    # 프리픽스를 벗기고 config.MAX_CHARS_PER_HIT 로 컷하는데, 여기서 h.text 를 그대로
    # 쓰면 HTML/JSON 만 프리픽스가 남아 ⓐ <pre> 첫 줄이 바로 위 헤더와 중복 표시되고
    # ⓑ 예산 판정(plan_response_budget, _hit_body_text 기준)과 실제 담기는 text 가
    # 어긋나며 ⓒ 2,500자 컷 경계가 텍스트 경로보다 더 일찍 걸린다(리뷰 실측: 코퍼스
    # 2,371조각 전건이 프리픽스를 가짐 — 프리픽스 포함 443건 vs 제거 후 370건이 컷
    # 대상, 텍스트 경로보다 73건 더 잘림). 언더스코어 함수지만 search.py 의 공개
    # 계약(Hit/SearchResult/search/format_result) 밖이라 그대로 가져다 쓴다 —
    # search.py 자체는 건드리지 않는다(dev-lead 지시).
    text, truncated = search_mod._hit_body_text(h)
    return {
        "rel": h.rel,
        "heading_path": h.heading_path,
        "start_line": h.start_line,
        "end_line": h.end_line,
        "category": h.category,
        "status": h.status,
        "score": h.score,
        "raw_score": raw_score,
        "text": text,
        "truncated": truncated,
        # ⑥(code-reviewer, 2026-08-13): 이 키가 생략 항목에만 있고 포함 항목엔 없으면
        # JSON 소비자 계약이 비대칭이다 — 기본 False 로 항상 포함한다(_run_search 의
        # 생략 분기가 True 로 덮어쓴다).
        "body_omitted": False,
    }


def _run_search(q: str, category: str, k_raw: str | None, caller_id: str | None = None) -> dict[str, Any]:
    """/search·/(HTML) 공용 검색 실행 — 응답 총량 예산(search.RESPONSE_CHAR_BUDGET)을
    hits 리스트에도 적용해 JSON 응답이 무제한으로 커지지 않게 한다(위임 브리프
    "server.py JSON 응답에도 같은 예산을 적용" 요구사항).

    3차 배치 결함② 수정: 어느 hit 의 본문을 생략할지는 search.plan_response_budget()
    로 판정하고 시작값은 search.response_prelude() 로 만든다 — search.format_result()
    (텍스트/MCP 경로)와 완전히 같은 두 함수라 같은 질의·k 에서 생략 건수·시작값이
    더 이상 갈리지 않는다(이전엔 이 함수가 항목 크기를 "len(text)+200 고정 오버헤드"로,
    시작값을 300 고정으로 따로 근사해 텍스트보다 더 많이 생략하는 경우가 있었다 —
    실측 "정합감사 리포트" k=20: 항목 크기만 고쳤을 때도 시작값 300 vs 텍스트 실제
    17자의 차이(약 283자)가 경계 항목 하나를 갈랐다, search.plan_response_budget()
    docstring 참고).
    """
    k = _clamp_k(k_raw)

    if not q.strip():
        # ⑥(code-reviewer, 2026-08-13): 빈 q 라도 category 유효성은 판정한다 — 이전엔
        # category_valid: True 를 무조건 반환해 `?q=&category=존재하지않음` 이 유효하다고
        # 응답했다. search.py L7 과 같은 정규화(공백 제거·소문자화)로 판정을 맞춘다.
        cat_norm = category.strip().lower() if isinstance(category, str) else category
        category_valid = cat_norm == "all" or cat_norm in config.CATEGORY_DESC
        return {
            "query": q, "category_requested": category, "category_used": "all",
            "category_valid": category_valid, "fallback_used": False, "raw_max": 0.0,
            "confidence": "none", "n_missing": 0, "fetch": 0, "hits": [],
            # 4차 배치 B — 빈 q 는 색인을 전혀 건드리지 않는 조기 반환이라(n_missing/fetch
            # 도 같은 이유로 0 고정) stale 판정도 하지 않은 기본값을 낸다.
            # M5(2차 배치) — 키 집합을 정상 응답과 맞춘다: stale_check_ok(O1, 신규 필드)
            # 도 여기 추가한다. True 인 이유는 SearchResult.index_stale_ok 의 기본값과
            # 같다 — "판정을 아예 안 한 것"과 "판정했는데 실패한 것"을 구분해, 후자만
            # False 로 표시하기 위함이다.
            # 2-4(qa 독립검증, 2026-08-14) — stale_check_pending/stale_detail(신규 필드)도
            # 같은 이유로 키 집합을 맞춘다(server.py 가 이미 M5 로 세운 규약 그대로 확장).
            # pending=False 인 이유는 SearchResult.index_stale_pending 기본값과 같다 —
            # 판정을 아예 시도조차 안 한 조기 반환이라 "보류 중"이라는 신호를 낼 이유가
            # 없다(보류는 "계산이 이미 걸려 있다"는 뜻인데 여기선 계산 자체를 안 건다).
            "stale_index": False, "stale_docs_count": 0, "stale_check_ok": True,
            "stale_check_pending": False, "stale_detail": "",
            # 2-1(qa 독립검증, 2026-08-14, 신규 필드) — SearchResult.coverage 등의 기본값과
            # 맞춘다(빈 q 는 색인을 안 여는 조기 반환이라 전부 0/빈 문자열).
            # L2(qa 독립검증 후속, 2026-08-14) — deficient_axis 는 리터럴 "" 대신 헬퍼를
            # 호출한다 — raw_max=0.0·coverage=0.0 상태에 대해 정상 검색 경로(response_prelude
            # 등)가 계산할 값과 다른 값("")을 하드코딩하면 표면 간 값이 어긋난다(라벨
            # 드리프트). `_deficient_axis(0.0, 0.0)` == "raw_max<=0(무매칭)".
            "coverage": 0.0, "n_query_tokens": 0, "n_matched_tokens": 0,
            "deficient_axis": search_mod._deficient_axis(0.0, 0.0),
            "budget": {
                "limit": search_mod.RESPONSE_CHAR_BUDGET, "used": 0,
                "omitted_count": 0, "omitted_files": [],
            },
        }

    ok, _ = _index_files()
    if not ok:
        return {
            "query": q, "error": "색인이 없다. python indexer.py 를 먼저 실행하라.",
            "hits": [],
        }

    try:
        # 2-6(qa 독립검증, 2026-08-14) — caller_id 는 선택적 상관 ID(기본값 None,
        # 하위호환). search_endpoint() 가 쿼리 파라미터로 받아 그대로 흘린다.
        r = search_mod.search(q, category=category, k=k, source="http", caller_id=caller_id)
    except Exception as e:
        # ⑤(code-reviewer, 2026-08-13): 여기서 나는 예외는 bm25s.BM25.load() 등 "색인을
        # 실제로 여는" 단계의 실패다(_index_files() 는 존재만 확인하지 무결성은 보장
        # 못 한다 — 부분 스왑·손상 파일이면 여기서 처음 터진다). /health 가 sqlite 손상을
        # 200+status="error" 로 내리는 것과 정책을 맞춘다: 색인 부재도 200, 색인 손상도
        # 200 — 둘 다 진단 가능한 형태로 낸다. 이 함수 나머지(이미 로드된 SearchResult
        # 만 다루는 예산 계산 등)에서 나는 예외는 이 try 밖이라 search_endpoint 의 바깥
        # except 로 올라가 여전히 500 이다(색인과 무관한 버그는 계속 500 로 드러나야 한다).
        return {
            "query": q,
            "error": f"색인 손상 또는 로드 실패: {type(e).__name__}: {e}",
            "hits": [],
        }

    budget_limit = search_mod.RESPONSE_CHAR_BUDGET
    # search.response_prelude() 로 텍스트 경로(format_result)와 정확히 같은 시작값을
    # 만든다(검색어·안내/경고 줄들을 실제로 만들어 그 길이를 잰다 — 300 같은 고정
    # 근사가 아니다).
    prelude_lines = search_mod.response_prelude(r, q)
    prelude_chars = sum(len(line) + 1 for line in prelude_lines)
    items = search_mod.plan_response_budget(r, max_total_chars=budget_limit, prelude_chars=prelude_chars)

    hits_out: list[dict[str, Any]] = []
    running = prelude_chars
    omitted_files: list[str] = []
    for i, h in enumerate(r.hits):
        raw_score = r.raw_scores[i] if i < len(r.raw_scores) else 0.0
        d = _hit_to_dict(h, raw_score)
        item = items[i]
        if item.include:
            hits_out.append(d)
        else:
            d["text"] = None
            d["body_omitted"] = True
            hits_out.append(d)
            omitted_files.append(item.rel)
        running += item.chars_added

    uniq_omitted = list(dict.fromkeys(omitted_files))

    return {
        "query": q,
        "category_requested": r.category_requested,
        "category_used": r.category_used,
        "category_valid": r.category_valid,
        "fallback_used": r.fallback_used,
        "raw_max": r.raw_max,
        "confidence": r.confidence,
        # 2-1(qa 독립검증, 2026-08-14, 신규 필드 4종) — confidence 판정에 실제로 쓰인
        # 신호를 HTTP JSON 표면에도 노출한다(SearchResult/질의 로그와 같은 값 — 단일
        # 계산을 재사용). deficient_axis 는 confidence="ok" 면 빈 문자열이다.
        "coverage": r.coverage,
        "n_query_tokens": r.n_query_tokens,
        "n_matched_tokens": r.n_matched_tokens,
        "deficient_axis": search_mod._deficient_axis(r.raw_max, r.coverage),
        "n_missing": r.n_missing,
        "fetch": r.fetch,
        # 4차 배치 B(원본 stale 감지) — MCP 텍스트 경고줄과 동일한 정보를 JSON 표면에도
        # 노출한다(요건 B-4, MCP·HTTP 양 표면 동일 노출).
        "stale_index": r.index_stale,
        "stale_docs_count": r.index_stale_docs,
        # O1(2차 배치) — dev-lead 판정: index_stale=False 로 조용히 수렴하는 건
        # 유지해도 되지만(드문 예외에 매 검색 경고를 안 띄우는 게 맞다), "판정
        # 불가" 자체는 어딘가에서 관측 가능해야 한다(무음 실패 금지). False 면 이번
        # 호출에서 stale 판정 자체가 실패했다는 뜻(원인은 서버 stderr 로그 참고).
        "stale_check_ok": r.index_stale_ok,
        # 2-4(qa 독립검증, 2026-08-14, 신규 필드) — stale_check_ok=False 가 "판정 불가"와
        # "판정 보류"를 겸해 HTTP 응답만으로 두 상태를 구분할 수 없었다(qa 실측: 두
        # 상태의 /search 응답이 키·값 완전 동일). stale_check_pending=True 면 "판정
        # 보류"(이 색인 세대에 대해 아직 계산 안 됨, 다음 호출부터 반영) — False 면(그리고
        # stale_check_ok 도 False 면) "판정 불가"(실패, stale_detail·서버 stderr 참고).
        # stale_detail 은 SearchResult.index_stale_detail 을 그대로 노출한다(수정 전에는
        # ok=False 일 때 이 값 자체가 빈 문자열로 덮여 있었고, 응답에 싣지도 않았다).
        "stale_check_pending": r.index_stale_pending,
        "stale_detail": r.index_stale_detail,
        "hits": hits_out,
        "budget": {
            "limit": budget_limit,
            "used": running,
            "omitted_count": len(omitted_files),
            "omitted_files": uniq_omitted,
        },
    }


@app.get("/health")
def health() -> JSONResponse:
    """색인이 없어도 500 이 아니라 200 + status 로 상태를 알린다(헬스체크가 죽으면
    진단이 안 된다 — 위임 브리프 명시 요구사항).

    O1(2차 배치) — `cache` 필드로 **프로세스 캐시** 상태(지문·누적 재로드 횟수)를
    디스크 값과 나란히 노출한다. 이 엔드포인트는 원래 sqlite 를 직접 읽어 **디스크
    상태만** 보고했다 — F-1 류 결함(전 채널 무음)이 재발해도 "프로세스 캐시가 실제로
    무효화됐는지"를 이걸로는 원리적으로 볼 수 없었다. `cache.cached_fingerprint_*` 가
    바로 아래 디스크 값(`created_at`/`n_chunks`)과 계속 어긋나 있으면(재로드가 전혀
    안 일어남) 운영자가 이 한 응답에서 바로 알아챌 수 있다.

    M4(코드리뷰) — `sqlite3.connect(str(sqlite_path))` 의 기본 모드는 파일이 없으면
    새로 만든다(search._get_conn() 의 N3 주석과 같은 함정). `_index_files()` 가 앞서
    존재를 확인하지만 그 사이 파일이 사라지는 좁은 경합 창이 남아 있었다 — 이 핸들러도
    노출돼 있었다. **M4 수정은 아래 `search_mod._open_index()` 호출 안에서도 유지된다**
    — `_open_index()` 내부의 모든 sqlite 연결이 `_get_conn()`(mode=ro)만 쓴다.

    2-3(qa 독립검증, 2026-08-14) — 이전엔 이 함수가 sqlite 만 열어 `COUNT(*)` + meta 를
    읽고 `status:"ok"` 를 냈다 — **bm25 아티팩트(`bm25s.BM25.load()`)는 한 번도 열어보지
    않았다.** 그래서 `bm25/params.index.json`·`.npy` 가 깨져도 `/health` 는 `status:"ok"`
    인데 `/search` 는 100% "색인 손상 또는 로드 실패"였다(qa 실측, `$SC\b2_health.py`) —
    헬스체크를 믿는 감시·재시작 자동화가 이 장애를 못 본다. 이제 `search_mod._open_index()`
    로 sqlite 커넥션 + bm25 리트리버를 **함께** 연다(그 커넥션을 그대로 재사용해 아래
    `COUNT(*)`/meta 조회도 한다 — 커넥션을 두 번 열지 않는다). `_open_index()` 는 지문이
    바뀌었을 때만 실제로 `bm25s.BM25.load()` + M1 교차검증(로드된 `num_docs` 가
    `meta.n_chunks` 와 같은지)을 수행하고, 그 로드가 실패하면 예외를 던진다 — 이 예외가
    바로 여기서 잡혀 기존과 동일하게 200 + `status="error"` 로 드러난다(헬스체크는 500
    금지 계약 그대로 유지 — 새 실패 경로도 이 `except` 하나로 흡수된다).

    **비용 설계(qa 요구 — "매 호출 전체 로드면 비용이 크다"):** `_open_index()` 는
    이미 이 모듈이 검색 경로에 쓰는 **신선도 지문 + 단일 스냅샷 캐시**(모듈 docstring
    "스레드 안전" 절, `_snapshot`/`_RetrieverSnapshot`)를 그대로 공유한다 — 캐시된
    지문과 지금 sqlite 의 지문이 **같으면**(재색인이 없었으면 항상 이 경우) 커넥션을
    열어 지문만 재확인하고 캐시된 리트리버를 즉시 반환한다(전체 재로드 없음, 실측은
    아래 "실측(콜드/웜)" 참고). 지문이 다를 때만(색인이 갓 바뀌었거나 이 프로세스의
    첫 `/health`·`/search`) 실제 `bm25s.BM25.load()` 를 치른다 — 그리고 그 비용은
    `/health` 가 처음 지불하든 `/search` 가 처음 지불하든 **어차피 한 번은 내야 하는
    비용**이다(검색 자체가 리트리버를 열어야 하므로). 즉 `/health` 를 자주 찌르는
    감시 루프에서는 첫 호출 이후 사실상 sqlite 커넥션 비용(~2ms, B5 실측)만 남는다.
    """
    # M5(qa 독립검증 후속, 2026-08-14) — `cache_status()` 를 예전엔 `_open_index()` 보다
    # **먼저** 불렀다 — 그래서 이번 호출이 지문 불일치로 실제 재로드를 유발해도(아래
    # `_open_index()` 가 그 경우), 응답에 실리는 `cache` 값은 재로드 **전** 스냅샷을
    # 가리켜 한 박자 뒤처졌다(관측 엔드포인트가 스스로 갱신한 값을 스스로 못 보는
    # 모순). 이제 각 분기에서 `_open_index()`(또는 `no_index` 분기처럼 아예 안 여는
    # 경우) **직후**에 `cache_status()` 를 불러 이번 호출이 만든 최신 상태를 반영한다.
    # **부작용 고지(README "관측" 절에도 명시)**: `/health` 가 지문 불일치를 감지하면
    # 이 호출 자체가 캐시를 비우고 다시 채운다 — 관측 엔드포인트가 관측 대상(캐시
    # 상태)을 바꾸는 부작용이 있다(정량 영향은 미측정 — `(확인 필요)`로 남긴다).
    ok, _sqlite_path = _index_files()
    if not ok:
        return JSONResponse({
            "status": "no_index",
            "index_dir": str(config.INDEX_DIR),
            "detail": "색인이 없다. python indexer.py 를 먼저 실행하라.",
            "cache": search_mod.cache_status(),
        })
    try:
        # 2-3 — sqlite 커넥션 + bm25 리트리버를 함께 연다(로드 가능성까지 확인).
        # M4 는 그대로 유지된다: _open_index() 가 여는 모든 sqlite 커넥션이 mode=ro다.
        conn, _retriever, _n_chunks_bm25, _fingerprint = search_mod._open_index()
        try:
            n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        return JSONResponse({
            "status": "ok",
            "index_dir": str(config.INDEX_DIR),
            "n_chunks": n_chunks,
            "created_at": meta.get("created_at"),
            "docs_root": meta.get("docs_root"),
            "cache": search_mod.cache_status(),  # M5 — _open_index() 이후 호출(최신 반영)
        })
    except Exception as e:
        # sqlite 파일이 손상된 경우 · bm25 아티팩트가 손상돼 로드/M1 교차검증이 실패한
        # 경우(2-3) 등 — 이것도 500 이 아니라 200+status.
        return JSONResponse({
            "status": "error",
            "index_dir": str(config.INDEX_DIR),
            "detail": f"{type(e).__name__}: {e}",
            "cache": search_mod.cache_status(),  # M5 — _open_index() 시도 이후 호출
        })


@app.get("/categories")
def categories() -> JSONResponse:
    return JSONResponse({"all": "전체(기본값)", **config.CATEGORY_DESC})


@app.get("/search")
def search_endpoint(
    q: str = Query(default=""),
    category: str = Query(default="all"),
    k: str | None = Query(default=None),
    # 2-6(qa 독립검증, 2026-08-14) — 선택적 상관 ID(기본값 None, 하위호환). R-001
    # 재질의율 판정에 쓰인다(search.py `_log_query`/`log_reader.py` 참고) — 호출자가
    # 안 넘기면 그냥 기존과 동일하게 동작한다.
    caller_id: str | None = Query(default=None),
) -> JSONResponse:
    try:
        result = _run_search(q, category, k, caller_id)
    except Exception as e:
        # ⑤: search() 내부의 색인 로드 실패는 이제 _run_search 안에서 200+error 로
        # 처리된다(그 외 경로 — 빈 q·색인 없음 — 도 200) — 여기 남는 예외는 색인과
        # 무관한(예산 계산 등) 진짜 버그다.
        return JSONResponse({"query": q, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse(result)


_PAGE_HEAD = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>docs-rag-mcp 검색</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "Malgun Gothic", sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  form { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  input[type=text] { flex: 1; min-width: 240px; padding: 0.5rem; font-size: 1rem; }
  select, input[type=number] { padding: 0.5rem; }
  button { padding: 0.5rem 1rem; cursor: pointer; }
  .hit { border: 1px solid #ddd; border-radius: 6px; padding: 0.75rem 1rem; margin-bottom: 1rem; }
  .hit-header { font-weight: 600; margin-bottom: 0.5rem; font-size: 0.9rem; color: #333; }
  .hit pre { white-space: pre-wrap; word-break: break-word; background: #f6f6f6; padding: 0.75rem; border-radius: 4px; font-size: 0.85rem; }
  .notice { background: #fff8e1; border: 1px solid #f0d878; padding: 0.5rem 0.75rem; border-radius: 4px; margin-bottom: 1rem; font-size: 0.9rem; }
  .warn { background: #fdecea; border: 1px solid #f5b5ae; }
  .meta { color: #666; font-size: 0.85rem; margin-bottom: 1rem; }
</style>
</head>
<body>
<h1>docs-rag-mcp 문서 검색</h1>
<p class="meta">팀 문서 로컬 검색(오프라인, LLM 호출 없음). API: <code>/search?q=&amp;category=&amp;k=</code></p>
"""

_PAGE_FOOT = """
</body>
</html>
"""


def _render_form(q: str, category: str, k_raw: str | None) -> str:
    k_display = _esc(k_raw) if k_raw else str(config.DEFAULT_K)
    cat_options = ['<option value="all"{sel}>all — 전체</option>'.format(
        sel=" selected" if category == "all" else ""
    )]
    for cat, desc in config.CATEGORY_DESC.items():
        sel = " selected" if category == cat else ""
        cat_options.append(f'<option value="{_esc(cat)}"{sel}>{_esc(cat)} — {_esc(desc)}</option>')
    return f"""<form method="get" action="/">
  <input type="text" name="q" value="{_esc(q)}" placeholder="검색어 (예: D-52 결정)">
  <select name="category">
    {''.join(cat_options)}
  </select>
  <input type="number" name="k" value="{k_display}" min="1" max="{config.MAX_K}">
  <button type="submit">검색</button>
</form>"""


def _render_results(result: dict[str, Any]) -> str:
    if "error" in result:
        return f'<div class="notice warn">{_esc(result["error"])}</div>'

    parts: list[str] = []
    hits = result.get("hits", [])

    if not result.get("category_valid", True):
        parts.append(
            f'<div class="notice">요청한 카테고리 \'{_esc(result["category_requested"])}\'는 '
            f'유효하지 않다 — 전체(all)로 검색했다.</div>'
        )
    elif result.get("fallback_used"):
        parts.append(
            f'<div class="notice">카테고리 \'{_esc(result["category_requested"])}\' 결과가 없어 '
            f'전체(all)로 폴백했다.</div>'
        )

    # 4차 배치 B(원본 stale 감지) — search.response_prelude() 와 같은 우선순위(카테고리
    # 안내 다음, confidence 경고 앞)로 배치한다. MCP 텍스트 경고와 동일한 두 분기
    # (문서 수를 아는 경우/dir-level 신호뿐인 경우)를 그대로 반영한다.
    if result.get("stale_index"):
        n = result.get("stale_docs_count", 0)
        if n > 0:
            parts.append(
                f'<div class="notice warn">색인이 원본보다 낡았다({n}개 문서가 색인 이후 '
                f'변경됨) — <code>python indexer.py</code> 를 실행하라.</div>'
            )
        else:
            parts.append(
                '<div class="notice warn">색인이 원본보다 낡았을 수 있다(신규/삭제 파일 '
                '가능성) — <code>python indexer.py</code> 를 실행하라.</div>'
            )

    if result.get("confidence") in ("low", "none"):
        # 2-1(qa 독립검증, 2026-08-14) — search.response_prelude() 의 저신뢰 경고줄과
        # 같은 신호(coverage·미달축)를 HTML 표면에도 노출한다.
        raw_max_str = f"{result['raw_max']:.2f}"
        coverage_str = f"{result.get('coverage', 0.0):.2f}"
        axis = result.get("deficient_axis") or "?"
        parts.append(
            f'<div class="notice warn">이 질의는 매칭 신뢰도가 낮다'
            f'(raw_max={_esc(raw_max_str)}, coverage={_esc(coverage_str)}, '
            f'미달축={_esc(axis)}, confidence={_esc(result["confidence"])})'
            f' — 결과는 참고용이다.</div>'
        )

    # ③(code-reviewer, 2026-08-13): format_result/response_prelude 가 사용자에게 직접
    # 내는 stale/deprecated·n_missing 경고를 HTML 표면에서만 떨어뜨리지 않는다 —
    # search.py response_prelude 주석("stderr 는 운영자만 본다 — 호출 에이전트가
    # 직접 보는 채널에도 남겨야 한다")의 "사용자 직접 채널"이 바로 이 HTML 화면이다.
    if any(h.get("status") in ("stale", "deprecated") for h in hits):
        parts.append(
            '<div class="notice warn">결과에 stale/deprecated 로 표시된(구버전일 수 있는) '
            '조각이 섞여 있다 — 각 항목의 status 를 확인하라.</div>'
        )

    if result.get("n_missing"):
        parts.append(
            f'<div class="notice warn">색인 정합 이상 — 후보 {result["n_missing"]}건을 '
            f'sqlite 에서 찾지 못해 건너뛰었다. 결과가 비정상적으로 적을 수 있다'
            f'(indexer.py 재실행 검토).</div>'
        )

    if not hits:
        parts.append('<p>결과 없음 — 용어를 바꿔 다시 검색하라.</p>')
        return "\n".join(parts)

    budget = result.get("budget", {})
    if budget.get("omitted_count"):
        omitted_list = ", ".join(_esc(f) for f in budget.get("omitted_files", []))
        parts.append(
            f'<div class="notice">응답 총량 예산({budget["limit"]:,}자) 초과로 '
            f'{budget["omitted_count"]}건의 본문을 생략했다 — 생략된 파일: {omitted_list}</div>'
        )

    for i, h in enumerate(hits, start=1):
        # 2-1(qa 독립검증, 2026-08-14) — search._hit_header_text()(MCP/텍스트 경로)와
        # 같은 이유로 raw 를 항상 병기한다 — "점수"가 질의 내 상대값(1위=1.000 고정)일
        # 뿐 절대 일치도가 아님을 HTML 표면에서도 드러낸다. h["raw_score"]는
        # server._hit_to_dict()가 이미 채워 두는 필드다(신규 필드 아님).
        header = (
            f'[{i}] {_esc(h["rel"])} ({_esc(h["heading_path"])}) 줄 '
            f'{_esc(h["start_line"])}~{_esc(h["end_line"])} '
            f'· 점수 {h["score"]:.3f}(상대값) · raw {h["raw_score"]:.2f} · status={_esc(h["status"])}'
        )
        if h.get("body_omitted"):
            body_html = '<em>(응답 총량 예산 초과로 본문 생략)</em>'
        else:
            body_html = f'<pre>{_esc(h["text"])}</pre>'
            if h.get("truncated"):
                # ②(code-reviewer, 2026-08-13): format_result 는 같은 상황에서
                # "...(잘림 — 원문 {rel} 줄 X~Y 참고)" 를 붙인다(search._hit_body_text
                # truncated=True 시, plan_response_budget 참고) — HTML 표면만 조용히
                # 자르던 것을 텍스트 경로와 맞춘다. 본문 자체가 생략된 경우(위 분기)엔
                # 텍스트 경로도 이 안내를 안 붙이므로 여기서도 붙이지 않는다.
                body_html += (
                    f'<p class="meta">...(잘림 — 원문 {_esc(h["rel"])} 줄 '
                    f'{_esc(h["start_line"])}~{_esc(h["end_line"])} 참고)</p>'
                )
        parts.append(f'<div class="hit"><div class="hit-header">{header}</div>{body_html}</div>')

    # ②(code-reviewer, 2026-08-13): 텍스트 경로(format_result)는 말미에 항상 이
    # Read(offset,limit) 에스컬레이션 안내를 붙이는데 HTML 에는 없었다 — 두 표면의
    # 에스컬레이션 경로가 갈리지 않게 맞춘다.
    parts.append(
        '<p class="meta">부족하면 해당 파일을 Read(offset=시작줄, limit=범위) 로 확대해서 '
        '읽어라 — 검색이 빗나가도 최악이 파일 통째 읽기로 떨어지지 않게 하는 안전장치다.</p>'
    )

    return "\n".join(parts)


@app.get("/", response_class=HTMLResponse)
def index_page(
    q: str = Query(default=""),
    category: str = Query(default="all"),
    k: str | None = Query(default=None),
) -> str:
    form_html = _render_form(q, category, k)
    results_html = ""
    if q.strip():
        try:
            result = _run_search(q, category, k)
            results_html = _render_results(result)
        except Exception as e:
            results_html = f'<div class="notice warn">검색 중 오류: {_esc(f"{type(e).__name__}: {e}")}</div>'
    return _PAGE_HEAD + form_html + results_html + _PAGE_FOOT


def _warm_staleness_cache() -> None:
    """H1(3) — uvicorn 기동 전 백그라운드 1회 원본 stale 판정 워밍(daemon 스레드).

    pending 창(H1(2) SWR 부트스트랩)이 실사용자의 첫 `/search` 요청에마저 보이면 "색인이
    낡았다" 경고가 그 요청에서만 빠질 수 있다 — 기동 시점에 미리 계산해 둔다.
    `search_mod.refresh_staleness_now()` 자체는 절대 예외를 던지지 않지만(색인이 없어도
    `StalenessCheck(ok=False, ...)` 를 그대로 스냅샷에 발행), 스레드 경계의 예상 밖 실패
    까지 best-effort 로 흡수해 서버 기동 자체를 방해하지 않는다.
    """
    try:
        search_mod.refresh_staleness_now()
    except Exception as e:
        sys.stderr.write(f"[server] stale 캐시 워밍 실패(best-effort, 기동은 계속): {type(e).__name__}: {e}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="docs-rag-mcp 로컬 HTTP 서버")
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"바인딩 호스트(기본 {DEFAULT_HOST} — 내부 문서라 기본값을 바꾸지 않는 것을 권장)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트(기본 {DEFAULT_PORT})")
    args = parser.parse_args()

    # M5 — 와일드카드 바인딩은 기동 시점에 거절한다(조용한 400 전면 실패는 최악의
    # 진단성이다). 이 검사는 아래 비루프백 경고·ALLOWED_HOSTS append 보다 먼저 한다 —
    # 어차피 뜨지 못할 서버를 위해 그 작업들을 할 이유가 없다.
    wildcard_error = _validate_bind_host(args.host)
    if wildcard_error is not None:
        sys.stderr.write(wildcard_error + "\n")
        sys.exit(2)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write(
            f"[server] 경고: 비루프백 호스트({args.host!r})로 바인딩한다 — 이 코퍼스는 "
            f"내부 기획·설계 문서다. 네트워크 노출 범위를 직접 확인하라.\n"
        )

    # F-5 요건 2 — 사용자가 --host 로 명시한 호스트도 허용 목록에 들어가야 한다(안 그러면
    # 그 호스트로 오는 모든 요청이 Host 헤더 검증에서 400 으로 막혀 --host 옵션 자체가
    # 죽는다). ALLOWED_HOSTS 는 HostGuardMiddleware 가 참조를 그대로 들고 있으므로, 이
    # append 가 uvicorn.run() 호출(미들웨어 스택이 실제로 구성되는 시점) 이전이기만
    # 하면 반영된다 — 여기가 그 지점이다(실 요청으로 검증함, 4차 배치 보고 참고).
    if not any(_normalize_host(args.host) == _normalize_host(h) for h in ALLOWED_HOSTS):
        ALLOWED_HOSTS.append(args.host)

    # H1(3) — import 시점이 아니라 여기(실제 프로세스 기동 지점)에서만 스레드를 띄운다.
    # indexer.py 가 search 를 import 하므로, import 시점에 백그라운드가 sqlite 커넥션을
    # 열면 H1 이 고치려는 그 rename 충돌을 정확히 되살린다.
    threading.Thread(target=_warm_staleness_cache, daemon=True).start()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
