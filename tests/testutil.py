"""회귀 스위트 공용 헬퍼 — 색인 격리(지시서 §2) + 합성 코퍼스 빌더.

이 모듈 자체는 테스트를 담지 않는다(`Test`로 시작하는 클래스가 없어 `unittest discover`
가 이 파일에서는 아무것도 수집하지 않는다 — 그냥 임포트만 되는 유틸리티).

격리 규약(지시서 §2, 실측 확인됨— scratchpad 사전 검증):
  · `config.py` 는 수정 금지라 `INDEX_DIR` 에 환경변수 오버라이드가 없다. 대신
    `search.py`/`indexer.py` 가 `config.INDEX_DIR`/`config.DOCS_ROOT` 를 **호출
    시점에** 읽으므로, 모듈 속성 자체를 갈아끼우면(런타임 몽키패치) 실 색인을 전혀
    건드리지 않고 임시 색인만 쓸 수 있다.
  · `search.py` 의 모듈 수준 캐시(`_snapshot`·`_stale_snapshot`·`_reload_count`·
    `_last_load_failure`·`_untrustworthy_fingerprint_warned`·`_stale_revalidating`)는
    `config.INDEX_DIR` 값과 무관하게 프로세스 생애주기 동안 살아남는다 — 테스트 간
    오염을 막으려면 매번 리셋해야 한다.
  · `search.LOG_PATH` 는 `config.RAG_ROOT`(= 이 파일이 실제로 놓인 위치, 오버라이드
    불가)에서 파생된 모듈 상수라 DOCS_ROOT/INDEX_DIR 패치로는 안 움직인다 — 로그
    스위치 자체를 검증하는 테스트(tests/test_log_switch.py)만 `search.LOG_PATH` 를
    직접 몽키패치한다.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import config
import indexer
import search


def reset_search_module_caches() -> None:
    """search.py 의 모듈 수준 캐시를 전부 리셋한다(§2 — "search.py 모듈 캐시도 리셋").

    `config.INDEX_DIR` 를 바꾼 것만으로는 부족하다 — F-1 수정 자체가 "지문이 다르면
    자동으로 재로드한다"는 설계라 이론적으로는 리셋 없이도 다음 호출이 스스로 치유
    하지만(지문 불일치 감지), 테스트 간 결정성을 지문 우연 일치에 기대지 않기 위해
    명시적으로 비운다.
    """
    search._snapshot = None
    search._reload_count = 0
    search._last_load_failure = None
    search._untrustworthy_fingerprint_warned = False  # M2(코드리뷰) — 경고 프로세스당 1회 제한 리셋
    search._stale_snapshot = None
    search._stale_revalidating = False


def write_corpus(docs_root: Path, corpus: dict[str, str]) -> None:
    """{상대경로: 마크다운 내용} 을 docs_root 아래에 실제 파일로 쓴다."""
    for rel, content in corpus.items():
        p = docs_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def reindex_in_process(extra_argv: list[str] | None = None, *, quiet: bool = True) -> tuple[int, str]:
    """indexer.main() 을 인프로세스로 호출한다(§2 — "인프로세스 호출 가능(sys.argv 통제 +
    redirect_stdout)"). sys.argv 를 통제하고, 실패 시 sys.exit(코드) 를 캡처해 반환한다
    (indexer.main() 은 `--check` 경로와 청킹 0건/왕복검증 실패 경로에서 SystemExit 를
    던진다 — 정상 전체 재색인 성공 시에는 그냥 반환한다).

    Returns: (exit_code, captured_stdout) — 정상 반환 시 exit_code=0.
    """
    orig_argv = sys.argv
    sys.argv = ["indexer.py"] + (extra_argv or [])
    buf = io.StringIO()
    exit_code = 0
    try:
        cm = contextlib.redirect_stdout(buf) if quiet else contextlib.nullcontext()
        with cm:
            try:
                indexer.main()
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = orig_argv
    return exit_code, buf.getvalue()


class IsolatedIndexCase(unittest.TestCase):
    """`config.DOCS_ROOT`/`config.INDEX_DIR` 를 임시 디렉터리로 몽키패치하고, `CORPUS`
    (서브클래스가 오버라이드하는 {상대경로: 마크다운} dict)를 색인한 뒤 검색 가능한
    상태로 만든다. 클래스당 1회만 색인한다(setUpClass) — 스위트 전체가 몇 초 안에
    끝나게 하려는 목적(§2 "합성 코퍼스는 작게 만들어 몇 초에 끝나게 하라").

    실 index/ 는 setUp 도 tearDown 도 전혀 건드리지 않는다 — `config.INDEX_DIR` 자체가
    임시 경로를 가리키므로 `indexer.py`/`search.py` 코드는 실 색인의 존재를 알지도
    못한다(경로 몽키패치만으로 완전 격리, 사전 실측 확인됨).
    """

    CORPUS: dict[str, str] = {}
    REINDEX_EXTRA_ARGV: list[str] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._orig_docs_root = config.DOCS_ROOT
        cls._orig_index_dir = config.INDEX_DIR
        cls._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_")
        cls.tmp_path = Path(cls._tmpdir)
        cls.docs_root = cls.tmp_path / "docs_root"
        cls.index_dir = cls.tmp_path / "index"

        write_corpus(cls.docs_root, cls.CORPUS)

        config.DOCS_ROOT = cls.docs_root
        config.INDEX_DIR = cls.index_dir
        reset_search_module_caches()

        exit_code, out = reindex_in_process(cls.REINDEX_EXTRA_ARGV)
        if exit_code != 0 or "색인 완료" not in out:
            # 픽스처 자체가 깨지면 이후 모든 테스트가 오진을 낳는다 — 조용히 넘어가지 않는다.
            raise RuntimeError(
                f"{cls.__name__} 픽스처 색인 실패(exit={exit_code}) — 아래는 indexer.py 출력 전문:\n{out}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        config.DOCS_ROOT = cls._orig_docs_root
        config.INDEX_DIR = cls._orig_index_dir
        reset_search_module_caches()
        shutil.rmtree(cls._tmpdir, ignore_errors=True)
        super().tearDownClass()

    # ── 공용 조회 헬퍼 ──────────────────────────────────────────────
    @classmethod
    def sqlite_row_for_idx(cls, idx: int) -> sqlite3.Row:
        """합성 색인 sqlite 에서 idx 로 조각 원본 행을 직접 읽는다(검증 대조용 — search.py
        가 낸 Hit 필드를 다시 search.py 로 확인하는 자기순환을 피하려고 sqlite 를 직접
        연다).
        """
        conn = sqlite3.connect(str(cls.index_dir / "chunks.sqlite"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM chunks WHERE idx = ?", (idx,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise AssertionError(f"idx={idx} 가 합성 색인 chunks 테이블에 없다")
        return row

    @classmethod
    def sqlite_meta(cls) -> dict[str, str]:
        conn = sqlite3.connect(str(cls.index_dir / "chunks.sqlite"))
        try:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()


def real_index_available() -> bool:
    """실 index/(sub-agents-RAG/index/) 가 존재하는지 — stdio 통합 티어의 skipUnless 조건.

    `config.INDEX_DIR`/`config.DOCS_ROOT` 를 건드리지 않고 호출해야 한다(호출 시점의
    config 값을 그대로 쓴다) — 이 함수를 부르는 시점에 어떤 테스트도 config 를
    몽키패치한 채로 남겨두면 안 된다(각 IsolatedIndexCase 는 tearDownClass 에서
    원복하므로 정상적인 순차 실행에서는 문제없다).
    """
    ok, _ = search.index_files_status()
    return ok
