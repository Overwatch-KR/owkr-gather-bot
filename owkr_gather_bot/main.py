from __future__ import annotations

import logging
import sys

from owkr_gather_bot.adapters.discord_adapter import GatherBot
from owkr_gather_bot.application.coordinator import SessionCoordinator
from owkr_gather_bot.application.scheduler import MatchScheduler
from owkr_gather_bot.application.tier_collector import TierCollector
from owkr_gather_bot.config import ConfigurationError, load_runtime_config
from owkr_gather_bot.domain.clock import SystemClock
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter
from owkr_gather_bot.infrastructure.sqlite_repository import SQLiteMatchRepository


logger = logging.getLogger(__name__)


def main() -> None:
    try:
        runtime = load_runtime_config()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=getattr(logging, runtime.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("owkr-gather-bot starting")
    logger.info("dotenv loaded=%s path=%s", runtime.dotenv_loaded, runtime.dotenv_path)
    logger.info("config path=%s", runtime.config_path)
    logger.info("SQLite database path=%s", runtime.database_path)
    logger.info("recruitment template path=%s", runtime.template_path)
    logger.info(
        "recruitment complete template path=%s",
        runtime.recruitment_complete_template_path,
    )
    repository = SQLiteMatchRepository(runtime.database_path)
    writer = PersistenceWriter(repository)
    clock = SystemClock()
    coordinator = SessionCoordinator(runtime.app, repository, writer, clock)
    tier_collector = TierCollector(coordinator, writer, clock)
    scheduler = MatchScheduler(coordinator, repository, writer, clock)
    bot = GatherBot(
        config=runtime.app,
        repository=repository,
        writer=writer,
        coordinator=coordinator,
        tier_collector=tier_collector,
        scheduler=scheduler,
        clock=clock,
        template_path=runtime.template_path,
        recruitment_complete_template_path=runtime.recruitment_complete_template_path,
        migration_path=runtime.working_directory / "migrations" / "001_initial.sql",
    )
    bot.run(runtime.token, log_handler=None)


if __name__ == "__main__":
    main()
