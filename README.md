# OBS prime

## 패치노트

### 2026-06-29

- 홈 `실행` 버튼 배치를 1줄 `전체 재연결 / 웹소켓 설정 / 좌표입력`, 2줄 `자동 감지 / 오버레이 / 1PC 모드`, 3줄 `1회 작동` 한 칸으로 정리했습니다.
- `1PC 모드`를 추가했습니다. 켜면 T1~T4 OBS 텍스트 출력은 비활성화하고, 일반 오버레이 창만 세로모드/오른쪽 위 기준으로 사용합니다. 끄면 기존 사용자의 T 출력/오버레이 설정으로 복원합니다.
- 자동 감지는 이전 결과가 T 출력 또는 오버레이에 남아 있으면 새 OCR을 시작하지 않습니다. 결과 출력 후 최소 10초 안전 대기를 두어 같은 보상창을 반복 기록하지 않게 했습니다.
- 일반 오버레이 창의 표시 순서를 보정하고, `오버레이 창 위치 초기화` 버튼을 추가했습니다. 현재 모니터 작업 영역의 중앙으로 오버레이 위치를 재설정합니다.
- 오버레이 모드 `콘솔`과 `창 오버레이` 선택값을 보존합니다. 일반 오버레이 테스트/위치 조절/가로세로 전환이 더 이상 임의로 `창 오버레이`로 바꾸지 않습니다.
- DB 갱신 실패 진단 기준을 문서화했습니다. 배포본은 `data/item_wiki`, `data/market_wiki` 로컬 DB를 포함하므로 일반 매칭/검색은 오프라인으로 동작하지만, `두캇 DB 갱신`과 `마켓 Wiki 갱신`은 외부 API 접근과 설치 폴더 쓰기 권한이 필요합니다.
- 새 배포본 생성 시 갱신 중 생긴 `data/item_wiki/*.bak`, `data/market_wiki/*.bak`, 가격 캐시, 보상 이력, debug 로그, 개인 설정은 제외합니다.
- 최신 배포본은 `output\OBS-Prime-runtime_26_06_29_001` 및 `output\OBS-Prime-runtime_26_06_29_001.zip`입니다.

### 2026-06-28

- OCR/출력 지연 기준을 `2.5초 이내 정상 목표`, `5초 이내 hard limit`로 확정했습니다.
- 기본 OCR 엔진을 PaddleOCR v5 Korean으로 전환했습니다. Tesseract는 보조/레거시 fallback으로 유지합니다.
- 검증된 OCR 설치 조합을 `paddleocr==3.7.0`, `paddlepaddle==3.3.1`, `aiohttp==3.9.5`로 고정했습니다.
- `install.bat`은 이제 Tesseract 설치가 아니라 PaddleOCR stack 설치/검사와 `run.bat --ocr-check`를 수행합니다.
- PaddleOCR는 B1~B4 crop을 세로 strip 한 장으로 합쳐 1회 OCR 후 슬롯별로 다시 분배합니다. 기존 4회 순차 OCR보다 인식률/시간이 안정적입니다.
- GUI 시작 후 PaddleOCR 모델을 백그라운드 prewarm하여 첫 `1회 작동`이 모델 로딩 비용을 직접 맞지 않게 했습니다.
- 실사용 반응속도 목표를 2.5초 내 피드백으로 잡고, 선택 가능성을 위해 5초 이내 출력은 hard limit로 유지합니다. 기본값은 OBS WebSocket `3000ms`, OCR `1000ms`, OCR 최소 신뢰도 `0.8`입니다.
- 홈 토글은 `자동 감지 : on/off`, `오버레이 : on/off`처럼 상태를 직접 표시합니다.
- `보상 결과`는 `data/reward_results.json`에 누적 저장하며, `수령`/`판매`/`사용` 자유 메모와 필터를 지원합니다.

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

게임 메모리 읽기, 엔진 훅, 인젝션, 패킷 검사는 하지 않습니다. OBS WebSocket, 화면/이미지 캡처, 로컬 OCR 엔진, 로컬 DB, Warframe Market API만 사용합니다.

## 현재 동작 기준

- GUI는 `run.bat`으로 실행합니다.
- OCR 엔진은 기본 PaddleOCR v5 Korean입니다. Tesseract는 보조/레거시 fallback으로 선택할 수 있습니다.
- PaddleOCR는 PP-OCRv5 Korean 경로를 사용하며, 검증된 설치 조합은 `paddleocr==3.7.0`, `paddlepaddle==3.3.1`, `aiohttp==3.9.5`입니다.
- PaddleOCR 모델은 GUI 시작 후 백그라운드에서 prewarm됩니다. prewarm 후 virtual MVP 샘플 기준 4슬롯 OCR은 약 2.3초대이며, 목표 범위는 2.5초 안쪽입니다.
- OBS WebSocket 연결을 통해 B1~B4 입력 소스 좌표를 받아 그대로 OCR crop 영역으로 씁니다.
- B1~B4는 OBS에서 사용자가 직접 아이템명 영역을 덮도록 맞춥니다. 자동 하단 축소는 기본 비활성화입니다.
- 출력 소스는 T1~T4이며 같은 번호끼리 매칭됩니다: B1 -> T1, B2 -> T2, B3 -> T3, B4 -> T4.
- T 출력 포맷은 `n Du / n pl` + 아이템명입니다.
- T 출력은 기본적으로 일정 시간 뒤 자동으로 지워져 송출 화면에 오래 남지 않게 합니다.
- `보상 결과`는 `data/reward_results.json`에 누적 저장하며, 자동 감지의 같은 4칸 반복 결과는 중복 추가하지 않습니다.
- `data/item_wiki`가 성유물 보상/두캇/플래티넘 캐시의 주 DB입니다.
- `data/market_wiki`가 전체 거래 가능 아이템 검색용 로컬 인덱스입니다.
- 배포본의 로컬 DB는 일반 사용에 충분한 기본 데이터입니다. `두캇 DB 갱신`은 WFCD `Relics.json`과 Warframe Market `v2/items`, `마켓 Wiki 갱신`은 Warframe Market `v2/items` 네트워크 접근과 프로젝트 폴더 쓰기 권한이 필요합니다.
- Warframe Market 가격은 거래 가능 아이템만 조회하고, 같은 날짜 캐시가 있으면 재사용합니다.
- Warframe Market live 조회는 초당 2회로 제한합니다. 알려진 초당 3회 제한보다 보수적으로 둡니다.

## GUI 주요 화면

- `홈`: OBS 연결상태, 입력/출력 상태, DB 상태, 자동감지, 핫키, OCR 상태를 보는 대시보드입니다.
- 홈 `실행` 영역은 1줄 `전체 재연결 / 웹소켓 설정 / 좌표입력`, 2줄 `자동 감지 / 오버레이 / 1PC 모드`, 3줄 `1회 작동` 순서입니다.
- `OBS 연결`: OBS WebSocket host/port/password 설정입니다. 비밀번호는 DPAPI 저장을 사용하고 평문 저장하지 않습니다.
- `OCR / 매칭`: OBS OCR 소스 캡처, 캡처 OCR, 현재 OCR 결과 확인에 사용합니다.
- `입력 / 좌표`: B1~B4 입력 소스와 T1~T4 출력 소스 이름을 관리합니다.
- `오버레이`: 창 오버레이, OBS용 창, 위치 직접 조절, 가로/세로 레이아웃을 관리합니다.
- `1PC 모드`: 홈에서 켜고 끕니다. 켜진 동안은 OBS T 출력 대신 일반 오버레이 창을 사용하며, 세로모드/오른쪽 위 기준으로 고정됩니다.
- `데이터 베이스`: `item_wiki`, `market_wiki`, 가격 캐시 상태와 갱신/검증 기능을 관리합니다.
- `보상 결과`: OCR 보상 이력을 누적 표시합니다. `수령`, `판매`, `사용`은 자유 텍스트 메모이며 입력된 행만 필터링할 수 있습니다.
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

DB 갱신이 다른 PC에서만 실패하면 먼저 `데이터 베이스` 페이지의 `마켓 API 검증 테스트`를 실행하고, 설치 폴더가 쓰기 가능한 일반 폴더인지 확인합니다. `raw.githubusercontent.com`, `api.warframe.market`, Windows `curl.exe`, 백신/OneDrive 보호 폴더 차단이 주요 원인입니다.

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
- OBS Studio + obs-websocket

Python 패키지는 `requirements.txt`, 개발 테스트용 패키지는 `requirements-dev.txt`에 있습니다.
`requirements.txt`는 기본 OCR stack인 PaddleOCR v5 Korean을 포함합니다. `requirements-ocr-paddle.txt`는 PaddleOCR만 복구 설치할 때 쓰는 보조 파일입니다.

새 PC에서는 `install.bat`을 먼저 실행하세요. 이 스크립트는 현재 PC에서 안정 확인한 PaddleOCR stack을 설치하고, 기본 OCR readiness check까지 실행합니다. 첫 실행에서 PP-OCRv5 모델 파일 다운로드가 필요할 수 있습니다.

## 배포본

실게임 PC 테스트용 최소 배포본은 `output\OBS-Prime-runtime_yy_mm_dd_###` 폴더 형식으로 생성합니다. 배포본에는 런타임 소스, 설정, `data/item_wiki`, `data/market_wiki`, 기본 fixture, presets, PaddleOCR 설치 요구사항, 실행/설치 배치 파일, 배포용 README를 포함합니다. `doctor.md`는 개발/복구용 문서라 배포본에는 넣지 않습니다.

`tests`, `debug`, pycache, 기존 market cache, 보상 결과 이력, DB 갱신 `.bak`, OBS 비밀번호, 개인 로그/스크린샷은 배포본에서 제외합니다.

## 민감 데이터

`debug`, 실제 스크린샷, OCR 원문, 가격 캐시, OBS 연결 정보는 로컬 검증 증거입니다. 외부 공유 전에 반드시 내용 확인이 필요합니다.
