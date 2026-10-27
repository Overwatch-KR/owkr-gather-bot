"""설정 로드·안전한 로그·실행 진입점. 서비스 조립은 bootstrap에 둔다."""
from __future__ import annotations

import logging
import sys

from src.bootstrap import build_bot
from src.config import ConfigurationError, load_runtime_config

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
    bot = build_bot(runtime)
    bot.run(runtime.token, log_handler=None)


if __name__ == "__main__":
    main()
