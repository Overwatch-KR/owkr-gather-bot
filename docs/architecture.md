# 코드 구조와 변경 원칙

이 문서는 현재 구현의 책임과 의존성, 실행·복구 순서를 설명한다.
제품 정책은 저장소의 `AGENTS.md`와 `README.md`를 따른다.
단일 길드·단일 프로세스·SQLite 운영과 6개 공개 명령은 유지한다.

## 구조 선택

기존 계층을 유지하고 외부 시스템과의 경계에는 포트와 어댑터를 사용한다.
폴더를 늘리는 것보다 운영 규칙을 바꿀 위치와 실패 시 책임을 분명히 하는 것이 목적이다.
각 함수에 대응하는 인터페이스나 별도 프로세스는 만들지 않는다.
Actor는 내전별 반응 순서를 지키는 실행 방식으로, 이 구조 안에서 계속 사용한다.

| 위치 | 책임 | 주요 진입점 |
| --- | --- | --- |
| `src/main.py` | 설정 로드, 토큰을 제외한 로그, 실행 | `main()` |
| `src/bootstrap.py` | 구체적인 객체를 만들고 하나의 저장소·writer를 공유 | `build_bot()` |
| `src/domain` | 상태, 시각 조건, 검증된 설정 값 | `models.py`, `settings.py` |
| `src/application` | 생성·취소·반응·티어·알림·실행 순서 | coordinator, actor, recruitment, runtime |
| `src/ports` | 저장·큐·전송에 필요한 계약 | repositories, persistence, recruitment, notifications |
| `src/adapters/discord` | Discord 입력과 응답·전송·재연결 처리 | bot, commands, views, events, transport, common |
| `src/adapters/safety_adapter.py` | 경고·본인 티어 삭제의 Discord 화면과 재적 확인 | SafetyCog |
| `src/infrastructure/sqlite` | SQL과 트랜잭션·스키마 호환 | base, sessions, tiers, mutations, notifications, safety |

`domain`은 외부 계층을 가져오지 않는다. `ports`는 application과 구체적인 외부 구현에
의존하지 않는다. `application`은 Discord SDK·SQLite 구현·설정 파일 로더를 가져오지
않는다. `tests/test_architecture.py`가 이 경계를 검사한다.

`application/rendering.py`와 공지 문구 로더는 템플릿과 파일을 다루는 기존 표시
기능이다. 모든 application 코드를 순수 함수로 만들지는 않았다. 운영 규칙을 SDK나
SQL 구현과 결합하지 않는 것을 우선한다.

## 코드를 읽는 순서

1. `bootstrap.build_bot()`에서 어떤 객체를 공유하는지 확인한다.
2. `ApplicationRuntime`에서 시작·종료 순서를 확인한다.
3. `discord/commands.py`에서 명령 입력을, `views.py`에서 확인 버튼을 찾는다.
4. 생성 흐름은 `RecruitmentService`, 생명주기는 `SessionCoordinator`를 읽는다.
5. 반응은 `discord/events.py` → coordinator → `SessionActor` → writer 순서로 따라간다.
6. 티어는 `TierCollector`, 알림은 `NotificationWorker`를 읽는다.
7. SQL이 필요할 때 해당 기능의 SQLite 파일을 확인한다.

## 내전 생성과 취소

`/내전`의 입력과 확인창은 Discord 어댑터에서 처리한다. 실제 생성 순서는
`RecruitmentService`가 조율한다.

1. coordinator가 중복 요청과 1시간 미만 중첩 대기실 예약을 검사하고 준비 상태를 저장한다.
   같은 시작 시각도 서로 다른 빈 대기실에 배정할 수 있으며, 방이 없으면 공지 전송 전에 거부한다.
2. `RecruitmentPublisher`가 공지를 전송한다.
3. coordinator가 공지 ID를 기록하고 Actor와 메시지 routing을 활성화한다.
4. 전송 결과 핸들이 최초 ✅를 추가한다.

일반 예외가 발생하면 기존 순서대로 해당 내전을 취소하고, 이미 전송한 공지를
삭제한다. Discord 삭제 실패는 어댑터에서 기록한다. 다른 내전은 취소하지 않는다.
대타·취소 명령도 기존 coordinator 생명주기를 통한다.

Discord 전송과 DB 기록은 원자적이지 않다. 공지 전송 직후 활성화 전에 프로세스가
강제 종료되면 고아 공지가 남을 수 있다. 재시작은 미완성 생성의 DB 기록을 취소하며
공지의 존재를 추측해 새로 전송하지 않는다. 이 변경은 그 실패 구간을 제거하지 않는다.

## 참가 순서와 저장 완료

각 SessionActor가 자신의 큐 접수 순서대로 반응을 처리하고 `arrival_seq`를 부여한다.
이는 Discord 이벤트를 봇이 수신한 순서이며 사용자가 클릭한 절대 시각의 순서는 아니다.
중복 ADD·REMOVE와 과거 상태 호환은 기존 규칙을 유지한다.

`full_reached_at`은 한 번 정원이 찬 이력이다. 이후 확정 참가자가 나가도 이 값을
되돌리지 않아 대기자가 자동 확정되지 않게 한다. 대타 모집은 관리자 요청으로 시작한다.

- `actor.ingest()` 성공: 메모리 큐에 접수됨.
- `actor.drain()` 완료: 접수한 반응의 상태 처리와 writer 제출이 끝남.
- `writer.submit()` 반환: 저장 큐에 접수됨. DB 커밋 완료는 아님.
- `writer.flush()` 완료: 앞서 제출한 변경의 커밋 완료.

writer는 실패한 배치를 건너뛰지 않고 재시도한다. 저장 장애가 계속되면 flush와 정상
종료가 오래 대기할 수 있다. 두 큐는 메모리에 있으므로 강제 종료 이전의 미커밋
이벤트까지 복구한다고 보장하지 않는다. 포트 분리는 이 성질을 바꾸지 않는다.

## 시작·취소·정상 종료

시작 시각부터 새 반응과 티어 이벤트를 받지 않는다. 내전 시작 처리는 Actor를
비우고 writer를 flush한 뒤 DB를 시작 상태로 변경하고 해당 내전의 routing을 지운다.
취소는 DB를 먼저 취소 상태로 만든 뒤 Actor의 대기 이벤트를 버린다. 뒤늦은 저장
변경은 활성 상태 재검사로 취소된 내전을 되살리지 못하게 한다.

프로세스 초기화는 다음 순서다.

1. SQLite 연결과 마이그레이션
2. writer 시작
3. 활성 내전·참가 명단·공지/대타/티어 안내 routing 복구
4. 과거 시작·취소 내전 정리
5. Discord Cog와 길드 명령 등록
6. outbox 재시도와 scheduler 시작
7. Discord 연결 완료 후 티어 채널 보완 수집 한 번 실행

정상 종료는 SafetyCog 유지보수 작업 → GatherCog 반응 보정·티어 보완 수집 → scheduler → 알림 worker → Actor → writer → DB
→ Discord 연결 순서다. Actor 접수분을 writer로 넘기고 커밋한 후 DB를 닫는다.
시작 도중 실패했을 때는 준비된 작업을 같은 종료 경로로 정리한다.

## 알림과 티어 복구

알림은 DB outbox의 예약·claim·재시도 정보를 사용한다. Discord 전송 성공 뒤 DB의
완료 기록 전에 강제 종료되면 재전송될 수 있다. 정확히 한 번 전송을 보장하지 않는다.
미작성자·전원 완료 알림의 과거 레코드는 공개 전송 없이 처리 완료하며 관련 호환
타입과 마이그레이션은 유지한다.

티어 신규 제출은 해당 안내 메시지 답장으로만 연결한다. 기존 DB에 이미 연결된
메시지의 수정·삭제는 계속 처리한다. 시작·마감 조건과 수집 중단 여부를 저장 시점에
다시 검사한다. 본인 삭제 시에는 수집 중단 기록과 원문·연결 삭제를 같은 트랜잭션으로
처리해 재시작 보완 수집으로 원문이 복원되지 않게 한다.

## 저장소 분리의 범위

SessionRepository·TierRepository·NotificationRepository·MutationRepository·SafetyRepository는
소비자가 필요한 기능을 표현한다. MatchRepository는 조립과 기존 코드 호환을 위한
통합 계약이다. writer는 변경 저장 계약만, 티어 수집은 티어 계약만 사용한다.

SQLiteMatchRepository 하나가 기능별 구현을 조립한다. 모든 구현은 SQLiteStore의
동일한 연결과 쓰기 잠금을 공유한다. 기능 파일을 따로 인스턴스화하거나 각자의
연결을 열어서는 안 된다. 반응·참가 상태·모집 완료 알림 예약을 함께 기록하는
트랜잭션, 삭제와 수집 중단을 함께 기록하는 트랜잭션은 나누지 않는다.

`sqlite_repository.py`와 `discord_adapter.py`는 기존 import 호환을 위한 얇은 진입점이다.
기능 코드를 그 파일에 다시 추가하지 않는다. DB 스키마·SQL 동작·기존 migration은
이번 구조 정리에서 변경하지 않았다.

## 주석과 검증

모듈 docstring은 책임과 경계를, 중요한 메서드는 부작용·완료 시 보장·실패 동작을
설명한다. 코드를 그대로 읽어주는 주석보다 순서·시간 경계·보관 정책과 그 이유를
한국어로 작성한다. 설명과 동작을 같은 변경에서 갱신한다.

전체 테스트·컴파일·의존성 검사를 실행한다. 구조 변경에서도 중복 요청, 동시에 들어온
반응, 시작 시각 경계, 취소된 내전과 다른 내전의 분리, 재시작 routing·roster 복구,
멘션 제한, 과거 스키마와 개인정보 삭제 복구 차단을 확인한다. mock만으로 DB의
원자성을 판단하지 않고 기존 SQLite 통합 테스트를 유지한다.
