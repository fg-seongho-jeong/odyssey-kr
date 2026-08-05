#!/usr/bin/env bash
# 크래시 시 자동 재시작하며 watcher.py를 상시 실행한다.
# 맥 절전으로 폴링이 멈추는 걸 막으려면:
#   caffeinate -s ./run.sh
set -u
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] watcher.py 시작"
  "$PYTHON_BIN" watcher.py
  code=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] watcher.py 종료(exit=$code), 5초 후 재시작"
  sleep 5
done
