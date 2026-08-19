"""M6(코드리뷰) — `indexer.py` 의 수동 `sys.argv` 파싱이 미지 인자(오타 `--chek` 등)를
조용히 무시하고 전체 재색인을 실행하던 결함. `argparse` 로 교체해 미지 인자는 exit(2)
로 거절해야 한다.

지시서 §7 대응.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config

from tests import testutil


class TestIndexerCliArgparse(unittest.TestCase):
    CORPUS = {
        "docs/cli/doc_a.md": "# 문서 A\n\n## 섹션\n\nCLIARGPARSETERM 내용이다.\n",
    }

    def setUp(self) -> None:
        self._orig_docs_root = config.DOCS_ROOT
        self._orig_index_dir = config.INDEX_DIR
        self._tmpdir = tempfile.mkdtemp(prefix="docs_rag_test_m6_")
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

    def test_typo_chek_rejected_not_full_reindex(self) -> None:
        """`--chek`(오타)는 종료코드 2 로 거절돼야 하고, 색인이 재생성되면 안 된다.
        수정 전이라면 미지 인자가 조용히 무시돼 전체 재색인이 돌고 exit 0 이라 둘 다
        실패한다.
        """
        before = self._sqlite_meta()

        # argparse 가 stderr 에 쓰는 usage 안내는 테스트 출력에서 억제한다.
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            exit_code, out = testutil.reindex_in_process(["--chek"])

        self.assertEqual(
            exit_code, 2,
            f"--chek(오타) 인자가 종료코드 2 로 거절되지 않았다(exit={exit_code}):\n{out}",
        )

        after = self._sqlite_meta()
        self.assertEqual(
            before["created_at"], after["created_at"],
            "M6 회귀 — --chek 오타로 색인이 재생성됐다(argparse 가 미지 인자를 못 거절함)",
        )
        self.assertIn("usage", stderr_buf.getvalue().lower(), "argparse usage 안내가 stderr 에 없다")

    def test_valid_check_flag_still_works(self) -> None:
        """--check(정상 철자)는 여전히 재색인 없이 판정만 해야 한다(회귀 방지 대조군)."""
        before = self._sqlite_meta()
        exit_code, out = testutil.reindex_in_process(["--check"])
        self.assertEqual(exit_code, 0, f"신선한 색인인데 --check 종료코드가 0 이 아니다:\n{out}")
        after = self._sqlite_meta()
        self.assertEqual(before["created_at"], after["created_at"], "--check 인데 색인이 재생성됐다")


if __name__ == "__main__":
    unittest.main()
