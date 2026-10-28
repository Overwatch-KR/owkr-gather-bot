#!/bin/sh
# Ubuntu VM only: systemd serializes this command with the backup service.
set -eu
cd "$(dirname "$0")/.."

# Use the operator's time zone even when the VM runs in UTC. Avoid arithmetic
# on zero-prefixed hours, which some shells interpret as octal numbers.
set -- $(TZ=Asia/Seoul date '+%u %H')
if [ "$#" -ne 2 ]; then
    echo '운영 시각을 확인할 수 없습니다. 현재 실행 상태를 유지합니다.' >&2
    exit 2
fi
case "$1:$2" in
    [1-7]:[01][0-9]|[1-7]:2[0-3]) ;;
    *) echo '잘못된 운영 시각입니다. 현재 실행 상태를 유지합니다.' >&2; exit 2 ;;
esac
case "$1:$2" in
    [567]:*|*:0[01]|*:2[0-3]) open=yes ;;
    *) open=no ;;
esac

running=$(docker compose ps --status running -q bot)
if [ "$open" = yes ] && [ -z "$running" ]; then
    docker compose up -d --no-build bot
elif [ "$open" = no ] && [ -n "$running" ]; then
    docker compose stop bot
fi
