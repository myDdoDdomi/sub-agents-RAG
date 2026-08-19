"""F-1 회귀 — 지시서 §4 최우선 테스트.

"재색인 후 같은 프로세스에서 재질의하면 결과가 바뀌면 안 된다"(정확히는: 결과가
**신 색인 기준으로 정합**해야 한다). 조각 수를 **늘리는** 재색인으로 무음 국면을
재현한다 — 줄이면 기존 `n_missing` 가드가 잡아버려 F-1 이 실제로 있었던 결함을
재현하지 못한다(모듈 docstring "F-1" 절 참고, 3배치를 통과한 이유가 바로 그것).

시나리오:
  v1: b_doc(고유어 F1TERM_BRAVO 반복) · c_doc(F1TERM_CHARLIE) — 2 조각, idx 0=b,1=c.
  같은 프로세스에서 F1TERM_BRAVO 질의 -> b_doc 1위 확인(bm25 idx=0 에 매핑됨을 실측 확인).
  v2: 알파벳상 가장 앞인 a_doc(F1TERM_ALPHA) 를 추가 -> 조각 수 2->3(증가) · 재색인 후
  idx 재매핑(0=a,1=b,2=c, 실측 확인 — scratchpad probe_isolation.py).
  같은 프로세스에서 F1TERM_BRAVO 를 다시 질의 -> 여전히 b_doc 이 1위여야 하고, 그
  본문이 **신 sqlite 의 실제 내용**과 일치해야 한다(구 bm25 순위 그대로 쓰면 idx=0 을
  1위로 반환하는데, 신 sqlite idx=0 은 이제 a_doc 이라 F1TERM_BRAVO 가 전혀 없는
  내용이 나온다 — 그게 이 결함의 무음 증상이다).
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config
import search

from tests import testutil


class TestF1CacheInvalidationRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_docs_root = config.DOCS_ROOT
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_f1_")
        self.tmp_path = Path(self._tmpdir)
        self.docs_root = self.tmp_path / "docs_root"
        self.index_dir = self.tmp_path / "index"

        config.DOCS_ROOT = self.docs_root
        config.INDEX_DIR = self.index_dir
        testutil.reset_search_module_caches()

    def tearDown(self) -> None:
        config.DOCS_ROOT = self._orig_docs_root
        config.INDEX_DIR = self._orig_index_dir
        testutil.reset_search_module_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _sqlite_meta(self) -> dict[str, str]:
        conn = sqlite3.connect(str(self.index_dir / "chunks.sqlite"))
        try:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()

    def test_reindex_with_more_chunks_mid_process_stays_consistent(self) -> None:
        # ── v1: b_doc, c_doc (2 조각) ──────────────────────────────
        v1_corpus = {
            "docs/f1/b_doc.md": (
                "# B 문서\n\n## 섹션\n\n"
                "F1TERM_BRAVO 관련 내용이다. F1TERM_BRAVO 를 한 번 더 반복한다.\n"
            ),
            "docs/f1/c_doc.md": "# C 문서\n\n## 섹션\n\nF1TERM_CHARLIE 관련 내용이다.\n",
        }
        testutil.write_corpus(self.docs_root, v1_corpus)
        exit_code, out = testutil.reindex_in_process()
        self.assertEqual(exit_code, 0, out)

        v1_meta = self._sqlite_meta()
        self.assertEqual(v1_meta["n_chunks"], "2")

        r1 = search.search("F1TERM_BRAVO", category="all", k=5, log=False, source="test")
        self.assertGreaterEqual(len(r1.hits), 1)
        self.assertEqual(r1.hits[0].rel, "docs/f1/b_doc.md")
        self.assertEqual(r1.hits[0].idx, 0)  # 사전조건 — v1 에서 b_doc 이 idx=0 임을 명시 확인

        cache_after_v1_search = search.cache_status()
        self.assertEqual(cache_after_v1_search["reload_count"], 1)
        self.assertEqual(cache_after_v1_search["cached_fingerprint_created_at"], v1_meta["created_at"])

        # ── v2: a_doc 추가(알파벳상 최선두) — idx 재배치 + 조각 수 2->3(증가) ──────
        testutil.write_corpus(self.docs_root, {
            "docs/f1/a_doc.md": "# A 문서\n\n## 섹션\n\nF1TERM_ALPHA 관련 내용이다.\n",
        })
        exit_code2, out2 = testutil.reindex_in_process()
        self.assertEqual(exit_code2, 0, out2)

        v2_meta = self._sqlite_meta()
        self.assertEqual(v2_meta["n_chunks"], "3", "조각 수가 늘지 않았다 — 이 테스트의 전제 위반")
        self.assertNotEqual(
            v2_meta["created_at"], v1_meta["created_at"],
            "재색인했는데 meta.created_at 이 안 바뀌었다(픽스처 이상)",
        )

        # idx 재배치 사전조건 — b_doc 이 이제 idx=1 이어야(0=a_doc) F-1 이 실제로 재현
        # 가능한 구도다(구 bm25 가 idx=0 을 1위로 낸다면, 신 sqlite idx=0 은 a_doc 이라
        # F1TERM_BRAVO 와 무관한 본문이 나온다).
        conn = sqlite3.connect(str(self.index_dir / "chunks.sqlite"))
        conn.row_factory = sqlite3.Row
        try:
            idx_map = {row["rel"]: row["idx"] for row in conn.execute("SELECT idx, rel FROM chunks")}
        finally:
            conn.close()
        self.assertEqual(idx_map["docs/f1/a_doc.md"], 0)
        self.assertEqual(idx_map["docs/f1/b_doc.md"], 1)

        # ── 같은 프로세스에서 재질의 ──────────────────────────────
        r2 = search.search("F1TERM_BRAVO", category="all", k=5, log=False, source="test")
        self.assertGreaterEqual(len(r2.hits), 1, "재색인 후 재질의 결과가 0건이다")
        top = r2.hits[0]

        # 판정 1 — rel/idx 가 신 색인에서 실제로 F1TERM_BRAVO 를 담은 문서(b_doc, idx=1)
        # 를 가리켜야 한다. F-1 결함이 있으면 구 bm25(2조각 공간) 랭킹이 idx=0 을
        # 1위로 내고, 그게 신 sqlite 에서는 a_doc 이 되어 이 assert 가 깨진다.
        self.assertEqual(
            top.rel, "docs/f1/b_doc.md",
            f"1위 결과가 b_doc 이 아니다(rel={top.rel!r}, idx={top.idx}) — 구 bm25 랭킹 + "
            f"신 sqlite 본문이 어긋난 F-1 재발로 의심된다",
        )
        self.assertEqual(top.idx, 1)

        # 판정 2 — 반환된 조각의 본문이 신 색인의 실제 내용과 일치하나(지시서 §4 명시
        # 판정 기준). Hit.text 자체(자기순환 위험)가 아니라 **원본 파일을 직접 읽어**
        # 대조한다.
        src_path = self.docs_root / top.rel
        src_lines = src_path.read_text(encoding="utf-8").splitlines()
        file_slice = "\n".join(src_lines[top.start_line - 1 : top.end_line])
        self.assertIn(
            "F1TERM_BRAVO", file_slice,
            f"반환된 줄범위[{top.start_line}:{top.end_line}] 의 원문 내용에 질의어가 없다 "
            f"— 원문 슬라이스: {file_slice!r}",
        )
        self.assertNotIn("F1TERM_ALPHA", top.text, "b_doc 결과에 a_doc 의 내용이 섞였다")

        # 판정 3 — 캐시가 실제로 무효화(재로드)됐는가.
        cache_after_v2_search = search.cache_status()
        self.assertEqual(
            cache_after_v2_search["reload_count"], 2,
            f"재색인 후 재질의했는데 reload_count 가 안 늘었다(재로드 미발생) — "
            f"실제: {cache_after_v2_search}",
        )

        # 판정 4 — fingerprint 가 갱신되어 신 meta.created_at 과 일치하는가.
        self.assertEqual(
            cache_after_v2_search["cached_fingerprint_created_at"], v2_meta["created_at"],
            "캐시된 fingerprint 가 신 색인의 created_at 과 다르다 — 무효화가 완전하지 않다",
        )
        self.assertEqual(cache_after_v2_search["cached_fingerprint_n_chunks"], "3")
        self.assertEqual(cache_after_v2_search["cached_n_chunks"], 3)


if __name__ == "__main__":
    unittest.main()
