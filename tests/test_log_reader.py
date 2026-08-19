"""2-6(qa 독립검증, 2026-08-14) 회귀 — 질의 로그 판독기(log_reader.py).

read-only 판독기 자체의 테스트다: v1/v2 두 세대를 모두 읽는지, 기준선 불순·
index_created_at 결측을 탐지하는지, R-001 두 조건에 대해 "측정 가능/불가능"을
정직하게 보고하는지, 그리고 **로그 파일을 절대 쓰지 않는지**를 확인한다.

실 index/·실 logs/queries.jsonl 은 건드리지 않는다 — 임시 JSONL 파일 + (일부는)
`testutil.IsolatedIndexCase` 격리 색인을 쓴다.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import log_reader

from tests import testutil


def _write_jsonl(path: Path, lines: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            if isinstance(line, str):
                f.write(line + "\n")
            else:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")


class TestLoadEntriesDualSchema(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_logreader_")
        self.log_path = Path(self._tmpdir) / "queries.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_v1_line_has_no_schema_version_key_and_is_tagged_v1(self) -> None:
        """v1 줄(schema_version 키 자체가 없음)을 판독기가 v1 로 식별하는지 — 기존
        339줄과 형태가 같은 최소 재현."""
        v1_entry = {
            "ts": "2026-08-13T00:00:00+00:00", "source": "http", "query": "D-52 결정",
            "category": "all", "category_used": "all", "k": 5, "fallback": False,
            "raw_max": 7.0, "confidence": "ok",
            "top": [{"rel": "a.md", "chunk_no": 1, "score": 1.0}],
            "index_created_at": None,
        }
        _write_jsonl(self.log_path, [v1_entry])
        entries, warnings = log_reader.load_entries(self.log_path)
        self.assertEqual(warnings, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["_schema_version"], 1)
        self.assertNotIn("schema_version", entries[0])  # v1 원본엔 이 키가 없다(변조 안 함 확인)

    def test_v2_line_is_tagged_v2(self) -> None:
        v2_entry = {
            "ts": "2026-08-14T00:00:00+00:00", "source": "mcp", "query": "SSE",
            "category": "all", "category_used": "all", "k": 5, "fallback": False,
            "raw_max": 2.0, "confidence": "ok",
            "top": [{"rel": "b.md", "chunk_no": 1, "score": 1.0, "status": "current", "raw": 2.0}],
            "index_created_at": "2026-08-14T00:00:00+00:00",
            "schema_version": 2, "run_id": "abc123", "pid": 999, "seq": 1,
            "caller_id": None, "coverage": 1.0, "n_query_tokens": 1, "n_matched_tokens": 1,
            "deficient_axis": "", "index_created_at_source": "live",
        }
        _write_jsonl(self.log_path, [v2_entry])
        entries, warnings = log_reader.load_entries(self.log_path)
        self.assertEqual(warnings, [])
        self.assertEqual(entries[0]["_schema_version"], 2)

    def test_mixed_v1_and_v2_both_readable(self) -> None:
        v1_entry = {"ts": "t1", "source": "http", "query": "q1", "top": []}
        v2_entry = {"ts": "t2", "source": "mcp", "query": "q2", "top": [], "schema_version": 2}
        _write_jsonl(self.log_path, [v1_entry, v2_entry])
        entries, warnings = log_reader.load_entries(self.log_path)
        self.assertEqual(warnings, [])
        self.assertEqual([e["_schema_version"] for e in entries], [1, 2])

    def test_malformed_line_is_skipped_with_warning_not_crash(self) -> None:
        good = {"ts": "t1", "source": "http", "query": "q1", "top": []}
        _write_jsonl(self.log_path, [good, "{이건 유효한 JSON 이 아니다", good])
        entries, warnings = log_reader.load_entries(self.log_path)
        self.assertEqual(len(entries), 2, "손상된 줄 때문에 전체 로드가 멈추면 안 된다")
        self.assertEqual(len(warnings), 1)

    def test_missing_log_file_returns_empty_not_error(self) -> None:
        entries, warnings = log_reader.load_entries(self.log_path)  # 아직 파일 없음
        self.assertEqual(entries, [])
        self.assertEqual(warnings, [])

    def test_reader_never_writes_to_log_file(self) -> None:
        """read-only 계약 — summarize() 호출 전후로 로그 파일 내용이 바이트 단위로 동일."""
        v1_entry = {"ts": "t1", "source": "http", "query": "q1", "top": [{"rel": "nope.md", "chunk_no": 1, "score": 1.0}]}
        _write_jsonl(self.log_path, [v1_entry])
        before = self.log_path.read_bytes()
        log_reader.summarize(self.log_path)
        after = self.log_path.read_bytes()
        self.assertEqual(before, after, "판독기가 로그 파일을 수정했다 — read-only 계약 위반")


class TestBaselineImpurityAndMissingCreatedAt(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_logreader2_")
        self.log_path = Path(self._tmpdir) / "queries.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_baseline_impurity_detects_unknown_rel(self) -> None:
        entries = [
            {"_lineno": 1, "ts": "t1", "query": "q1", "top": [{"rel": "known.md", "chunk_no": 1}]},
            {"_lineno": 2, "ts": "t2", "query": "q2", "top": [{"rel": "gone.md", "chunk_no": 1}]},
        ]
        impure = log_reader.find_baseline_impurity(entries, known_rels={"known.md"})
        self.assertEqual(len(impure), 1)
        self.assertEqual(impure[0]["lineno"], 2)
        self.assertEqual(impure[0]["bad_rels"], ["gone.md"])

    def test_baseline_impurity_none_when_no_index_access(self) -> None:
        entries = [{"_lineno": 1, "ts": "t1", "query": "q1", "top": [{"rel": "anything.md", "chunk_no": 1}]}]
        impure = log_reader.find_baseline_impurity(entries, known_rels=None)
        self.assertEqual(impure, [], "known_rels=None(판정 불가)이면 탐지 결과가 없어야 한다(오탐 방지)")

    def test_missing_index_created_at_detected(self) -> None:
        entries = [
            {"_lineno": 1, "_schema_version": 1, "ts": "t1", "index_created_at": None},
            {"_lineno": 2, "_schema_version": 2, "ts": "t2", "index_created_at": "2026-08-14T00:00:00+00:00"},
        ]
        missing = log_reader.find_missing_index_created_at(entries)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["lineno"], 1)

    def test_baseline_impurity_handles_missing_rel_key_without_crash(self) -> None:
        """L6(qa 독립검증 후속, 2026-08-14) — `top[]` 원소에 `rel` 키가 없는 손상 줄
        (`h.get("rel")` 이 `None`)이 섞이면 그 집합에 `None` 과 `str` 이 함께 들어간다.
        수정 전이라면 `sorted({None, "known.md"})` 가 `TypeError` 로 죽었을 것이다.
        """
        entries = [
            {
                "_lineno": 1, "ts": "t1", "query": "q1",
                "top": [
                    {"rel": "gone.md", "chunk_no": 1},
                    {"chunk_no": 2},  # rel 키 자체가 없음(손상 줄 시뮬레이션) → None
                ],
            },
        ]
        # 예외 없이 완료돼야 한다(그 자체가 회귀 방지의 핵심 단언).
        impure = log_reader.find_baseline_impurity(entries, known_rels={"known.md"})
        self.assertEqual(len(impure), 1)
        self.assertIn("gone.md", impure[0]["bad_rels"])
        self.assertIn(None, impure[0]["bad_rels"], "손상 사실(None) 자체는 조용히 걸러지지 않고 남아야 한다")


class TestR001Reports(unittest.TestCase):
    def test_condition1_always_reports_not_measurable(self) -> None:
        """성패 라벨이 없으므로 measurable 은 항상 False 여야 한다(측정 불가를 측정
        가능으로 포장하지 않는다는 브리프 명시 요구)."""
        entries = [
            {"_schema_version": 2, "confidence": "low", "deficient_axis": "coverage<0.75"},
            {"_schema_version": 2, "confidence": "ok", "deficient_axis": ""},
        ]
        report = log_reader.r001_condition1_report(entries)
        self.assertFalse(report["measurable"])
        self.assertIsInstance(report["reason"], str)
        self.assertTrue(report["reason"])

    def test_condition2_not_measurable_when_no_caller_id(self) -> None:
        entries = [{"_schema_version": 2, "caller_id": None, "query": "q1", "seq": 1}]
        report = log_reader.r001_condition2_report(entries)
        self.assertFalse(report["measurable"])
        self.assertEqual(report["with_caller_id"], 0)

    def test_condition2_measurable_when_caller_id_present(self) -> None:
        entries = [
            {"_schema_version": 2, "caller_id": "agent-1", "query": "foo", "seq": 1},
            {"_schema_version": 2, "caller_id": "agent-1", "query": "foo", "seq": 2},  # 재질의(같은 질의 재등장)
            {"_schema_version": 2, "caller_id": "agent-1", "query": "bar", "seq": 3},
        ]
        report = log_reader.r001_condition2_report(entries)
        self.assertTrue(report["measurable"])
        self.assertEqual(report["with_caller_id"], 3)
        self.assertAlmostEqual(report["requery_rate_among_caller_id"], 1 / 3)


class TestKnownCorpusRelsAgainstRealIndex(testutil.IsolatedIndexCase):
    """실제(격리) 색인을 상대로 known_corpus_rels()/summarize() 를 검증한다."""

    CORPUS = {
        "docs/logreader/only_doc.md": "# 로그판독 테스트 문서\n\n## 섹션\n\nLOGREADERTERM 표식.\n",
    }

    def test_known_corpus_rels_matches_indexed_docs(self) -> None:
        rels = log_reader.known_corpus_rels(index_dir=self.index_dir)
        self.assertIsNotNone(rels)
        self.assertIn("docs/logreader/only_doc.md", rels)

    def test_summarize_end_to_end_with_impure_and_v2_lines(self) -> None:
        tmp_log = self.tmp_path / "queries.jsonl"
        v2_ok = {
            "ts": "t1", "source": "test", "query": "q_ok", "top": [
                {"rel": "docs/logreader/only_doc.md", "chunk_no": 1, "score": 1.0, "status": "current", "raw": 3.0}
            ],
            "index_created_at": "2026-08-14T00:00:00+00:00", "schema_version": 2,
            "run_id": "r1", "pid": 1, "seq": 1, "caller_id": None,
            "coverage": 1.0, "n_query_tokens": 1, "n_matched_tokens": 1,
            "deficient_axis": "", "index_created_at_source": "live", "confidence": "ok",
        }
        v2_low = dict(v2_ok, query="q_low", confidence="low", deficient_axis="coverage<0.75",
                      top=[{"rel": "docs/logreader/gone_doc.md", "chunk_no": 1, "score": 1.0,
                            "status": "current", "raw": 3.0}])
        v1_line = {"ts": "t0", "source": "http", "query": "q_v1", "top": [], "index_created_at": None}
        _write_jsonl(tmp_log, [v1_line, v2_ok, v2_low])

        # log_reader.summarize() 는 config.INDEX_DIR 를 통해 색인을 읽는다 —
        # IsolatedIndexCase 가 이미 config.INDEX_DIR 를 이 격리 색인으로 몽키패치해 뒀다.
        report = log_reader.summarize(tmp_log)
        self.assertEqual(report["n_lines"], 3)
        self.assertEqual(report["n_v1"], 1)
        self.assertEqual(report["n_v2"], 2)
        self.assertTrue(report["baseline_impurity"]["measurable"])
        bad_rels_flat = [r for row in report["baseline_impurity"]["impure_lines"] for r in row["bad_rels"]]
        self.assertIn("docs/logreader/gone_doc.md", bad_rels_flat)
        self.assertEqual(len(report["missing_index_created_at"]), 1)  # v1_line 만

    def test_summarize_index_dir_param_overrides_config_index_dir(self) -> None:
        """L5(qa 독립검증 후속, 2026-08-14) — `summarize(log_path, index_dir=...)` 가
        `config.INDEX_DIR` 대신 명시된 색인을 본다. 다른 세대 로그를 다른(빈) 색인과
        비교하면, 로그가 가리키는 rel 이 전부 "코퍼스에 없음"으로 잡혀야 한다(그 로그
        세대와 무관한 config.INDEX_DIR 를 그냥 봤다면 이 격리 코퍼스의 rel 이 우연히
        일치해 impure_lines 가 비었을 수도 있다 — 이 테스트는 명시 지정이 실제로
        먹히는지를 확인한다).
        """
        other_tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_logreader_otheridx_")
        try:
            empty_index_dir = Path(other_tmpdir) / "empty_index"
            # 빈 디렉터리 — known_corpus_rels() 는 chunks.sqlite 가 없으면 None(판정 불가)을
            # 낸다. 그래서 "실제로 다른 색인을 봤는지"는 known_rels 가 None 인지로 확인한다
            # (본문 색인(self.index_dir)을 봤다면 known_rels 는 not None 이었을 것).
            v1_line = {"ts": "t0", "source": "http", "query": "q_v1", "top": [], "index_created_at": None}
            tmp_log = self.tmp_path / "other_index_queries.jsonl"
            _write_jsonl(tmp_log, [v1_line])

            report_default = log_reader.summarize(tmp_log)  # config.INDEX_DIR(=self.index_dir) 사용
            report_other = log_reader.summarize(tmp_log, index_dir=empty_index_dir)

            self.assertTrue(report_default["baseline_impurity"]["measurable"])
            self.assertFalse(
                report_other["baseline_impurity"]["measurable"],
                "index_dir 인자가 실제로 반영되지 않았다 — 여전히 config.INDEX_DIR 를 본 것으로 보인다",
            )
        finally:
            shutil.rmtree(other_tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
