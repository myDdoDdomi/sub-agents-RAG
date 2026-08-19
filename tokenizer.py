"""BM25용 토크나이저.

이 저장소 문서의 특성에 맞춘 세 갈래 처리:

  1. 식별자 보존   D-52, D-52d, API-084, FN-041, L01-08, v1.4, POST /courses/recommend
                   → 쪼개면 검색이 죽는다. 통째로 토큰화하고 부분 토큰도 함께 낸다.
  2. 라틴/숫자     user_id → user_id, user, id  (snake_case·camelCase 분해)
  3. 한글          2-gram. SQLite FTS5 trigram 이 3글자 미만을 못 색인해
                   "코스"·"결정"·"인증" 같은 2글자 단어가 통째로 빠지는 문제를 피한다.

문서 색인과 질의에 같은 함수를 쓰되, 질의에는 용어집 확장을 추가로 적용한다.
"""

from __future__ import annotations

import re
from pathlib import Path

# ── 패턴 ────────────────────────────────────────────────────
# D-52 / D-52d / API-084 / FN-041 / C-27 / L01-08 / D37-46
ID_RE = re.compile(r"\b[A-Z]{1,5}-?\d{1,4}[a-zA-Z]?(?:-\d{1,4})?\b")
# v1, v1.4, v0.1.2
VER_RE = re.compile(r"\bv\d+(?:\.\d+)*\b", re.I)
# /courses/recommend, /v1/users/{id}
PATH_RE = re.compile(r"/[a-zA-Z0-9_\-{}]+(?:/[a-zA-Z0-9_\-{}]+)+")
# HTTP 메서드
METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")

LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+")
HANGUL_RUN_RE = re.compile(r"[가-힣]+")
CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

# 한글 조사 — 2-gram 노이즈를 줄이기 위해 런 끝에서만 떼어낸다.
JOSA = ("으로서", "으로써", "에서는", "에게서", "이라고", "라고는",
        "에서", "에게", "으로", "이나", "라도", "만큼", "부터", "까지",
        "와의", "과의", "의", "은", "는", "이", "가", "을", "를", "에", "도", "로", "와", "과", "만")

_GLOSSARY: dict[str, list[str]] | None = None


def _load_glossary() -> dict[str, list[str]]:
    """glossary.tsv → {용어: [동의어...]} 양방향."""
    global _GLOSSARY
    if _GLOSSARY is not None:
        return _GLOSSARY

    path = Path(__file__).resolve().parent / "glossary.tsv"
    table: dict[str, list[str]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            terms = [t.strip() for t in line.split("\t") if t.strip()]
            if len(terms) < 2:
                continue
            for t in terms:
                key = t.lower()
                table.setdefault(key, [])
                for other in terms:
                    if other.lower() != key and other not in table[key]:
                        table[key].append(other)
    _GLOSSARY = table
    return table


def _strip_josa(run: str) -> str:
    """런 끝의 조사를 1회만 떼어낸다. 2글자 이하는 건드리지 않는다."""
    if len(run) <= 2:
        return run
    for j in JOSA:
        if len(run) - len(j) >= 2 and run.endswith(j):
            return run[: -len(j)]
    return run


def _hangul_tokens(run: str) -> list[str]:
    """한글 런 → 2-gram 들 (+ 3글자 이상이면 원형).

    2글자 런은 2-gram 이 곧 원형이므로 한 번만 낸다.
    원형을 따로 또 내면 2글자 단어의 tf 가 2배가 되어 스코어가 뒤틀린다.
    """
    base = _strip_josa(run)
    if len(base) <= 1:
        return [base] if base else []

    out = [base[i : i + 2] for i in range(len(base) - 1)]
    if len(base) >= 3:
        out.append(base)  # 정확 일치 가산점
    return out


def tokenize(text: str) -> list[str]:
    """문서·질의 공통 토크나이저."""
    if not text:
        return []

    tokens: list[str] = []
    work = text

    # 1) 식별자류를 먼저 뽑고 원문에서 마스킹 (뒤 단계가 쪼개지 못하게)
    for rx, lower in ((PATH_RE, True), (ID_RE, False), (VER_RE, True), (METHOD_RE, True)):
        for m in rx.findall(work):
            frag = m if isinstance(m, str) else m[0]
            tokens.append(frag.lower() if lower else frag)
            if rx is PATH_RE:
                tokens.extend(p.lower() for p in frag.split("/") if p)
            elif rx is ID_RE and "-" in frag:
                tokens.append(frag.replace("-", "").lower())
        work = rx.sub(" ", work)

    # 2) 라틴/숫자
    for m in LATIN_RE.findall(work):
        low = m.lower()
        tokens.append(low)
        if "_" in low:
            tokens.extend(p for p in low.split("_") if p)
        elif not m.islower() and not m.isupper():
            parts = CAMEL_RE.findall(m)
            if len(parts) > 1:
                tokens.extend(p.lower() for p in parts)

    # 3) 한글
    for run in HANGUL_RUN_RE.findall(work):
        tokens.extend(_hangul_tokens(run))

    return [t for t in tokens if t]


def tokenize_query(text: str) -> list[str]:
    """질의용 — 용어집으로 양방향 확장한 뒤 토큰화."""
    glossary = _load_glossary()
    tokens = tokenize(text)

    extra: list[str] = []
    seen = set(tokens)
    for t in tokens:
        for syn in glossary.get(t, []):
            for st in tokenize(syn):
                if st not in seen:
                    seen.add(st)
                    extra.append(st)

    # 원문 그대로도 한 번 (띄어쓰기 포함 복합어 대응)
    for syn in glossary.get(text.strip().lower(), []):
        for st in tokenize(syn):
            if st not in seen:
                seen.add(st)
                extra.append(st)

    return tokens + extra


if __name__ == "__main__":
    samples = [
        "D-52 결정에 따른 POST /courses/recommend 응답 스키마",
        "user_id 컬럼 타입이랑 courseEngine 설정",
        "코스 추천 알고리즘을 어떻게 구현하나요",
        "API-084 가명화 드릴다운 FN-041~053",
    ]
    for s in samples:
        print(f"\n입력: {s}")
        print("토큰:", tokenize(s))
