from __future__ import annotations

import logging
from pathlib import Path

from owkr_gather_bot.adapters.discord_adapter import GatherBot
from owkr_gather_bot.application.coordinator import SessionCoordinator
from owkr_gather_bot.application.scheduler import MatchScheduler
from owkr_gather_bot.application.tier_collector import TierCollector
from owkr_gather_bot.config import load_runtime_config
from owkr_gather_bot.domain.clock import SystemClock
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter
from owkr_gather_bot.infrastructure.sqlite_repository import SQLiteMatchRepository


def main() -> None:
    runtime = load_runtime_config()
    logging.basicConfig(
        level=getattr(logging, runtime.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    repository = SQLiteMatchRepository(runtime.database_path)
    writer = PersistenceWriter(repository)
    clock = SystemClock()
    coordinator = SessionCoordinator(runtime.app, repository, writer, clock)
    tier_collector = TierCollector(coordinator, writer, clock)
    scheduler = MatchScheduler(coordinator, repository, writer, clock)
    project_root = Path(__file__).resolve().parents[2]
    bot = GatherBot(
        config=runtime.app,
        repository=repository,
        writer=writer,
        coordinator=coordinator,
        tier_collector=tier_collector,
        scheduler=scheduler,
        clock=clock,
        template_path=runtime.template_path,
        migration_path=project_root / "migrations" / "001_initial.sql",
    )
    bot.run(runtime.token, log_handler=None)


if __name__ == "__main__":
    main()

