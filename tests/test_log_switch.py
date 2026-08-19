"""질의 로그 스위치(4차 배치 D, F-8) — 지시서 §3 예외 규정.

"테스트 스위트는 반드시 DOCS_RAG_NO_LOG=1 을 켠 상태로 돈다"(tests/__init__.py 가
프로세스 진입 시 강제) — 이 파일만 예외적으로 그 값을 잠깐 바꿔치기하고, `search.LOG_PATH`
자체도 임시 파일로 몽키패치해 **실 logs/queries.jsonl 은 절대 건드리지 않는다**.
`unittest.mock.patch.dict`/직접 저장-복원으로 각 테스트가 끝나면 원래 값(NO_LOG=1)이
그대로 복원되게 한다 — 이후 도는 다른 테스트 파일에 스위치가 새면 안 된다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import search

from tests import testutil


class TestEnvTruthy(unittest.TestCase):
    """`_env_truthy()` 해석 규약(4차 배치 D4) — 1/true/yes/on(대소문자 무시)만 참."""

    ENV_NAME = "DOCS_RAG_TEST_TRUTHY_PROBE"

    def tearDown(self) -> None:
        os.environ.pop(self.ENV_NAME, None)

    def _set_and_check(self, value: str | None, expected: bool) -> None:
        if value is None:
            os.environ.pop(self.ENV_NAME, None)
        else:
            os.environ[self.ENV_NAME] = value
        self.assertEqual(search._env_truthy(self.ENV_NAME), expected, f"value={value!r}")

    def test_truthy_values(self) -> None:
        for v in ["1", "true", "TRUE", "True", "yes", "YES", "on", "On"]:
            self._set_and_check(v, True)

    def test_falsy_values(self) -> None:
        for v in ["0", "false", "no", "off", "", "random", "2"]:
            self._set_and_check(v, False)

    def test_unset_is_falsy(self) -> None:
        self._set_and_check(None, False)

    def test_whitespace_stripped(self) -> None:
        self._set_and_check(" 1 ", True)
        self._set_and_check(" true ", True)


class TestLogSwitch(unittest.TestCase):
    """§3 명시 예외 — LOG_PATH 를 tmp 로 몽키패치하고 스위치 자체를 검증한다."""

    def setUp(self) -> None:
        self._orig_log_path = search.LOG_PATH
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_log_")
        self.tmp_log_path = Path(self._tmpdir) / "queries.jsonl"
        search.LOG_PATH = self.tmp_log_path
        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()
        search.LOG_PATH = self._orig_log_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        # tests/__init__.py 가 세운 기본값(NO_LOG=1)이 이 테스트가 끝난 뒤에도 유효한지
        # 명시적으로 복구한다 — patch.dict 가 os.environ 변경분은 되돌리지만 이 값 자체는
        # 원래 프로세스 시작 시 __init__.py 가 넣은 것이라 이중으로 보장한다.
        os.environ["DOCS_RAG_NO_LOG"] = "1"

    def _call_log_query(self, *, source: str = "http") -> None:
        search._log_query(
            query="테스트 질의", category_requested="all", category_used="all", k=5,
            fallback=False, raw_max=1.23, confidence="ok", top=[{"rel": "x.md", "chunk_no": 1, "score": 1.0}],
            source=source, index_created_at="2026-01-01T00:00:00+00:00",
        )

    def test_no_log_true_writes_nothing(self) -> None:
        os.environ["DOCS_RAG_NO_LOG"] = "1"
        os.environ.pop("DOCS_RAG_LOG_SOURCE", None)
        self._call_log_query()
        self.assertFalse(self.tmp_log_path.exists(), "NO_LOG=1 인데 로그 파일이 생성됐다")

    def test_no_log_false_writes_one_line(self) -> None:
        os.environ["DOCS_RAG_NO_LOG"] = "0"
        os.environ.pop("DOCS_RAG_LOG_SOURCE", None)
        self._call_log_query(source="http")
        self.assertTrue(self.tmp_log_path.exists())
        lines = self.tmp_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["source"], "http")

    def test_index_created_at_source_default_is_unknown_not_live(self) -> None:
        """L1(qa 독립검증 후속, 2026-08-14 — 정정) — `index_created_at_source` 를 안
        넘긴 호출(`_call_log_query()` 가 그렇다 — 위 helper 시그니처 참고)은 이제
        "unknown" 을 낸다. 수정 전이라면(기본값 "live") 이 값이 "live" 였을 것이다 —
        가장 신뢰도 높은 값이 몰라서 빠뜨린 호출부의 기본값이면 안 된다는 회귀 방지."""
        os.environ["DOCS_RAG_NO_LOG"] = "0"
        os.environ.pop("DOCS_RAG_LOG_SOURCE", None)
        self._call_log_query(source="http")
        entry = json.loads(self.tmp_log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["index_created_at_source"], "unknown")

    def test_index_created_at_source_explicit_value_still_respected(self) -> None:
        """기존 두 호출부(search() 안)는 여전히 "live"/"cached_snapshot" 을 명시적으로
        넘긴다 — 기본값 변경이 그 명시적 호출까지 바꾸면 안 된다(test_log_schema.py 가
        이미 그 경로를 검증하지만, 여기서는 `_log_query` 를 직접 불러 한 번 더 확인)."""
        os.environ["DOCS_RAG_NO_LOG"] = "0"
        os.environ.pop("DOCS_RAG_LOG_SOURCE", None)
        search._log_query(
            query="테스트 질의", category_requested="all", category_used="all", k=5,
            fallback=False, raw_max=1.23, confidence="ok",
            top=[{"rel": "x.md", "chunk_no": 1, "score": 1.0}],
            source="http", index_created_at="2026-01-01T00:00:00+00:00",
            index_created_at_source="cached_snapshot",
        )
        entry = json.loads(self.tmp_log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["index_created_at_source"], "cached_snapshot")

    def test_log_source_override_wins(self) -> None:
        """LOG_SOURCE 가 있으면 호출자가 넘긴 source 값을 덮어쓴다(테스트/QA 트래픽 분리)."""
        os.environ["DOCS_RAG_NO_LOG"] = "0"
        os.environ["DOCS_RAG_LOG_SOURCE"] = "qa"
        self._call_log_query(source="http")
        lines = self.tmp_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["source"], "qa", "LOG_SOURCE 오버라이드가 적용 안 됐다")

    def test_no_log_wins_over_log_source(self) -> None:
        """NO_LOG=1 이면 LOG_SOURCE 가 있어도 아무것도 안 쓴다(1차 방어가 2차 방어보다 우선)."""
        os.environ["DOCS_RAG_NO_LOG"] = "1"
        os.environ["DOCS_RAG_LOG_SOURCE"] = "qa"
        self._call_log_query()
        self.assertFalse(self.tmp_log_path.exists())

    def test_two_calls_append_two_lines(self) -> None:
        os.environ["DOCS_RAG_NO_LOG"] = "0"
        os.environ.pop("DOCS_RAG_LOG_SOURCE", None)
        self._call_log_query()
        self._call_log_query()
        lines = self.tmp_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # 유효한 JSON 이어야 한다(RFC 8259 — allow_nan=False 계약)


class TestM7NoLogCliFlag(unittest.TestCase):
    """M7(코드리뷰) — 로그 오염 방지 스위치를 `rag-reindex` 스킬 절차가 실제로 쓰는지
    (문서 드리프트 가드) + `search.py --no-log` 플래그 자체가 실제로 로그를 억제하는지
    (서브프로세스 E2E)."""

    def test_skill_doc_uses_no_log_flag(self) -> None:
        """`.claude/skills/rag-reindex/SKILL.md` 의 자기검색 스모크 단계(5단계)는
        `--no-log`(또는 `DOCS_RAG_NO_LOG`)를 써야 한다 — 안 그러면 합성 검증 질의가
        R-001 근거 로그(`logs/queries.jsonl`)에 섞인다. 수정 전이라면 이 문서의 `search.py`
        호출 줄에 아무 스위치도 없어 이 단언이 실패한다(문서 드리프트 가드).
        """
        skill_path = (
            Path(__file__).resolve().parent.parent
            / ".claude" / "skills" / "rag-reindex" / "SKILL.md"
        )
        self.assertTrue(skill_path.exists(), f"SKILL.md 를 찾을 수 없다: {skill_path}")
        text = skill_path.read_text(encoding="utf-8")

        search_py_lines = [
            line for line in text.splitlines()
            if "search.py" in line and "--k" in line
        ]
        self.assertTrue(search_py_lines, "SKILL.md 에서 search.py 호출 줄(--k 포함)을 찾지 못했다")

        for line in search_py_lines:
            self.assertTrue(
                "--no-log" in line or "DOCS_RAG_NO_LOG" in line,
                f"search.py 호출 줄에 로그 억제 스위치가 없다(M7 문서 드리프트): {line!r}",
            )

    @unittest.skipUnless(
        testutil.real_index_available(),
        "실 index/ 가 없다 — python indexer.py 를 먼저 실행하라",
    )
    def test_no_log_flag_suppresses_logging_end_to_end_subprocess(self) -> None:
        """`--no-log` 가 실제로 로그를 안 남기는지 서브프로세스로 확인한다.

        실 색인(index/)을 read-only 로 읽는 E2E 라 실 색인이 있을 때만 돈다(다른
        stdio 통합 티어와 같은 skipUnless 게이트 — 신규 클론에서는 색인 생성 후 실행).

        `search.LOG_PATH` 는 `config.RAG_ROOT` 파생 모듈 상수(오버라이드 불가 — 이 파일
        상단 docstring 참고)라, 서브프로세스에서 실행할 작은 드라이버 스크립트 안에서
        `search` 를 import 하기 **전에** `config.RAG_ROOT` 를 임시 경로로 갈아끼워
        `LOG_PATH` 를 그 임시 경로 아래로 유도한다(실 `logs/queries.jsonl` 절대 안 건드림).
        `config.DOCS_ROOT`/`INDEX_DIR` 는 `config.py` import 시점에 이미 원래 `RAG_ROOT`
        로 확정돼 있어(내 재대입은 그 이후) 실 문서·실 색인을 그대로 읽는다(read-only,
        안전 — --no-log 가 실제로 로그를 안 남기는지가 검증 대상이지 검색 결과 내용은
        무관하다).
        """
        tmp_dir = tempfile.mkdtemp(prefix="docs_rag_test_m7_")
        try:
            tmp_log = Path(tmp_dir) / "logs" / "queries.jsonl"
            repo_root = Path(__file__).resolve().parent.parent

            driver = Path(tmp_dir) / "_m7_driver.py"
            driver.write_text(
                "import sys\n"
                # `python <script>` 실행은 sys.path[0] 이 스크립트 위치(tmp_dir)가 되고
                # cwd 는 자동으로 안 잡힌다(`-c`/`-m` 과 다름) — repo_root 를 명시적으로
                # 앞에 꽂아야 config/search 등 로컬 모듈을 찾는다.
                f"sys.path.insert(0, {str(repo_root)!r})\n"
                "import config\n"
                f"config.RAG_ROOT = __import__('pathlib').Path({tmp_dir!r})\n"
                "sys.argv = ['search.py', 'STALECHECKTERM', '--no-log']\n"
                "import runpy\n"
                "runpy.run_path('search.py', run_name='__main__')\n",
                encoding="utf-8",
            )

            # 지시서 §0-7 — env= 는 부모 환경을 상속하지 않고 전체 대체된다. 반드시
            # os.environ.copy() 후 변수를 얹는다.
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            # --no-log 플래그 "단독"으로도 억제되는지 보려고 환경변수 스위치는 명시적으로
            # 끈다 — 테스트 러너(부모 프로세스)가 DOCS_RAG_NO_LOG=1 을 이미 켜 둔
            # 상태라 그대로 두면 --no-log 가 아무 일도 안 해도 이 테스트가 통과해버린다.
            env.pop("DOCS_RAG_NO_LOG", None)
            env.pop("DOCS_RAG_LOG_SOURCE", None)

            proc = subprocess.run(
                [sys.executable, str(driver)],
                cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"드라이버 서브프로세스 실패:\nstdout={proc.stdout}\nstderr={proc.stderr}",
            )
            self.assertFalse(
                tmp_log.exists(),
                f"M7 회귀 — --no-log 인데 로그 파일이 생성됐다: {tmp_log}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}",
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestCallerIdSanitization(unittest.TestCase):
    """M3(qa 독립검증 후속, 2026-08-14) — caller_id 길이·문자셋 캡(`_sanitize_caller_id()`).

    브리프 요구: "캡·문자셋 값과 그 근거를 보고하라" — 여기서 실제 동작을 검증한다.
    """

    def setUp(self) -> None:
        self._orig_warned = search._caller_id_warned
        search._caller_id_warned = False  # 각 테스트가 경고 발생 여부를 독립적으로 볼 수 있게

    def tearDown(self) -> None:
        search._caller_id_warned = self._orig_warned

    def test_none_and_empty_passthrough(self) -> None:
        self.assertIsNone(search._sanitize_caller_id(None))
        self.assertEqual(search._sanitize_caller_id(""), "")

    def test_short_conforming_value_untouched(self) -> None:
        self.assertEqual(search._sanitize_caller_id("w1"), "w1")
        self.assertEqual(search._sanitize_caller_id("sess-2026-08-14.a:1"), "sess-2026-08-14.a:1")

    def test_overlong_value_truncated_to_cap(self) -> None:
        long_id = "B" * 200000
        out = search._sanitize_caller_id(long_id)
        self.assertEqual(len(out), search._CALLER_ID_MAX_LEN, "캡(128자)으로 절단되지 않았다")
        self.assertEqual(out, long_id[: search._CALLER_ID_MAX_LEN])

    def test_nonconforming_charset_still_truncated_and_kept(self) -> None:
        """문자셋 위반 값도 임의로 치환·제거하지 않고(json.dumps 이스케이프가 안전을
        책임진다) 길이 캡만 적용해 그대로 남긴다 — 값 자체를 고치면 사후 분석이 어려워진다."""
        raw = "worker 1 (한글/공백!)"
        out = search._sanitize_caller_id(raw)
        self.assertEqual(out, raw[: search._CALLER_ID_MAX_LEN])

    def test_warning_emitted_once_per_process(self) -> None:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            search._sanitize_caller_id("B" * 200000)
            search._sanitize_caller_id("B" * 200000)  # 2회째는 경고 재출력 안 함(도배 방지)
        occurrences = buf.getvalue().count("caller_id")
        self.assertEqual(occurrences, 1, f"경고가 프로세스당 1회로 제한되지 않았다: {buf.getvalue()!r}")


class TestCallerIdLogLineSizeCap(unittest.TestCase):
    """M3 — `_log_query()` 를 실제로 호출했을 때 로그 한 줄의 크기가 caller_id 폭주로
    무한정 커지지 않는지 확인한다(§3 예외 규정 — LOG_PATH 는 임시 파일로 몽키패치).
    """

    def setUp(self) -> None:
        self._orig_log_path = search.LOG_PATH
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_log_capid_")
        self.tmp_log_path = Path(self._tmpdir) / "queries.jsonl"
        search.LOG_PATH = self.tmp_log_path
        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        os.environ["DOCS_RAG_NO_LOG"] = "0"
        os.environ.pop("DOCS_RAG_LOG_SOURCE", None)

    def tearDown(self) -> None:
        self._env_patcher.stop()
        search.LOG_PATH = self._orig_log_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        os.environ["DOCS_RAG_NO_LOG"] = "1"

    def test_oversized_caller_id_does_not_bloat_log_line(self) -> None:
        """수정 전이라면(캡 없음) 이 한 줄이 200KB+ 였을 것이다 — 이제 caller_id 필드
        자체가 128자로 캡돼 줄 크기가 유계다."""
        search._log_query(
            query="테스트 질의", category_requested="all", category_used="all", k=5,
            fallback=False, raw_max=1.23, confidence="ok",
            top=[{"rel": "x.md", "chunk_no": 1, "score": 1.0}],
            source="http", index_created_at="2026-01-01T00:00:00+00:00",
            caller_id="B" * 200000,
        )
        line = self.tmp_log_path.read_text(encoding="utf-8").splitlines()[0]
        entry = json.loads(line)
        self.assertEqual(len(entry["caller_id"]), search._CALLER_ID_MAX_LEN)
        # 줄 전체 크기도 캡 근방이어야 한다(다른 필드는 짧으므로 수백 바이트 수준).
        self.assertLess(len(line.encode("utf-8")), 2000, f"로그 줄이 예상보다 훨씬 크다: {len(line)}자")


if __name__ == "__main__":
    unittest.main()
