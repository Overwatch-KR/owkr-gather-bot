"""기능별 SQL을 하나의 저장소로 조립한다. 기존 import 경로와 DB 형식을 유지한다."""
from src.ports.repositories import MatchRepository
from .sqlite.sessions import SessionStorage
from .sqlite.tiers import TierStorage
from .sqlite.notifications import NotificationStorage
from .sqlite.safety import SafetyStorage
from .sqlite.mutations import MutationStorage


class SQLiteMatchRepository(
    SessionStorage, TierStorage, NotificationStorage, SafetyStorage, MutationStorage,
    MatchRepository,
):
    """모든 기능이 SQLiteStore의 동일한 연결과 쓰기 잠금을 사용한다.

    기능 분리는 파일의 책임만 나눈다. 트랜잭션을 기능별로 쪼개거나 연결을 추가하지 않는다.
    """
