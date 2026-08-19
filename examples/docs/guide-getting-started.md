# 시작 가이드 (Getting Started)

status: current

온보딩 첫 문서다. 설치 절차와 기본 구조를 다룬다.

## 설치

설치 절차는 아래 순서를 따른다.

1. Python 3.12 이상으로 가상환경(venv)을 만든다.
2. `pip install -r requirements.txt` 로 의존성을 설치한다.
3. `python indexer.py` 로 색인을 만든다.

설치 요구사항: Python 3.12+, 외부 API 키 불필요, 완전 로컬 동작.

## Architecture

The search_engine module builds a BM25 index over markdown chunks.
Each chunk keeps its source file path and line range, so every result
points back to the exact location in the original document.
