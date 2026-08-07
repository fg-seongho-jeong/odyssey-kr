"""텔레그램 알림 전송, '괜찮은 자리' 근사 판정, 구독자별 맞춤 발송/중복 억제."""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from cgv import Showtime
from subscribers import SubscriberStore, WatchPrefs

logger = logging.getLogger("notifier")


@dataclass
class SendResult:
    ok: bool
    blocked: bool = False
    error: str | None = None


class TelegramClient:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}"

    def send(self, chat_id: str, text: str) -> SendResult:
        body = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                return SendResult(ok=True)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            desc = detail
            try:
                desc = json.loads(detail).get("description", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            blocked = e.code == 403
            logger.error("텔레그램 전송 실패 chat_id=%s: %s %s", chat_id, e.code, desc)
            return SendResult(ok=False, blocked=blocked, error=desc)
        except urllib.error.URLError as e:
            logger.error("텔레그램 전송 URLError chat_id=%s: %s", chat_id, e)
            return SendResult(ok=False, error=str(e))

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        params: dict[str, int] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        url = f"{self._base}/getUpdates?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=timeout + 15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            logger.error("getUpdates 실패: %s", e)
            return []
        if not payload.get("ok"):
            logger.error("getUpdates API 오류: %s", payload)
            return []
        return payload.get("result", [])


@dataclass
class _SeenState:
    last_notified_at: float
    last_free_seats: int


@dataclass
class AlertManager:
    """회차별로 조건에 맞는 구독자에게만 맞춤 알림을 보내고, 재알림 주기를 관리한다."""

    telegram: TelegramClient
    subscribers: SubscriberStore
    theater_name: str
    booking_url: str
    repeat_seconds: int
    good_seat_enabled: bool = True
    good_seat_min_free_ratio: float = 0.12
    _state: dict[str, _SeenState] = field(default_factory=dict)

    def _required_free_seats(self, total_seats: int) -> int:
        """'괜찮은 자리' 근사 임계값. 비활성화 시 기존 동작(좌석 1석이라도 있으면 알림)으로 되돌아간다.

        CGV IMAX 같은 인기 상영관은 중앙 블록 좌석이 가장 먼저 팔리는 경향이 있어,
        잔여석 비율이 낮을수록 남은 좌석이 가장자리/맨 앞줄일 가능성이 높다는
        경험적 가정을 사용한다. 실제 좌석 위치는 로그인 후에만 확인 가능하므로
        어디까지나 근사치다.
        """
        if not self.good_seat_enabled or self.good_seat_min_free_ratio <= 0 or total_seats <= 0:
            return 1
        return max(1, math.ceil(total_seats * self.good_seat_min_free_ratio))

    def _broadcast(self, text: str) -> int:
        """하트비트/시스템 메시지 등, 조건과 무관하게 전체 구독자에게 보내는 공지."""
        sent = 0
        for chat_id in self.subscribers.list():
            result = self.telegram.send(chat_id, text)
            if result.ok:
                sent += 1
            elif result.blocked:
                self._drop_subscriber(chat_id)
        return sent

    def _drop_subscriber(self, chat_id: str) -> None:
        logger.info("구독자 %s 가 봇을 차단하여 목록에서 제거", chat_id)
        self.subscribers.remove(chat_id)
        self.forget_subscriber(chat_id)

    def forget_subscriber(self, chat_id: str) -> None:
        prefix = f"{chat_id}:"
        for k in [k for k in self._state if k.startswith(prefix)]:
            del self._state[k]

    def process(self, showtimes: list[Showtime], prefs_by_chat: dict[str, WatchPrefs]) -> int:
        """영화/상영관 조건은 이미 걸러진 showtimes를 받아, 각자의 날짜/시간대(WatchPrefs)에
        맞는 구독자에게만 맞춤 알림을 보낸다. 반환값은 실제로 전송된 알림 이벤트 수."""
        now = time.time()
        sent_events = 0

        for st in showtimes:
            recipients = [
                chat_id for chat_id, prefs in prefs_by_chat.items() if prefs.contains(st.play_date, st.start_minutes)
            ]
            if not recipients:
                continue

            required = self._required_free_seats(st.total_seats)
            if st.free_seats < required:
                for chat_id in recipients:
                    self._state.pop(f"{chat_id}:{st.key}", None)
                continue

            message: str | None = None
            for chat_id in recipients:
                skey = f"{chat_id}:{st.key}"
                seen = self._state.get(skey)
                should_send = seen is None or (now - seen.last_notified_at) >= self.repeat_seconds
                if not should_send:
                    continue
                if message is None:
                    message = self._format_message(st)
                result = self.telegram.send(chat_id, message)
                if result.ok:
                    self._state[skey] = _SeenState(last_notified_at=now, last_free_seats=st.free_seats)
                    sent_events += 1
                elif result.blocked:
                    self._drop_subscriber(chat_id)

        return sent_events

    def _format_message(self, st: Showtime) -> str:
        date_fmt = f"{st.play_date[:4]}-{st.play_date[4:6]}-{st.play_date[6:]}"
        time_fmt = self._format_time(st.start_time)
        ratio_pct = round(st.free_seats / st.total_seats * 100) if st.total_seats else 0
        label = "🎯 괜찮은 자리 나왔을 가능성!" if self.good_seat_enabled else "🎬 예매 가능!"
        note = (
            "\n⚠️ 정확한 좌석 위치는 예매 화면(로그인 필요)에서 직접 확인하세요."
            if self.good_seat_enabled
            else ""
        )
        return (
            "{label}\n"
            "<b>{movie}</b> · {screen}\n"
            "📍 {theater}\n"
            "📅 {date} {time}\n"
            "💺 잔여 {free}석 / {total}석 (여유 {ratio}%){note}\n"
            "👉 {url}"
        ).format(
            label=label,
            movie=st.movie_name,
            screen=st.screen_name,
            theater=self.theater_name,
            date=date_fmt,
            time=time_fmt,
            free=st.free_seats,
            total=st.total_seats,
            ratio=ratio_pct,
            note=note,
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
        self._broadcast(f"✅ 봇 정상 작동 중\n{extra}")

    def send_system(self, text: str) -> None:
        self._broadcast(text)
