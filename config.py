"""config.yaml + .env 로딩과 검증."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: str

    site_no: str
    theater_name: str

    movie_match: list[str]
    screen_match: list[str]

    start_date: date
    end_date: date
    start_minutes: int
    end_minutes: int

    interval_seconds: float
    jitter_seconds: float
    session_refresh_minutes: int
    backoff_initial_seconds: float
    backoff_max_seconds: float

    repeat_minutes: int
    heartbeat_minutes: int

    def date_range(self) -> list[date]:
        days = (self.end_date - self.start_date).days
        if days < 0:
            raise ConfigError("watch.end_date는 start_date보다 빠를 수 없습니다.")
        return [self.start_date + timedelta(days=i) for i in range(days + 1)]

    @property
    def repeat_seconds(self) -> int:
        return self.repeat_minutes * 60

    @property
    def heartbeat_seconds(self) -> int:
        return self.heartbeat_minutes * 60


def _parse_hhmm(value: str, field_name: str) -> int:
    try:
        hh, mm = value.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as e:
        raise ConfigError(f"{field_name} 형식이 잘못되었습니다 (HH:MM 필요): {value!r}") from e


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as e:
        raise ConfigError(f"{field_name} 형식이 잘못되었습니다 (YYYY-MM-DD 필요): {value!r}") from e


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> Config:
    load_dotenv(env_path)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or "your-bot-token" in token:
        raise ConfigError(
            f"{env_path}에 TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. "
            "@BotFather에서 발급받은 토큰을 입력하세요."
        )
    if not chat_id or "your-chat-id" in chat_id:
        raise ConfigError(
            f"{env_path}에 TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
            "get_chat_id.py를 실행해 값을 확인하세요."
        )

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    try:
        theater = raw["theater"]
        movie = raw["movie"]
        screen = raw.get("screen", {})
        watch = raw["watch"]
        poll = raw.get("poll", {})
        alerts = raw.get("alerts", {})

        cfg = Config(
            telegram_token=token,
            telegram_chat_id=chat_id,
            site_no=str(theater["site_no"]),
            theater_name=theater["name"],
            movie_match=list(movie["match"]),
            screen_match=list(screen.get("match", []) or []),
            start_date=_parse_date(watch["start_date"], "watch.start_date"),
            end_date=_parse_date(watch["end_date"], "watch.end_date"),
            start_minutes=_parse_hhmm(watch["start_time"], "watch.start_time"),
            end_minutes=_parse_hhmm(watch["end_time"], "watch.end_time"),
            interval_seconds=float(poll.get("interval_seconds", 1.0)),
            jitter_seconds=float(poll.get("jitter_seconds", 1.0)),
            session_refresh_minutes=int(poll.get("session_refresh_minutes", 30)),
            backoff_initial_seconds=float(poll.get("backoff_initial_seconds", 5)),
            backoff_max_seconds=float(poll.get("backoff_max_seconds", 300)),
            repeat_minutes=int(alerts.get("repeat_minutes", 5)),
            heartbeat_minutes=int(alerts.get("heartbeat_minutes", 60)),
        )
    except KeyError as e:
        raise ConfigError(f"{config_path}에 필수 항목이 없습니다: {e}") from e

    if not cfg.movie_match:
        raise ConfigError("movie.match는 최소 1개 이상 필요합니다.")
    if cfg.start_minutes > cfg.end_minutes:
        raise ConfigError("watch.start_time은 watch.end_time보다 늦을 수 없습니다.")
    cfg.date_range()  # start/end 검증

    return cfg
