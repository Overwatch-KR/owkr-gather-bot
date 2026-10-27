"""서비스 시작·종료 순서. 구체적인 객체 생성은 bootstrap에서 수행한다."""

from dataclasses import dataclass
from pathlib import Path

from src.ports.persistence import ManagedMutationWriter
from src.ports.repositories import MatchRepository

from .coordinator import SessionCoordinator
from .notification_worker import NotificationWorker
from .recruitment import RecruitmentService
from .safety import SafetyService
from .scheduler import MatchScheduler
from .tier_collector import TierCollector


@dataclass
class ApplicationRuntime:
    repository: MatchRepository
    writer: ManagedMutationWriter
    coordinator: SessionCoordinator
    tier_collector: TierCollector
    scheduler: MatchScheduler
    safety: SafetyService
    migration_path: Path
    # Discord 클라이언트 생성 후 bootstrap이 연결한다. 시작 전에는 둘 다 준비돼야 한다.
    notification_worker: NotificationWorker | None = None
    recruitment: RecruitmentService | None = None

    async def start_storage(self) -> None:
        """마이그레이션 → writer 시작 → routing 복구 → 종료 내전 정리 순서다."""
        await self.repository.initialize(self.migration_path)
        self.writer.start()
        await self.coordinator.restore()
        await self.safety.purge_closed()

    async def start_workers(self) -> None:
        """명령 등록 후 알림 재시도와 시각 점검을 시작한다."""
        if self.notification_worker is None:
            raise RuntimeError("notification worker is not configured")
        await self.notification_worker.start()
        self.scheduler.start()

    async def stop(self) -> None:
        """새 작업을 멈춘 뒤 Actor → writer → DB를 닫아 미기록 변경을 비운다."""
        await self.scheduler.stop()
        if self.notification_worker is not None:
            await self.notification_worker.stop()
        await self.coordinator.stop()
        await self.writer.stop()
        await self.repository.close()
