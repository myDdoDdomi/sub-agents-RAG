"""sub-agents-RAG MCP stdio 서버 — search_docs / list_doc_categories 툴 2개.

stdout 오염 금지 계약(중요, 3차 배치 실측 2026-08-13):
  · mcp 2.0.0 의 `mcp.server.stdio.stdio_server()` 는 서빙 중 OS 레벨에서 fd 1
    (stdout) 을 claim 해 실제 프로토콜 wire 로 쓰고, 원래 fd 1 자리는 fd 2(stderr)
    의 dup 으로 diversion 한다(win32 의 콘솔 핸들 재배선까지 포함 —
    `mcp.server.stdio._claim_fd`/`_open_stdout_diversion` 소스 실측 확인). 그래서
    서빙 중에는 print() 를 호출해도 실제 wire(함수 지역 변수로 받은 별도 파일객체)
    는 깨지지 않는다 — sys.stdout 이라는 전역을 wire 가 안 쓰기 때문이다.
  · 단 이 방어는 라이브러리 docstring 이 스스로 "best-effort" 라고 밝힌 것이다 —
    fd 가 진짜 OS 파일디스크립터로 뒷받침되지 않는 특이 실행 환경(이미 캡처된
    스트림 위에서 재실행 등)에서는 diversion 이 조용히 폴백되어 sys.stdout 이
    그대로 wire 로 쓰일 수 있다. 그래서 애플리케이션 레벨에서도 이중 방어로 각
    툴 실행 구간을 `contextlib.redirect_stdout(sys.stderr)` 로 감싼다 — sys.stdout
    전역을 바꿔도 프로토콜 wire 는 영향받지 않음을 소스로 확인했고, 실제 stdio
    왕복 스모크로 이 가드가 initialize/list_tools/call_tool 흐름을 깨지 않음을
    확인했다(위임 브리프 self-check 3).
  · bm25s.BM25.load()/retrieve() 는 이 코퍼스 규모에서 실측상 stdout/stderr 어느
    쪽에도 진행률을 쓰지 않는다(2026-08-13 실측, os.pipe 로 fd 1/2 를 직접 가로채
    확인 — show_progress 기본값 True 인 load() 경로 포함). search.py 는 이미
    retrieve() 를 show_progress=False 로 호출한다. 그래도 위 가드가 방어망으로 남는다.
  · print() 는 이 파일에서 쓰지 않는다. 진단은 sys.stderr.write() 만 쓴다.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import traceback
from typing import Annotated

# Windows 콘솔 인코딩 방어 — .mcp.json 이 보통 PYTHONUTF8=1 을 넘겨주지만, 그 값 없이
# 실행돼도 한글 출력이 죽지 않게 한다(search.py CLI 진입점과 동일 패턴).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from mcp.server import MCPServer  # noqa: E402 (인코딩 재설정을 import 보다 먼저 하기 위함)
from pydantic import Field  # noqa: E402 — mcp/fastapi 가 이미 요구하는 기존 의존성(신규 아님)

import config  # noqa: E402
import search as search_mod  # noqa: E402

# MCP 상한. config.MAX_K(20) 는 HTTP/CLI 상한이고 이것과 다르다 — MCP 호출자(Claude
# 등 에이전트)는 자기 컨텍스트를 더 아껴야 해서 10 으로 더 좁게 잡는다(3차 배치
# 위임 브리프 지정값).
_MCP_MAX_K = 10

app = MCPServer(name="docs-rag", version="1.0.0")


def _clamp_k(k: object) -> int:
    """k 입력 방어 — 문자열/None/음수/float 무엇이 와도 안전한 정수로 만든다.

    int() 변환 자체가 실패하면(예: "abc", None) config.DEFAULT_K 로 폴백한다
    (조용히 1건으로 깎지 않는다 — search.search() 의 k<=0 처리와 같은 태도).
    변환에 성공해도 k<=0 이면 마찬가지로 config.DEFAULT_K 로 보정한다 — k<=0 은
    "1건만 달라"가 아니라 잘못된 입력일 가능성이 높다(search.py L6/server._clamp_k
    와 같은 태도, 3차 배치 결함① 수정: 이전엔 여기서만 0/음수가 조용히 1로 깎였다).
    그 외엔 1~_MCP_MAX_K 로 클램프한다.
    """
    try:
        k_int = int(k)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # ⑥(code-reviewer, 2026-08-13): int(float('inf'))/int(float('nan')) 는
        # OverflowError/ValueError 를 던진다 — inf 케이스는 OverflowError 라 기존
        # (TypeError, ValueError) 로는 못 잡고 그대로 터졌다.
        k_int = config.DEFAULT_K
    if k_int <= 0:
        k_int = config.DEFAULT_K
    return max(1, min(_MCP_MAX_K, k_int))


def _index_missing_message() -> str | None:
    """색인 파일 3종이 없으면(또는 부분 스왑으로 일부만 있으면) 안내 문자열을, 있으면
    None 을 낸다.

    import 시점이 아니라 툴 호출 시점에만 확인한다 — search.py 의 지연 로드
    계약(모듈 로드만으로는 인덱스를 건드리지 않음)을 그대로 잇는다.

    O5(2차 배치) — `search_mod.index_files_status()` 로 위임한다(이전엔 이 함수가
    `bm25_dir.exists()`·`sqlite_path.exists()` 만 봐서, HTTP 쪽(`server._index_files`)
    이 이미 쓰고 있던 `params.index.json` canary 체크가 빠져 있었다 — 부분 스왑 상태를
    두 표면이 다르게 판단하는 불일치였다. `_open_index()` 로 지문 조회가 매 호출
    최상단으로 올라가 이 경로를 더 자주 밟게 된 이번 배치에서 통일한다).
    """
    ok, _sqlite_path = search_mod.index_files_status()
    if not ok:
        # ⑥(code-reviewer, 2026-08-13): format_result 는 [안내]/[경고] 접두로 톤을
        # 구분한다 — 이 색인 부재 안내도 정상 검색 결과와 형식상 구별되게 맞춘다.
        return "[안내] 색인이 없다. python indexer.py 를 먼저 실행하라."
    return None


@app.tool(
    description=(
        "팀 기획·설계 문서를 조각 단위로 검색한다. 스펙·결정 확인 시 "
        "Grep/Read보다 먼저 사용. 결과의 경로·줄범위로 부족하면 그 부분만 "
        "Read로 확대. category는 list_doc_categories 참고."
    )
)
def search_docs(
    query: str,
    category: str = "all",
    k: int = 5,
    # M4(qa 독립검증 후속, 2026-08-14) — 2-6 이 caller_id 를 열어뒀지만(선택적 상관 ID)
    # 채택 경로(호출 에이전트가 이 인자를 실제로 채우는 것)가 없어 §2-6 회귀(로그
    # r001_condition2 가 구조적으로 항상 measurable=False)가 영구화됐다 — 툴 스키마엔
    # 노출돼도 설명이 없어 호출자가 채울 근거가 없었다. `Annotated[..., Field(description=...)]`
    # 로 인자 단위 설명만 최소로 추가한다(툴 전체 description 문자열은 안 건드린다 —
    # 이 서버가 실제로 붙어서 돌고 있는 라이브 표면이라 변경을 최소로 유지한다).
    caller_id: Annotated[
        str | None,
        Field(
            description=(
                "선택 — 호출자(세션/워커) 식별자. 예: 'w1'·'sess-2026-08-14-a'. R-001 "
                "재질의율 판정 근거를 남기려면 세션·워커별로 안정적인 값을 넘겨라(생략 시 "
                "None — 기존 동작 그대로, 재질의율은 이 값이 있는 호출만 대상으로 측정된다)."
            )
        ),
    ] = None,
) -> str:
    try:
        with contextlib.redirect_stdout(sys.stderr):  # 이중 방어 — 모듈 docstring 참고
            missing = _index_missing_message()
            if missing is not None:
                return missing
            k_clamped = _clamp_k(k)
            result = search_mod.search(
                query, category=category, k=k_clamped, source="mcp", caller_id=caller_id,
            )
            return search_mod.format_result(result, query)
    except Exception as e:
        # 스택트레이스는 stderr 로만 — 호출자에게는 짧은 문자열만 반환한다.
        # ⑥(code-reviewer, 2026-08-13): [오류] 접두로 정상 검색 결과와 형식상 구별한다.
        traceback.print_exc(file=sys.stderr)
        return f"[오류] 검색 중 오류: {type(e).__name__}: {e}"


@app.tool(description="search_docs의 category에 쓸 수 있는 값과 설명을 반환한다.")
def list_doc_categories() -> str:
    try:
        with contextlib.redirect_stdout(sys.stderr):  # 이중 방어 — 모듈 docstring 참고
            lines = ["카테고리 (all 도 검색어 제한 없이 쓸 수 있다 — 기본값):", "", "all — 전체(기본값)"]
            for cat, desc in config.CATEGORY_DESC.items():
                lines.append(f"{cat} — {desc}")
            return "\n".join(lines)
    except Exception as e:
        # ⑥(code-reviewer, 2026-08-13): [오류] 접두로 정상 결과와 형식상 구별한다.
        traceback.print_exc(file=sys.stderr)
        return f"[오류] 카테고리 조회 중 오류: {type(e).__name__}: {e}"


def _warm_staleness_cache() -> None:
    """H1(3) — `app.run()` 진입 전 백그라운드 1회 원본 stale 판정 워밍(daemon 스레드).

    pending 창(H1(2) SWR 부트스트랩)이 이 프로세스의 첫 `search_docs` 호출에마저 보이면
    "색인이 낡았다" 경고가 그 호출에서만 빠질 수 있다 — 기동 시점에 미리 계산해 둔다.
    `search_mod.refresh_staleness_now()` 는 절대 예외를 던지지 않지만(색인이 없어도
    `StalenessCheck(ok=False, ...)` 를 그대로 스냅샷에 발행), 스레드 경계의 예상 밖 실패
    까지 best-effort 로 흡수한다. print 금지 유지 — stderr 로만 남긴다.
    """
    try:
        search_mod.refresh_staleness_now()
    except Exception as e:
        sys.stderr.write(f"[mcp_server] stale 캐시 워밍 실패(best-effort): {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    # H1(3) — import 시점이 아니라 여기(실제 프로세스 기동 지점)에서만 스레드를 띄운다.
    # indexer.py 가 search 를 import 하므로, import 시점에 백그라운드가 sqlite 커넥션을
    # 열면 H1 이 고치려는 그 rename 충돌을 정확히 되살린다.
    threading.Thread(target=_warm_staleness_cache, daemon=True).start()
    app.run(transport="stdio")
