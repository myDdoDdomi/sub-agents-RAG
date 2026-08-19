"""docs-rag-mcp 회귀 스위트.

이 패키지가 임포트되는 순간(= 어떤 실행 방식으로 스위트를 돌리든 — `unittest discover`,
개별 모듈 실행, IDE 러너 등 전부 `tests` 패키지 임포트를 거친다) 질의 로그 스위치를
켠다. 개별 테스트가 깜빡 잊고 안 켜도 실사용 로그(`logs/queries.jsonl`, R-001 판정의
유일한 근거 데이터)가 새지 않게 하는 마지막 방어선이다(지시서 §3).

`DOCS_RAG_LOG_SOURCE` 자체를 검증하는 테스트(tests/test_log_switch.py)는 이 값을
건드리지 않는다 — `search._log_query()` 를 몽키패치된 임시 LOG_PATH 로 직접 호출하면서
그 안에서 `os.environ`/`NO_LOG` 값을 잠깐 바꿔치기하고 반드시 원복한다(개별 테스트가
자기 변경을 책임진다 — 여기서는 기본값만 보장한다).
"""
from __future__ import annotations

import logging
import os

os.environ["DOCS_RAG_NO_LOG"] = "1"

# fastapi.testclient.TestClient 가 이 venv 에서 httpx2(호환 포크)를 쓴다(실측 확인,
# 2026-08-13) — 기본 INFO 레벨이 매 요청마다 한 줄씩 찍어 대량 매트릭스 테스트에서
# 리포트 가독성을 해친다. 순수 로그 억제라 검증 결과에는 영향 없다.
logging.getLogger("httpx2").setLevel(logging.WARNING)
