# odyssey-kr

CGV 예매 오픈/좌석 발생을 감시해 텔레그램으로 알려주는 봇.
기본값은 **CGV 용산아이파크몰 IMAX관, '오디세이', 2026-08-22 09:00~22:00** 이지만
`config.yaml`만 바꾸면 다른 영화/극장/기간/시간대도 감시할 수 있다.

## 동작 원리

CGV(cgv.co.kr)는 Cloudflare 봇 차단이 걸려 있는데, 이게 TLS 지문(JA3) 레벨 차단이라
`requests`/`curl`로 헤더를 아무리 브라우저처럼 꾸며도 403이 돌아온다.
이 프로젝트는 [`curl_cffi`](https://github.com/lexiforest/curl_cffi)로 Chrome의 TLS 지문을
그대로 흉내내 우회한다. (`cgv.py` 참고)

핵심 API는 `GET /api/v1/booking/searchMovScnInfo?coCd=A420&siteNo=<극장코드>&scnYmd=<날짜>&rtctlScopCd=08`
하나다. 이걸 날짜별로 호출해 상영 스케줄 전체를 받아온 뒤, 영화명/상영관명/시간대 조건에
맞는 회차 중 잔여 좌석(`frSeatCnt`)이 있는 것을 찾아 알린다.

날짜 범위가 여러 날이어도 매 tick마다 **하루씩 순환 조회**하므로 요청 속도는 항상
`interval_seconds`(기본 1초 + 지터)로 고정된다.

## 설치

```bash
cd odyssey-kr
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 텔레그램 봇 설정 (직접 해야 하는 부분)

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)와 대화해 봇 토큰 발급
   (봇 username은 이미 `odyssey_kr_bot`로 만들어둔 상태라면 `/token` 등으로 기존 토큰 확인 가능)
2. `.env`의 `TELEGRAM_BOT_TOKEN`에 토큰 입력
3. 텔레그램에서 `@odyssey_kr_bot`을 검색해 아무 메시지나 전송
4. 아래 명령으로 chat_id 확인:
   ```bash
   python get_chat_id.py
   ```
5. 출력된 chat_id를 `.env`의 `TELEGRAM_CHAT_ID`에 입력

## 설정 (`config.yaml`)

```yaml
theater:
  site_no: "0013"            # CGV 용산아이파크몰
  name: "CGV 용산아이파크몰"

movie:
  match: ["오디세이"]         # 영화명 부분일치, 여러 개 등록 가능

screen:
  match: ["IMAX관"]           # 상영관명 부분일치, 비우면([]) 전체 상영관

watch:
  start_date: "2026-08-22"
  end_date:   "2026-08-22"    # start~end 범위 지정 가능
  start_time: "09:00"
  end_time:   "22:00"

poll:
  interval_seconds: 1.0
  jitter_seconds: 1.0

alerts:
  repeat_minutes: 5           # 좌석이 남아있는 동안 재알림 주기
  heartbeat_minutes: 60       # 정상 작동 알림 주기
```

다른 극장을 감시하려면 `site_no`를 바꿔야 한다. CGV 사이트에서 극장 선택 시
`/api/v1/booking/searchRegnList?coCd=A420` 응답의 `siteNo` 값을 확인하면 된다.

## 실행

```bash
python watcher.py
```

크래시 시 자동 재시작하며 상시 실행하려면:

```bash
./run.sh
```

맥이 절전 모드로 들어가면 폴링이 멈추므로, 상시 감시가 목적이면:

```bash
caffeinate -s ./run.sh
```

## 주의사항

- **알림은 "예매창이 열렸다"는 신호일 뿐, 좌석을 잡아주지 않는다.** 인기 상영관은
  오픈 즉시 매진되므로 알림을 받으면 최대한 빨리 직접 예매해야 한다.
- 1초 폴링도 Cloudflare 차단 위험을 완전히 없애지는 못한다. 403/429가 감지되면
  자동으로 백오프(점진적으로 대기 시간을 늘림) 후 복구를 시도한다.
- 감시 대상 날짜에 해당 영화/상영관이 아예 편성되지 않을 수도 있다. 봇은 편성이
  뜨는 즉시 감지하지만, 편성 자체를 보장하지는 않는다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `watcher.py` | 메인 루프 (날짜 순환 폴링, 백오프, 하트비트) |
| `cgv.py` | curl_cffi 기반 CGV API 클라이언트 |
| `notifier.py` | 텔레그램 전송, 재알림/중복 억제 |
| `config.py` | `config.yaml` + `.env` 로딩/검증 |
| `get_chat_id.py` | chat_id 확인용 1회성 헬퍼 |
| `config.yaml` | 감시 조건 설정 |
| `run.sh` | 크래시 시 자동 재시작 래퍼 |
