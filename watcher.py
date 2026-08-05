#!/usr/bin/env python3
"""CGV 예매 오픈 감시 메인 루프.

날짜 범위 내에서 tick마다 하루씩 순환 조회하여, 날짜 범위가 넓어져도
초당 요청 수가 늘지 않도록 한다. 조건에 맞는 상영 회차에 좌석이 보이면
텔레그램으로 알리고, 주기적으로 정상 작동 하트비트를 보낸다.
"""

from __future__ import annotations

import logging
import random
import signal
import sys
import time

from cgv import CGVClient, CGVClientError, RateLimitedError, Showtime
from config import Config, ConfigError, load_config
from notifier import AlertManager, TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("watcher")

BOOKING_URL = "https://cgv.co.kr/cnm/movieBook"


class Watcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = CGVClient(session_refresh_seconds=cfg.session_refresh_minutes * 60)
        telegram = TelegramClient(cfg.telegram_token, cfg.telegram_chat_id)
        self.alerts = AlertManager(
            telegram=telegram,
            theater_name=cfg.theater_name,
            booking_url=BOOKING_URL,
            repeat_seconds=cfg.repeat_seconds,
        )
        self._dates = cfg.date_range()
        self._date_idx = 0
        self._poll_count = 0
        self._success_count = 0
        self._last_success_at: float | None = None
        self._last_heartbeat_at: float = time.time()
        self._backoff = cfg.backoff_initial_seconds
        self._running = True

    def _matches(self, st: Showtime) -> bool:
        if not any(m in st.movie_name for m in self.cfg.movie_match):
            return False
        if self.cfg.screen_match and not any(m in st.screen_name for m in self.cfg.screen_match):
            return False
        if not (self.cfg.start_minutes <= st.start_minutes <= self.cfg.end_minutes):
            return False
        return True

    def _next_date(self):
        d = self._dates[self._date_idx]
        self._date_idx = (self._date_idx + 1) % len(self._dates)
        return d

    def _tick(self) -> None:
        play_date = self._next_date()
        ymd = play_date.strftime("%Y%m%d")
        self._poll_count += 1

        showtimes = self.client.fetch_schedule(self.cfg.site_no, ymd)
        self._success_count += 1
        self._last_success_at = time.time()
        self._backoff = self.cfg.backoff_initial_seconds  # 성공 시 백오프 원복

        matched = [st for st in showtimes if self._matches(st)]
        if matched:
            sent = self.alerts.process(matched)
            if sent:
                logger.info(
                    "알림 발송: %s건 (날짜=%s, 매칭 회차=%s개)", sent, ymd, len(matched)
                )
        else:
            # 매칭 회차가 전무하면(조건 자체가 스케줄에서 사라짐) 이전 알림 상태도 정리
            self.alerts.process([])

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_at < self.cfg.heartbeat_seconds:
            return
        last_ok = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_success_at))
            if self._last_success_at
            else "없음"
        )
        watched = ", ".join(self.cfg.movie_match)
        screens = ", ".join(self.cfg.screen_match) if self.cfg.screen_match else "전체"
        extra = (
            f"극장: {self.cfg.theater_name}\n"
            f"감시 영화: {watched} ({screens})\n"
            f"기간: {self.cfg.start_date} ~ {self.cfg.end_date}, "
            f"{self.cfg.start_minutes // 60:02d}:{self.cfg.start_minutes % 60:02d}"
            f"~{self.cfg.end_minutes // 60:02d}:{self.cfg.end_minutes % 60:02d}\n"
            f"누적 폴링: {self._poll_count}회 (성공 {self._success_count}회)\n"
            f"마지막 성공: {last_ok}"
        )
        self.alerts.send_heartbeat(extra)
        self._last_heartbeat_at = now

    def run(self) -> None:
        self.alerts.send_system(
            "🚀 CGV 예매 감시 봇 시작\n"
            f"극장: {self.cfg.theater_name}\n"
            f"영화: {', '.join(self.cfg.movie_match)}\n"
            f"기간: {self.cfg.start_date} ~ {self.cfg.end_date}"
        )
        logger.info("감시 시작: %s ~ %s, %d개 날짜 순환", self.cfg.start_date, self.cfg.end_date, len(self._dates))

        while self._running:
            try:
                self._tick()
                self._maybe_heartbeat()
                sleep_for = self.cfg.interval_seconds + random.uniform(0, self.cfg.jitter_seconds)
            except RateLimitedError as e:
                logger.warning("차단/제한 감지, 백오프 %.0f초: %s", self._backoff, e)
                sleep_for = self._backoff
                self._backoff = min(self._backoff * 2, self.cfg.backoff_max_seconds)
            except CGVClientError as e:
                logger.error("CGV 조회 오류, 백오프 %.0f초: %s", self._backoff, e)
                sleep_for = self._backoff
                self._backoff = min(self._backoff * 2, self.cfg.backoff_max_seconds)
            except Exception:
                logger.exception("예상치 못한 오류, 5초 후 재시도")
                sleep_for = 5

            time.sleep(sleep_for)

    def stop(self) -> None:
        self._running = False
        self.alerts.send_system("🛑 CGV 예매 감시 봇 종료")


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        logger.error("설정 오류: %s", e)
        return 1

    watcher = Watcher(cfg)

    def _handle_sigterm(signum, frame):
        logger.info("종료 신호 수신")
        watcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    watcher.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
