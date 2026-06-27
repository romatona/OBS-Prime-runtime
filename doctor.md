---
title: OBS prime Doctor Guide
scope: project diagnostics and recovery
updated: 2026-06-27
---

# OBS prime Doctor Guide

`doctor.md`는 별도 doctor 기능이 없을 때도 프로젝트 상태를 복구할 수 있게 하는 수동 점검 가이드입니다.

## 1. 현재 기준

- 프로젝트 루트: 배포본 폴더 루트
- GUI 진입점: `run.bat`
- 주 실행 방식: Python/Tkinter desktop utility
- GUI 기본 크기: `1280x800`, 리사이즈 불가
- OCR 엔진: Tesseract 고정
- OCR 언어: `kor+eng`
- 입력 소스: OBS B1~B4
- 출력 소스: OBS T1~T4
- 필수 매핑: B1 -> T1, B2 -> T2, B3 -> T3, B4 -> T4
- 주 성유물/두캇 DB: `data/item_wiki`
- 주 마켓 검색 인덱스: `data/market_wiki`
- 가격 캐시: `data/market_cache/warframe_market_prices.json`
- 가격 정책: Warframe Market `ingame` live lookup + same-day cache fallback
- `goals.md`: 삭제됨
- `plan.md`: MVP 완료 후 의도적으로 빈 파일

## 2. 핵심 구조

```text
OBS B1~B4 source rects
  -> bottom name-band crop
  -> Tesseract OCR
  -> item_wiki match
  -> ducat + market plat lookup
  -> recommendation
  -> T1~T4 text output / overlay / debug artifacts
```

마켓 검색기 구조:

```text
user query
  -> local autocomplete from item_wiki + market_wiki
  -> item_wiki first, market_wiki fallback
  -> Warframe Market ingame sell orders
  -> top endpoint first
  -> full orders fallback when a specific rank has no top result
  -> optional item_wiki plat cache update for ducat items
```

주요 경로:

```text
obs_prime/app.py                 CLI entry
obs_prime/gui/main_window.py     Tkinter GUI and market search UI
obs_prime/app_controller.py      shared pipeline and stage runner
obs_prime/ocr/name_band.py       B-card to item-name OCR rect
obs_prime/matcher/item_matcher.py
obs_prime/data/item_wiki.py      item_wiki refresh and market API probe
obs_prime/data/market_wiki.py    full tradable market index refresh
obs_prime/data/warframe_market.py
obs_prime/data/item_wiki_store.py
obs_prime/obs/websocket_client.py
data/item_wiki/_index.json
data/market_wiki/_index.json
config/default.json
debug/
```

## 3. 1분 점검

```bat
run.bat --compilecheck
run.bat --config-check
run.bat --unit-smoke
run.bat --gui-smoke
run.bat --ocr-check
```

기대:

- `compilecheck passed`
- `config_check.status == PASS`
- `Unit smoke passed`
- `gui create ok`
- `ocr_check.status == ready`
- `available_languages`에 `eng`, `kor`, `osd` 포함

`config-check`에서 `obs_websocket.password_dpapi cannot be decrypted` 경고가 나오면 OBS 비밀번호를 GUI에서 다시 입력하고 저장합니다.

## 4. OBS 저장 캡쳐 재현 점검

네트워크 없이 실제 OBS 캡쳐 기반 OCR/매칭을 재현합니다.

```bat
run.bat --obs-saved-functional "debug\20260626-114811\obs_probe_source_5s.png"
```

기대:

- `OBS saved functional passed`
- B1 포르마 설계도
- B2 패리스 프라임 스트링
- B3 버스튼 프라임 스톡
- B4 듀얼 조런 프라임 핸들
- 4/4 matched
- 5초 미만, 정상 기준 약 1~2초

## 5. 샘플 기반 점검

```bat
run.bat --run-sample-set --samples samples\reward_screens
run.bat --detector-functional --samples samples\reward_screens
run.bat --ocr-functional --samples samples\reward_screens
```

현재 `samples/reward_screens`에 실제 스크린샷이 없으면 `BLOCKED`가 정상입니다. 이 경우 장애가 아니라 검증 증거 부족입니다.

## 6. 네트워크/API 점검

사용자가 명시했거나 DB 갱신 목적일 때만 실행합니다.

```bat
run.bat --market-api-probe
run.bat --ducat-db-update
run.bat --market-wiki-update
```

주의:

- public API를 반복 호출하지 않습니다.
- 가격은 같은 날짜 캐시가 있으면 재사용합니다.
- 일반 사용 흐름은 필요한 항목만 조회합니다.
- `market-price-update` 전체 일괄 가격 캐시 갱신은 유지보수 명령으로만 취급합니다.
- 포르마 설계도처럼 거래 불가 항목은 market lookup 대상이 아닙니다.

## 7. GUI 실전 확인 순서

1. 실행 중인 구버전 GUI가 있으면 종료합니다.
2. `run.bat`으로 다시 실행합니다.
3. 홈에서 `전체 재연결` 또는 `OBS 연결`을 확인합니다.
4. `인풋 좌표 갱신`으로 B1~B4 좌표를 적용합니다.
5. 홈에서 `출력테스트`를 실행해 T1~T4가 반응하는지 확인합니다.
6. `OCR / 매칭`에서 `캡쳐 OCR`을 실행합니다.
7. `1회 작동`, `자동감지 on/off`, 핫키가 같은 pipeline 결과를 내는지 확인합니다.
8. T1~T4가 같은 번호 B 슬롯 결과를 표시하고, clear timeout 뒤 지워지는지 확인합니다.

## 8. 마켓 검색기 점검

마켓 검색기는 홈 화면에서 `홈` 버튼을 한 번 더 누르면 열립니다.

1. 검색창에 `패리스 프라임`을 입력해 두캇 DB 후보가 먼저 뜨는지 확인합니다.
2. 검색창에 `아케인 핫`을 입력해 마켓 Wiki 후보가 뜨는지 확인합니다.
3. `아케인 핫 샷`을 선택하고 `최대 랭크`로 검색합니다.
4. `rank 5`, `ingame`, 최저가와 주문 수가 표시되는지 확인합니다.

마켓 검색 결과에서 성유물 보상 아이템이고 두캇 값이 있으면 해당 `data/item_wiki/*.json`의 `plat`, `plat_display`, `plat_date`가 갱신됩니다.

## 9. 실패 패턴

### OBS screenshot timeout

- `config/default.json`의 `obs_websocket.connect_timeout_ms`가 `5000` 이상인지 확인합니다.
- OBS가 응답 중인지 확인합니다.

### OCR timeout 또는 빈 결과

- `ocr.timeout_ms`가 `2500` 이상인지 확인합니다.
- `runtime\tessdata`에 `kor.traineddata`, `eng.traineddata`, `osd.traineddata`가 있는지 확인합니다.
- B1~B4 좌표가 전체 카드 좌표인지 확인합니다. 이름 band crop은 코드에서 자동 적용됩니다.

### 매칭 실패

- `data/item_wiki/_index.json`이 있는지 확인합니다.
- `run.bat --unit-smoke`가 item_wiki 500개 이상 로드를 통과하는지 확인합니다.
- OCR 원문은 `debug/<run-id>/raw_ocr.txt`에서 확인합니다.

### 마켓 검색 실패

- `data/market_wiki/_index.json`이 있는지 확인합니다.
- `데이터 베이스` 페이지에서 `상태 새로고침`을 누릅니다.
- `마켓 API 검증 테스트`로 API 연결을 확인합니다.
- 특정 랭크 검색이 실패하면 전체 주문 fallback이 동작하는지 확인합니다.
- Warframe Market API timeout 또는 rate limit일 수 있으니 반복 클릭하지 않습니다.

### 가격 누락

- 거래 가능 아이템인지 확인합니다.
- 같은 날짜 cache가 없으면 live lookup이 필요합니다.
- network/API 권한 또는 Warframe Market 응답 실패 시 `market_prices.json`을 확인합니다.

### GUI 변경 미반영

- 실행 중인 `pythonw.exe` GUI는 코드 변경을 자동 반영하지 않습니다.
- 앱을 종료하고 `run.bat`으로 다시 실행합니다.

## 10. 복구 기준 파일

- `README.md`: 현재 사용법과 패치노트
- `AGENTS.md`: 작업 규칙
- `DESIGN.md`: UI/UX 규칙
- `doctor.md`: 복구 절차
- `docs/status.md`: 구현 상태
- `docs/verification.md`: 검증 명령
- `docs/live_validation_20260626.md`: 과거 라이브 검증 기록

## 11. 금지/주의

- 게임 메모리 읽기, 엔진 훅, 인젝션, 패킷 검사는 금지입니다.
- OBS 비밀번호는 평문 저장하지 않습니다. DPAPI 저장만 사용합니다.
- `debug`, OCR 원문, 실제 스크린샷, 가격 캐시는 민감 데이터로 취급합니다.
- 네트워크/API 호출은 명시적으로 실행하고 무한 반복하지 않습니다.

