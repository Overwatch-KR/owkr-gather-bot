from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from scripts.retention import prune_backups

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def bundle(root: Path, days: int) -> Path:
    created = NOW - timedelta(days=days)
    path = root / ("backup-" + created.strftime("%Y%m%dT%H%M%S%fZ"))
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps({
        "created_at": created.isoformat(), "database": "data/owkr-gather-bot.sqlite3",
    }))
    return path


def test_boundary_dry_run_and_repeated_cleanup(tmp_path):
    old = bundle(tmp_path, 8)
    boundary = bundle(tmp_path, 7)
    recent = bundle(tmp_path, 6)
    assert prune_backups(tmp_path, now=NOW) == sorted([old, boundary])
    assert old.exists()
    assert prune_backups(tmp_path, now=NOW, apply=True) == sorted([old, boundary])
    assert not old.exists() and not boundary.exists() and recent.exists()
    assert prune_backups(tmp_path, now=NOW, apply=True) == []


def test_invalid_bundle_prevents_partial_deletion(tmp_path):
    old = bundle(tmp_path, 8)
    broken = bundle(tmp_path, 9)
    (broken / "manifest.json").write_text("broken")
    with pytest.raises(ValueError):
        prune_backups(tmp_path, now=NOW, apply=True)
    assert old.exists() and broken.exists()


def test_links_and_unrelated_files_are_never_traversed(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "preserve").write_text("safe")
    old = bundle(root, 8)
    (old / "nested-link").symlink_to(outside, target_is_directory=True)
    (root / "unrelated").write_text("safe")
    prune_backups(root, now=NOW, apply=True)
    assert (outside / "preserve").exists() and (root / "unrelated").exists()
    old = bundle(root, 9)
    manifest = old / "manifest.json"
    manifest.unlink()
    manifest.symlink_to(outside / "preserve")
    with pytest.raises(ValueError):
        prune_backups(root, now=NOW, apply=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        prune_backups(alias, now=NOW, apply=True)


def test_future_or_mismatched_time_is_not_purged(tmp_path):
    future = bundle(tmp_path, -1)
    assert prune_backups(tmp_path, now=NOW, apply=True) == []
    (future / "manifest.json").write_text(json.dumps({
        "created_at": (NOW - timedelta(days=30)).isoformat(),
        "database": "data/owkr-gather-bot.sqlite3",
    }))
    with pytest.raises(ValueError):
        prune_backups(tmp_path, now=NOW, apply=True)
    assert future.exists()
