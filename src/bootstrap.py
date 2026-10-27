"""실행 시 사용하는 유일한 객체 조립 지점. 네트워크 연결은 bot.run 이후에 시작한다."""

from src.adapters.discord.bot import GatherBot
from src.adapters.discord.transport import (
    DiscordNotificationTransport,
    DiscordRecruitmentPublisher,
)
from src.application.coordinator import SessionCoordinator
from src.application.notification_worker import NotificationWorker
from src.application.recruitment import RecruitmentService
from src.application.rendering import NotificationRenderer, RecruitmentTemplateRenderer
from src.application.runtime import ApplicationRuntime
from src.application.safety import SafetyService
from src.application.scheduler import MatchScheduler
from src.application.tier_collector import TierCollector
from src.config import RuntimeConfig
from src.domain.clock import SystemClock
from src.infrastructure.persistence_writer import PersistenceWriter
from src.infrastructure.sqlite_repository import SQLiteMatchRepository


def build_bot(config: RuntimeConfig) -> GatherBot:
    """하나의 저장소 연결과 writer를 모든 서비스에 공유한다."""
    repository = SQLiteMatchRepository(config.database_path)
    writer = PersistenceWriter(repository)
    clock = SystemClock()
    coordinator = SessionCoordinator(config.app, repository, writer, clock)
    tiers = TierCollector(repository, clock)
    runtime = ApplicationRuntime(
        repository=repository,
        writer=writer,
        coordinator=coordinator,
        tier_collector=tiers,
        scheduler=MatchScheduler(coordinator, repository, writer, clock),
        safety=SafetyService(repository, clock, config.app.guild_id),
        migration_path=config.working_directory / "migrations/001_initial.sql",
    )
    bot = GatherBot(config=config.app, runtime=runtime, clock=clock)
    runtime.recruitment = RecruitmentService(
        coordinator,
        DiscordRecruitmentPublisher(
            bot,
            config.app,
            RecruitmentTemplateRenderer(config.app, config.template_path),
        ),
    )
    runtime.notification_worker = NotificationWorker(
        repository,
        coordinator,
        NotificationRenderer(config.app, config.recruitment_complete_template_path),
        DiscordNotificationTransport(bot),
        clock,
    )
    return bot
