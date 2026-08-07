"""구독자별 감시 조건(날짜/시간대) 저장과 텔레그램 대화형 등록/해지 처리.

영화/상영관/극장은 config.yaml에서 전역으로 공유하지만, 감시할 날짜 범위와
시간대는 구독자마다 다르게 설정할 수 있다. 봇에게 처음 말을 걸면(또는 /start)
날짜/시간을 순서대로 물어보는 대화형 흐름으로 등록한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("subscribers")

DATE_FMT = "%Y-%m-%d"

_STEP_START_DATE, _STEP_END_DATE, _STEP_START_TIME, _STEP_END_TIME = range(4)

_PROMPTS = {
    _STEP_START_DATE: "감시를 시작할 날짜를 입력해주세요 (예: 2026-08-20)",
    _STEP_END_DATE: "감시를 종료할 날짜를 입력해주세요 (예: 2026-08-25, 시작일과 같아도 됩니다)",
    _STEP_START_TIME: "감시할 시작 시각을 입력해주세요 (24시간제, 예: 09:00)",
    _STEP_END_TIME: "감시할 종료 시각을 입력해주세요 (예: 22:00)",
}

_HELP_TEXT = (
    "이미 구독 중이에요.\n"
    "설정을 바꾸려면 /start, 그만 받으려면 /stop, 현재 설정 확인은 /status 를 보내주세요."
)


def _parse_hhmm(value: str) -> int:
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


def _fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class WatchPrefs:
    start_date: date
    end_date: date
    start_minutes: int
    end_minutes: int

    def date_range(self) -> list[date]:
        days = (self.end_date - self.start_date).days
        return [self.start_date + timedelta(days=i) for i in range(days + 1)]

    def contains(self, play_ymd: str, start_minutes: int) -> bool:
        d = datetime.strptime(play_ymd, "%Y%m%d").date()
        return self.start_date <= d <= self.end_date and self.start_minutes <= start_minutes <= self.end_minutes

    def summary(self) -> str:
        return (
            f"기간: {self.start_date} ~ {self.end_date}\n"
            f"시간대: {_fmt_hhmm(self.start_minutes)} ~ {_fmt_hhmm(self.end_minutes)}"
        )

    def to_json(self) -> dict:
        return {
            "start_date": self.start_date.strftime(DATE_FMT),
            "end_date": self.end_date.strftime(DATE_FMT),
            "start_time": _fmt_hhmm(self.start_minutes),
            "end_time": _fmt_hhmm(self.end_minutes),
        }

    @classmethod
    def from_json(cls, data: dict) -> WatchPrefs:
        return cls(
            start_date=datetime.strptime(data["start_date"], DATE_FMT).date(),
            end_date=datetime.strptime(data["end_date"], DATE_FMT).date(),
            start_minutes=_parse_hhmm(data["start_time"]),
            end_minutes=_parse_hhmm(data["end_time"]),
        )


class SubscriberStore:
    """chat_id별 WatchPrefs와 텔레그램 getUpdates offset을 JSON에 영속화."""

    def __init__(self, path: str = "subscribers.json"):
        self._path = Path(path)
        self._prefs: dict[str, WatchPrefs] = {}
        self._last_update_id: int | None = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("%s 로드 실패, 빈 상태로 시작: %s", self._path, e)
            return
        for chat_id, prefs_json in data.get("subscribers", {}).items():
            try:
                self._prefs[str(chat_id)] = WatchPrefs.from_json(prefs_json)
            except (KeyError, ValueError) as e:
                logger.error("구독자 %s 설정 파싱 실패, 건너뜀: %s", chat_id, e)
        self._last_update_id = data.get("last_update_id")

    def _save(self) -> None:
        data = {
            "subscribers": {cid: p.to_json() for cid, p in self._prefs.items()},
            "last_update_id": self._last_update_id,
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def seed_if_absent(self, chat_id: str, prefs: WatchPrefs) -> None:
        """.env의 TELEGRAM_CHAT_ID 등 초기 구독자를 기본 조건으로 등록. 이미 있으면 무시."""
        chat_id = str(chat_id).strip()
        if chat_id and chat_id not in self._prefs:
            self._prefs[chat_id] = prefs
            self._save()

    def set(self, chat_id: str, prefs: WatchPrefs) -> None:
        self._prefs[str(chat_id)] = prefs
        self._save()

    def get(self, chat_id: str) -> WatchPrefs | None:
        return self._prefs.get(str(chat_id))

    def remove(self, chat_id: str) -> bool:
        chat_id = str(chat_id)
        if chat_id not in self._prefs:
            return False
        del self._prefs[chat_id]
        self._save()
        return True

    def list(self) -> list[str]:
        return sorted(self._prefs.keys())

    def all_prefs(self) -> dict[str, WatchPrefs]:
        return dict(self._prefs)

    def __len__(self) -> int:
        return len(self._prefs)

    @property
    def last_update_id(self) -> int | None:
        return self._last_update_id

    def set_last_update_id(self, value: int) -> None:
        self._last_update_id = value
        self._save()


class SubscriptionBot:
    """텔레그램 메시지를 짧은 폴링으로 확인해 대화형 등록/해지/조회를 처리한다."""

    def __init__(self, telegram, store: SubscriberStore, movie_screen_summary: Callable[[], str]):
        self._telegram = telegram
        self._store = store
        self._movie_screen_summary = movie_screen_summary
        self._conversations: dict[str, dict] = {}

    def poll_once(self) -> None:
        offset = self._store.last_update_id
        updates = self._telegram.get_updates(offset=(offset + 1) if offset is not None else None)
        if not updates:
            return

        max_update_id = offset or 0
        for update in updates:
            max_update_id = max(max_update_id, update.get("update_id", 0))
            message = update.get("message")
            if not message:
                continue
            chat = message.get("chat") or {}
            if chat.get("type") != "private":
                continue
            chat_id = str(chat.get("id"))
            text = (message.get("text") or "").strip()
            self._handle_message(chat_id, text)

        self._store.set_last_update_id(max_update_id)

    def _handle_message(self, chat_id: str, text: str) -> None:
        if text == "/stop":
            self._conversations.pop(chat_id, None)
            if self._store.remove(chat_id):
                logger.info("구독 해지: %s", chat_id)
                self._telegram.send(chat_id, "🔕 알림 구독을 해지했습니다. 다시 받고 싶으면 /start 를 보내주세요.")
            else:
                self._telegram.send(chat_id, "구독 중이 아니에요. /start 로 시작할 수 있어요.")
            return

        if text == "/status":
            prefs = self._store.get(chat_id)
            if prefs:
                self._telegram.send(
                    chat_id, "📋 현재 구독 설정\n\n" + self._movie_screen_summary() + "\n" + prefs.summary()
                )
            else:
                self._telegram.send(chat_id, "아직 구독하지 않았어요. /start 로 시작해보세요.")
            return

        if text == "/start":
            self._start_conversation(chat_id)
            return

        if chat_id in self._conversations:
            self._continue_conversation(chat_id, text)
            return

        if self._store.get(chat_id) is None:
            # 첫 메시지(명령어가 아니어도) - 바로 등록 절차 시작
            self._start_conversation(chat_id)
            return

        self._telegram.send(chat_id, _HELP_TEXT)

    def _start_conversation(self, chat_id: str) -> None:
        self._conversations[chat_id] = {"step": _STEP_START_DATE, "data": {}}
        self._telegram.send(
            chat_id,
            "👋 알림 받을 조건을 설정할게요.\n\n"
            + self._movie_screen_summary()
            + "\n\n"
            + _PROMPTS[_STEP_START_DATE],
        )

    def _continue_conversation(self, chat_id: str, text: str) -> None:
        conv = self._conversations[chat_id]
        step = conv["step"]
        data = conv["data"]

        if step == _STEP_START_DATE:
            d = self._try_parse_date(chat_id, text)
            if d is None:
                return
            data["start_date"] = d
            conv["step"] = _STEP_END_DATE
            self._telegram.send(chat_id, _PROMPTS[_STEP_END_DATE])

        elif step == _STEP_END_DATE:
            d = self._try_parse_date(chat_id, text)
            if d is None:
                return
            if d < data["start_date"]:
                self._telegram.send(chat_id, "⚠️ 종료 날짜는 시작 날짜보다 빠를 수 없어요. 다시 입력해주세요 (예: 2026-08-25)")
                return
            data["end_date"] = d
            conv["step"] = _STEP_START_TIME
            self._telegram.send(chat_id, _PROMPTS[_STEP_START_TIME])

        elif step == _STEP_START_TIME:
            m = self._try_parse_time(chat_id, text)
            if m is None:
                return
            data["start_minutes"] = m
            conv["step"] = _STEP_END_TIME
            self._telegram.send(chat_id, _PROMPTS[_STEP_END_TIME])

        elif step == _STEP_END_TIME:
            m = self._try_parse_time(chat_id, text)
            if m is None:
                return
            if m < data["start_minutes"]:
                self._telegram.send(chat_id, "⚠️ 종료 시각은 시작 시각보다 빠를 수 없어요. 다시 입력해주세요 (예: 22:00)")
                return
            data["end_minutes"] = m
            prefs = WatchPrefs(
                start_date=data["start_date"],
                end_date=data["end_date"],
                start_minutes=data["start_minutes"],
                end_minutes=data["end_minutes"],
            )
            self._store.set(chat_id, prefs)
            del self._conversations[chat_id]
            logger.info("구독 등록/갱신: %s", chat_id)
            self._telegram.send(
                chat_id,
                "✅ 설정 완료! 아래 조건에 맞는 자리가 나오면 알려드릴게요.\n\n"
                + self._movie_screen_summary()
                + "\n"
                + prefs.summary()
                + "\n\n설정을 바꾸려면 /start, 그만 받으려면 /stop, 확인은 /status 를 보내주세요.",
            )

    def _try_parse_date(self, chat_id: str, text: str) -> date | None:
        try:
            return datetime.strptime(text, DATE_FMT).date()
        except ValueError:
            self._telegram.send(chat_id, "⚠️ 날짜 형식이 올바르지 않아요. YYYY-MM-DD 형식으로 입력해주세요 (예: 2026-08-20)")
            return None

    def _try_parse_time(self, chat_id: str, text: str) -> int | None:
        try:
            return _parse_hhmm(text)
        except (ValueError, IndexError):
            self._telegram.send(chat_id, "⚠️ 시간 형식이 올바르지 않아요. HH:MM 형식으로 입력해주세요 (예: 09:00)")
            return None
