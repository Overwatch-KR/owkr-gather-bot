"""취소 DB와 함께 저장된 최소 정리 작업을 재시도한다. 내전 원본 보관과 분리한다."""
import asyncio
import logging

from src.ports.cancellation import CancellationTransport
from src.ports.repositories import SessionRepository

logger = logging.getLogger(__name__)


class CancellationWorker:
    def __init__(self, repository: SessionRepository, transport: CancellationTransport, guild_id: int) -> None:
        self._repository = repository
        self._transport = transport
        self._guild_id = guild_id
        self._lock = asyncio.Lock()

    async def process_once(self) -> None:
        async with self._lock:
            for job in await self._repository.pending_cancellations(self._guild_id):
                try:
                    cleaned = await self._transport.cleanup(job)
                    sent = job.notice_sent
                    if not sent:
                        sent = await self._transport.notify(job)
                        if sent:
                            # Discord 전송 직후 이 기록 전 강제 종료에는 안내가 중복될 수 있다.
                            await self._repository.mark_cancellation_notice_sent(job.match_id)
                    if cleaned and sent:
                        await self._repository.complete_cancellation(job.match_id)
                except Exception:
                    logger.exception("cancellation cleanup failed match_id=%s", job.match_id)
