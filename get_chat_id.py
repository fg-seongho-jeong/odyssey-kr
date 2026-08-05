#!/usr/bin/env python3
"""텔레그램 chat_id 확인용 1회성 헬퍼.

사용법:
  1. 텔레그램에서 @odyssey_kr_bot 을 검색해 대화를 시작하고 아무 메시지나 전송
  2. .env에 TELEGRAM_BOT_TOKEN을 먼저 입력
  3. python get_chat_id.py 실행
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from dotenv import load_dotenv


def main() -> int:
    load_dotenv(".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or "your-bot-token" in token:
        print(".env에 TELEGRAM_BOT_TOKEN을 먼저 설정하세요.", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    with urllib.request.urlopen(url, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if not payload.get("ok"):
        print("텔레그램 API 오류:", payload, file=sys.stderr)
        return 1

    results = payload.get("result", [])
    if not results:
        print(
            "받은 업데이트가 없습니다. 먼저 텔레그램에서 봇에게 메시지를 보낸 뒤 다시 실행하세요."
        )
        return 1

    seen = {}
    for update in results:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg["chat"]
        seen[chat["id"]] = chat

    print("발견된 chat_id 목록:")
    for chat_id, chat in seen.items():
        label = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
        print(f"  {chat_id}  ({chat.get('type')}, {label})")

    print("\n.env의 TELEGRAM_CHAT_ID에 위 chat_id 값을 넣으세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
