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

저장소의 `Dockerfile`, `compose.yaml`, `.dockerignore`를 사용한다.
Python 3.13, 단일 컨테이너, 자동 재시작, 영구 DB, 읽기 전용 설정·템플릿,
로그 크기 제한을 포함한다. 실행 파일은 이미지에, 운영 자료는 호스트에 둔다.
백업은 `scripts/backup.sh`로 정지 후 생성한다.

호스트의 사용자 ID로 컨테이너를 실행한다. 이후 모든 Compose 작업을 같은
저장소·사용자 환경에서 수행하고 다른 VM이나 로컬 PC에서 같은 봇을 동시에 켜지 않는다.

```bash
mkdir -p data backups
chmod 700 data backups
```

## 6. Discord 운영 설정

예시 파일을 복사한다.

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
chmod 600 .env config/config.yaml
# 두 값을 .env에 저장한다. 기존 값이 있으면 중복 추가하지 않고 수정한다.
echo "OWKR_UID=$(id -u)" >> .env
echo "OWKR_GID=$(id -g)" >> .env
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

이미지를 먼저 빌드한다. 사전점검은 봇이 중지된 상태에서 실행한다.

```bash
docker compose build bot
docker compose run --rm --no-deps bot python -m src.e2e_preflight
```

첫 설치에서 명령이 아직 등록되지 않아 실패하면, 다른 설정 오류를 먼저 해결하고
9절처럼 봇을 한 번 실행하여 명령 등록 로그를 확인한다. 그다음 봇을 중지하고 위
사전점검을 다시 실행한다. 임시 컨테이너의 명령은 점검 도구로 대체되므로 봇을
추가로 실행하지 않는다. 설치된 점검 명령은 다음과 같다.

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

### main 반영과 배포 이미지

main 대상 PR에서는 CI가 테스트·컴파일·의존성과 Docker 빌드를 검사한다.
main push에서는 CI를 반복하지 않고 `Bot release image`와 `Public policy pages`를
실행한다. Pages는 공개 안내 웹만 배포한다. 배포 이미지 워크플로는 Linux amd64용
이미지·커밋 파일·체크섬을 14일간 아티팩트로 보관하며 실제 봇 서버를 실행하지 않는다.

서버가 준비되면 해당 커밋의 저장소와 운영 설정을 준비한 후, Actions 실행 화면에서
`bot-image-<커밋 SHA>`를 받아 저장소 루트에 압축 해제하고 다음처럼 사용한다.
ARM 서버에서는 아래 이미지를 사용하지 않고 서버에서 직접 빌드한다.

```bash
sha256sum -c SHA256SUMS
docker load -i owkr-gather-bot.tar.gz
export OWKR_IMAGE="owkr-gather-bot:$(cat commit.txt)"
docker compose up -d --no-build bot
```

체크섬 실패 시 실행하지 않는다. 이후 같은 서버의 작업에도 `OWKR_IMAGE`를 동일하게
설정해야 하므로 실제 값을 `.env`에도 저장한다. 새 이미지 전환 전에는 13절대로
백업한다. 이미지 로드만으로 기존 컨테이너가 바뀌지는 않는다.

### 서버에서 직접 빌드

설정·디렉터리 권한과 사전점검 결과를 확인한 뒤 실행한다.

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
6. 참가자가 해당 티어 안내 메시지에 `뿅뿅이#31243/챔1/챔1/챔1`처럼 답장한다.
7. `/티어현황`에서 참가·대기·티어 작성 상태를 확인한다.
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

배포할 커밋의 GitHub Actions CI(테스트·컴파일·의존성 검사)가 통과했는지 먼저 확인한다.
CI는 배포하거나 Discord에 연결하지 않으므로 실제 서버 E2E 결과도 별도로 확인한다.

업데이트 전 `sh scripts/backup.sh`로 DB·설정·문구를 백업하고 백업 폴더를
VM 밖으로 복사한다. 배포 커밋도 기록한다. 그다음 원격 변경을 가져와 다시 빌드한다.

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=200 bot
```

업데이트 후 사전점검과 핵심 관리자 명령을 다시 확인한다.

## 13. 백업

백업 명령은 실행 중이던 봇을 중지하고 DB와 설정·문구를 보관한 뒤 다시 켠다.
이미 중지된 봇은 계속 중지 상태로 둔다. 강제 종료(종료 코드 137)를 발견하면
자동으로 재개하지 않는다. 로그와 DB 상태를 확인한 후 수동으로 시작한다.
다른 배포 작업과 동시에 실행하지 않는다.

```bash
sh scripts/backup.sh
```

`backups/backup-<UTC시각>/`에 다음을 보관한다.

- SQLite backup API로 WAL을 합친 단독 DB와 무결성 검사 결과 통과
- `config/config.yaml`과 `templates/` 전체
- 생성 시각과 복원 주의사항을 담은 `manifest.json`

실패한 백업 폴더는 지우고 완성된 백업만 남긴다. 토큰이 있는 `.env`는 포함하지
않으므로 별도 비밀 저장소에서 복구할 수 있도록 준비한다. 백업은 Git과 이미지에서
제외한다. 백업 주기·보관 기간·암호화·외부 복사 위치를 정하고, 최소한 배포 전과
내전 준비 후에 실행한다. 봇이 사용하는 실제 경로는 Compose에서 고정하며,
호스트 `.env`의 경로 재정의는 컨테이너에 적용하지 않는다.

### 복원 연습과 장애 복원

테스트 복사본에서 먼저 연습한다. 운영 복원은 봇을 멈추고 진행한다.
아래 백업 이름은 실제 폴더로 바꾼다. `data.before-restore`가 이미 있으면 다른
이름을 사용하고 덮어쓰지 않는다.

```bash
docker compose stop bot
mv data data.before-restore
mkdir -m 700 data
cp backups/backup-실제시각/data/owkr-gather-bot.sqlite3 data/
```

기존 WAL·SHM은 새 `data/`로 복사하지 않는다. 백업 DB는 WAL을 합친 파일이다.
필요할 때만 현재 설정과 문구를 별도 보관한 후 백업본을 복사하고, `.env`의 토큰은
별도 비밀 저장소에서 준비한다. 호스트 사용자와 `.env`의 UID/GID가 일치하는지 확인한다.
이전 백업 이후의 개인정보 삭제 요청·수집 중단을 **재시작 전에** 반영한다.
반영 방법이 불명확하면 봇을 중지한 채 담당자가 확인한다.

DB 검사와 사전점검 후 봇을 시작한다. 검사 컨테이너는 Discord에 연결하지 않는다.

```bash
docker compose run --rm --no-deps bot python -c 'import sqlite3; c=sqlite3.connect("file:/app/data/owkr-gather-bot.sqlite3?mode=ro", uri=True); result=c.execute("PRAGMA integrity_check").fetchall(); fk=c.execute("PRAGMA foreign_key_check").fetchall(); print(result, fk); raise SystemExit(0 if result == [("ok",)] and not fk else 1)'
docker compose run --rm --no-deps bot python -m src.e2e_preflight
docker compose up -d bot
docker compose logs --tail=200 bot
```

검사 실패 시 시작하지 않는다. 복원 후 활성 내전·참가·티어 현황과 알림 중복을
확인한다. 마이그레이션 뒤 이전 코드로 돌아가야 한다면 해당 코드와 호환되는
배포 전 DB도 함께 복원해야 하며, 코드만 되돌려 실행하지 않는다.

## 14. 장애 시 대응

### 개인정보 정리와 복원 시 유의사항

새 버전은 과거 DB의 시작·취소 내전도 시작 시 정리합니다. 업데이트 전에 필요한
운영 복구용 백업을 만들되 개인정보 삭제 완료를 백업 생성으로 대체하지 않습니다.
운영 DB 정리는 Discord 메시지·기존 백업·로그를 지우지 않습니다.

운영자는 백업·로그의 보관 기한과 암호화·접근자를 확정해야 합니다. 개인정보 요청
처리 범위와 시각은 DB 백업과 분리한 접근 제한 대장으로 관리하고, 이전 백업을
복원할 때는 봇을 시작하기 전에 해당 요청을 반영해야 합니다. 요청이 반영되지 않은
DB를 Discord에 연결하지 않습니다. 이 대장과 백업 파기는 현재 봇이 자동화하지 않습니다.
티어 수집 중단 정보도 복원 대상이므로 삭제 이전 백업을 무검토로 복구하면 안 됩니다.

서비스 종료·봇 제거 시 경고를 포함한 DB와 복사본의 파기 여부를 확인합니다.
경고 탈퇴 정리는 봇이 서버에 연결되어 대상 조회가 가능할 때만 작동합니다.
Guild Members Intent는 추가하지 않으며 재적 조회 실패 시 기록을 지우지 않습니다.
배포 후 `/경고` 사용 전 `channels.warning`, 관리자 역할과 채널 권한을 확인합니다.

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
