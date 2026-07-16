# owkr-gather-bot

기존 내전봇의 모집·참가자·티어 수집 기능을 대체하는 Discord 봇 MVP입니다.

## MVP 동작

- `.내전 시간 [모드]`로 한 길드의 활성 세션을 교체 생성합니다.
- 세션 actor가 raw reaction event를 받은 순서대로 `arrival_seq`를 부여합니다.
- 최초 10명은 확정 참가자, 이후 사용자는 대기자입니다.
- 한 번 FULL이 된 뒤 확정 참가자가 반응을 제거해도 대기자를 자동 승격하지 않습니다.
- 모집 완료 공지가 Discord에 성공적으로 전송된 시각부터 티어 마감 직전까지 작성·수정된 최신 원문만 저장합니다.
- 시작 시각부터 반응과 티어 이벤트를 무시하고 세션 상태를 `STARTED`로 바꿉니다.
- SQLite가 로컬 원본이며 외부 API는 호출하지 않습니다.
- 재시작 복구 시 티어 채널을 한 번 확인해 유효 시간대의 작성·수정 메시지를 보완합니다.

## 요구 사항

- Python 3.13 이상
- Discord bot token
- Discord guild 및 채널 ID

## 설치

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

macOS에서 Python 3.14를 사용하는 경우 첫 줄을 `/opt/homebrew/bin/python3.14 -m venv .venv`로 바꿀 수 있습니다.

## 설정

`config/config.yaml`에서 다음 값을 실제 Discord snowflake ID로 변경합니다.

- `guild_id`: 운영 guild
- `channels.command`: 관리자 명령 채널
- `channels.announcement`: `번개-내전공지` 채널
- `channels.tier`: `내전 티어` 채널
- `channels.admin`: 관리자 알림 채널
- `admin_user_ids`: 명령을 허용할 관리자 ID
- `admin_role_ids`: 선택적인 관리자 역할 ID
- `managers`: 관리자별 기본 모드·픽 제한·밴 목록·추가 문구

모드는 명령과 관리자 설정 모두에서 생략할 수 있습니다. 이때 DB에는 `null`로 저장되고, `mode_display_fallback`도 `null`이면 모집 공지의 모드 줄을 생략합니다.

환경 변수:

| 이름 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `DISCORD_BOT_TOKEN` | 예 | 없음 | Discord bot token |
| `OWKR_CONFIG_PATH` | 아니요 | `config/config.yaml` | 단일 YAML 설정 경로 |
| `OWKR_DATABASE_PATH` | 아니요 | `data/owkr-gather-bot.sqlite3` | SQLite 파일 경로 |
| `OWKR_TEMPLATE_PATH` | 아니요 | `templates/recruitment.txt` | 단일 모집 템플릿 경로 |
| `OWKR_LOG_LEVEL` | 아니요 | `INFO` | Python 로그 레벨 |

`.env` 파일은 자동으로 읽지 않습니다. 불필요한 dotenv 의존성을 피하기 위한 선택입니다. 실행 전에 셸 환경으로 export합니다.

```bash
set -a
source .env
set +a
owkr-gather-bot
```

또는:

```bash
python -m owkr_gather_bot.main
```

## 명령어

- `.내전 오후6시20분 6ㄷ6클래식`
- `.내전 오후2시20분`
- `.내전 18:20`
- `.내전 23시 6ㄷ6클래식`
- `.티어현황`
- `.티어미작성알림`
- `.내전상태`
- `.내전취소`

지난 시각은 자동으로 다음 날로 만들지 않습니다. 봇이 생성을 거부하고, 설정이 켜져 있으면 명령 실행자에게만 사용할 수 있는 “내일 같은 시각으로 생성” 버튼을 120초 동안 표시합니다.

## Discord Developer Portal

Bot 설정의 Privileged Gateway Intents에서 다음을 활성화합니다.

- `MESSAGE CONTENT INTENT`

코드는 다음 Gateway Intents를 요청합니다.

- `GUILDS`
- `GUILD_MESSAGES`
- `GUILD_MESSAGE_REACTIONS`
- `MESSAGE_CONTENT`

`GUILD_MEMBERS`와 `GUILD_PRESENCES`는 사용하지 않습니다.

## 최소 채널 권한

관련 명령·공지·티어·관리자 채널에 필요한 권한:

- View Channel
- Send Messages
- Read Message History
- Add Reactions — 모집 공지 채널에서만 필요

봇은 텍스트 메시지만 사용하므로 Embed Links는 필수가 아닙니다. Administrator, Manage Messages, Mention Everyone, Manage Roles, Kick Members, Ban Members는 부여하지 않습니다.

## 테스트

테스트는 추가 개발 의존성 없이 표준 라이브러리 `unittest`로 실행합니다.

```bash
python -m unittest discover -s tests -v
```

테스트 범위에는 18명 burst, 중복/재반응, 완료 알림 멱등성, 티어 수집 시간 경계, 삭제 후 재작성, 대기자 미승격, 시작 이후 무시, 세션 교체, 재시작 복구, mention 제한, 4필드 웹 mapper가 포함됩니다.

## 웹 DTO

현재 확정 참가자이면서 티어를 작성한 사용자만 기본 export 대상입니다.

```json
{
  "discordUserId": "123456789012345678",
  "discordDisplayName": "레몬",
  "rawTierMessage": "lemon#32146\n마4 / 마4! / 마4",
  "reactionOrder": 17
}
```

`reactionOrder`는 actor가 부여한 원본 `arrival_seq`이므로 연속된 1~10이 아닐 수 있습니다.

## MVP에서 제외한 범위

- CLOSED 상태, `.내전종료`, 자동 종료
- 자동 경고, 노쇼 판정, 대기자 자동 승격
- 공식 roster snapshot과 roster lock
- 시작 이후 반응·티어 처리
- 티어 history, `source_deleted`, 삭제 fallback
- Redis, 다중 인스턴스, 분산 lock
- FastAPI/PostgreSQL 및 실제 원격 sync
- 티어 문자열 파싱과 팀 밸런싱

`MatchRepository`와 `SyncSink` 경계 및 `NoOpSyncSink`는 후속 API 연동을 위해 유지합니다.

## 실제 테스트 guild 확인 항목

1. 최소 권한만 부여한 상태에서 공지 전송과 ✅ 추가가 되는지
2. raw reaction add/remove가 실제 수신 순서대로 기록되는지
3. 첫 10명만 완료 공지에서 실제 ping되는지
4. 대기자가 완료·미작성 알림에서 ping되지 않는지
5. 과거 티어 메시지가 자동 연결되지 않고, 완료 이후 실제 수정 시에만 인정되는지
6. 마감 시각과 시작 시각 경계에서 이벤트가 차단되는지
7. 봇 재시작 후 활성 세션·알림 여부·5분 cooldown이 유지되는지
8. 새 `.내전` 생성 시 이전 세션의 actor와 예약 알림이 중단되는지
9. 봇에 `@everyone`, 역할 mention 권한이 없어도 참가자 user mention만 동작하는지
