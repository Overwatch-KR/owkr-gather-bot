#!/bin/sh
# Run from any directory. Only this Compose deployment is stopped/restarted.
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
running=$(docker compose ps --status running -q bot)
resume() {
    if [ -n "$running" ]; then
        docker compose start bot
    fi
}
trap resume EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose stop bot
container=$(docker compose ps -a -q bot)
if [ -n "$container" ]; then
    code=$(docker inspect --format '{{.State.ExitCode}}' "$container")
    if [ "$code" = 137 ]; then
        running=''
        echo '강제 종료가 감지됐습니다. 로그와 DB 상태를 확인하세요.' >&2
        exit 1
    fi
fi
docker compose run --rm --no-deps --name owkr-gather-bot-backup \
    -v "$PWD/backups:/backups" bot python scripts/backup.py
