"""2-6(qa 독립검증, 2026-08-14) 회귀 — 질의 로그 스키마 v2.

§3 명시 예외를 그대로 따른다(test_log_switch.py 와 동일 패턴): `search.LOG_PATH` 를 임시
파일로 몽키패치하고 `DOCS_RAG_NO_LOG` 를 잠깐 0 으로 켠다 — **실 logs/queries.jsonl
은 절대 건드리지 않는다.** 색인은 `testutil.IsolatedIndexCase` 로 격리한다(실 index/
무관).

여기서 확인하는 것(수정 전이라면 전부 KeyError/None 으로 실패했을 새 필드들):
  1. v2 로그 줄에 schema_version·run_id·pid·seq·caller_id·coverage·n_query_tokens·
     n_matched_tokens·deficient_axis·index_created_at_source 가 실린다.
  2. `top[]` 원소에 기존 rel·chunk_no·score 는 그대로 있고 status·raw 가 추가된다.
  3. `seq` 가 프로세스 내에서 호출마다 증가한다.
  4. `caller_id` 가 `search()` 인자→로그로 그대로 전달된다.
  5. 토큰 0개 조기 반환에서도 `index_created_at` 이 (캐시가 있으면) None 이 아니다
     — 수정 전에는 이 경로가 항상 None 이었다.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import search

from tests import testutil


def _doc(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class TestLogSchemaV2(testutil.IsolatedIndexCase):
    CORPUS = {
        "docs/logtest/doc1.md": _doc(
            "# 로그 테스트 문서", "", "## 섹션", "",
            "LOGSCHEMATERM 이 이 문서의 핵심 표식이다.",
        ),
    }

    def setUp(self) -> None:
        self._orig_log_path = search.LOG_PATH
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_logv2_")
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
        os.environ["DOCS_RAG_NO_LOG"] = "1"  # tests/__init__.py 가 세운 기본값 복구

    def _read_lines(self) -> list[dict]:
        if not self.tmp_log_path.exists():
            return []
        return [json.loads(l) for l in self.tmp_log_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_v2_entry_has_new_top_level_fields(self) -> None:
        search.search("LOGSCHEMATERM", category="all", k=5, source="test")
        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        e = lines[0]

        self.assertEqual(e["schema_version"], 2)
        self.assertIsInstance(e["run_id"], str)
        self.assertTrue(e["run_id"])
        self.assertEqual(e["pid"], os.getpid())
        # seq 는 프로세스 전역 카운터(모듈 상단 `_log_seq_counter`)라 절대값(=1)을
        # 단언하지 않는다 — 같은 프로세스에서 먼저 실행된 다른 테스트가 이미 로그를
        # 남겼을 수 있다(테스트 실행 순서에 의존하지 않게 하려는 의도적 설계). 여기서는
        # "정수이고 1 이상"만 확인한다 — 순서 자체는 `test_seq_increments_within_process`
        # 가 별도로 확인한다.
        self.assertIsInstance(e["seq"], int)
        self.assertGreaterEqual(e["seq"], 1)
        self.assertIn("coverage", e)
        self.assertIn("n_query_tokens", e)
        self.assertIn("n_matched_tokens", e)
        self.assertIn("deficient_axis", e)
        self.assertIn("caller_id", e)
        self.assertIsNone(e["caller_id"])  # 안 넘겼으므로 기본값 None
        self.assertEqual(e["index_created_at_source"], "live")
        self.assertIsNotNone(e["index_created_at"], "정상 검색인데 index_created_at 이 None 이다")

        # 기존 v1 필드는 그대로 유지돼야 한다(하위호환).
        for legacy_key in (
            "ts", "source", "query", "category", "category_used", "k", "fallback",
            "raw_max", "confidence", "top", "index_created_at",
        ):
            self.assertIn(legacy_key, e, f"기존 필드 {legacy_key!r} 가 빠졌다")

    def test_hit_level_top_entries_keep_legacy_keys_and_add_status_raw(self) -> None:
        search.search("LOGSCHEMATERM", category="all", k=5, source="test")
        lines = self._read_lines()
        top = lines[0]["top"]
        self.assertTrue(top, "top[] 가 비었다 — 픽스처 재설계 필요")
        for item in top:
            # 기존 계약 — 유지.
            self.assertIn("rel", item)
            self.assertIn("chunk_no", item)
            self.assertIn("score", item)
            # 신규 — 2-6.
            self.assertIn("status", item)
            self.assertIn("raw", item)
            self.assertIsInstance(item["raw"], float)

    def test_seq_increments_within_process(self) -> None:
        search.search("LOGSCHEMATERM", category="all", k=5, source="test")
        search.search("LOGSCHEMATERM", category="all", k=3, source="test")
        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        self.assertLess(
            lines[0]["seq"], lines[1]["seq"],
            "seq 가 호출 순서대로 증가하지 않았다",
        )
        # 같은 프로세스 안에서는 run_id/pid 도 동일해야 한다(상관 ID 안정성).
        self.assertEqual(lines[0]["run_id"], lines[1]["run_id"])
        self.assertEqual(lines[0]["pid"], lines[1]["pid"])

    def test_caller_id_passes_through_to_log(self) -> None:
        search.search("LOGSCHEMATERM", category="all", k=5, source="test", caller_id="agent-42")
        lines = self._read_lines()
        self.assertEqual(lines[0]["caller_id"], "agent-42")

    def test_zero_token_query_gets_cached_index_created_at(self) -> None:
        """토큰 0개 조기 반환 경로 — 사전에 정상 검색 1회로 스냅샷을 채운 뒤라면
        index_created_at 이 캐시된 값으로 채워져야 한다(수정 전에는 항상 None 이었다).
        """
        search.search("LOGSCHEMATERM", category="all", k=5, source="test")  # 스냅샷 워밍
        search.search("!!!???", category="all", k=5, source="test")  # 토큰 0개(조기 반환)
        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        zero_tok_entry = lines[1]
        self.assertEqual(zero_tok_entry["confidence"], "none")
        self.assertIsNotNone(
            zero_tok_entry["index_created_at"],
            "토큰 0개 조기 반환인데도 캐시된 스냅샷이 있었으므로 index_created_at 이 채워져야 한다",
        )
        self.assertEqual(zero_tok_entry["index_created_at_source"], "cached_snapshot")
        self.assertEqual(zero_tok_entry["coverage"], 0.0)
        self.assertEqual(zero_tok_entry["n_query_tokens"], 0)
        self.assertEqual(zero_tok_entry["n_matched_tokens"], 0)

    def test_zero_token_query_without_any_prior_snapshot_is_still_none(self) -> None:
        """이 프로세스가 이 색인 세대에 대해 한 번도 검색한 적 없으면(캐시 자체가 없음)
        토큰 0개 경로는 여전히 None + source="unavailable" 이어야 한다(정직한 한계 —
        "캐시가 있으면 채운다"이지 "항상 채운다"가 아니다).
        """
        testutil.reset_search_module_caches()  # 다른 테스트가 남긴 전역 스냅샷 제거
        search.search("!!!???", category="all", k=5, source="test")
        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0]["index_created_at"])
        self.assertEqual(lines[0]["index_created_at_source"], "unavailable")


if __name__ == "__main__":
    unittest.main()
