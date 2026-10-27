"""내전 생성 → 공지 전송 → 활성화의 흐름과 일반 실패 보상을 조율한다."""

from src.domain.models import MatchSession
from src.parsing.match_command import ParsedMatchCommand
from src.ports.recruitment import PublishedRecruitment, RecruitmentPublisher

from .coordinator import CreateMatchRequest, SessionCoordinator


class RecruitmentService:
    """Discord 전송과 DB는 원자적이지 않다. 중단 시 복구는 coordinator가 담당한다."""

    def __init__(
        self, coordinator: SessionCoordinator, publisher: RecruitmentPublisher
    ) -> None:
        self._coordinator = coordinator
        self._publisher = publisher

    async def create(
        self,
        *,
        manager_user_id: int,
        command_channel_id: int,
        parsed: ParsedMatchCommand,
        source_request_id: str | None = None,
        source_request_type: str | None = None,
    ) -> MatchSession:
        # 중복 요청은 공지를 전송하기 전에 coordinator에서 거부한다.
        session = await self._coordinator.create_match(
            CreateMatchRequest(
                manager_user_id=manager_user_id,
                command_channel_id=command_channel_id,
                parsed=parsed,
                source_request_id=source_request_id,
                source_request_type=source_request_type,
            )
        )
        announcement: PublishedRecruitment | None = None
        try:
            announcement = await self._publisher.publish(session)
            await self._coordinator.activate(session, announcement.message_id)
            await announcement.add_seed_reaction()
            return session
        except Exception:
            # 기존 동작과 같은 순서로 취소한다. 프로세스 강제 종료까지 보상하지는 못한다.
            await self._coordinator.cancel_match(session.id)
            if announcement is not None:
                await announcement.delete_incomplete()
            raise
