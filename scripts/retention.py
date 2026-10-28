"""Prune only validated OWKR backup bundles older than seven days."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil

BACKUP_NAME = re.compile(r"backup-(\d{8}T\d{6})(\d{0,6})Z")
RETENTION = timedelta(days=7)


def expired_backups(directory: Path, now: datetime) -> list[Path]:
    """Validate the complete plan before deleting anything; never follow links."""
    if now.tzinfo is None:
        raise ValueError("현재 시각에 시간대가 필요합니다.")
    if directory.is_symlink():
        raise ValueError("백업 경로는 심볼릭 링크일 수 없습니다.")
    if not directory.exists():
        return []
    cutoff = now - RETENTION
    result: list[Path] = []
    for bundle in sorted(directory.iterdir()):
        match = BACKUP_NAME.fullmatch(bundle.name)
        if not match:
            continue
        if bundle.is_symlink() or not bundle.is_dir():
            raise ValueError(f"백업 폴더 형식 오류: {bundle.name}")
        manifest = bundle / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 65536:
            raise ValueError(f"백업 생성 기록 확인 필요: {bundle.name}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
            named = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
                microsecond=int(match.group(2).ljust(6, "0") or "0"), tzinfo=timezone.utc,
            )
            if created.tzinfo is None or data["database"] != "data/owkr-gather-bot.sqlite3":
                raise ValueError("invalid manifest")
            if abs((created - named).total_seconds()) > 60:
                raise ValueError("timestamp mismatch")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"백업 생성 기록 오류: {bundle.name}") from exc
        if created <= cutoff and named <= cutoff:
            result.append(bundle)
    return result


def prune_backups(directory: Path, *, now: datetime, apply: bool = False) -> list[Path]:
    plan = expired_backups(directory, now)
    if apply:
        for bundle in plan:
            # rmtree removes nested links themselves, never their targets.
            shutil.rmtree(bundle)
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backups", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="기본은 미리보기이며 지정 시 만료 백업을 삭제합니다.")
    args = parser.parse_args()
    for item in prune_backups(args.backups, now=datetime.now(timezone.utc), apply=args.apply):
        print(("removed " if args.apply else "would remove ") + item.name)
