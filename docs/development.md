# 시간을달리는소녀 개발 및 운영 안내

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
- `channels.warning`: 경고 전용 채널 ID. 없거나 null이면 경고 명령 거부
- `policy.privacy_url`, `policy.terms_url`: 최종 공개 문서의 HTTPS 주소
- `policy.support_contact`: 담당자와 문의 경로
- `admin_user_ids`: 역할과 별개로 허용할 관리자 사용자 ID
- `admin_role_ids`: 내전 관리자·스태프처럼 운영 명령을 허용할 역할 ID 목록
- `defaults.recruitment_role_id`: 모집 공지에서 한 번 멘션할 알림 역할 ID
- `defaults.lobby_voice_channel_id`: 대기실 1 음성 채널 ID
- `defaults.lobby_voice_channel_2_id`: 겹치는 내전에 사용할 대기실 2 음성 채널 ID
- `defaults.lobby_name`, `defaults.lobby_2_name`: 공지에 표시할 대기실 이름
- `managers`: 관리자별 기본 모드·추가 문구. 과거 `pick_limit`·`ban_list` 설정은 읽기 오류 없이 무시하며 공지에 표시하지 않습니다.

모드는 명령과 관리자 설정에서 모두 생략할 수 있습니다. 이때 DB에는 `null`로 저장되고 `mode_display_fallback`도 `null`이면 공지에서 모드 줄을 생략합니다.

## 모집 완료 공지 문구

Discord의 공지 편집 명령은 제공하지 않습니다. 기본 공지는 그대로 사용하고,
변경이 필요하면 운영자가 `templates/recruitment_complete.txt`를 수정합니다.
내전별 티어 안내에 쓰이는 작성 양식·예시는 `templates/recruitment_complete.simple.yaml`에서
관리합니다. 한 파일을 바꿔도 다른 파일을 자동 생성하거나 덮어쓰지 않습니다.

수정한 파일은 다음 전송 때 읽습니다. 템플릿 오류·메시지 길이는 전송 단계에서
검사되므로 운영 반영 전에 테스트 서버에서 문구와 멘션 차단을 확인해야 합니다.
모집 완료 공지는 참가자 전원 멘션과 공개 내부 코드 없이 일반 텍스트로 보냅니다.
기존 운영 템플릿과 문구 파일은 자동 삭제·초기화하지 않습니다.

기본 원본으로 되돌릴 때는 기존 파일을 보관한 뒤 다음 예제를 사용합니다.

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

Oracle Cloud 무료 VM에서 테스트 운영을 준비하려면
[Oracle Cloud 무료 VM 배포 안내](oracle-free-deployment.md)를 참고하세요.

저장소에는 Docker 배포 구성과 정지 후 백업 도구가 포함되어 있습니다.
서버 준비·권한·최초 명령 등록·백업 복원은 위 안내 순서로 진행합니다.
Docker에서는 DB를 호스트의 `data/`에, 설정과 공지 문구를 각각 `config/`와
`templates/`에 보존합니다. 자동 재시작과 journald 로그 보관 제한을 사용하며,
중지 시 정상 종료를 위해 SIGINT와 최대 60초 대기를 적용합니다.
실제 서버 배포와 Discord E2E 완료 여부는 별도 기록으로 확인해야 합니다.

현재 파일럿 운영 시간은 **한국 시간 월~목 20:00~다음 날 02:00, 금~일 24시간**입니다.
일요일에서 이어지는 월요일 새벽도 02:00까지 운영합니다. VM과 디스크는 유지하고
봇 프로세스만 정지하며, 시각 확인 간격 때문에 전환에 약 1분이 걸릴 수 있습니다.
운영 시간 밖에는 명령·반응·티어·알림·개인정보 정리가 실행되지 않습니다.
중지 중 시작·취소된 내전 정리는 다음 가동 시 처리하며, 누락된 안내는 관리자가
수동 진행 여부를 판단합니다. 설치·일시 점검은 [운영 시간 설정](oracle-free-deployment.md#운영-시간-자동-조정)을 따릅니다.

## SQLite 데이터

기본 데이터베이스 위치는 현재 작업 디렉터리 기준 `data/owkr-gather-bot.sqlite3`입니다. 경로는 `OWKR_DATABASE_PATH`로 변경할 수 있습니다. `migrations/`의 SQL은 파일명 순서대로 한 번씩 자동 적용됩니다.

`002_multi_match.sql`은 기존 단일 세션 데이터를 보존하면서 길드당 활성 세션 UNIQUE 인덱스를 제거하고 다음 항목을 추가합니다.

- `match_code`, source request, `tier_anchor_message_id`
- 대기 사유와 충돌 대상 내전
- `tier_message_bindings`
- 티어 기준 메시지 관련 notification outbox 종류

기존 `tier_submissions`는 migration 시 `tier_message_bindings`로 자동 backfill되며, 기존 세션에는 결정적인 4자리 코드가 할당됩니다. 운영 적용 전에는 `scripts/backup.sh`로 봇을 정상 종료하고 SQLite backup API로 WAL을 합친 백업을 만드세요. 실행 중인 파일의 단순 복사는 일관된 백업을 보장하지 않습니다.

초기 `002` 적용본과의 호환성을 위해 `003_tier_binding_backfill.sql`도 동일한 backfill을 idempotent하게 보장합니다.

`004_lobby_assignment.sql`은 각 내전에 배정된 대기실 채널과 이름을 저장해 재시작 후에도 배정이 바뀌지 않게 합니다.

`005_substitute_recruitment.sql`은 내전별 대타 모집을, `006_privacy_and_warnings.sql`은
경고와 티어 재수집 중단 기록을 추가합니다. 마이그레이션은 기존 DB를 확장하지만,
봇의 후속 정리 작업은 시작·취소된 내전 기록을 삭제합니다. 배포 전 복구용 백업과
개인정보 요청 반영 절차를 확인해야 합니다.

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

- View Channel · 명령·공지·티어·관리자 채널과 설정된 음성 대기실에서 필요
- Send Messages
- Read Message History
- Add Reactions · 모집 공지 채널에서 필요
- Embed Links · 모집 공지 카드 전송에 필요

`recruitment_role_id`가 실제 알림을 보내려면 해당 역할을 멘션 가능으로 설정하거나, 모집 채널에서 봇 역할에 `Mention @everyone, @here, and All Roles`를 허용해야 합니다. 최소 권한 운영에서는 전용 알림 역할만 멘션 가능으로 설정하는 방식을 권장합니다. 코드는 설정된 역할 ID 하나만 `allowed_mentions.roles`에 포함합니다.

대기실 입장 확인에는 두 음성 채널의 **채널 보기**만 필요하며 **연결**, **말하기** 권한은 필요하지 않습니다. 해당 내전에 저장된 대기실을 조회하지 못하면 전체 참가자를 임의로 멘션하지 않고 outbox 재시도로 처리합니다.

사용자 멘션에는 별도 멘션 권한이 필요하지 않습니다. Discord에는 `@everyone`만
제외하고 멘션 불가 역할 모두를 멘션하도록 허용하는 별도 권한이 없습니다.
전용 역할을 멘션 가능으로 설정하면 일반 사용자도 그 역할을 멘션할 수 있습니다.
역할 알림이 필요 없으면 `recruitment_role_id`를 비워 둡니다. 봇 코드는
`@everyone`·`@here`를 허용하지 않습니다.

✅ 같은 기본 이모지에 이모지 관리 권한은 필요하지 않습니다. 첫 반응과 복구를
위해 모집 채널의 Add Reactions와 Read Message History는 유지합니다.
반응 수신과 사용자·역할 멘션 자체에는 Message Content Intent가 필요하지 않으며,
현재 그 Intent는 티어 원문 수집·수정·복구에 사용합니다. 검증 전에는 끄지 않습니다.
경고 카운트는 관리자가 명시적으로 기록하며 횟수 저장 자체에 Discord 제재·역할
관리 권한은 필요하지 않습니다. 경고 재적 확인은 대상별 REST 조회를 사용하며
Guild Members Intent를 새로 요청하지 않습니다.

권한은 해당 기능이 사용하는 채널에서만 허용합니다. 관련 없는 채널 접근을
제한하고 다른 역할과 `@everyone`에서 상속한 권한까지 포함해 실제 권한을 확인합니다.

다음 권한은 부여하지 않습니다.

- Administrator
- Manage Messages
- Mention Everyone
- Manage Roles
- Kick Members
- Ban Members

## 슬래시 명령어

공개 명령은 다음 6개입니다.

| 명령 | 기능 | 실행 자격·채널 |
| --- | --- | --- |
| `/내전 시간 [모드] [취소시각]` | 확인 후 모집 생성 | 관리자·명령 채널 |
| `/티어현황 [내전]` | 참가·대기 인원과 티어 작성·미작성 이름 | 관리자·명령 채널 |
| `/내전대타 내전` | 대기열이 빈 모집 완료 내전의 1명 대타 모집 | 관리자·명령 채널 |
| `/내전취소 내전` | 확인 후 선택 내전 취소 | 관리자·명령 채널 |
| `/경고 대상 [작업] [사유]` | 조회·기록·최근 경고 취소·전체삭제 | 관리자·경고 채널 |
| `/개인정보` | 보관·문의 안내와 본인 티어 삭제 | 일반 이용자·운영 서버 |

`/내전 시간:오후 6시 20분 모드:6ㄷ6클래식` 또는 `/내전 시간:18:20`처럼 입력합니다.
모드는 생략할 수 있고 입력한 시간은 한국 시간의 오늘 일정으로 해석합니다.
`/티어현황`은 활성 내전이 하나면 자동 선택하고 여러 개면 내전을 선택해야 합니다.
모집 완료 전에는 참가·대기 인원, 완료 후에는 티어 현황도 함께 보여줍니다.
대타는 자동 확정 승격되지 않고 대기자로 등록되며 본인에게만 티어 작성 멘션이 갑니다.
기존 `.내전`·`.경고`, 제거한 `/내전상태`·`/공지문구`는 현재 사용 경로가 아닙니다.

내전 운영 명령은 `admin_user_ids` 또는 `admin_role_ids` 중 하나를 만족하고 `channels.command`에서 실행한 경우에만 처리됩니다. 이는 `app_commands.check` 기반의 봇 내부 검사로, discord.py의 `has_any_role`과 같은 계층에서 동작하지만 YAML 역할 ID와 사용자 예외를 함께 지원합니다. `/경고`는 같은 관리자 자격을 검사하되 `channels.warning`에서만 실행하고, `/개인정보`는 일반 이용자도 본인 정보를 위해 실행할 수 있습니다. 역할이 없는 사용자가 Discord 명령 목록에서 관리 명령 자체를 보지 못하게 하려면 서버 설정의 **연동 → OWKR Gather Bot → 명령어 권한**에서도 역할과 채널을 제한해야 합니다.

`/내전`의 `시간`과 `모드`, 세션을 받는 관리자 명령의 `내전` 옵션에는 자동완성을 제공합니다. 내전 선택지는 한국어 시각·관리자·모드·대기실을 표시하며 내부 선택 값으로 내전을 구분합니다. 최대 25개까지 가까운 미래 순으로 표시하며 코드 검색이 가능합니다.

`/내전`은 시작 시각과 일정을 본인에게만 미리 보여줍니다. **시간·모드·취소 시각 수정**으로 기존 값이 채워진 수정창을 열 수 있고, **생성**을 눌러야 실제 모집 공지를 만듭니다. 생성 후에는 모집 공지로 이동하는 링크가 본인에게만 표시됩니다. `/내전취소`도 확인 버튼을 거치며, 취소가 끝나면 기존 모집 공지와 모든 반응을 삭제하고 시작 시각을 포함한 취소 안내를 새로 남기며, 관리자에게도 취소 결과가 본인 전용으로 남습니다. 취소 안내에는 참가용 ✅ 반응을 붙이지 않습니다. 취소 안내 …1122 tokens truncated…개 문서가 확정되면 `policy.privacy_url`, `policy.terms_url`에 HTTPS 주소를,
`policy.support_contact`에 실제 문의 경로를 설정합니다. 미설정 시 링크를 꾸며내지 않고
서버 관리자에게 비공개 문의하도록 안내합니다. 예제의 담당자는 개발자 겸 내전 총관리자
qtaghdi이며 이메일은 qtaghdi@gmail.com, 보조 경로는 Discord `qtaghdi`의 DM입니다.
운영 설정의 `policy.support_contact`에도 반영해야 합니다. 법정 책임자 표시 요건과
도쿄 VM·오전 6시 백업·수동 Mac 복사본과 확인한 보호 조치는 [정책 대조 기록](https://github.com/Overwatch-KR/owkr-docs/blob/main/docs/gather-bot/review-2026-10-28.md)에 정리했습니다. 로그는 용량 제한이며, 로그·백업·문의 자료의 일수 기준 보관 기한과 국외 이전 등 최종 고지 사항은 [문서 관리 안내](legal/README.md)의 미완료 항목으로 유지합니다.

## 자동 테스트

GitHub Actions의 [CI](https://github.com/Overwatch-KR/owkr-gather-bot/actions/workflows/ci.yml)는
`main` 대상 PR에서 Ubuntu·Python 3.13으로 아래 필수 검사를 실행합니다.
`main` push에서는 CI를 실행하지 않습니다.
기본 브랜치에 반영한 뒤 Actions 화면에서 수동 실행할 수도 있습니다.

- 테스트 의존성까지 설치: `python -m pip install -e ".[test]"`
- 전체 자동 테스트: `python -m pytest`
- 소스 컴파일: `python -m compileall src`
- 의존성 호환성: `python -m pip check`
- Docker 이미지 빌드·실행 파일 import·Compose 설정 검사

같은 PR·브랜치의 새 실행이 시작되면 이전 실행을 취소하며 최대 실행 시간은 10분입니다.
CI는 저장소 읽기 권한만 사용하고 Discord 토큰·운영 설정·운영 DB를 요구하지 않습니다.
실제 Discord 연결과 서버 E2E는 [별도 점검](discord-e2e.md)으로 진행합니다.
자동 배포나 병합은 수행하지 않습니다.

main에 반영하면 별도 `Bot release image` 워크플로가 Linux amd64 배포 이미지를
빌드하고 `bot-image-<커밋 SHA>` 아티팩트로 14일간 보관합니다. 봇을 상시 실행하는
작업은 아니며 실제 서버와 설정이 필요합니다. `Public policy pages`도 모든 main
push에서 실행해 공개 안내 웹을 배포합니다. 자세한 사용법은
[VM 배포 안내](oracle-free-deployment.md)를 따릅니다.

CI 통과를 병합 필수 조건으로 사용할 때는 첫 실행을 확인한 후 저장소 규칙에서
`Python 3.13 checks` 상태 검사를 지정합니다. 워크플로 추가만으로 병합 규칙이
자동 설정되지는 않습니다.

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

자동 테스트는 `.env` 우선순위와 경로 검증, 동일 시각 내전 분리·대기실 정원 검사, 세션별 18명 burst, source request 멱등성, 다중 Actor 재시작 복구, 티어 답장 전용 신규 수집/기존 메시지 연결 호환, 기준 메시지 재생성, bulk delete, 세션별 알림 멱등성, mention 제한, 4필드 웹 mapper를 포함합니다.

### 자동 검증 완료

다음 항목은 자동 테스트에서 검증했습니다.

- [x] 동일 시각 내전 분리·대기실 정원 검사
- [x] 세션별 18명 동시 반응의 첫 10명·대기열 결정
- [x] 중복 ADD, REMOVE 후 재ADD와 반응 순서 보존
- [x] 모집 완료·대기실 예약 알림의 중복 방지
- [x] 모집 완료 전 티어 거부와 유효 시간 상한 미포함 처리
- [x] 티어 안내 답장만 신규 제출로 수집, 기존 연결 메시지 수정·삭제 호환
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

## Discord 서버 점검

[Discord E2E 점검](discord-e2e.md)에서 테스트 길드의 모집·티어·대기실 안내와 재시작 복구를 확인합니다.

## 웹 DTO

실제 외부 서비스 전송은 구현하지 않았습니다. 유지 중인 export 준비 코드에서는
현재 확정 참가자이면서 티어를 작성한 사용자만 대상으로 합니다.

```json
{
  "discordUserId": "123456789012345678",
  "discordDisplayName": "뿅뿅이",
  "rawTierMessage": "뿅뿅이#31243/챔1/챔1/챔1",
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

## 예약 내전의 모집 미완료 자동 취소

`/내전 시간:오후 8시 취소시각:오후 7시 30분`처럼 선택적으로 지정합니다.
취소 시각은 오늘의 미래 시각이면서 시작보다 빨라야 합니다. 비워 두면 자동 취소하지 않습니다.
생성 확인과 수정창·모집 공지에 취소 시각을 표시합니다. 확인 중 시간이 지났으면 생성하지 않고 다시 입력하도록 안내합니다.

지정 시각 전까지 최초 10명 모집을 완료하지 못한 내전만 취소합니다. 한 번 모집 완료된 내전은
이후 이탈로 10명 미만이 되어도 자동 취소하지 않으며 기존 대타 모집 정책을 따릅니다.
시각 경계에서는 취소 시각 직전까지 접수한 반응을 순서대로 처리하고 판정하며, 마감 시각부터의 신규 반응은 모집을 완료시키지 않습니다.

가동 중 약 10초 간격으로 확인하고, 비운영 시간에는 동작하지 않습니다. 다음 가동 때 시작 시각이
이미 지났더라도 모집 미완료 취소를 먼저 처리합니다. 취소는 대기실 예약을 해제하고 공지·반응·티어 안내·대타 공지를 정리하며 멘션 없는 취소 안내를 남깁니다.
자동 취소의 메시지 정리에 필요한 시각·채널·메시지 ID만 별도 저장하여 DB 원본 정리 후에도 실패·재시작 시 재시도하고, 완료하면 삭제합니다.
공지 권한 오류 등으로 오래 남은 작업은 운영 로그에서 확인하고 권한을 복구합니다.
Discord 전송 직후 성공 기록 전에 강제 종료되면 취소 안내가 중복될 수 있어 관리자가 확인합니다.

### 모집 공지를 삭제한 경우

시작 전 모집 원본 메시지를 삭제하면 그 내전도 취소됩니다. 여러 메시지를 한꺼번에 삭제한 경우에도
각 모집 메시지와 연결된 내전만 취소합니다. 티어 안내 삭제는 기존처럼 안내 재생성으로 처리하고,
대타 공지·일반 메시지·취소 안내 삭제로 내전 전체를 취소하지 않습니다.
봇이 직접 취소 처리 중 모집 공지를 지운 삭제 이벤트도 취소를 중복 생성하지 않습니다.

접속·재접속 때 기존 모집 공지를 확인하여, 중지 중 삭제된 공지가 확실히 없는 경우에도 취소합니다.
권한 거부나 일시적인 통신 오류는 삭제로 판단하지 않습니다. 이미 시작된 내전은 자동 처리 대상이 아닙니다.
취소 이후 메시지 정리는 자동 취소와 같은 최소 기록·재시도 절차를 사용합니다.

## 보관 기한 집행

운영 담당자가 2026-10-28 승인한 기준은 로그 최대 30일, VM·Mac 백업 생성 후 7일, 문의 메일 처리 종료 후 90일, 최소 요청 처리 기록 처리 종료 후 1년이다. 내전 DB·경고 보관 기준은 바뀌지 않는다. 공개 정책 본문은 OWKR Docs만 원본으로 사용한다.

VM·Mac의 만료 백업은 `scripts/retention.py`로 매시간 확인한다. 기본 실행은 미리보기이며 `--apply`에서만 삭제한다. VM은 기존 운영·백업 잠금을 사용한다. 미완성·잘못된 백업 기록은 자동 파기하지 않고 오류를 확인한다. Mac이 중지되면 다음 로그인·가동 시 정리한다. 문의 메일·요청 기록은 담당자가 매일 만료 여부와 실제 파기를 확인한다.

Ubuntu VM에서는 `journald`로 봇 로그를 모으고 `deploy/journald-owkr.conf`의 30일·용량 제한 및 매시간 rotate/vacuum을 사용한다. 용량 한도로 30일보다 먼저 삭제될 수 있다. 기존 json-file 로그는 컨테이너 재생성으로 정리하며, 별도 syslog·접속 로그 사본은 호스트 logrotate의 30일 제한을 함께 확인한다. `docker compose logs`는 계속 사용할 수 있다.

### 삭제 요청 검증 기록

[삭제 요청·백업 복원 검증](deletion-request-verification.md)은 가상 데이터로 티어 삭제·백업 복원·재수집 차단·만료 정리를 확인한 기록입니다. 실제 운영 복사본의 전체 삭제 완료를 뜻하지 않으며, 삭제 전 백업을 복원할 때는 Discord 연결 전에 요청을 재반영해야 합니다.
