from pathlib import Path
import sqlite3
import os
import shutil
import subprocess

import pytest

from scripts.backup import create_backup


def prepare(root: Path) -> None:
    (root / "data").mkdir()
    (root / "config").mkdir()
    (root / "config/config.yaml").write_text("guild_id: 123")
    (root / "templates").mkdir()
    (root / "templates/recruitment.txt").write_text("운영 공지", encoding="utf-8")
    (root / ".env").write_text("SECRET=never-copy")


def test_backup_restores_committed_wal_and_excludes_secrets(tmp_path):
    prepare(tmp_path)
    with sqlite3.connect(tmp_path / "data/owkr-gather-bot.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('preserved')")
        db.commit()
        backup = create_backup(tmp_path, tmp_path / "backups")
    with sqlite3.connect(backup / "data/owkr-gather-bot.sqlite3") as restored:
        assert restored.execute("SELECT value FROM sample").fetchall() == [("preserved",)]
    assert not (backup / ".env").exists()
    assert not list((backup / "data").glob("*-wal"))
    assert (backup / "config/config.yaml").read_text() == "guild_id: 123"
    assert (backup / "templates/recruitment.txt").read_text(encoding="utf-8") == "운영 공지"
    assert (backup.stat().st_mode & 0o777) == 0o700
    assert ((backup / "data/owkr-gather-bot.sqlite3").stat().st_mode & 0o777) == 0o600


def test_failed_backup_leaves_no_completed_bundle(tmp_path):
    prepare(tmp_path)
    (tmp_path / "data/owkr-gather-bot.sqlite3").write_text("broken database")
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(tmp_path, tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_cannot_copy_into_its_source(tmp_path):
    prepare(tmp_path)
    with pytest.raises(ValueError):
        create_backup(tmp_path, tmp_path / "templates/backups")


@pytest.mark.parametrize("running,exit_code,backup_code,restarts,success", [
    (True, 0, 0, True, True),
    (False, 0, 0, False, True),
    (True, 0, 1, True, False),
    (True, 137, 0, False, False),
])
def test_backup_wrapper_preserves_running_state(
    tmp_path, running, exit_code, backup_code, restarts, success,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "scripts/backup.sh", scripts / "backup.sh")
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text('''#!/bin/sh
echo "$*" >> "$TEST_LOG"
case "$*" in
  'compose ps --status running -q bot')
    if [ "$TEST_RUNNING" = 1 ]; then echo container-id; fi ;;
  'compose ps -a -q bot') echo container-id ;;
  inspect*) echo "$TEST_EXIT" ;;
  'compose run '*) exit "$TEST_BACKUP_EXIT" ;;
esac
''')
    docker.chmod(0o700)
    log = tmp_path / "events"
    result = subprocess.run(["sh", str(scripts / "backup.sh")], env={
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "TEST_LOG": str(log), "TEST_RUNNING": "1" if running else "0",
        "TEST_EXIT": str(exit_code), "TEST_BACKUP_EXIT": str(backup_code),
    }, capture_output=True)
    assert (result.returncode == 0) == success
    events = log.read_text().splitlines()
    assert ("compose start bot" in events) == restarts
    if exit_code == 137:
        assert not any(event.startswith("compose run ") for event in events)
    else:
        assert events.index("compose stop bot") < next(
            i for i, event in enumerate(events) if event.startswith("compose run ")
        )
