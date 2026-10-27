"""Create an offline recovery bundle. The caller must stop the bot first."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile


def create_backup(root: Path, destination: Path) -> Path:
    root = root.resolve()
    destination = destination.resolve()
    for source in (root / "data", root / "templates", root / "config"):
        if destination == source or source in destination.parents:
            raise ValueError("백업 위치는 data/config/templates 밖이어야 합니다.")
    database = root / "data/owkr-gather-bot.sqlite3"
    if not database.is_file() or not (root / "config/config.yaml").is_file():
        raise ValueError("DB와 운영 설정이 있어야 백업할 수 있습니다.")
    if not (root / "templates").is_dir():
        raise ValueError("공지 문구 폴더가 없습니다.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=destination))
    stage.chmod(0o700)
    try:
        # SQLite backup consolidates any remaining WAL into a standalone database.
        (stage / "data").mkdir(mode=0o700)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(stage / "data/owkr-gather-bot.sqlite3")) as target:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("DB 무결성 검사에 실패했습니다.")
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("DB 연결 무결성 검사에 실패했습니다.")
        (stage / "config").mkdir(mode=0o700)
        shutil.copy2(root / "config/config.yaml", stage / "config/config.yaml")
        shutil.copytree(root / "templates", stage / "templates")
        (stage / "manifest.json").write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": "data/owkr-gather-bot.sqlite3",
            "note": "토큰 제외. 복원 전 개인정보 삭제 요청을 별도로 반영하세요.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        for path in stage.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        final = destination / ("backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        stage.rename(final)
        return final
    except BaseException:
        shutil.rmtree(stage)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/app"))
    parser.add_argument("--destination", type=Path, default=Path("/backups"))
    args = parser.parse_args()
    print(create_backup(args.root, args.destination))
