"""조각(chunk) → BM25 색인 + SQLite 메타데이터 저장.

CLI 진입점이다 (search.py · mcp_server.py 와 달리 여기는 stdout 사용이 허용된다).

    python indexer.py            # 전체 재색인 + 왕복 검증
    python indexer.py --stats    # 위 + 통계 출력
    python indexer.py --check    # 재색인 없이 "원본이 색인보다 낡았는가"만 판정(4차 배치 B-7)

설계 요점
  · **전체 재색인만 한다.** 증분 색인은 하지 않는다 — 수백 개 규모는 몇 초면 끝나고,
    증분은 "삭제된 문서 · 이동된 조각"까지 추적해야 해서 이 규모에서는 득보다
    복잡도가 크다.
  · **idx 정합이 이 파일의 존재 이유다.** `chunks.sqlite` 의 `chunks.idx` 컬럼은
    bm25s 코퍼스 배열 인덱스와 반드시 같아야 한다 — 이게 어긋나면 검색이 엉뚱한
    문서를 돌려주면서도 예외 하나 던지지 않는다(가장 조용히 고장나는 방식이다).
    그래서 저장 직후 반드시 디스크에서 다시 읽어(load) 자기 자신을 검색해 idx 가
    그대로 돌아오는지 확인한다(`_verify_roundtrip`). 메모리에 남아있는 retriever 로
    검사하면 "저장 자체가 깨졌는지"는 못 잡으므로 save → load 왕복을 반드시 거친다.
    이 검증이 실패하면 통계를 내지 않고 즉시 비정상 종료한다 — 깨진 인덱스를
    통계로 포장하지 않는다.
  · **교체는 3단 rename.** 재색인 중 실패가 기존 인덱스를 반쪽으로 덮어쓰면 안
    되므로, 새 인덱스는 `index.new` 에서 완전히 만들고(bm25s save + sqlite write +
    왕복 검증까지) 검증까지 통과한 뒤에만 기존 `index` 와 자리를 바꾼다. Windows 는
    디렉터리를 `os.replace` 로 덮어쓸 수 없어(대상이 있으면 실패) 3단 rename 을 쓴다.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import bm25s

import chunker
import config
import search
import tokenizer

# ── 검증 파라미터 (지시서 고정값 — 재현 가능해야 하므로 시드 고정) ──
VERIFY_SAMPLE_SIZE = 5
VERIFY_K = 5
VERIFY_SEED = 20260813

# ── 기준선 (직전 정상 색인과의 대조용 — 어긋나도 실패 아님, 급변 감지 참고치) ──
# 아래는 동봉 예제 코퍼스(examples/docs/) 기준값이다. 자기 코퍼스를 처음 색인한 뒤
# --stats 출력값으로 갱신해 두면 이후 재색인 때 문서·조각 수 급변을 눈으로 잡을 수 있다.
BASELINE_DATE = "examples"
BASELINE_DOCS = 3
BASELINE_CHUNKS = 3
BASELINE_STALE_DOCS = 1


def _fix_windows_console() -> None:
    """cp949 콘솔에서 한글 출력이 UnicodeEncodeError 로 죽는 것을 막는다."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# ── 1) 로드 + 청킹 + 토큰화 ──────────────────────────────────
def _load_all_chunks() -> tuple[list[chunker.DocMeta], list[chunker.Chunk]]:
    """모든 대상 문서를 청킹해 (문서메타 리스트, 전체 조각 리스트) 로 합친다.

    조각 리스트의 순서가 곧 idx(0-based) 다. bm25s 코퍼스와 sqlite 양쪽이 이
    순서를 그대로 물려받으므로, 여기서 만든 순서를 뒤에서 절대 바꾸면 안 된다.
    """
    docs: list[chunker.DocMeta] = []
    all_chunks: list[chunker.Chunk] = []
    for path in config.source_paths():
        meta, chunks = chunker.chunk_file(path)
        docs.append(meta)
        all_chunks.extend(chunks)
    return docs, all_chunks


def _tokenize_all(chunks: list[chunker.Chunk]) -> tuple[list[list[str]], int]:
    """조각마다 tokenizer.tokenize 적용. 빈 토큰 조각 개수를 함께 센다.

    bm25s 기본 토크나이저(bm25s.tokenize)는 쓰지 않는다 — 한글을 깨고
    "D-52" 같은 식별자를 "52" 로 뭉갠다(실측 확인됨). 우리 tokenizer.tokenize 로
    미리 토큰화한 list[list[str]] 를 그대로 bm25s.index() 에 넣는다.
    """
    corpus_tokens: list[list[str]] = []
    n_empty = 0
    for c in chunks:
        toks = tokenizer.tokenize(c.text)
        if not toks:
            n_empty += 1
        corpus_tokens.append(toks)
    return corpus_tokens, n_empty


# ── 2) bm25s 색인 ────────────────────────────────────────────
def _build_bm25(corpus_tokens: list[list[str]], bm25_dir: Path) -> None:
    retriever = bm25s.BM25()
    retriever.index(corpus_tokens, show_progress=False)
    retriever.save(str(bm25_dir))  # bm25s 가 bm25_dir 자체를 만든다 — 미리 mkdir 불필요


# ── 3) SQLite 저장 ───────────────────────────────────────────
_SCHEMA_SQL = """
CREATE TABLE docs(rel TEXT PRIMARY KEY, title TEXT, category TEXT,
  status TEXT, status_evidence TEXT, n_lines INT, n_chars INT);
CREATE TABLE chunks(
  idx INTEGER PRIMARY KEY,
  rel TEXT, chunk_no INT, heading_path TEXT, text TEXT,
  start_line INT, end_line INT, category TEXT, status TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX ix_chunks_rel ON chunks(rel);
CREATE INDEX ix_chunks_category ON chunks(category);
"""


def _write_sqlite(
    sqlite_path: Path,
    docs: list[chunker.DocMeta],
    chunks: list[chunker.Chunk],
) -> None:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO docs(rel, title, category, status, status_evidence, n_lines, n_chars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (d.rel, d.title, d.category, d.status, d.status_evidence, d.n_lines, d.n_chars)
                for d in docs
            ],
        )
        conn.executemany(
            "INSERT INTO chunks(idx, rel, chunk_no, heading_path, text, "
            "start_line, end_line, category, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    i, c.rel, c.chunk_no, c.heading_path, c.text,
                    c.start_line, c.end_line, c.category, c.status,
                )
                for i, c in enumerate(chunks)  # enumerate 순서 == bm25s 코퍼스 idx
            ],
        )
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("created_at", datetime.now(timezone.utc).isoformat()),
                ("n_docs", str(len(docs))),
                ("n_chunks", str(len(chunks))),
                ("docs_root", str(config.DOCS_ROOT)),
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ── 4) 안전한 교체 (3단 rename) ──────────────────────────────
def _cleanup_stale_dirs(*dirs: Path) -> None:
    """이전 실행이 비정상 종료해 남긴 .new/.old 잔재를 정리한다."""
    for d in dirs:
        if d.exists():
            print(f"[정리] 잔재 디렉터리 삭제: {d}")
            shutil.rmtree(d)


def _swap_in(final_dir: Path, new_dir: Path, old_dir: Path) -> None:
    """new_dir 를 final_dir 자리로 3단 rename 한다.

    Windows 는 대상이 이미 있으면 os.replace 로 디렉터리를 덮어쓸 수 없다.
    그래서 기존 것을 old 로 비켜준 뒤 new 를 final 자리로 옮기고 old 를 지운다.
    두 번째 rename 이 실패하면(디스크 문제 등) old 를 되돌려 최대한 이전
    상태를 보존한 뒤 예외를 다시 던진다.
    """
    moved_old = False
    if final_dir.exists():
        os.rename(final_dir, old_dir)
        moved_old = True
    try:
        os.rename(new_dir, final_dir)
    except OSError:
        if moved_old:
            os.rename(old_dir, final_dir)
        raise
    if moved_old:
        shutil.rmtree(old_dir)


# ── 5) 왕복 검증 ─────────────────────────────────────────────
def _verify_roundtrip(
    bm25_dir: Path,
    sqlite_path: Path,
    corpus_tokens: list[list[str]],
    chunks: list[chunker.Chunk],
) -> tuple[bool, str]:
    """저장한 것을 디스크에서 다시 읽어(load) idx 정합을 확인한다.

    불변식: 조각 idx 의 토큰으로 검색하면 그 조각 자신이 1위로 나와야 한다.
    그 1위 idx 로 sqlite 를 조회하면 bm25s 코퍼스의 같은 위치와 같은 조각이
    나와야 한다(bm25s idx ↔ sqlite idx 정합). text 는 앞부분만이 아니라
    전체를 비교한다(스펙의 "앞부분 확인"보다 엄격하게 — 비용이 들지 않는
    강화이므로 낮추지 않았다). 진단 출력에는 가독성을 위해 앞부분만 찍는다.
    """
    n = len(chunks)
    retriever = bm25s.BM25.load(str(bm25_dir))

    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row

    rng = random.Random(VERIFY_SEED)
    population = [i for i in range(n) if corpus_tokens[i]]  # 빈 토큰 조각은 자기검색 불가 → 제외
    n_excluded = n - len(population)
    sample_size = min(VERIFY_SAMPLE_SIZE, len(population))
    sample_idxs = sorted(rng.sample(population, sample_size)) if sample_size else []

    header = (
        f"[검증] 무작위 표본 {sample_size}개 (시드={VERIFY_SEED}) · "
        f"빈 토큰 조각 {n_excluded}개는 자기검색이 불가능해 후보에서 제외"
    )

    if sample_size == 0:
        conn.close()
        return False, header + "\n[검증 실패] 검증 가능한(비어있지 않은) 조각이 0개다."

    rows: list[dict] = []
    for idx in sample_idxs:
        target = chunks[idx]
        k = min(VERIFY_K, n)
        hit_idx_arr, hit_score_arr = retriever.retrieve(
            [corpus_tokens[idx]], k=k, show_progress=False
        )
        hit_ids = [int(x) for x in hit_idx_arr[0]]
        hit_scores = [float(x) for x in hit_score_arr[0]]

        top_idx = hit_ids[0]
        top_score = hit_scores[0]
        top_chunk = chunks[top_idx]

        bm25_ok = top_idx == idx
        target_rank = hit_ids.index(idx) if idx in hit_ids else None
        target_score = hit_scores[target_rank] if target_rank is not None else None

        row = conn.execute(
            "SELECT rel, chunk_no, text FROM chunks WHERE idx = ?", (top_idx,)
        ).fetchone()
        if row is None:
            sqlite_ok = False
            sqlite_detail = f"idx={top_idx} 가 chunks 테이블에 없음"
        else:
            sqlite_ok = (
                row["rel"] == top_chunk.rel
                and row["chunk_no"] == top_chunk.chunk_no
                and row["text"] == top_chunk.text
            )
            sqlite_detail = "OK" if sqlite_ok else (
                f"불일치 — sqlite(rel={row['rel']!r}, chunk_no={row['chunk_no']}, "
                f"text[:40]={row['text'][:40]!r}) vs 기대(rel={top_chunk.rel!r}, "
                f"chunk_no={top_chunk.chunk_no}, text[:40]={top_chunk.text[:40]!r})"
            )

        rows.append(dict(
            idx=idx, rel=target.rel, chunk_no=target.chunk_no,
            top_idx=top_idx, top_rel=top_chunk.rel, top_chunk_no=top_chunk.chunk_no,
            top_score=top_score, target_rank=target_rank, target_score=target_score,
            n_hits=len(hit_ids), sqlite_detail=sqlite_detail,
            ok=(bm25_ok and sqlite_ok),
        ))

    conn.close()

    all_ok = all(r["ok"] for r in rows)
    lines = [header]

    if all_ok:
        lines.append("[검증] 통과 — bm25s idx ↔ sqlite 정합 확인됨")
        for r in rows:
            lines.append(
                f"  idx={r['idx']:>5} [{r['rel']}#{r['chunk_no']}] -> "
                f"1위 idx={r['top_idx']} score={r['top_score']:.4f} · sqlite {r['sqlite_detail']}"
            )
    else:
        lines.append("[검증 실패] 아래 상세를 보고 원인을 판단할 것 — 기준을 임의로 낮추지 않는다")
        for r in rows:
            if r["ok"]:
                continue
            lines.append(f"  --- 실패 표본 idx={r['idx']} ---")
            lines.append(f"    기대(target) : idx={r['idx']} rel={r['rel']} chunk_no={r['chunk_no']}")
            lines.append(
                f"    반환 1위     : idx={r['top_idx']} rel={r['top_rel']} "
                f"chunk_no={r['top_chunk_no']} score={r['top_score']:.4f}"
            )
            if r["target_rank"] is not None:
                lines.append(f"    대상 순위    : {r['target_rank'] + 1}위 (score={r['target_score']:.4f})")
            else:
                lines.append(f"    대상 순위    : top-{r['n_hits']} 밖 (검색 결과에 없음)")
            lines.append(f"    sqlite 정합  : {r['sqlite_detail']}")

    return all_ok, "\n".join(lines)


# ── 6) 통계 ──────────────────────────────────────────────────
def _format_counter(counter: Counter, order: list[str]) -> str:
    known = [f"{k}={counter.get(k, 0)}" for k in order]
    extra = [f"{k}={v}" for k, v in counter.items() if k not in order]
    return " ".join(known + extra)


def _print_baseline_diff(docs: list[chunker.DocMeta], chunks: list[chunker.Chunk]) -> None:
    """파일 상단 BASELINE_* 기준선과 차이만 보여준다.

    문서는 계속 갱신되므로 차이 자체는 결함 신호가 아니다 — 실패로 처리하지 않는다.
    """
    n_docs = len(docs)
    n_chunks = len(chunks)
    n_stale = sum(1 for d in docs if d.status == "stale")

    def fmt(label: str, base: int, cur: int) -> str:
        delta = cur - base
        if delta == 0:
            return f"{label} {base} (변동 없음)"
        sign = "+" if delta > 0 else ""
        return f"{label} {base} -> {cur} ({sign}{delta})"

    print(f"기준선 대조 ({BASELINE_DATE}):")
    print(f"  {fmt('문서', BASELINE_DOCS, n_docs)}")
    print(f"  {fmt('조각', BASELINE_CHUNKS, n_chunks)}")
    print(f"  {fmt('stale', BASELINE_STALE_DOCS, n_stale)}")


def _print_stats(
    docs: list[chunker.DocMeta],
    chunks: list[chunker.Chunk],
    n_empty: int,
    timings: dict[str, float],
    final_dir: Path,
) -> None:
    print("\n=== 통계 ===")
    print(f"문서 수: {len(docs)}")
    print(f"조각 수: {len(chunks)}")

    lengths = [len(c.text) for c in chunks]
    if lengths:
        print(f"조각 길이: 중앙값 {statistics.median(lengths):,.0f}자 · 최대 {max(lengths):,}자")
    else:
        print("조각 길이: (조각 없음)")

    status_order = ["current", "stale", "deprecated", "unknown"]
    doc_status = Counter(d.status for d in docs)
    chunk_status = Counter(c.status for c in chunks)
    print(f"status 분포 (문서 기준): {_format_counter(doc_status, status_order)}")
    print(f"status 분포 (조각 기준): {_format_counter(chunk_status, status_order)}")

    cat_counter = Counter(c.category for c in chunks)
    print("카테고리 분포 (조각 기준):")
    for cat, n in cat_counter.most_common():
        print(f"  {cat:<10} {n:>5}")

    print(f"빈 토큰 조각 수: {n_empty}")

    print("소요 시간:")
    for label, seconds in timings.items():
        print(f"  {label}: {seconds:.2f}s")

    size_bytes = sum(p.stat().st_size for p in final_dir.rglob("*") if p.is_file())
    print(f"인덱스 디스크 크기: {size_bytes:,} bytes ({size_bytes / 1024 / 1024:.2f} MB)")

    _print_baseline_diff(docs, chunks)


# ── --check (4차 배치 B-7/O3 — 재색인 없이 신선도만 판정) ──────────────────────
def _run_check() -> int:
    """`indexer.py --check` 진입점 — 재색인하지 않고 "원본 .md 가 색인보다 새로운가"만
    판정하고 끝낸다(`.claude/skills/rag-reindex/SKILL.md` 1단계가 이 명령을 쓴다).

    O3(2차 배치, 반드시 닫아야 하는 결함) — 이 진입점이 생기기 전(3차 배치 원본)에는
    `indexer.py` 에 argparse 가 없고 `main()` 이 `"--stats" in sys.argv[1:]` 만 봤다 —
    그러면 **`indexer.py --check` 는 "알 수 없는 인자"로 무시돼 그냥 전체 재색인 +
    스왑을 실행했다**(`.claude/skills/rag-reindex/SKILL.md` 는 이미 `--check` 를
    "재색인하지 않고 신선도만 센다"로 안내하고 있었으므로, 문서가 "안 한다"고 적은
    파괴적 동작을 실제로 하는 상태였다). `main()`(아래) 최상단에서 `--check` 를 먼저
    가로채 재색인 분기(청킹→bm25 색인→sqlite 저장→왕복검증→스왑)에 **도달하기 전에**
    `sys.exit()` 하는 것으로 닫는다 — 인자 조합 4종(`--stats`/`--check`/
    `--check --stats`/오타 `--chek`) 실측 결과는 2차 배치 보고 참고.

    `search.compute_staleness()` 를 그대로 재사용한다(요건 B-7/O3 — 로직을 복제하지
    않는다). search.py 가 매 검색 호출마다 내는 경고와 **동일한 판정**이다: M0(2차
    배치)로 `config.source_paths()` 전체 glob 기반이라 수정·추가·삭제 세 축을 모두
    다룬다(1차 배치의 rel-목록-only stat 방식은 신규/삭제를 원리적으로 못 잡는
    결함이 있어 폐기됨 — search.py 모듈 docstring "M0" 절 참고).

    H1(코드리뷰) — 예전에는 이 함수가 `search._get_conn()` 을 열고 그 커넥션을 쥔 채
    `search.check_staleness(conn)` 을 불러 글롭+stat 스캔(중앙값 47ms+9.7ms) 내내 sqlite
    핸들을 쥐고 있었다 — 재색인 직후(가장 흔한 호출 시점) 다른 프로세스의 3단 rename 과
    부딪힐 창을 재도입하는 셈이었다. `compute_staleness()` 는 자기 커넥션을 열어 meta 만
    읽고 즉시 닫은 뒤 스캔한다(스캔 중 핸들 0개) — 그리고 절대 예외를 던지지 않으므로
    (색인 없음·meta 손상 등 모든 실패를 `StalenessCheck(ok=False, ...)` 로 흡수) 이
    함수도 더 이상 conn-open 실패를 따로 잡을 필요가 없다(아래 `check.ok` 분기 하나로
    통일).

    종료코드 규약(이 커맨드가 새로 정의 — 셸/CI/운영 스킬이 그대로 소비한다):
      0 = 원본이 색인보다 안 낡음(재색인 불필요)
      1 = 원본이 색인보다 낡음(재색인 권장) — 이 파일의 기존 실패 종료(청킹 0건·
          왕복검증 실패, 아래 main() 참고)와 같은 "sys.exit(1)=조치 필요" 관례를 따른다.
      2 = 판정 불가(색인 없음·meta 손상 등) — "판정 자체를 못 냈다"는 뜻으로 1(=낡음
          확정)과 구분한다. 0/1 은 불리언 판정, 2 는 판정 실행 자체의 실패라는 점에서
          흔한 CLI 관례(예: `git diff --exit-code` 의 0/1 과 별개로 실행 오류는 다른
          코드를 쓰는 관례)와 같은 축이다.

      **M6(코드리뷰) — 이 2 는 argparse 의 "미지 인자" usage error 종료코드(2)와 겹친다
      (의도적, 되돌리지 말 것).** 근거: ① 둘 다 "이 실행은 판정을 내지 못했다"는 같은
      축이고, 호출자가 능동적으로 분기하는 0(fresh)/1(stale) 과는 절대 혼동되지 않는다
      ② `2`=usage error 는 argparse/POSIX 관례라 바꾸면 오히려 놀랍다 ③ 둘은 stderr 의
      `usage:` 출력 유무로 구분 가능하다 ④ 소비자(`SKILL.md` 등)는 "0 이면 건너뛴다"만
      보므로 인자 오타는 **안전한 방향**(재색인 쪽)으로 떨어진다.
    """
    ok, _sqlite_path = search.index_files_status()  # O5 — server/mcp_server 와 같은 판정 공유
    if not ok:
        print("[안내] 색인이 없다(또는 불완전하다) — python indexer.py 를 먼저 실행하라.")
        return 2

    check = search.compute_staleness()  # H1 — 스캔 중 sqlite 핸들 0개, 절대 예외 안 던짐

    if not check.ok:
        print(f"[판정 불가] {check.detail}")
        return 2
    if check.stale:
        if check.n_changed_docs > 0:
            print(
                f"[낡음] 색인이 원본보다 낡았다({check.n_changed_docs}개 문서가 색인 이후 "
                f"변경됨, 기준 created_at={check.created_at}). python indexer.py 를 실행하라."
            )
        else:
            print(
                f"[낡음 가능성] {check.detail} (기준 created_at={check.created_at}). "
                f"python indexer.py 를 실행하라."
            )
        return 1
    print(
        f"[신선함] 색인이 원본보다 낡지 않았다(기준 created_at={check.created_at}, "
        f"문서 {check.n_current_docs}개)."
    )
    return 0


# ── 진입점 ───────────────────────────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    """M6(코드리뷰) — 예전엔 `"--check" in sys.argv[1:]`/`"--stats" in sys.argv[1:]` 수동
    파싱이라 인식 못 한 인자(오타 `--chek` 등)가 조용히 무시되고 그냥 전체 재색인이
    돌았다. `argparse` 로 바꿔 미지 인자는 exit(2) 로 거절한다(argparse 기본 동작 —
    stderr 에 usage 를 남긴다). `--check`·`--stats` 두 플래그만 정의하고, 둘을 같이
    주면 `main()` 이 `--check` 를 먼저 가로채는 기존 동작을 그대로 유지한다.
    """
    parser = argparse.ArgumentParser(
        prog="indexer.py",
        description="조각(chunk) → BM25 색인 + SQLite 메타데이터 저장(전체 재색인).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="재색인 없이 원본이 색인보다 낡았는지만 판정한다(종료코드 0/1/2, 재색인 안 함)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="재색인 후 통계를 출력한다(--check 와 같이 주면 --check 가 먼저 가로챈다)",
    )
    return parser


def main() -> None:
    _fix_windows_console()
    args = _build_arg_parser().parse_args()  # M6 — 미지 인자는 여기서 exit(2)
    if args.check:
        sys.exit(_run_check())
    show_stats = args.stats

    final_dir = config.INDEX_DIR
    new_dir = final_dir.with_name(final_dir.name + ".new")
    old_dir = final_dir.with_name(final_dir.name + ".old")

    _cleanup_stale_dirs(new_dir, old_dir)

    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    try:
        docs, chunks = _load_all_chunks()
        t_chunk = time.perf_counter()
        timings["청킹"] = t_chunk - t_start
        if not chunks:
            print("[오류] 색인 대상 조각이 0개다 — config.source_paths() 결과를 확인하라.", file=sys.stderr)
            sys.exit(1)
        print(f"[1/4] 청킹 완료 — 문서 {len(docs)}개 · 조각 {len(chunks)}개 ({timings['청킹']:.2f}s)")

        corpus_tokens, n_empty = _tokenize_all(chunks)
        t_tok = time.perf_counter()
        timings["토큰화"] = t_tok - t_chunk
        print(f"[2/4] 토큰화 완료 — 빈 토큰 조각 {n_empty}개 ({timings['토큰화']:.2f}s)")

        new_dir.mkdir(parents=True, exist_ok=True)

        bm25_dir = new_dir / "bm25"
        _build_bm25(corpus_tokens, bm25_dir)
        t_bm25 = time.perf_counter()
        timings["bm25s 색인"] = t_bm25 - t_tok
        print(f"[3/4] bm25s 색인 완료 ({timings['bm25s 색인']:.2f}s)")

        sqlite_path = new_dir / "chunks.sqlite"
        _write_sqlite(sqlite_path, docs, chunks)
        t_sqlite = time.perf_counter()
        timings["SQLite 쓰기"] = t_sqlite - t_bm25
        print(f"[4/4] SQLite 저장 완료 ({timings['SQLite 쓰기']:.2f}s)")

        verify_ok, verify_report = _verify_roundtrip(bm25_dir, sqlite_path, corpus_tokens, chunks)
        t_verify = time.perf_counter()
        timings["왕복 검증"] = t_verify - t_sqlite
    except Exception:
        # 예외든 검증 실패든 임시 디렉터리는 남기지 않는다 — 기존 index 는 손대지 않았다.
        if new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)
        raise

    print(verify_report)

    if not verify_ok:
        shutil.rmtree(new_dir, ignore_errors=True)
        print("\n[중단] 왕복 검증 실패 — 기존 index 는 그대로 두고 종료한다.", file=sys.stderr)
        sys.exit(1)

    _swap_in(final_dir, new_dir, old_dir)
    timings["합계"] = time.perf_counter() - t_start

    print(f"\n색인 완료: 문서 {len(docs)}개 · 조각 {len(chunks)}개 -> {final_dir}")

    if show_stats:
        _print_stats(docs, chunks, n_empty, timings, final_dir)


if __name__ == "__main__":
    main()
