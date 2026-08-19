# API 명세 v0

status: stale

최신 정본은 `api-spec_v1.md` 를 볼 것. 이 파일은 비교·이력용으로만 남긴다.

## 검색

`GET /search?q=` — v0 에는 category·k 파라미터가 없다.

## 요청 한도

요청 한도 정책: 클라이언트당 분당 30회. 한도 초과 시 HTTP 429 를 반환한다.
