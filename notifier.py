"""텔레그램 알림 전송 및 중복/재알림 제어."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from cgv import Showtime

logger = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self._url = TELEGRAM_API.format(token=token)
        self._chat_id = chat_id

    def send(self, text: str) -> bool:
        body = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = 200 <= resp.status < 300
                if not ok:
                    logger.error("텔레그램 전송 실패: status=%s", resp.status)
                return ok
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            logger.error("텔레그램 전송 HTTPError: %s %s", e.code, detail)
            return False
        except urllib.error.URLError as e:
            logger.error("텔레그램 전송 URLError: %s", e)
            return False


@dataclass
class _SeenState:
    last_notified_at: float
    last_free_seats: int


@dataclass
class AlertManager:
    """감지된 회차별로 알림 발송 여부/재알림 주기를 관리한다."""

    telegram: TelegramClient
    theater_name: str
    booking_url: str
    repeat_seconds: int
    _state: dict[str, _SeenState] = field(default_factory=dict)

    def process(self, matched: list[Showtime]) -> int:
        """조건에 이미 맞는(필터링 완료된) 회차 목록을 받아 필요한 만큼 알림을 보낸다.
        반환값은 실제로 전송한 알림 수."""
        now = time.time()
        matched_keys = set()
        sent = 0

        for st in matched:
            matched_keys.add(st.key)
            if st.free_seats <= 0:
                # 좌석이 없으면(또는 다시 없어지면) 추적만 갱신하고 알리지 않는다
                self._state.pop(st.key, None)
                continue

            seen = self._state.get(st.key)
            should_send = seen is None or (now - seen.last_notified_at) >= self.repeat_seconds
            if should_send:
                if self.telegram.send(self._format_message(st)):
                    self._state[st.key] = _SeenState(last_notified_at=now, last_free_seats=st.free_seats)
                    sent += 1
            else:
                seen.last_free_seats = st.free_seats

        # 더 이상 매칭되지 않는(=매진되었거나 스케줄에서 사라진) 키는 정리
        stale = [k for k in self._state if k not in matched_keys]
        for k in stale:
            del self._state[k]

        return sent

    def _format_message(self, st: Showtime) -> str:
        date_fmt = f"{st.play_date[:4]}-{st.play_date[4:6]}-{st.play_date[6:]}"
        time_fmt = self._format_time(st.start_time)
        return (
            "🎬 <b>{movie}</b> · {screen} 예매 가능!\n"
            "📍 {theater}\n"
            "📅 {date} {time}\n"
            "💺 잔여 {free}석 / {total}석\n"
            "👉 {url}"
        ).format(
            movie=st.movie_name,
            screen=st.screen_name,
            theater=self.theater_name,
            date=date_fmt,
            time=time_fmt,
            free=st.free_seats,
            total=st.total_seats,
            url=self.booking_url,
        )

    @staticmethod
    def _format_time(hhmm: str) -> str:
        hh = int(hhmm[:-2])
        mm = hhmm[-2:]
        if hh >= 24:
            hh -= 24
            return f"{hh:02d}:{mm} (익일)"
        return f"{hh:02d}:{mm}"

    def send_heartbeat(self, extra: str) -> None:
        self.telegram.send(f"✅ 봇 정상 작동 중\n{extra}")

    def send_system(self, text: str) -> None:
        self.telegram.send(text)
