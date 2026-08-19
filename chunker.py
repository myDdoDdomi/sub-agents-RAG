"""마크다운 → 조각(chunk).

설계 요점
  · 헤딩 경계에서 먼저 자르고, 너무 큰 섹션만 크기로 재분할한다.
  · 각 조각 앞에 헤딩 경로를 붙여 조각 단독으로 의미가 통하게 한다.
  · 원문 줄 범위(start_line~end_line)를 기록한다 — 조각이 부족할 때
    에이전트가 Read(offset, limit)로 그 부분만 확대할 수 있어야 한다.
  · 코드펜스 안에서는 자르지 않고, 펜스 안의 '#' 을 헤딩으로 오인하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import config

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class DocMeta:
    rel: str
    title: str
    category: str
    status: str            # current | stale | deprecated | unknown
    status_evidence: str
    n_lines: int
    n_chars: int


@dataclass
class Chunk:
    rel: str
    chunk_no: int
    heading_path: str
    text: str
    start_line: int        # 1-based, 원문 기준
    end_line: int
    category: str
    status: str


# ── 상태 판정 ────────────────────────────────────────────────
def detect_status(lines: list[str]) -> tuple[str, str]:
    """머리말에서 '이 문서 자체가 폐기/대체됨' 신호만 찾는다.

    단순 키워드 스캔은 실제 코퍼스에서 무너진다(원 프로젝트 실측 2026-08-13):
    감사·결정 기록 문서들이 **폐기를 주제로 다루기** 때문에
    "X 폐기", ".gitignore 무효", "자동구독 폐기" 같은 본문 문장에 걸려
    정본 결정 문서까지 강등됐다.

    오탐과 미탐의 비용이 다르다:
      · 오탐 → 정본이 강등된다 (적극적으로 해롭다)
      · 미탐 → 그냥 평소 가중치다 (무해하다)
    그래서 **고정밀·저재현** 쪽으로 설계한다. 애매하면 current 다.
    """
    header = _header_region(lines)
    joined = "\n".join(header)

    for rx, status in _STATUS_PATTERNS:
        for m in rx.finditer(joined):
            # 줄 한참 뒤에서 나온 건 변경이력 본문이다.
            # (줄 길이로 거르면 "[SUPERSEDED — 정본 아님] …" 처럼 선언 뒤에
            #  설명이 길게 붙은 진짜 케이스까지 날아간다 — 실측으로 확인)
            if _col_in_line(joined, m.start()) > STATUS_MAX_COL:
                continue
            if _negated(joined, m.end()):
                continue
            return status, _evidence_line(joined, m.start())

    return "current", ""


_NEGATION_RE = re.compile(r"(하지\s*않|지\s*않|아니|없다|없음|말\s*것)")


def _negated(text: str, end: int) -> bool:
    """매칭 직후 구간에 부정어가 오면 무시한다.

    실측 오탐: "이 문서는 `W1_...`를 **대체하지 않는다**" → '대체' 로 잡혔다.
    한국어는 부정이 뒤에 붙어서, 앞만 보는 패턴은 의미가 정반대로 뒤집힌다.
    """
    return bool(_NEGATION_RE.search(text[end : end + 25]))


# 상태 선언은 줄 앞머리에 온다. 이보다 뒤에서 발견되면 본문으로 본다.
STATUS_MAX_COL = 120


def _header_region(lines: list[str]) -> list[str]:
    """머리말 = 첫 `##` 헤딩 전까지 (최대 15줄)."""
    out: list[str] = []
    for line in lines[:15]:
        if re.match(r"^#{2,6}\s", line):
            break
        out.append(line)
    return out


def _col_in_line(text: str, idx: int) -> int:
    """매칭 지점이 그 줄의 몇 번째 칸인지."""
    return idx - (text.rfind("\n", 0, idx) + 1)


# 고정밀 패턴만. "이 문서가" 폐기·대체됐다는 선언에 가까운 것들.
_STATUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"SUPERSEDED", re.I), "stale"),
    (re.compile(r"정본\s*(이)?\s*아(님|니)"), "stale"),
    # "이 문서는 ~ 대체/폐기" 는 완료형만 받는다. 느슨하게 두면
    # "채택·병합·폐기는 수신 측 판정" 같은 본문에 걸린다(실측 오탐).
    (re.compile(r"이\s*문서(는|가)?\s*[^\n]{0,25}(폐기|대체)(되었|됐|됨|된)"), "deprecated"),
    (re.compile(r"\[\s*폐기\s*\]"), "deprecated"),
    (re.compile(r"^\s*>?\s*⚠️?\s*\**\s*(폐기|DEPRECATED)\b", re.I | re.M), "deprecated"),
    (re.compile(r"^\s*status\s*:\s*(deprecated|archived)", re.I | re.M), "deprecated"),
    (re.compile(r"^\s*status\s*:\s*stale", re.I | re.M), "stale"),
    (re.compile(r"(최신|현행)\s*(문서|정본)\s*(은|는)\s*[`\[]"), "stale"),
]


def _evidence_line(text: str, idx: int) -> str:
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    if end < 0:
        end = len(text)
    return text[start:end].strip()[:200]


def extract_title(lines: list[str], fallback: str) -> str:
    for line in lines[:30]:
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    for line in lines[:30]:
        m = HEADING_RE.match(line)
        if m:
            return m.group(2).strip()
    return fallback


# ── 섹션 분해 ────────────────────────────────────────────────
@dataclass
class _Section:
    heading_path: str
    start_line: int
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def end_line(self) -> int:
        return self.start_line + len(self.lines) - 1


def split_sections(lines: list[str], title: str) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[str] = []          # 레벨별 헤딩 텍스트
    cur: list[str] = []
    cur_start = 1
    cur_path = title
    in_fence = False

    def flush(end_idx: int) -> None:
        if any(l.strip() for l in cur):
            sections.append(_Section(cur_path, cur_start, list(cur)))

    for i, line in enumerate(lines, start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            cur.append(line)
            continue

        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush(i - 1)
            level = len(m.group(1))
            heading = m.group(2).strip()
            del stack[level - 1 :]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(heading)
            cur_path = " > ".join([title] + [s for s in stack if s])
            cur = [line]
            cur_start = i
        else:
            cur.append(line)

    flush(len(lines))
    return sections


# ── 크기 조정 ────────────────────────────────────────────────
TABLE_RE = re.compile(r"^\s*\|")


def _split_large(sec: _Section) -> list[_Section]:
    """MAX 를 넘는 섹션을 쪼갠다.

    이 코퍼스는 표 행이 53.8%, 코드블록이 54.8% 라 **빈 줄이 없는 거대 블록**이
    흔하다. 빈 줄만 경계로 삼으면 4만 자짜리 조각이 그대로 나온다(실측).
    그래서 두 단계로 자른다:
      1) 빈 줄 경계에서 TARGET 이상이면 자른다 (이상적)
      2) 경계가 없어도 MAX 를 넘으면 줄 단위로 강제로 자른다 (안전망)

    강제 분할 시:
      · 표는 헤더 2줄(제목행·구분행)을 다음 조각 앞에 다시 붙인다 — 안 붙이면
        컬럼이 뭐였는지 모르는 숫자 나열이 된다.
      · 코드펜스 안이면 펜스를 닫고 다음 조각에서 다시 연다.
    """
    out: list[_Section] = []
    buf: list[str] = []
    buf_start = sec.start_line
    size = 0
    in_fence = False
    fence_open = ""
    table_header = _table_header(sec.lines)

    def flush(next_offset: int) -> None:
        nonlocal buf, buf_start, size
        if not any(l.strip() for l in buf):
            return
        body = list(buf)
        if in_fence and fence_open:
            body.append(fence_open)          # 잘린 펜스 닫기
        out.append(_Section(sec.heading_path, buf_start, body))

        carry: list[str] = []
        if in_fence and fence_open:
            carry.append(fence_open)         # 다음 조각에서 다시 열기
        elif table_header and TABLE_RE.match(buf[-1] or ""):
            carry.extend(table_header)       # 표 헤더 반복
        else:
            carry.extend(_tail_lines(buf, config.CHUNK_OVERLAP_CHARS))

        buf_start = sec.start_line + next_offset - len([c for c in carry if c in buf])
        buf = carry
        size = sum(len(l) + 1 for l in buf)

    for offset, raw_line in enumerate(sec.lines):
        # 한 줄이 MAX 를 넘는 경우가 실제로 있다 — 변경이력 표의 한 행이
        # 5,000자짜리인 문서가 있다(Phase2_API설계_증분_초안). 줄 단위로는
        # 못 자르므로 공백 경계에서 줄 자체를 토막낸다.
        for line in _wrap_line(raw_line, config.CHUNK_MAX_CHARS):
            if FENCE_RE.match(line):
                if in_fence:
                    in_fence, fence_open = False, ""
                else:
                    in_fence, fence_open = True, line.strip()

            # 붙이기 전에 넘칠지 본다 — 붙인 뒤 확인하면 조각이 최대 2×MAX 까지 커진다
            if buf and size + len(line) + 1 > config.CHUNK_MAX_CHARS:
                flush(offset)

            buf.append(line)
            size += len(line) + 1

            at_blank = (not in_fence) and (not line.strip())
            if at_blank and size >= config.CHUNK_TARGET_CHARS:
                flush(offset + 1)
            elif size >= config.CHUNK_MAX_CHARS:
                flush(offset + 1)            # 안전망 — 경계가 없어도 자른다

    if any(l.strip() for l in buf):
        out.append(_Section(sec.heading_path, buf_start, buf))
    return out or [sec]


def _wrap_line(line: str, limit: int) -> list[str]:
    """limit 를 넘는 한 줄을 공백 경계에서 토막낸다. 넘지 않으면 그대로."""
    if len(line) <= limit:
        return [line]
    out: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = rest.rfind(" ", limit // 2, limit)
        if cut <= 0:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def _table_header(lines: list[str]) -> list[str]:
    """섹션 안 첫 표의 헤더 2줄(제목행 + 구분행)."""
    for i, line in enumerate(lines[:-1]):
        if TABLE_RE.match(line) and set(lines[i + 1].strip()) <= set("|-: "):
            if TABLE_RE.match(lines[i + 1]):
                return [line, lines[i + 1]]
    return []


def _tail_lines(lines: list[str], max_chars: int) -> list[str]:
    keep: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > max_chars:
            break
        keep.insert(0, line)
        total += len(line) + 1
    return keep


def _merge_small(sections: list[_Section]) -> list[_Section]:
    """헤딩만 있고 본문이 짧은 섹션들을 목표 크기까지 합친다."""
    out: list[_Section] = []
    for sec in sections:
        if out:
            prev = out[-1]
            if len(prev.text) + len(sec.text) <= config.CHUNK_TARGET_CHARS:
                merged_lines = prev.lines + sec.lines
                out[-1] = _Section(prev.heading_path, prev.start_line, merged_lines)
                continue
        out.append(sec)
    return out


# ── 진입점 ───────────────────────────────────────────────────
def chunk_file(path: Path) -> tuple[DocMeta, list[Chunk]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    rel = path.relative_to(config.DOCS_ROOT).as_posix()
    lines = raw.splitlines()

    title = extract_title(lines, path.stem)
    status, evidence = detect_status(lines)
    category = config.categorize(rel)

    meta = DocMeta(
        rel=rel, title=title, category=category,
        status=status, status_evidence=evidence,
        n_lines=len(lines), n_chars=len(raw),
    )

    sections = _merge_small(split_sections(lines, title))

    sized: list[_Section] = []
    for sec in sections:
        if len(sec.text) > config.CHUNK_MAX_CHARS:
            sized.extend(_split_large(sec))
        else:
            sized.append(sec)

    chunks: list[Chunk] = []
    for n, sec in enumerate(sized, start=1):
        body = sec.text
        if not body:
            continue
        header = f"[{rel}] {sec.heading_path}"
        chunks.append(Chunk(
            rel=rel, chunk_no=n, heading_path=sec.heading_path,
            text=f"{header}\n{body}",
            start_line=sec.start_line, end_line=sec.end_line,
            category=category, status=status,
        ))
    return meta, chunks
