# OBS prime

## 패치노트

### 2026-06-27

- GUI 창 크기를 `1280x800` 고정값으로 변경하고 리사이즈를 막았습니다.
- 다크모드/라이트모드 토글을 추가했고 저장 시 다음 실행에도 유지됩니다.
- 홈 대시보드와 좌측 메뉴를 현재 구조에 맞게 정리했습니다.
- 빠른 실행의 자동감지 버튼은 `자동감지 on/off`로 표시됩니다.
- `데이터 베이스` 페이지를 상태 요약, 경로, 가격 정책, 검증/갱신, 폴더 관리 중심으로 개편했습니다.
- `data/market_wiki` 기반 마켓 검색기를 추가했습니다. 홈 화면에서 `홈` 버튼을 한 번 더 누르면 열리는 히든 페이지입니다.
- 마켓 검색기에 로컬 자동완성을 추가했습니다. `item_wiki`를 우선하고, 그 다음 `market_wiki`를 사용합니다.
- 마켓 검색기는 한국어/영어/slug/초성 일부 검색을 지원하며 후보 선택 후 Enter로 바로 검색할 수 있습니다.
- 마켓 검색기는 Warframe Market `ingame` 판매 주문 기준 최저가를 표시합니다.
- 모드/아케인 랭크 검색을 보강했습니다. `/top` 결과에 지정 랭크가 없으면 전체 주문 API로 fallback하여 최대랭크 주문을 찾습니다.
- 성유물 보상 아이템을 마켓 검색기로 조회해 가격이 잡히면 `data/item_wiki`의 `plat`, `plat_display`, `plat_date`를 오늘 기준으로 갱신합니다.

## 개요

Warframe 성유물 보상 화면용 Python/Tkinter OCR 보조툴입니다. 목표는 보상 4칸을 빠르게 읽고 두캇/플래티넘 기준으로 선택 판단을 돕는 것입니다. 한국어 Warframe 클라이언트 기준으로 동작합니다.

게임 메모리 읽기, 엔진 훅, 인젝션, 패킷 검사는 하지 않습니다. OBS WebSocket, 화면/이미지 캡처, Tesseract OCR, 로컬 DB, Warframe Market API만 사용합니다.

## 현재 동작 기준

- GUI는 `run.bat`으로 실행합니다.
- OCR 엔진은 현재 Tesseract 고정입니다.
- OCR 언어는 `kor+eng` 기준입니다.
- OBS WebSocket 연결을 통해 B1~B4 입력 소스 좌표를 받아 OCR 대상으로 씁니다.
- B1~B4 전체 카드 좌표는 OCR 전에 하단 아이템명 영역으로 자동 축소됩니다.
- 출력 소스는 T1~T4이며 같은 번호끼리 매칭됩니다: B1 -> T1, B2 -> T2, B3 -> T3, B4 -> T4.
- T 출력 포맷은 `n Du / n pl` + 아이템명입니다.
- T 출력은 기본적으로 일정 시간 뒤 자동으로 지워져 송출 화면에 오래 남지 않게 합니다.
- `data/item_wiki`가 성유물 보상/두캇/플래티넘 캐시의 주 DB입니다.
- `data/market_wiki`가 전체 거래 가능 아이템 검색용 로컬 인덱스입니다.
- Warframe Market 가격은 거래 가능 아이템만 조회하고, 같은 날짜 캐시가 있으면 재사용합니다.
- Warframe Market live 조회는 초당 2회로 제한합니다. 알려진 초당 3회 제한보다 보수적으로 둡니다.

## GUI 주요 화면

- `홈`: OBS 연결상태, 입력/출력 상태, DB 상태, 자동감지, 핫키, OCR 상태를 보는 대시보드입니다.
- `OBS 연결`: OBS WebSocket host/port/password 설정입니다. 비밀번호는 DPAPI 저장을 사용하고 평문 저장하지 않습니다.
- `OCR / 매칭`: OBS OCR 소스 캡처, 캡처 OCR, 현재 OCR 결과 확인에 사용합니다.
- `입력 / 좌표`: B1~B4 입력 소스와 T1~T4 출력 소스 이름을 관리합니다.
- `오버레이`: 창 오버레이, OBS용 창, 위치 직접 조절, 가로/세로 레이아웃을 관리합니다.
- `데이터 베이스`: `item_wiki`, `market_wiki`, 가격 캐시 상태와 갱신/검증 기능을 관리합니다.
- `마켓 검색기`: 홈 상태에서 `홈` 버튼을 한 번 더 누르면 열립니다. 한국어 검색, 자동완성, 랭크별 가격 확인을 지원합니다.

## 주요 명령

```bat
run.bat
run.bat --compilecheck
run.bat --unit-smoke
run.bat --gui-smoke
run.bat --config-check
run.bat --ocr-check
run.bat --mvp-functional
run.bat --obs-saved-functional "debug\20260626-114811\obs_probe_source_5s.png"
```

샘플 검증 명령은 실제 스크린샷이 없으면 `BLOCKED`가 정상입니다.

```bat
run.bat --run-sample-set --samples samples\reward_screens
run.bat --detector-functional --samples samples\reward_screens
run.bat --ocr-functional --samples samples\reward_screens
```

명시적 네트워크/API 점검 및 DB 갱신:

```bat
run.bat --market-api-probe
run.bat --ducat-db-update
run.bat --market-wiki-update
```

`run.bat --market-price-update`는 코드에는 유지되어 있지만 전체 가격 캐시 갱신용 유지보수 명령입니다. 일반 사용 흐름에서는 마켓 검색기와 실사용 OCR 결과가 필요한 항목만 조회하며, 전체 일괄 가격 스캔은 권장하지 않습니다.

## 검증 상태

최근 기준:

- `compilecheck`: PASS
- `gui-smoke`: PASS
- `config-check`: PASS
- 마켓 검색 자동완성: `아케인 핫`, `패리스 프라임` 후보 확인 PASS
- 마켓 검색 최대랭크 fallback: `아케인 핫 샷` rank 5 ingame 판매 주문 조회 PASS
- 저장 OBS 캡쳐 functional: PASS, 4/4 매칭
- 저장 OBS 캡쳐 20회 안정성 루프: 20/20 PASS, 평균 약 1초

자세한 검증 명령은 `docs/verification.md`, 현재 구현 상태는 `docs/status.md`를 봅니다.

## 문서 기준

- `AGENTS.md`: 개발/안전 규칙
- `doctor.md`: 구조 점검과 복구 절차
- `DESIGN.md`: GUI/UX 기준
- `docs/status.md`: 현재 구현 상태
- `docs/verification.md`: 검증 명령과 기대 결과
- `goals.md`: 삭제됨
- `plan.md`: MVP 완료 후 의도적으로 비워둠

## 필요 프로그램

- Python 3.12 호환 런타임 또는 `run.bat`의 Codex 번들 Python
- Tesseract OCR 설치본
- `runtime\tessdata`의 `kor`, `eng`, `osd` 언어 데이터
- OBS Studio + obs-websocket

Python 패키지는 `requirements.txt`, 개발 테스트용 패키지는 `requirements-dev.txt`에 있습니다.

## 배포본

실게임 PC 테스트용 최소 배포본은 `output\OBS-Prime-runtime_yy_mm_dd_###` 폴더 형식으로 생성합니다. 배포본에는 런타임 소스, 설정, `data/item_wiki`, `data/market_wiki`, 기본 fixture, presets, `runtime/tessdata`, 실행 배치 파일, 배포용 README를 포함합니다.

`tests`, `samples`, `debug`, pycache, 기존 market cache, OBS 비밀번호, 개인 로그/스크린샷은 배포본에서 제외합니다.

## 민감 데이터

`debug`, 실제 스크린샷, OCR 원문, 가격 캐시, OBS 연결 정보는 로컬 검증 증거입니다. 외부 공유 전에 반드시 내용 확인이 필요합니다.
