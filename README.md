# owkr-gather-bot

기존 내전봇의 모집·참가자·티어 수집 기능을 대체하는 Discord 봇 MVP입니다. 현재 구현은 단일 프로세스와 한 길드를 기준으로 하며, 여러 활성 내전을 동시에 운영할 수 있습니다.

## MVP 동작

- `/내전 시간 [모드]`를 실행할 때마다 기존 내전을 유지한 채 새 세션을 생성합니다.
- 정확히 같은 시작 시각의 활성 내전이 있으면 새 내전 생성을 막고 시간 변경 또는 기존 내전 취소를 안내합니다.
- 모집 공지별 Session Actor가 반응을 독립적으로 처리합니다.
- 각 세션 actor가 자신의 raw reaction event 수신 순서대로 독립적인 `arrival_seq`를 부여합니다.
- 실제 참가자가 없을 때는 봇의 ✅를 유지하고, 참가자가 생기면 제거해 반응 숫자를 실제 인원과 맞춥니다. 마지막 참가자가 취소하면 ✅를 다시 추가합니다.
- 최초 10명은 확정 참가자, 이후 사용자는 대기자입니다.
- 서로 다른 시작 시각의 내전은 동시에 예약·운영할 수 있습니다.
- 한 번 `FULL`이 된 뒤 확정 참가자가 반응을 제거해도 대기자를 자동 승격하지 않습니다.
- 모집 완료 공지가 Discord에 성공적으로 전송된 시각부터 티어 마감 직전까지 작성·수정된 최신 원문만 저장합니다.
- 모집 완료 후 공용 티어 채널에 내전별 티어 안내 메시지를 생성하며, 참가자는 해당 메시지에 답장해 티어를 제출할 수 있습니다.
- 답장하지 않은 티어 메시지는 제출 가능한 내전이 하나일 때만 자동 연결합니다. 후보가 여러 개면 저장하지 않고 기준 메시지 답장을 안내합니다.
- Discord 티어 메시지는 `tier_message_bindings`에서 한 내전에 영구 귀속되며 수정·삭제도 귀속된 내전에만 적용됩니다.
- 티어 기준 메시지가 삭제되면 같은 세션의 기준 메시지만 재생성하고 관리자 채널에 알립니다.
- 티어 작성 여부는 `/티어현황`과 `/내전상태` 상세에서 확인합니다. 미작성 멘션과 전원 완료 알림은 보내지 않습니다.
- 시작 시각이 기존 활성 내전과 1시간 미만으로 차이나면 나중에 생성한 내전은 대기실 2로 배정합니다. 시작 10분 전에는 배정된 대기실에 아직 들어오지 않은 확정 참가자만 멘션합니다.
- 각 내전은 자신의 시작 시각부터 반응과 티어 이벤트를 무시하고 `STARTED`가 되며, 다른 내전은 계속 동작합니다.
- SQLite가 로컬 원본이며 외부 API는 호출하지 않습니다.
- 재시작 시 모든 활성 세션·Actor·공지/티어 기준 메시지 routing·roster·알림 상태를 복구하고 티어 채널을 한 번 보완 수집합니다.

## 요구 사항과 설치

- Python 3.13 이상
- Discord bot token
- 테스트 또는 운영할 Discord 길드와 채널 ID

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

테스트 도구까지 설치하려면 다음을 사용합니다.

```bash
python -m pip install -e ".[test]"
```

`pyproject.toml`은 Python `>=3.13`을 요구하며 런타임 의존성으로 `python-dotenv`를 포함합니다.

## 환경 변수와 `.env`

애플리케이션은 시작 시 현재 작업 디렉터리의 `.env`를 다음과 같이 자동 로드합니다.

```python
load_dotenv(override=False)
```

우선순위는 다음과 같습니다.

1. 이미 셸 또는 호스팅 환경에 설정된 환경 변수
2. 현재 작업 디렉터리의 `.env`
3. 코드 기본값

`.env`가 없어도 로드는 실패하지 않습니다. 단, 셸 환경과 `.env` 어디에도 `DISCORD_BOT_TOKEN`이 없으면 토큰 값을 출력하지 않는 설정 오류와 종료 코드 2로 시작을 중단합니다.

| 이름 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `DISCORD_BOT_TOKEN` | 예 | 없음 | Discord bot token |
| `OWKR_CONFIG_PATH` | 아니요 | `config/config.yaml` | YAML 설정 파일 |
| `OWKR_DATABASE_PATH` | 아니요 | `data/owkr-gather-bot.sqlite3` | SQLite 파일 |
| `OWKR_TEMPLATE_PATH` | 아니요 | `templates/recruitment.txt` | 모집 텍스트 템플릿 |
| `OWKR_RECRUITMENT_COMPLETE_TEMPLATE_PATH` | 아니요 | `templates/recruitment_complete.txt` | 모집 완료 공지 템플릿 |
| `OWKR_LOG_LEVEL` | 아니요 | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` 중 하나 |

경로 환경 변수는 절대 경로와 상대 경로를 모두 지원합니다. 상대 경로는 `.env` 파일 위치가 아니라 **봇을 실행한 현재 작업 디렉터리**를 기준으로 해석됩니다. 저장소 루트에서 실행하는 것을 권장합니다. 지원하지 않는 로그 레벨은 기본값으로 대체하지 않고 설정 오류로 거부합니다.

로그에는 토큰, 환경 변수 전체 내용, 티어 메시지 원문을 기록하지 않습니다. `DEBUG`에서는 reaction 식별자와 처리 결과가 추가로 기록됩니다.

## YAML 설정

`config/config.yaml`에서 다음 값을 실제 Discord snowflake ID로 변경합니다.

- `guild_id`: 테스트 길드 ID
- `channels.command`: 관리자 명령 채널 ID
- `channels.announcement`: 모집 공지 채널 ID
- `channels.tier`: 티어 작성 채널 ID
- `channels.admin`: 관리자 알림 채널 ID
- `admin_user_ids`: 역할과 별개로 허용할 관리자 사용자 ID
- `admin_role_ids`: 내전 관리자·스태프처럼 운영 명령을 허용할 역할 ID 목록
- `defaults.recruitment_role_id`: 모집 공지에서 한 번 멘션할 알림 역할 ID
- `defaults.lobby_voice_channel_id`: 대기실 1 음성 채널 ID
- `defaults.lobby_voice_channel_2_id`: 겹치는 내전에 사용할 대기실 2 음성 채널 ID
- `defaults.lobby_name`, `defaults.lobby_2_name`: 공지에 표시할 대기실 이름
- `managers`: 관리자별 기본 모드·픽 제한·밴 목록·추가 문구

모드는 명령과 관리자 설정에서 모두 생략할 수 있습니다. 이때 DB에는 `null`로 저장되고 `mode_display_fallback`도 `null`이면 공지에서 모드 줄을 생략합니다.

## 모집 완료 공지 편집

일반 관리자는 `/공지문구`만 사용하면 됩니다. 본인에게만 보이는 메뉴에서 **문구 편집** 또는 **미리보기**를 선택할 수 있습니다. 편집창에는 다음 다섯 항목만 표시됩니다.

- 시작 제목
- 티어 작성 제목
- 티어 작성 안내
- 작성 양식과 예시
- 추가 안내

시작 시각, 티어 채널, 티어 마감, 대기실 입장 시각과 매너 안내는 봇이 자동으로 넣습니다. 모집 완료 공지에는 참가자 목록이나 사용자 멘션을 넣지 않습니다. 관리자는 변수나 Jinja 문법을 알 필요가 없습니다. 편집 화면, 저장 결과와 미리보기는 실행한 관리자 본인에게만 보입니다.

편집 값은 `templates/recruitment_complete.simple.yaml`에 저장되고, 봇이 내부용 `templates/recruitment_complete.txt`를 자동 생성합니다. 파일을 직접 수정할 필요가 없습니다. 기본값은 `templates/recruitment_complete.simple.example.yaml`에 있습니다.

저장한 문구는 다음 모집 완료 공지부터 재시작 없이 적용됩니다. 모집 완료 공지는 사용자 ping을 비활성화하며 `@everyone`, `@here`, 임의 역할 멘션도 실제 ping되지 않습니다. Discord 일반 메시지의 2,000자 제한도 저장 전에 검증합니다.

기본 원본으로 되돌리려면 다음처럼 복사합니다.

```bash
cp templates/recruitment_complete.example.txt templates/recruitment_complete.txt
cp templates/recruitment_complete.simple.example.yaml templates/recruitment_complete.simple.yaml
```

## 실행

`.env`를 별도로 source할 필요가 없습니다.

```bash
python -m src.main
```

또는 설치된 엔트리포인트를 사용합니다.

```bash
owkr-gather-bot
```

## SQLite 데이터

기본 데이터베이스 위치는 현재 작업 디렉터리 기준 `data/owkr-gather-bot.sqlite3`입니다. 경로는 `OWKR_DATABASE_PATH`로 변경할 수 있습니다. `migrations/`의 SQL은 파일명 순서대로 한 번씩 자동 적용됩니다.

`002_multi_match.sql`은 기존 단일 세션 데이터를 보존하면서 길드당 활성 세션 UNIQUE 인덱스를 제거하고 다음 항목을 추가합니다.

- `match_code`, source request, `tier_anchor_message_id`
- 대기 사유와 충돌 대상 내전
- `tier_message_bindings`
- 티어 기준 메시지 관련 notification outbox 종류

기존 `tier_submissions`는 migration 시 `tier_message_bindings`로 자동 backfill되며, 기존 세션에는 결정적인 4자리 코드가 할당됩니다. 운영 적용 전에는 SQLite 파일과 WAL 파일을 함께 백업하세요.

초기 `002` 적용본과의 호환성을 위해 `003_tier_binding_backfill.sql`도 동일한 backfill을 idempotent하게 보장합니다.

`004_lobby_assignment.sql`은 각 내전에 배정된 대기실 채널과 이름을 저장해 재시작 후에도 배정이 바뀌지 않게 합니다.

테스트 데이터를 완전히 초기화하려면 봇을 먼저 정상 종료하고 필요한 백업을 만든 뒤 SQLite 본 파일과 WAL 보조 파일을 삭제합니다.

```bash
rm -f data/owkr-gather-bot.sqlite3
rm -f data/owkr-gather-bot.sqlite3-wal
rm -f data/owkr-gather-bot.sqlite3-shm
```

다시 시작하면 빈 데이터베이스와 스키마가 생성됩니다. 운영 데이터에는 이 절차를 사용하지 마세요.

## Discord Developer Portal과 최소 권한

Developer Portal의 Bot 설정에서 Privileged Gateway Intent 중 다음만 활성화합니다.

- Message Content Intent

코드는 다음 Gateway Intents를 요청합니다.

- Guilds
- Guild Messages
- Guild Message Reactions
- Message Content
- Guild Voice States

Guild Voice States는 Privileged Gateway Intent가 아니므로 Developer Portal에서 별도 스위치를 켤 필요는 없습니다. Guild Members와 Presence Intent는 사용하지 않습니다.

OAuth2 URL Generator에서는 `bot`, `applications.commands` scope를 선택하고 관련 채널에 다음 최소 권한을 부여합니다.

- View Channel — 명령·공지·티어·관리자 채널과 설정된 음성 대기실에서 필요
- Send Messages
- Read Message History
- Add Reactions — 모집 공지 채널에서 필요
- Embed Links — 모집 공지 카드 전송에 필요

`recruitment_role_id`가 실제 알림을 보내려면 해당 역할을 멘션 가능으로 설정하거나, 모집 채널에서 봇 역할에 `Mention @everyone, @here, and All Roles`를 허용해야 합니다. 최소 권한 운영에서는 전용 알림 역할만 멘션 가능으로 설정하는 방식을 권장합니다. 코드는 설정된 역할 ID 하나만 `allowed_mentions.roles`에 포함합니다.

대기실 입장 확인에는 두 음성 채널의 **채널 보기**만 필요하며 **연결**, **말하기** 권한은 필요하지 않습니다. 해당 내전에 저장된 대기실을 조회하지 못하면 전체 참가자를 임의로 멘션하지 않고 outbox 재시도로 처리합니다.

다음 권한은 부여하지 않습니다.

- Administrator
- Manage Messages
- Mention Everyone
- Manage Roles
- Kick Members
- Ban Members

## 슬래시 명령어

- `/내전 시간:오후 6시 20분 모드:6ㄷ6클래식`
- `/내전 시간:오후 2시 20분`
- `/내전 시간:18:20`
- `/내전 시간:23시`
- `/티어현황 [내전]` — 활성 내전이 하나면 자동 선택하고, 여러 개면 선택이 필요합니다.
- `/내전상태 [내전]` — 생략하면 가까운 미래의 활성 목록, 선택하면 시각·상태·참가·대기·티어 현황을 표시합니다.
- `/내전대타 내전` — 대기열이 비어 있는 모집 완료 내전에 연결된 1명짜리 대타 모집을 엽니다. 첫 ✅ 사용자는 대기열에 등록되고 본인만 멘션된 티어 작성 안내를 받습니다.
- `/내전취소 내전` — 선택한 내전만 취소합니다.
- `/공지문구` — 문구 편집과 미리보기를 한 메뉴에서 제공합니다.

모든 운영 명령은 `admin_user_ids` 또는 `admin_role_ids` 중 하나를 만족하고 `channels.command`에서 실행한 경우에만 처리됩니다. 이는 `app_commands.check` 기반의 봇 내부 검사로, discord.py의 `has_any_role`과 같은 계층에서 동작하지만 YAML 역할 ID와 사용자 예외를 함께 지원합니다. 역할이 없는 사용자가 Discord 명령 목록에서 명령 자체를 보지 못하게 하려면 서버 설정의 **연동 → OWKR Gather Bot → 명령어 권한**에서도 역할과 채널을 제한해야 합니다.

`/내전`의 `시간`과 `모드`, 세션을 받는 관리자 명령의 `내전` 옵션에는 자동완성을 제공합니다. 내전 선택지는 날짜·시각·관리자·모드·내전 코드를 표시하고 선택 값도 짧은 내전 코드를 사용합니다. 최대 25개까지 가까운 미래 순으로 표시하며 코드 검색이 가능합니다.

`/내전`은 시작 시각과 일정을 본인에게만 미리 보여줍니다. **시간·모드 수정**으로 기존 값이 채워진 수정창을 열 수 있고, **생성**을 눌러야 실제 모집 공지를 만듭니다. 생성 후에는 모집 공지로 이동하는 링크가 본인에게만 표시됩니다. `/내전취소`도 확인 버튼을 거치며, 취소가 끝나면 확인 메시지는 자동으로 사라집니다.

관리자 확인·오류 응답은 ephemeral로 보내며, 대기실 안내만 미입장 대상자를 멘션합니다. 활성 내전이 하나면 `/티어현황`은 내전 선택을 생략할 수 있습니다. 모집 완료 공지는 참가자 멘션과 공개 내전 코드 없이 전송합니다. `/공지문구`는 변수 없는 간단 편집과 미리보기를 한 메뉴에서 제공하며, 화면과 저장 결과는 실행한 관리자 본인에게만 보입니다. 편집한 모집 완료 공지는 Embed가 아닌 일반 텍스트 메시지로 전송됩니다. 길드 전용 명령은 봇 시작 시 동기화되므로 전역 명령 전파를 기다릴 필요가 없습니다.

`channels.announcement`는 `/내전` 실행 위치와 무관하게 모집 공지가 전송되는 고정 채널입니다. 공지 채널을 공지 전용으로 운영하려면 `channels.command`와 다른 채널 ID를 지정하고, Discord 채널 권한에서 일반 역할의 **메시지 보내기**를 차단해야 합니다. 봇의 역할 검사만으로 다른 사용자의 일반 메시지 작성까지 막을 수는 없습니다.

지난 시각은 자동으로 다음 날로 만들지 않으며, 확인 버튼 없이 거부합니다. 미래 시각을 다시 입력해야 합니다.

## 자동 테스트

pytest:

```bash
python3.13 -m pytest
```

기존 unittest 방식도 유지합니다.

```bash
python3.13 -m unittest discover -s tests -v
```

추가 검증:

```bash
python3.13 -m compileall src
python3.13 -m pip check
```

자동 테스트는 `.env` 우선순위와 경로 검증, 동일 시각 중복 생성 차단, 세션별 18명 burst, source request 멱등성, 다중 Actor 재시작 복구, 티어 답장/후보/영구 바인딩, 기준 메시지 재생성, bulk delete, 세션별 알림 멱등성, mention 제한, 4필드 웹 mapper를 포함합니다.

### 자동 검증 완료

다음 항목은 자동 테스트에서 검증했습니다.

- [x] 동일 시각 활성 내전의 중복 생성 차단
- [x] 세션별 18명 동시 반응의 첫 10명·대기열 결정
- [x] 중복 ADD, REMOVE 후 재ADD와 반응 순서 보존
- [x] 모집 완료·대기실 예약 알림의 중복 방지
- [x] 모집 완료 전 티어 거부와 유효 시간 상한 미포함 처리
- [x] 티어 답장, 단일 후보 자동 연결과 복수 후보 거부
- [x] 최신 티어 수정·삭제·재작성과 bulk delete 처리
- [x] FULL 이후 대기자 자동 승격 방지
- [x] 시작 이후 반응·티어 이벤트 무시
- [x] 다중 Session Actor와 SQLite 재시작 복구
- [x] 비활성화된 티어 후속 알림의 무음 처리와 notification outbox 복구
- [x] 1시간 미만 중첩 내전의 대기실 2 배정과 영속화
- [x] 내전별 1명 대타 모집과 첫 반응자의 대기열·티어 안내 연결
- [x] 지정 사용자 외 mention 차단
- [x] 웹 export DTO를 정확히 네 필드로 제한
- [x] 슬래시 명령어 등록 구조와 E2E 사전점검 검사

## 테스트 서버 E2E 체크리스트

운영 서버에는 배포하지 말고 별도의 테스트 길드에서 진행합니다. 사용자 계정을 자동화하거나 self-bot을 사용하지 않습니다. burst 테스트는 실제 사람의 테스트 계정 또는 관리자 협조로만 수행합니다.

테스트 기록에는 커밋 SHA, 테스트 시각, 사용한 봇 계정, 자동 테스트의 burst 수와 실제 Discord에서 수동 검증한 계정 수를 구분해 남깁니다.

테스트를 시작하기 전에 읽기 전용 사전점검을 실행합니다.

```bash
python -m src.e2e_preflight
```

설치된 엔트리포인트는 `owkr-gather-bot-e2e-check`입니다. 상세한 2명 테스트 순서는 [Discord E2E 문서](docs/discord-e2e.md)를 참고하세요.

### 1. 사전 설정

- [ ] 테스트 길드·명령·모집 공지·티어·관리자 채널 ID 입력
- [ ] 허용 관리자 사용자 또는 역할 ID 입력
- [ ] `.env`에 테스트 봇 토큰 입력
- [ ] Message Content Intent 활성화
- [ ] Guilds, Guild Messages, Guild Message Reactions, Message Content, Guild Voice States 수신 확인
- [ ] `defaults.lobby_voice_channel_id`에 실제 대기실 음성 채널 ID 입력
- [ ] `defaults.lobby_voice_channel_2_id`에 대기실 2 음성 채널 ID 입력
- [ ] 봇이 두 음성 채널을 모두 볼 수 있는지 확인
- [ ] 최소 권한만 부여하고 금지 권한이 없는지 확인
- [ ] `OWKR_LOG_LEVEL=DEBUG`로 테스트 로그 준비

### 2. 내전 생성

다음 명령을 각각 미래 시각으로 실행합니다.

```text
/내전 시간:오후 6시 20분 모드:6ㄷ6클래식
/내전 시간:오후 2시 20분
/내전 시간:18:20
/내전 시간:23시
```

- [ ] 각 시간 형식 파싱과 Asia/Seoul 일시 확인
- [ ] 모드 저장 및 모드 생략 허용 확인
- [ ] 시간·모드 수정창에 기존 값이 채워지고 확인 화면이 갱신됨
- [ ] 생성 확인창에서 취소하면 모집 공지가 생성되지 않음
- [ ] 생성 확인 후 본인에게만 모집 공지 링크가 표시됨
- [ ] 지정 모집 채널에 공지 전송 및 봇의 ✅ 반응 확인
- [ ] 지난 시각 입력 시 세션을 만들지 않고 미래 시각 재입력을 안내함
- [ ] 권한 없는 사용자와 잘못된 채널의 명령이 안전하게 거부됨
- [ ] 활성 내전과 같은 시작 시각으로 새 내전 생성 시 안전하게 거부됨
- [ ] 기존 내전 취소 또는 시간 변경 안내가 표시됨
- [ ] 공개 모집·모집 완료·티어 안내에 내부 내전 코드가 표시되지 않음
- [ ] 새 `/내전`을 만들어도 기존 공지·Actor·예약 알림이 유지됨

### 3. 참가 반응 burst

- [ ] 각 모집 공지에 12~18개 실제 계정으로 짧은 시간에 ✅ 반응
- [ ] 공지별 raw reaction이 섞이지 않고 첫 10명·대기열 분리
- [ ] 동일 사용자 중복 등록 없음
- [ ] 마지막 참가자 반응 제거 후 봇의 ✅가 자동으로 복구됨
- [ ] SQLite의 `reaction_events.arrival_seq`와 roster 순서 확인
- [ ] 최초 10명 도달 시 완료 공지가 한 번만 전송됨
- [ ] 완료 공지에서 참가자·대기자 사용자 ping이 발생하지 않음
- [ ] `@everyone`, `@here`, 역할 멘션이 실제 ping되지 않음
- [ ] FULL 이후 확정 참가자 제거에도 대기자 자동 승격이 없음

18개 계정 확보가 어렵다면 “자동 테스트 18명 통과 / 실제 Discord 수동 N명 확인”처럼 결과를 분리합니다.

### 4. 반응 제거와 시작 경계

- [ ] 확정 참가자와 대기자 반응 제거 시 현재 상태 갱신
- [ ] `/내전상태`의 확정·대기 인원 확인
- [ ] FULL 이후 자동 승격 없음
- [ ] 시작 시각 이후 반응 추가·제거 무시
- [ ] 시작 이후 일반 채널에 불필요한 안내가 없음
- [ ] 한 내전 취소·시작 후에도 다른 내전의 반응 처리가 유지됨

### 5. 티어 메시지

```text
우람한오크#3390 / 골5? / 실2 / 플3! (모이라 원챔) X
```

- [ ] `배틀태그 / 탱커 / 딜러 / 힐러` 순서가 안내됨
- [ ] `!` 선호·자신 있는 포지션, `?` 자신 없는 포지션으로 안내됨
- [ ] 마이크·브리핑이 어려운 경우 맨 끝에 `X`를 적도록 안내됨
- [ ] 현 시즌 최고 티어, 3단계 이상 차이면 현재 티어 기준이 안내됨
- [ ] 미배치·빠대 예상 티어, 부계정 통합, 원챔 영웅 표기가 안내됨
- [ ] 모집 완료 전 작성한 메시지는 불인정
- [ ] 모집 완료 후 내전별 티어 기준 메시지가 생성됨
- [ ] 기준 메시지 답장이 정확한 세션에만 연결됨
- [ ] 참가하지 않은 내전의 기준 메시지 답장은 거부됨
- [ ] 답장 없는 메시지의 후보가 하나면 자동 연결됨
- [ ] 답장 없는 메시지의 후보가 여러 개면 미저장되고 티어 안내 답장이 안내됨
- [ ] 일반 메시지는 `/` 또는 `#`가 없는 경우 자동 연결되지 않음
- [ ] 완료 전 기존 메시지를 완료 후 실제 수정하면 수정 시각 기준 인정
- [ ] 완료 후 새 메시지는 인정
- [ ] Discord 사용자 ID로 연결하고 표시 이름·배틀태그로 식별하지 않음
- [ ] 같은 사용자의 최신 `activity_at` 원문만 저장
- [ ] 같은 시각이면 더 큰 Discord message ID가 선택됨
- [ ] 대기자 티어는 저장 가능하지만 완료·미작성 알림에서 제외
- [ ] 현재 저장 메시지 삭제 시 미작성 전환
- [ ] 과거의 비현재 티어 메시지 삭제는 현재 제출에 영향 없음
- [ ] 빈 문자열 수정은 삭제와 동일하게 처리됨
- [ ] bulk delete가 각 바인딩 세션에만 적용됨
- [ ] 기준 메시지 삭제 시 같은 내전의 기준 메시지만 재생성됨
- [ ] 기준 메시지 재생성 후 기존 티어 데이터와 binding이 유지됨
- [ ] 유효 시간 안에 재작성 또는 기존 메시지 수정 시 완료 복구
- [ ] 티어 마감 시각 이상 작성·수정 무시
- [ ] 티어 마감 정각에도 미작성자 자동 멘션이 전송되지 않음
- [ ] 시작 시각 이후 작성·수정·삭제 무시
- [ ] 로그와 DB에 별도 티어 파싱 결과가 생성되지 않음

### 6. 관리자 명령과 티어 현황

- [ ] `/내전상태` 생략 시 활성 목록, 선택 시 해당 세션 상세 표시
- [ ] `/티어현황`이 선택한 세션의 확정 참가자 작성 상태만 표시
- [ ] `/내전상태` 상세에 티어 작성 완료 인원이 표시됨
- [ ] 티어 미작성·전원 완료 관련 자동 또는 수동 멘션 명령이 없음
- [ ] `/내전취소` 후 선택한 세션의 모집 공지·기준 메시지·Actor·예약만 중단
- [ ] 취소 확인창에서 돌아가기를 누르면 아무 변경이 없음
- [ ] 취소 성공 후 관리자 확인 메시지가 남지 않음
- [ ] `/공지문구`에서 편집과 미리보기를 모두 사용할 수 있음
- [ ] 모든 관리자 명령에서 권한 없는 사용자 거부

### 7. 대기실 안내

- [ ] 1시간 미만으로 겹치는 두 번째 내전은 대기실 2로 배정됨
- [ ] 정확히 1시간 이상 차이나는 내전은 대기실 1로 배정됨
- [ ] 생성 확인·모집 완료·대기실 안내에 배정된 대기실 이름이 표시됨
- [ ] 시작 10분 전 배정된 음성 대기실에 없는 확정 참가자만 멘션
- [ ] 이미 대기실에 들어온 확정 참가자는 멘션과 안내 본문에서 제외
- [ ] 확정 참가자 전원이 입장했다면 공개 안내가 전송되지 않음
- [ ] 대기자 제외 및 `allowed_mentions` 제한 확인
- [ ] 동일 안내 중복 전송 없음
- [ ] 안내 전 재시작 후 정상 복구
- [ ] 안내 시각 이후·시작 전 재시작 시 늦은 안내 한 번 전송
- [ ] 시작 이후 재시작 시 stale 안내 생략

### 8. 재시작 복구

모집 중, 10명 완료 직후, 일부 티어 작성 후, 대기실 안내 직전, outbox pending 상태에서 각각 프로세스를 정상 종료·재시작합니다.

- [ ] 모든 활성 세션과 모집 메시지 ID routing 복구
- [ ] 모든 티어 기준 메시지 ID routing 복구
- [ ] 세션별 참가자·대기열·다음 `arrival_seq` 복구
- [ ] 세션별 티어 binding과 제출 상태 복구
- [ ] 모집 완료·대기실 안내 중복 없음
- [ ] 세션별 대기실 1·2 배정 복구
- [ ] pending notification outbox 재시도
- [ ] 재시작 이후 새 내전 생성 시 기존 세션이 유지됨

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

정확히 이 네 필드만 생성합니다. 대기자, 철회자, 티어 미작성자는 제외하며 Discord UI 헤더를 재구성하거나 티어 문자열을 파싱하지 않습니다.

## 운영 서버 적용 전 주의사항

- 테스트 길드 E2E 결과와 실제 수동 계정 수를 기록합니다.
- 운영 토큰을 테스트 `.env`에 사용하지 않고 `.env`를 Git에 커밋하지 않습니다.
- SQLite 파일을 백업하고 프로세스를 한 인스턴스만 실행합니다.
- Discord REST 성공과 SQLite outbox 완료 기록 사이의 극히 짧은 crash window에서는 알림 재전송 가능성이 남습니다.
- 티어 채널이 매우 크면 재시작 보완 수집의 Discord history 확인 시간이 늘어날 수 있습니다.
- 실제 시작·마감 경계와 재시작 시나리오를 확인하기 전 운영 서버에 적용하지 않습니다.

`MatchRepository`와 `SyncSink` 경계 및 `NoOpSyncSink`는 후속 API 연동을 위해 유지합니다.
