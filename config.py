"""설정 — 이 파일이 이 도구의 공개 설정 표면이다.

문서 저장소 위치는 기본적으로 이 폴더의 부모를 본다.
다른 곳에 두려면 환경변수 DOCS_RAG_ROOT 로 덮어쓴다.

자기 코퍼스에 붙일 때 고칠 것 (흔한 순서):
  1. SOURCE_DIRS      — DOCS_RAG_ROOT 아래에서 색인할 하위 폴더 이름들
  2. CATEGORY_RULES   — 경로 조각 → 카테고리. 여기 안 걸리면 `etc` 로 떨어지고,
                        `etc` 문서는 카테고리 필터로는 영영 못 찾는다.
  3. glossary.tsv     — 질의 확장 동의어 (형식·등재 기준은 그 파일 머리말 참고)
  4. eval.py GOLDEN   — 검증 게이트 골든 질의 (동봉분은 examples/ 예제용)

청킹·검색 상수는 원 프로젝트(한국어+영문 혼합 개발 문서 ~200개, 문서당 평균 ~2만 자)
실측으로 잡은 값이라 대부분 그대로 써도 된다.
"""

from __future__ import annotations

import os
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parent

_env_root = os.environ.get("DOCS_RAG_ROOT")
DOCS_ROOT = Path(_env_root).resolve() if _env_root else RAG_ROOT.parent

# 색인 대상
SOURCE_DIRS = ["docs"]
SOURCE_GLOB = "**/*.md"

# 색인에서 제외할 경로 조각
EXCLUDE_PATTERNS = [
    "/archive/",
    "/node_modules/",
    "/.git/",
]

# 인덱스 저장 위치 (커밋하지 않음)
INDEX_DIR = RAG_ROOT / "index"

# 청킹
CHUNK_TARGET_CHARS = 1800      # 조각 목표 크기
CHUNK_MAX_CHARS = 3000         # 이 이상이면 강제 분할
CHUNK_OVERLAP_CHARS = 200

# 검색
DEFAULT_K = 5
MAX_K = 20
MAX_CHARS_PER_HIT = 2500

# 폐기/stale 판정에 쓰는 머리말 스캔 범위
HEADER_SCAN_CHARS = 1500

DEPRECATED_MARKERS = ["폐기", "deprecated", "사용 중단", "무효"]
STALE_MARKERS = ["stale", "구버전", "이전 버전", "대체됨", "superseded"]

# 검색 시 status 별 점수 배수 (1.0 = 그대로, 0 = 제외)
STATUS_WEIGHT = {
    "current": 1.0,
    "unknown": 1.0,
    "stale": 0.35,
    "deprecated": 0.15,
}

# 카테고리 규칙 — 경로 조각 → 카테고리. 위에서부터 먼저 맞는 것.
# 아래는 동봉 예제 코퍼스(examples/docs/)에 맞춘 데모 규칙이다 — 자기 코퍼스의
# 실제 폴더·파일 명명에 맞게 교체한다(부분문자열 매칭·대소문자 구분 주의).
CATEGORY_RULES: list[tuple[str, str]] = [
    ("api", "api"),
    ("db", "db"),
    ("schema", "db"),
    ("spec", "spec"),
    ("요구사항", "spec"),
    ("guide", "guide"),
    ("가이드", "guide"),
    ("decision", "decision"),
    ("결정", "decision"),
    ("legal", "legal"),
    ("약관", "legal"),
]
DEFAULT_CATEGORY = "etc"

CATEGORY_DESC: dict[str, str] = {
    "api": "엔드포인트, 요청/응답 스펙, 상태코드, 인증",
    "db": "테이블·컬럼·인덱스, 스키마, 데이터 모델",
    "spec": "기능명세, 요구사항, 수용기준, 예외 흐름",
    "guide": "가이드, 온보딩, 사용법, 운영 절차",
    "decision": "확정 결정, 검토·감사 리포트",
    "legal": "약관, 개인정보, 동의 문구",
    "etc": "그 외",
}


def categorize(relative_path: str) -> str:
    for needle, category in CATEGORY_RULES:
        if needle in relative_path:
            return category
    return DEFAULT_CATEGORY


def source_paths() -> list[Path]:
    """색인 대상 .md 경로 목록."""
    out: list[Path] = []
    for d in SOURCE_DIRS:
        base = DOCS_ROOT / d
        if not base.exists():
            continue
        for p in base.glob(SOURCE_GLOB):
            if not p.is_file():
                continue
            rel = "/" + p.relative_to(DOCS_ROOT).as_posix()
            if any(pat in rel for pat in EXCLUDE_PATTERNS):
                continue
            out.append(p)
    return sorted(out)
