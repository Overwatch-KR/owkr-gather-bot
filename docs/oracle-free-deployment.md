# Oracle Cloud 무료 VM 배포 안내

이 문서는 OWKR Gather Bot을 테스트 서버에서 24시간 체험하고, 이후 제한된 실제
내전 파일럿으로 이어가기 위한 배포 절차를 설명한다.

현재 봇은 단일 프로세스와 SQLite를 사용한다. 웹 요청이 없어도 Discord Gateway
연결을 계속 유지해야 하므로 절전되는 무료 웹 호스팅보다 영구 디스크가 있는 VM이
적합하다.

## 권장 배포 구조

```text
GitHub 저장소
    ↓ 배포 또는 업데이트
Oracle Cloud Ubuntu VM
    ├─ 봇 프로세스 1개
    ├─ 자동 재시작
    ├─ SQLite 영구 데이터
    └─ 로그와 백업
            ↓
        Discord 테스트 서버
```

Oracle Always Free 리소스와 정책은 변경될 수 있다. VM 생성 화면에서 반드시
`Always Free eligible` 표시를 확인한다.

- Oracle Always Free 리소스:
  <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
- Oracle Free Tier 안내:
  <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm>

무료 인스턴스는 용량 부족으로 생성하지 못하거나 장기간 낮은 사용량 때문에 회수될
수 있다. 무료 VM 자체를 영구 보관소로 신뢰하지 말고 별도 백업을 유지한다.

## 1. 준비물

- Oracle Cloud 계정
- Discord 테스트용 봇 토큰
- 테스트 길드 ID
- 명령·모집 공지·티어·관리자 채널 ID
- 대기실 1·2 음성 채널 ID
- 관리자 사용자 또는 역할 ID
- 모집 알림 역할 ID(사용하는 경우)
- SSH 키

운영 봇 토큰과 테스트 봇 토큰은 분리한다. 토큰, `.env`, 실제 설정 파일과 DB는
GitHub에 올리지 않는다.

## 2. Oracle VM 생성

Oracle Cloud Console에서 Compute Instance를 생성한다.

권장값:

| 항목 | 권장값 |
| --- | --- |
| 운영체제 | Ubuntu |
| Shape | `Always Free eligible` |
| CPU·메모리 | 1 OCPU, 1~2GB부터 시작 |
| Boot Volume | 기본값 또는 약 50GB |
| Public IPv4 | 활성화 |
| SSH | 본인 공개키 등록 또는 새 키 다운로드 |

네트워크 인바운드는 관리자 IP에서 들어오는 SSH 22번만 우선 허용한다. 봇은 외부의
Discord로 연결하므로 웹 서비스용 80·443 인바운드 포트는 필요하지 않다.

무료 Shape 용량이 없다면 유료 Shape를 선택하지 말고 다른 Availability Domain을
확인하거나 나중에 다시 시도한다.

## 3. SSH 접속

Mac 터미널에서 다음 형식으로 접속한다.

```bash
chmod 600 /path/to/oracle-key.pem
ssh -i /path/to/oracle-key.pem ubuntu@SERVER_IP
```

`SERVER_IP`는 Oracle Console에 표시되는 공용 IP로 바꾼다. Oracle Linux 이미지를
선택했다면 기본 사용자 이름이 다를 수 있으므로 Console의 접속 안내를 따른다.

## 4. 서버 기본 준비

서버 패키지를 갱신하고 Git, Docker와 Docker Compose를 설치한다. 배포 시점의 Ubuntu
버전에 맞는 Docker 공식 설치 안내를 사용한다.

- Docker Ubuntu 설치 안내:
  <https://docs.docker.com/engine/install/ubuntu/>
- Docker Compose 설치 안내:
  <https://docs.docker.com/compose/install/linux/>

설치 후 확인한다.

```bash
git --version
docker --version
docker compose version
```

Docker 서비스는 VM 재부팅 후에도 시작되도록 활성화한다. 일반 사용자에게 Docker
권한을 추가했다면 로그아웃 후 다시 SSH로 접속해야 적용될 수 있다.

## 5. 저장소 내려받기

```bash
git clone https://github.com/Overwatch-KR/owkr-gather-bot.git
cd owkr-gather-bot
```

현재 저장소에 Docker 배포 파일이 없다면 실제 실행 전에 다음 파일을 별도 변경으로
추가한다.

- `Dockerfile`
- `compose.yaml`
- `.dockerignore`
- SQLite 백업 스크립트

배포 파일은 다음 조건을 만족해야 한다.

- Python 3.13 이상
- 봇 컨테이너 한 개만 실행
- `restart: unless-stopped` 같은 자동 재시작 정책
- `data/`와 편집 가능한 공지 템플릿의 영구 보존
- `.env`를 이미지에 복사하지 않고 실행 시 주입
- SQLite를 여러 컨테이너가 동시에 열지 않음

## 6. Discord 운영 설정

예시 파일을 복사한다.

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
chmod 600 .env config/config.yaml
```

`config/config.yaml`에 실제 Discord ID를 입력한다. 특히 다음을 확인한다.

```yaml
defaults:
  participant_limit: 10
```

필수 설정:

- `guild_id`
- `channels.command`
- `channels.announcement`
- `channels.tier`
- `channels.admin`
- `admin_user_ids` 또는 `admin_role_ids`
- `defaults.lobby_voice_channel_id`
- 필요 시 `defaults.lobby_voice_channel_2_id`

`.env`에는 테스트 봇 토큰을 입력한다.

```dotenv
DISCORD_BOT_TOKEN=실제-테스트-봇-토큰
OWKR_LOG_LEVEL=INFO
```

토큰을 터미널 출력, 로그, 스크린샷 또는 Git 커밋에 남기지 않는다.

## 7. Discord Developer Portal 설정

Bot 설정에서 Message Content Intent를 활성화한다.

초대 URL에는 다음 scope를 사용한다.

- `bot`
- `applications.commands`

관련 채널에만 다음 권한을 준다.

- View Channel
- Send Messages
- Read Message History
- Add Reactions
- Embed Links

Administrator, Manage Roles, Kick Members와 Ban Members는 주지 않는다. 모집 역할을
실제로 멘션하려면 해당 역할을 멘션 가능하게 하거나 봇에 필요한 역할 멘션 권한만
부여한다.

## 8. 읽기 전용 사전점검

봇을 중지한 상태에서 사전점검을 실행한다. Docker 배포 파일이 준비되면 컨테이너
안에서 다음 엔트리포인트를 실행하도록 구성한다.

```bash
owkr-gather-bot-e2e-check
```

다음 항목을 확인한다.

- 테스트 봇 토큰과 길드 연결
- Message Content Intent
- 모든 설정 채널과 음성 대기실 존재
- 최소 Discord 권한
- 관리자와 모집 역할
- 필수 슬래시 명령 6개

`FAIL`이 하나라도 있으면 봇을 운영하지 않는다. `WARN`은 내용을 이해하고 허용할 수
있는 경우에만 진행한다.

## 9. 봇 실행

Docker 배포 파일이 준비된 뒤 실행한다.

```bash
docker compose up -d --build
docker compose logs -f bot
```

다음 로그를 확인한다.

```text
SQLite connected and migrations completed
active session recovery completed
guild application commands synced
bot ready
```

명령 구조가 바뀌지 않았다면 `guild application commands unchanged; sync skipped`가
표시될 수 있다.

한 VM에서 봇 컨테이너가 정확히 하나만 실행 중인지 확인한다.

```bash
docker compose ps
```

## 10. 테스트 서버 체험 순서

정식 운영 전에 다음 순서로 관리자 체험을 진행한다.

1. `/내전`으로 미래 시각 내전을 생성한다.
2. 같은 시각으로 다시 만들었을 때 생성이 거부되는지 확인한다.
3. 시간·모드 수정과 생성 확인 버튼을 사용한다.
4. 서로 다른 10명이 ✅ 반응한다.
5. 모집 완료 공지와 티어 안내 메시지를 확인한다.
6. 참가자가 해당 티어 안내 메시지에 답장한다.
7. `/티어현황`과 `/내전상태`를 확인한다.
8. 참가자 이탈 상황에서 `/내전대타`를 실행한다.
9. 시작 10분 전 대기실 미입장자만 안내되는지 확인한다.
10. 봇을 재시작하고 상태와 메시지 연결이 복구되는지 확인한다.
11. `/내전취소`로 테스트 데이터를 정리한다.

상세 E2E 절차는 [discord-e2e.md](discord-e2e.md)를 따른다.

## 11. 재시작과 로그 확인

```bash
docker compose restart bot
docker compose logs --tail=200 bot
```

재시작 후 다음을 확인한다.

- `bot ready`
- 활성 세션 복구 수
- migration 오류 없음
- Discord 권한 오류 없음
- 알림의 중복 전송 없음

## 12. 업데이트

업데이트 전 DB를 백업한다. 그다음 원격 변경을 가져와 다시 빌드한다.

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=200 bot
```

업데이트 후 사전점검과 핵심 관리자 명령을 다시 확인한다.

## 13. 백업

최소 백업 대상:

- `data/owkr-gather-bot.sqlite3`
- `data/owkr-gather-bot.sqlite3-wal`
- `data/owkr-gather-bot.sqlite3-shm`
- `config/config.yaml`
- 편집된 모집 완료 공지 템플릿과 단순 설정 YAML

처음 운영할 때는 가장 단순하고 안전한 방식으로 봇을 잠깐 멈추고 전체 `data/`와
설정 파일을 함께 복사한다.

```bash
docker compose stop bot
mkdir -p backups/manual-backup
cp -a data config/config.yaml templates/recruitment_complete.txt templates/recruitment_complete.simple.yaml backups/manual-backup/
docker compose start bot
```

`manual-backup`은 매번 다른 이름을 사용하고, 같은 VM 안에만 두지 말고 주기적으로
개인 PC나 별도 저장소로 복사한다. 자동 백업을 추가할 때는 실행 중 DB 파일을 단순
복사하지 말고 SQLite backup API 또는 안전한 정지·복사 절차를 사용한다.

## 14. 장애 시 대응

봇이 응답하지 않으면 다음 순서로 확인한다.

1. Oracle VM이 실행 중인지 확인한다.
2. `docker compose ps`로 컨테이너 상태를 확인한다.
3. 최근 로그를 확인한다.
4. Discord 토큰과 Gateway Intent를 확인한다.
5. 채널·역할 ID와 권한 변경 여부를 확인한다.
6. 디스크 공간과 SQLite 파일 존재 여부를 확인한다.
7. 재시작 전에 DB를 백업한다.

DB가 손상됐거나 사라졌다면 새 빈 DB로 임의 실행하지 말고 봇을 중지한 상태에서
가장 최근 백업을 복원한다.

## 15. 정식 도입 판단 기준

다음 조건을 모두 충족하면 실제 내전 한 회의 제한 파일럿을 진행한다.

- 사전점검 `FAIL` 0개
- 관리자와 참가자 전체 흐름 성공
- 10명 모집과 티어 작성 확인
- 같은 시각 중복 생성 차단 확인
- 대타와 대기실 안내 확인
- 재시작 복구 확인
- 백업 파일을 다른 위치에서 확인
- 장애 시 기존 수동 모집으로 전환할 담당자 지정

파일럿이 안정적으로 끝난 뒤에만 정식 운영으로 전환한다.
