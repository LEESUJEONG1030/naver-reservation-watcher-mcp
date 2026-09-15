# naver-reservation-watcher-mcp

네이버 예약(식당/카페/호텔 등)의 빈자리를 백그라운드에서 계속 감시하고, 사용자가 지정한
조건이 충족되면 **자동으로 예약**하거나 **알려주고 승인 후 예약**하거나 **알림만** 보내는
시스템입니다. Claude Desktop이 꺼져 있어도 감시는 계속됩니다.

**이 프로젝트는 각자 자신의 PC에 설치해서 자신의 네이버 계정으로 직접 로그인해 쓰는
용도입니다.** 어떤 서버도 사용자의 로그인 정보나 예약 데이터를 수집하지 않습니다 - 코드를
내려받아 실행하는 이 PC 안에만 모든 데이터(로그인 세션, 감시 목록, 예약 기록)가 남습니다.

## 구조

```
Claude Desktop  <--(MCP, stdio)-->  nrw.mcp_server.server   ┐
                                                              ├── 같은 SQLite DB (data/nrw.sqlite3) 공유
Windows 작업 스케줄러 --(상시 실행)--> nrw.watcher.service    ┘
                                          │
                                          └── Playwright persistent profile (data/browser_profile) 로
                                              네이버 로그인 세션을 재사용해 실제 감시/예약 수행
```

- **MCP 서버**는 브라우저를 직접 조작하지 않습니다. `check_availability`/`reserve_now`/
  `approve_reservation` 은 SQLite `jobs` 테이블에 작업을 넣고, watcher가 처리한 결과를
  기다려서 돌려주는 얇은 클라이언트입니다. (Playwright persistent profile은 한 번에 하나의
  프로세스만 열 수 있기 때문입니다.)
- **watcher 서비스**가 실제로 Playwright로 브라우저를 열어 감시/예약을 수행하는 유일한
  프로세스입니다. Claude Desktop과 별개로, Windows 작업 스케줄러에 등록되어 로그온 시
  자동으로 계속 실행됩니다.
- 감시 내역, 예약 결과, 작업 큐, 이벤트 로그는 모두 `data/nrw.sqlite3` (SQLite, WAL 모드)에
  저장됩니다. 이 `data/` 폴더는 설치 후 각 사용자의 PC에서 자동으로 새로 생성되며,
  **저장소에 포함되어 있지도, 커밋되지도, 어디로도 전송되지도 않습니다.**

## 설치

```powershell
git clone https://github.com/LEESUJEONG1030/naver-reservation-watcher-mcp.git
cd naver-reservation-watcher-mcp
powershell -ExecutionPolicy Bypass -File scripts\setup_venv.ps1
```

가상환경 생성, 의존성 설치, Playwright Chromium 다운로드까지 한 번에 진행합니다. 이후
`data/` 디렉터리(SQLite DB, 로그, 브라우저 프로필)는 이 프로젝트 폴더 안에 자동으로
생성됩니다 - 설정 없이 그대로 써도 되고, `NRW_DATA_DIR` 환경변수로 다른 위치를 지정할
수도 있습니다.

> Windows 전용입니다 (Task Scheduler, `win11toast` 알림에 의존). Playwright/Python 부분
> 자체는 크로스플랫폼이지만, 지금은 Windows에서의 상시 실행 경로만 만들어졌습니다.

## 1) 네이버 로그인 (최초 1회, 반드시 직접 해야 하는 단계)

비밀번호는 코드/설정 어디에도 저장하지 않습니다. Playwright persistent profile을 이용해
로그인 세션(쿠키)만 이 PC의 `data/browser_profile` 에 재사용합니다 - 이 폴더는 다른 곳으로
복사/전송/커밋되지 않아야 합니다(`.gitignore`에 이미 포함되어 있습니다).

```powershell
.venv\Scripts\python.exe scripts\login_naver.py
```

열리는 브라우저 창에서 직접 로그인(2단계 인증 포함)하면, 로그인 완료를 자동으로 감지해
세션을 저장합니다. watcher가 실행 중이어도 실행할 수 있습니다.

## 2) watcher 서비스 실행

수동으로 (디버깅용, 창을 띄워 로그를 바로 보고 싶을 때):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_watcher.ps1
```

**재부팅해도 자동으로 계속 실행**되게 하려면 Windows 작업 스케줄러에 등록:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task_scheduler.ps1
```

- 이 사용자로 로그온할 때 자동 시작, 실패 시 최대 999회까지 1분 간격으로 재시작하도록
  등록됩니다. 시스템에 영구적으로 남는 작업이므로 필요 없어지면
  `scripts\unregister_task_scheduler.ps1` 로 제거하세요.
- 로그는 `data\logs\watcher.log` 에 남습니다.

## 3) Claude Desktop에 MCP 서버 연결

Claude Desktop 버전/설치 방식에 따라 두 가지 방법 중 하나를 씁니다. **Extensions 설치
UI가 있으면 방법 A**를, 그 메뉴가 없거나 동작하지 않으면 **방법 B**를 쓰세요 - 실제로
Microsoft Store(MSIX)로 설치된 Claude Desktop 일부 버전에는 방법 A의 메뉴 자체가 없는
경우가 있었고, 그때는 방법 B로 정상적으로 연결됐습니다.

### 방법 A: `.mcpb` Desktop Extension 설치 (Extensions 설치 UI가 있는 경우)

1. `mcpb/` 폴더가 `manifest.json` + `server/main.py`(얇은 launcher)로 구성되어 있습니다.
   패키징하려면:
   ```powershell
   npx --yes @anthropic-ai/mcpb validate mcpb\manifest.json
   npx --yes @anthropic-ai/mcpb pack mcpb dist\naver-reservation-watcher-mcp.mcpb
   ```
2. Claude Desktop에서 **Settings → Extensions → Advanced settings → Install Extension…**
   로 방금 만든 `.mcpb` 파일을 설치합니다.
3. 설치 시 "프로젝트 설치 경로" 설정에 **이 프로젝트를 클론/설치한 실제 폴더**를 직접
   지정합니다. 이 확장은 그 경로의 `.venv\Scripts\python.exe` 로 실행되고, 같은 경로의
   `data\nrw.sqlite3` 를 watcher와 공유합니다 - 확장 자체는 Playwright나 watcher를 직접
   실행하지 않고, 이미 실행 중인 watcher와 SQLite `jobs`/`watches` 테이블을 통해서만
   통신하는 얇은 MCP 인터페이스입니다.
4. 일반 채팅에서 `add_reservation_watch` 등 7개 도구가 보이는지 확인합니다.

### 방법 B: 로컬 MCP 서버 직접 등록 (Extensions 설치 UI가 없는 경우)

일부 Claude Desktop 설치(특히 Microsoft Store/MSIX 버전)는 **Settings → Extensions**
메뉴 자체가 없을 수 있습니다. 이 경우 **Settings → Developer(개발자) → Local MCP
servers(로컬 MCP 서버) → Edit Config(구성 편집)** 로 들어가면 열리는 설정 파일에
`mcpServers` 항목을 직접 추가합니다.

`<프로젝트경로>` 는 이 프로젝트를 클론/설치한 **본인의 실제 폴더 경로**로 바꿔주세요
(예: `C:\Users\you\naver-reservation-watcher-mcp-oss`). 아래는 형태를 보여주는
예시이며, 실제 값은 각자 환경에 맞게 채워야 합니다:

```json
"mcpServers": {
  "naver-reservation-watcher-mcp": {
    "command": "<프로젝트경로>\\.venv\\Scripts\\python.exe",
    "args": ["-m", "nrw.mcp_server.server"],
    "cwd": "<프로젝트경로>"
  }
}
```

기존에 다른 MCP 서버 항목이 이미 있다면 `mcpServers` 객체 안에 이 항목만 추가하고
나머지는 그대로 두세요. 저장 후 Claude Desktop을 재시작하면 방법 A와 동일하게
`add_reservation_watch` 등 7개 도구가 보여야 합니다.

두 방법 모두 이 MCP 서버는 Playwright나 watcher를 직접 실행하지 않습니다 - 이미
실행 중인 watcher와 SQLite `jobs`/`watches` 테이블을 통해서만 통신하는 얇은
인터페이스입니다. (1)/(2) 단계를 먼저 완료해 watcher가 실행 중이어야 합니다.

## 사용 예시 (Claude 채팅에서)

- "OO식당 10월 3일 저녁 6시~8시, 2명, 19시 우선으로 감시해줘. 자리 나면 자동으로 예약해줘."
  → `add_reservation_watch(store_name="OO식당", target_date="2026-10-03", time_min="18:00", time_max="20:00", party_size=2, time_priority=["19:00"], mode="AUTO")`
- "지금 감시 목록 보여줘" → `list_reservation_watches()`
- "지금 예약 가능한 시간 있어?" → `check_availability(watch_id=...)`
- "지금 바로 19시로 예약해줘" → `reserve_now(watch_id=..., time="19:00")`
- "실제로 예약하기 전에 흐름만 점검해줘 (클릭은 하지 마)" → `reserve_now(watch_id=..., time="19:00", dry_run=True)`
- "그 예약 진행해줘" (ASK 알림을 받은 뒤) → `approve_reservation(watch_id=..., approve=True)`
- "예약 상태 어때?" → `reservation_status(watch_id=...)`

## 예약 방식 (mode)

- `AUTO`: 조건(시간대/우선순위)에 정확히 맞는 자리가 나오면 자동으로 예약을 진행합니다.
  단, **결제/선결제/보증금/취소수수료/마케팅성 동의가 필요하면 절대 자동으로 진행하지
  않고** `AWAITING_APPROVAL` 상태로 전환해 사용자에게 알립니다. 예약 완료에 필수인 일반
  이용약관/개인정보 동의(화면에 "필수"로 명확히 표시된 것만)는 자동으로 체크합니다 -
  아래 "필수 동의 자동 처리" 참고.
- `ASK`: 자리를 발견하면 "OO식당 10/3 19:00 2명 자리가 생겼습니다. 예약할까요?" 알림을
  Windows 토스트로 보냅니다. 토스트에는 **예약하기 / 이번 자리 넘기기** 버튼이 있어
  Claude Desktop을 열지 않아도 바로 승인/거절할 수 있습니다. 채팅에서
  `approve_reservation` 으로도 동일하게 처리할 수 있습니다. **승인 즉시 클릭하지
  않습니다** - 예약을 시도하기 직전에 그 슬롯이 여전히 유효한지 다시 조회하고, 이미
  사라졌다면 예약을 진행하지 않고 `WATCHING` 상태로 돌아가 사용자에게 알립니다.
- `NOTIFY`: 알림만 보내고 예약은 하지 않습니다.

**중복 알림 방지**: 같은 시간대가 계속 열려 있는 동안은 같은 알림을 반복해서 보내지
않습니다. 다만 그 시간이 마감되었다가 취소 등으로 다시 열리면(재오픈), 새로운 발견으로
간주해 다시 알릴 수 있습니다.

## 안전장치

- CAPTCHA, 추가 본인인증, 비밀번호 재입력이 감지되면 절대 우회하지 않고 `NEEDS_HUMAN`
  상태로 전환해 사용자에게 알리고 대기합니다. `NEEDS_HUMAN` 상태인 동안은 자동 감시를
  완전히 일시 중지합니다 - 사용자가 직접 확인을 완료한 뒤 Claude에게 "다시 확인해줘"
  (=`check_availability(watch_id=...)`)라고 요청하면 감시가 재개됩니다.
- 결제/선결제/보증금/취소수수료/마케팅성 동의 화면이 보이면 `AWAITING_APPROVAL` 로
  전환하고 절대 자동으로 결제/동의하지 않습니다.
- **필수 동의 자동 처리 (AUTO_ACCEPT_REQUIRED_TERMS)**: 예약 완료에 반드시 필요한, 화면에
  "필수"로 명확히 표시된 이용약관/개인정보 동의 체크박스만 자동으로 체크합니다.
  마케팅 수신/알림받기/광고성/멤버십 가입 등은 필수로 표시돼 있어도 절대 자동 체크하지
  않습니다. 필수/선택 여부를 확실히 판별할 수 없는(새로운/다른 형태의) 동의 항목을
  만나면 추측하지 않고 `NEEDS_HUMAN` 으로 멈춥니다. 자동으로 체크한 항목 이름은 이벤트
  로그와 알림에 남습니다.
- 실제 예약 버튼을 누르기 직전, 화면에 표시된 날짜/시간/인원을 다시 읽어 요청과 다르면
  중단합니다 (`reserve_now`, ASK 승인, AUTO 모두 동일하게 적용).
- 예약 성공 시 업체명/날짜/시간/인원/예약번호(확인 가능한 경우)/적용한 쿠폰/자동 체크한
  필수 동의 항목을 이벤트 로그와 Windows 토스트에 남기고 해당 감시를 자동 종료합니다.
  실패해도 원인을 명확히 남기고, 재시도 가능한 실패(검증 실패/슬롯 선점 등)는 감시를
  계속합니다.
- `reserve_now(..., dry_run=True)`: 실제 예약과 완전히 동일한 절차(날짜/인원/시간 선택,
  필수 동의 자동 체크, 쿠폰 확인, 안전 게이트 확인, 클릭 직전 재검증)를 그대로 수행하되
  **최종 확인 버튼은 절대 클릭하지 않고** 어디까지 도달했는지, 어떤 버튼을 누르게
  됐을지를 보고합니다. 사이트가 바뀐 뒤에도 안전하게 흐름을 점검할 수 있는 도구입니다.

## 쿠폰 자동 수집/적용

`nrw/coupon_utils.py` 가 선택 로직을, 각 어댑터가 조회/다운로드/적용을 담당합니다. 쿠폰은
두 곳에서 확인합니다: 업체 홈 페이지(`/home`)의 쿠폰 목록, 그리고 예약 화면 자체에 내장된
쿠폰 선택 UI(있는 경우) - 후자가 실제 이 예약에 적용 가능한지에 대해 더 신뢰도 높은
소스입니다.

- **무료로 즉시 받을 수 있는/선택 가능한 쿠폰은 자동으로 받아 적용**합니다 (ASK/AUTO 모드
  모두). 이미 보유 중인 쿠폰은 다시 받지 않습니다.
- 여러 쿠폰을 받을 수 있으면 **그 예약의 예상 결제금액 기준으로 실제 할인액이 가장 큰
  쿠폰**을 고릅니다.
- **유료 멤버십 가입/구독/개인정보 추가 제공/마케팅 동의/별도 결제가 필요한 쿠폰은
  자동으로 받거나 적용하지 않습니다** - 존재는 알림/이벤트 로그에 남기되("확인 필요"),
  항상 사용자 확인을 거쳐야 합니다.
- **예약 버튼을 누르기 직전, 선택했던 쿠폰이 여전히 유효한지 다시 확인**합니다. 그
  사이 만료되거나 더 이상 적용할 수 없게 됐다면 할인 없이 강행하지 않고 멈춰서 알립니다.
- 예약 성공 기록(`reservation_status`)에 적용한 쿠폰명과 할인액이 함께 저장됩니다.
- 실제 네이버 업체 페이지 두 곳(서로 다른 업체)을 대상으로 쿠폰 조회/게이트 감지 및
  `dry_run` 전체 흐름(최종 확인 버튼 직전까지)을 검증했습니다. 실제 예약 확정 클릭은
  검증하지 않았습니다 - 업체별 DOM 구조 차이가 있을 수 있어 셀렉터는
  `naver_selectors.py` 의 `COUPON_*`/`SUBMIT_BUTTON_*` 항목에 모여 있습니다.

## 폴링 정책 (네이버 서버에 과도한 요청 방지)

`config/config.toml` (없으면 `config/config.example.toml` 기본값 사용)에서 조정 가능:

- 평상시 45~120초 사이 랜덤 간격으로 확인.
- 에러가 나면 30초부터 지수 백오프(최대 15분).
- 페이지에 "O월 O일 O시부터 예약 가능" 같은 오픈 예정 문구가 보이면, 그 시각 2분 전부터
  5~15초 간격으로 타이트하게 전환해 오픈 직후를 빠르게 잡습니다.
- 마감된 시간이 취소 등으로 다시 열리는 것도 매 확인마다 이전 스냅샷과 비교해 감지합니다.

## 알림 (Notifier)

기본은 Windows Toast (`win11toast`). `nrw/notifier/base.py` 의 `Notifier` 인터페이스만
구현하면 다른 채널(Telegram 등)로 쉽게 바꿀 수 있습니다 - 뼈대는
`nrw/notifier/telegram_stub.py` 에 있습니다. 채널 전환은 `config.toml` 의
`[notify] channel = "telegram"` 로 지정합니다.

## 테스트 (실제 네이버를 건드리지 않는 mock/로컬 픽스처)

```powershell
.venv\Scripts\python.exe -m pytest -q
```

`mock_site/server.py` 가 로컬에 "오픈 전 → 오픈 → 마감 → 취소자리 발생 → 예약 성공"
전체 흐름을 흉내내는 페이지를 띄우고, `tests/test_e2e_mock_flow.py` 가 watcher의 핵심
로직(오픈 감지, 우선순위 매칭, AUTO/ASK/NOTIFY 분기, 결제/캡차 안전장치, 클릭 직전
재검증)을 실제 Playwright로 그 페이지를 조작하며 검증합니다.

쿠폰 시나리오, dry-run 흐름, 필수 동의 자동 처리 정책, 실제 예약 위젯의 DOM 구조(로컬
정적 HTML 픽스처로 재현)는 각각 `tests/test_coupons_e2e.py`, `tests/test_dry_run.py`,
`tests/test_naver_widget_flow.py`, `tests/test_reservation_logging.py` 에서 검증합니다.

## 한계 / 고지

- **네이버 예약 페이지의 실제 DOM 구조는 업체/업종마다 다르고 네이버가 자주 개편합니다.**
  `src/nrw/browser/naver_selectors.py` 에 문구/셀렉터를 한곳에 모아뒀으니, 특정 업체에서
  동작이 어긋나면 이 파일만 조정하면 됩니다.
- 실제 예약 확정(최종 버튼 클릭)은 검증하지 않았습니다 - 로그인은 사용자 본인만 할 수
  있고, 실제 결제/예약을 만드는 행위이기 때문입니다. `reserve_now(dry_run=True)` 로
  최종 클릭 직전까지의 전체 흐름을 안전하게 점검할 수 있습니다.
- headless 여부는 `config.toml` 의 `[browser] headless` 로 조정합니다. 기본은 `false`
  (창이 보이는 채로 실행) 이며, 결제/캡차 등 사용자 확인이 필요할 때 화면을 볼 수 있게
  하기 위함입니다.
- watcher는 PC가 켜져 있고 로그온되어 있어야 동작합니다 (완전 종료/절전 상태에서는 동작
  하지 않습니다). 이 프로젝트는 로컬 실행 구조라 "PC가 꺼져 있어도 클라우드에서 계속
  감시"는 지원하지 않습니다.

## SQLite 스키마 요약

- `watches`: 감시 등록 정보 + 현재 상태
- `reservations`: 성공한 예약 기록
- `jobs`: MCP → watcher 작업 큐 (즉시 확인/예약/승인/거절)
- `events`: 감시별 이벤트/알림 로그
- `heartbeat`: watcher 생존 확인용

## 라이선스

[MIT](LICENSE)
