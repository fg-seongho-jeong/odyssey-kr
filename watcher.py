#!/usr/bin/env python3
"""CGV 예매 오픈 감시 메인 루프.

전체 구독자의 날짜 범위를 합쳐 tick마다 하루씩 순환 조회하여, 구독자가 늘거나
날짜 범위가 넓어져도 초당 요청 수는 고정된다. 영화/상영관 조건은 config.yaml로
전역 공유하고, 날짜/시간대는 구독자별(WatchPrefs)로 다르게 적용해 맞춤 알림을
보낸다. 그 외 정상 작동 하트비트는 전체 구독자에게 공지한다.
"""

from __future__ import annotations

import logging
import random
import signal
import sys
import time
from datetime import date

from cgv import CGVClient, CGVClientError, RateLimitedError, Showtime
from config import Config, ConfigError, load_config
from notifier import AlertManager, TelegramClient
from subscribers import SubscriberStore, SubscriptionBot, WatchPrefs

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

        telegram = TelegramClient(cfg.telegram_token)
        self.subscribers = SubscriberStore(path="subscribers.json")
        if cfg.telegram_chat_id:
            self.subscribers.seed_if_absent(
                cfg.telegram_chat_id,
                WatchPrefs(cfg.start_date, cfg.end_date, cfg.start_minutes, cfg.end_minutes),
            )

        self.alerts = AlertManager(
            telegram=telegram,
            subscribers=self.subscribers,
            theater_name=cfg.theater_name,
            booking_url=BOOKING_URL,
            repeat_seconds=cfg.repeat_seconds,
            good_seat_enabled=cfg.good_seat_enabled,
            good_seat_min_free_ratio=cfg.good_seat_min_free_ratio,
        )
        self.sub_bot = SubscriptionBot(telegram, self.subscribers, movie_screen_summary=self._movie_screen_text)

        self._tick_counter = 0
        self._poll_count = 0
        self._success_count = 0
        self._last_success_at: float | None = None
        self._last_heartbeat_at: float = time.time()
        self._last_subscriber_poll_at: float = 0.0
        self._backoff = cfg.backoff_initial_seconds
        self._running = True

    def _matches_movie_screen(self, st: Showtime) -> bool:
        if not any(m in st.movie_name for m in self.cfg.movie_match):
            return False
        if self.cfg.screen_match and not any(m in st.screen_name for m in self.cfg.screen_match):
            return False
        return True

    def _date_union(self) -> list[date]:
        """관리자 기본 범위 + 전체 구독자 범위를 합친, 지금 감시해야 할 날짜 목록."""
        dates = set(self.cfg.date_range())
        for prefs in self.subscribers.all_prefs().values():
            dates.update(prefs.date_range())
        return sorted(dates)

    def _next_date(self) -> date:
        dates = self._date_union()
        if not dates:
            dates = [date.today()]
        idx = self._tick_counter % len(dates)
        self._tick_counter += 1
        return dates[idx]

    def _tick(self) -> None:
        play_date = self._next_date()
        ymd = play_date.strftime("%Y%m%d")
        self._poll_count += 1

        showtimes = self.client.fetch_schedule(self.cfg.site_no, ymd)
        self._success_count += 1
        self._last_success_at = time.time()
        self._backoff = self.cfg.backoff_initial_seconds  # 성공 시 백오프 원복

        matched = [st for st in showtimes if self._matches_movie_screen(st)]
        sent = self.alerts.process(matched, self.subscribers.all_prefs())
        if sent:
            logger.info("알림 발송: %s건 (날짜=%s, 매칭 회차=%s개)", sent, ymd, len(matched))

    def _movie_screen_text(self) -> str:
        watched = ", ".join(self.cfg.movie_match)
        screens = ", ".join(self.cfg.screen_match) if self.cfg.screen_match else "전체 상영관"
        return f"🎬 {watched} ({screens})\n📍 {self.cfg.theater_name}"

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_at < self.cfg.heartbeat_seconds:
            return
        last_ok = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_success_at))
            if self._last_success_at
            else "없음"
        )
        good_seat_note = (
            f"괜찮은 자리 기준: 잔여 {self.cfg.good_seat_min_free_ratio * 100:.0f}% 이상"
            if self.cfg.good_seat_enabled
            else "좌석 1석 이상이면 알림(근사 판정 꺼짐)"
        )
        extra = (
            f"{self._movie_screen_text()}\n"
            f"{good_seat_note}\n"
            f"구독자: {len(self.subscribers)}명 (감시 날짜 {len(self._date_union())}일 순환)\n"
            f"누적 폴링: {self._poll_count}회 (성공 {self._success_count}회)\n"
            f"마지막 성공: {last_ok}"
        )
        self.alerts.send_heartbeat(extra)
        self._last_heartbeat_at = now

    def _maybe_poll_subscribers(self) -> None:
        now = time.time()
        if now - self._last_subscriber_poll_at < self.cfg.subscriber_poll_seconds:
            return
        try:
            self.sub_bot.poll_once()
        except Exception:
            logger.exception("구독자 메시지 폴링 중 오류")
        self._last_subscriber_poll_at = now

    def run(self) -> None:
        self.alerts.send_system("🚀 CGV 예매 감시 봇 시작\n" + self._movie_screen_text())
        logger.info("감시 시작: %s", self._movie_screen_text().replace("\n", " / "))

        while self._running:
            try:
                self._tick()
                self._maybe_heartbeat()
                self._maybe_poll_subscribers()
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
