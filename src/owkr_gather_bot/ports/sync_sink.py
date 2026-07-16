from __future__ import annotations

from typing import Protocol, Sequence

from owkr_gather_bot.domain.models import WebTierDTO


class SyncSink(Protocol):
    async def publish(self, match_id: str, participants: Sequence[WebTierDTO]) -> None: ...


class NoOpSyncSink:
    async def publish(self, match_id: str, participants: Sequence[WebTierDTO]) -> None:
        return None

