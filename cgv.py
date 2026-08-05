"""CGV 예매 API 클라이언트.

CGV(cgv.co.kr)는 Cloudflare 봇 차단이 TLS 지문(JA3) 레벨에서 걸려 있어
일반 requests/urllib로는 헤더를 아무리 맞춰도 403이 돌아온다.
curl_cffi로 브라우저 TLS 지문을 흉내내면 통과한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from curl_cffi import requests as cf_requests

logger = logging.getLogger("cgv")

BASE_URL = "https://cgv.co.kr"
BOOKING_PAGE = f"{BASE_URL}/cnm/movieBook"
SCHEDULE_ENDPOINT = f"{BASE_URL}/api/v1/booking/searchMovScnInfo"
CO_CD = "A420"
RTCTL_SCOP_CD = "08"  # 브라우저가 실제로 보내는 값(연령등급 조회 범위)

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
}


@dataclass
class Showtime:
    movie_name: str
    screen_name: str
    play_date: str  # YYYYMMDD
    start_time: str  # HHMM, 24시 초과 표기 가능 (심야)
    free_seats: int
    total_seats: int
    scns_no: str
    scn_sseq: str
    prod_no: str

    @property
    def key(self) -> str:
        return f"{self.play_date}:{self.scns_no}:{self.scn_sseq}:{self.prod_no}:{self.start_time}"

    @property
    def start_minutes(self) -> int:
        """HHMM 문자열을 자정 기준 분으로 변환 (심야는 24시 이상 값 그대로 유지)."""
        hh = int(self.start_time[:-2])
        mm = int(self.start_time[-2:])
        return hh * 60 + mm


class CGVClientError(Exception):
    pass


class RateLimitedError(CGVClientError):
    pass


class CGVClient:
    """Cloudflare를 우회하는 세션을 유지하며 상영 스케줄을 조회한다."""

    def __init__(self, session_refresh_seconds: int = 1800):
        self._session_refresh_seconds = session_refresh_seconds
        self._session: cf_requests.Session | None = None
        self._session_created_at: float = 0.0

    def _ensure_session(self) -> cf_requests.Session:
        now = time.monotonic()
        needs_new = (
            self._session is None
            or (now - self._session_created_at) > self._session_refresh_seconds
        )
        if needs_new:
            logger.info("세션(쿠키) 새로 발급")
            session = cf_requests.Session(impersonate="chrome")
            resp = session.get(BOOKING_PAGE, timeout=20)
            if resp.status_code != 200:
                raise CGVClientError(
                    f"예매 페이지 방문 실패: status={resp.status_code}"
                )
            self._session = session
            self._session_created_at = now
        return self._session

    def fetch_schedule(self, site_no: str, play_ymd: str) -> list[Showtime]:
        """지정한 극장(site_no)의 특정 상영일(play_ymd, YYYYMMDD) 전체 스케줄을 반환."""
        session = self._ensure_session()
        resp = session.get(
            SCHEDULE_ENDPOINT,
            params={
                "coCd": CO_CD,
                "siteNo": site_no,
                "scnYmd": play_ymd,
                "rtctlScopCd": RTCTL_SCOP_CD,
            },
            headers=COMMON_HEADERS,
            timeout=20,
        )

        if resp.status_code == 429:
            raise RateLimitedError(f"429 Too Many Requests (site={site_no}, date={play_ymd})")
        if resp.status_code == 403:
            # 세션이 밴/만료되었을 수 있으니 다음 호출에서 강제 재발급
            self._session = None
            raise RateLimitedError(f"403 Forbidden (site={site_no}, date={play_ymd})")
        if resp.status_code != 200:
            raise CGVClientError(f"예상치 못한 status={resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise CGVClientError(f"JSON 파싱 실패: {e}") from e

        if payload.get("statusCode") != 0:
            raise CGVClientError(f"API 오류: {payload.get('statusMessage')}")

        rows = payload.get("data") or []
        result: list[Showtime] = []
        for row in rows:
            free = row.get("frSeatCnt")
            total = row.get("stcnt")
            try:
                free_i = int(free) if free is not None else 0
                total_i = int(total) if total is not None else 0
            except (TypeError, ValueError):
                free_i, total_i = 0, 0
            result.append(
                Showtime(
                    movie_name=row.get("movNm") or "",
                    screen_name=row.get("scnsNm") or "",
                    play_date=row.get("scnYmd") or play_ymd,
                    start_time=row.get("scnsrtTm") or "",
                    free_seats=free_i,
                    total_seats=total_i,
                    scns_no=row.get("scnsNo") or "",
                    scn_sseq=row.get("scnSseq") or "",
                    prod_no=row.get("prodNo") or "",
                )
            )
        return result
