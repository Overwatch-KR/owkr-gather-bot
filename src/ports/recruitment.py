"""모집 공지의 외부 전송 계약. application에는 Discord 메시지 객체를 넘기지 않는다."""

from typing import Protocol

from src.domain.models import MatchSession


class PublishedRecruitment(Protocol):
    @property
    def message_id(self) -> int: ...

    async def add_seed_reaction(self) -> None: ...

    async def delete_incomplete(self) -> None:
        """실패한 생성의 공지를 정리한다. 전송 시스템의 삭제 실패는 구현에서 기록한다."""
        ...


class RecruitmentPublisher(Protocol):
    async def publish(self, session: MatchSession) -> PublishedRecruitment:
        """공지를 보내고 정리 가능한 전송 결과를 반환한다."""
        ...
