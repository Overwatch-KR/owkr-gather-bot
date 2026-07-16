from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from src.config import ConfigurationError, RuntimeConfig, load_runtime_config
from src.main import main

from tests.helpers import make_config


class RuntimeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "config").mkdir()
        source = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
        (self.root / "config" / "config.yaml").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_env(self, content: str) -> None:
        (self.root / ".env").write_text(content, encoding="utf-8")

    def test_dotenv_is_loaded_automatically(self) -> None:
        self.write_env(
            "DISCORD_BOT_TOKEN=dotenv-secret\n"
            "OWKR_LOG_LEVEL=warning\n"
            "OWKR_DATABASE_PATH=state/test.sqlite3\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            runtime = load_runtime_config(base_directory=self.root)

        self.assertTrue(runtime.dotenv_loaded)
        self.assertEqual(runtime.token, "dotenv-secret")
        self.assertEqual(runtime.log_level, "WARNING")
        self.assertEqual(
            runtime.database_path, (self.root / "state" / "test.sqlite3").resolve()
        )
        self.assertNotIn("dotenv-secret", repr(runtime))

    def test_recruitment_role_id_is_loaded_as_a_snowflake(self) -> None:
        config_path = self.root / "config" / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "recruitment_role_id: null",
                'recruitment_role_id: "1527196510431871007"',
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "secret"}, clear=True):
            runtime = load_runtime_config(base_directory=self.root)

        self.assertEqual(
            runtime.app.defaults.recruitment_role_id,
            1527196510431871007,
        )

    def test_lobby_voice_channel_id_is_loaded_as_a_snowflake(self) -> None:
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "secret"}, clear=True):
            runtime = load_runtime_config(base_directory=self.root)

        self.assertEqual(
            runtime.app.defaults.lobby_voice_channel_id,
            123456789012345686,
        )
        self.assertEqual(
            runtime.app.defaults.lobby_voice_channel_2_id,
            123456789012345687,
        )

    def test_lobby_voice_channel_id_is_required(self) -> None:
        config_path = self.root / "config" / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                '  lobby_voice_channel_id: "123456789012345686"\n',
                "",
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "secret"}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                load_runtime_config(base_directory=self.root)

        self.assertIn(
            "defaults.lobby_voice_channel_id is required",
            str(raised.exception),
        )

    def test_existing_environment_has_priority_over_dotenv(self) -> None:
        self.write_env(
            "DISCORD_BOT_TOKEN=dotenv-secret\n"
            "OWKR_LOG_LEVEL=ERROR\n"
        )
        environment = {
            "DISCORD_BOT_TOKEN": "shell-secret",
            "OWKR_LOG_LEVEL": "DEBUG",
        }
        with patch.dict(os.environ, environment, clear=True):
            runtime = load_runtime_config(base_directory=self.root)

        self.assertEqual(runtime.token, "shell-secret")
        self.assertEqual(runtime.log_level, "DEBUG")
        self.assertNotIn("shell-secret", repr(runtime))

    def test_missing_dotenv_uses_optional_defaults(self) -> None:
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "shell-secret"}, clear=True):
            runtime = load_runtime_config(base_directory=self.root)

        self.assertFalse(runtime.dotenv_loaded)
        self.assertEqual(
            runtime.config_path, (self.root / "config" / "config.yaml").resolve()
        )
        self.assertEqual(
            runtime.database_path,
            (self.root / "data" / "owkr-gather-bot.sqlite3").resolve(),
        )
        self.assertEqual(
            runtime.template_path,
            (self.root / "templates" / "recruitment.txt").resolve(),
        )
        self.assertEqual(
            runtime.recruitment_complete_template_path,
            (self.root / "templates" / "recruitment_complete.txt").resolve(),
        )
        self.assertEqual(runtime.log_level, "INFO")

    def test_missing_token_fails_without_exposing_sensitive_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                load_runtime_config(base_directory=self.root)

        message = str(raised.exception)
        self.assertIn("DISCORD_BOT_TOKEN is required", message)
        self.assertNotIn("token=", message.lower())

    def test_invalid_log_level_is_rejected_without_exposing_token(self) -> None:
        environment = {
            "DISCORD_BOT_TOKEN": "do-not-log-this-token",
            "OWKR_LOG_LEVEL": "VERBOSE",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                load_runtime_config(base_directory=self.root)

        message = str(raised.exception)
        self.assertIn("OWKR_LOG_LEVEL must be one of", message)
        self.assertNotIn(environment["DISCORD_BOT_TOKEN"], message)

    def test_relative_paths_are_resolved_from_working_directory(self) -> None:
        environment = {
            "DISCORD_BOT_TOKEN": "secret",
            "OWKR_CONFIG_PATH": "config/config.yaml",
            "OWKR_DATABASE_PATH": "relative/data.sqlite3",
            "OWKR_TEMPLATE_PATH": "relative/template.txt",
            "OWKR_RECRUITMENT_COMPLETE_TEMPLATE_PATH": "relative/complete.txt",
        }
        with patch.dict(os.environ, environment, clear=True):
            runtime = load_runtime_config(base_directory=self.root)

        self.assertEqual(
            runtime.config_path, (self.root / "config" / "config.yaml").resolve()
        )
        self.assertEqual(
            runtime.database_path, (self.root / "relative" / "data.sqlite3").resolve()
        )
        self.assertEqual(
            runtime.template_path, (self.root / "relative" / "template.txt").resolve()
        )
        self.assertEqual(
            runtime.recruitment_complete_template_path,
            (self.root / "relative" / "complete.txt").resolve(),
        )

    def test_absolute_paths_are_preserved(self) -> None:
        config_path = self.root / "config" / "config.yaml"
        database_path = self.root / "absolute.sqlite3"
        template_path = self.root / "absolute-template.txt"
        complete_template_path = self.root / "absolute-complete-template.txt"
        environment = {
            "DISCORD_BOT_TOKEN": "secret",
            "OWKR_CONFIG_PATH": str(config_path),
            "OWKR_DATABASE_PATH": str(database_path),
            "OWKR_TEMPLATE_PATH": str(template_path),
            "OWKR_RECRUITMENT_COMPLETE_TEMPLATE_PATH": str(complete_template_path),
        }
        with patch.dict(os.environ, environment, clear=True):
            runtime = load_runtime_config(base_directory=Path("/"))

        self.assertEqual(runtime.config_path, config_path.resolve())
        self.assertEqual(runtime.database_path, database_path.resolve())
        self.assertEqual(runtime.template_path, template_path.resolve())
        self.assertEqual(
            runtime.recruitment_complete_template_path,
            complete_template_path.resolve(),
        )

    def test_main_stops_with_clear_configuration_error(self) -> None:
        stderr = StringIO()
        error = ConfigurationError("DISCORD_BOT_TOKEN is required")
        with patch("src.main.load_runtime_config", side_effect=error):
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Configuration error: DISCORD_BOT_TOKEN is required",
        )

    def test_startup_logs_do_not_include_token(self) -> None:
        secret = "never-print-this-token"
        runtime = RuntimeConfig(
            token=secret,
            app=make_config(),
            config_path=self.root / "config" / "config.yaml",
            database_path=self.root / "data.sqlite3",
            template_path=self.root / "template.txt",
            recruitment_complete_template_path=self.root / "complete-template.txt",
            log_level="INFO",
            dotenv_path=self.root / ".env",
            dotenv_loaded=True,
            working_directory=self.root,
        )
        targets = (
            "SQLiteMatchRepository",
            "PersistenceWriter",
            "SystemClock",
            "SessionCoordinator",
            "TierCollector",
            "MatchScheduler",
            "GatherBot",
        )
        patches = [patch(f"src.main.{target}") for target in targets]
        started = [item.start() for item in patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        with patch("src.main.load_runtime_config", return_value=runtime):
            with self.assertLogs("src.main", level="INFO") as captured:
                main()

        logs = "\n".join(captured.output)
        self.assertNotIn(secret, logs)
        started[-1].return_value.run.assert_called_once_with(secret, log_handler=None)
