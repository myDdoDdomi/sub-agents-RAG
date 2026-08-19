# API 명세 v1 (API Spec v1)

status: current

## API-001 — 문서 검색 (search)

`GET /search?q=&category=&k=`

| 필드 | 타입 | 설명 |
|---|---|---|
| q | string | 질의어 (필수) |
| category | string | 카테고리 필터 (기본 all) |
| k | int | 반환 개수 (기본 5, 최대 20) |

응답: JSON, 상태코드 200. 색인이 없으면 안내 메시지를 반환한다.

## API-002 — 요청 한도 (rate limit)

요청 한도 정책: 클라이언트당 분당 60회. 한도 초과 시 HTTP 429 와 함께
"rate limit exceeded" 에러 코드를 반환한다.

| 에러 코드 | 의미 |
|---|---|
| rate_limit_exceeded | 요청 한도 초과 |
| invalid_query | 질의어 형식 오류 |
| index_missing | 색인 없음 |
