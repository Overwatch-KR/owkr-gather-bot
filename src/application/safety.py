from datetime import datetime

from src.domain.clock import Clock
from src.domain.warnings import WarningReason, WarningSummary
from src.ports.safety import SafetyRepository


class SafetyService:
    """Explicit manager decisions only; no participant evaluation or sanctions."""

    def __init__(self, repository: SafetyRepository, clock: Clock, guild_id: int) -> None:
        self._repository = repository
        self.clock = clock
        self.guild_id = guild_id

    async def summary(self, user_id: int) -> WarningSummary:
        return await self._repository.warning_summary(self.guild_id, user_id)

    async def record(self, user_id: int, reason: WarningReason,
                     manager_id: int, request_id: int) -> WarningSummary:
        await self._repository.add_warning(
            self.guild_id, user_id, reason, manager_id, request_id, self.clock.now()
        )
        return await self.summary(user_id)

    async def revoke(self, user_id: int, warning_id: int) -> bool:
        return await self._repository.revoke_warning(
            self.guild_id, user_id, warning_id, self.clock.now()
        )

    async def purge_closed(self) -> int:
        return await self._repository.purge_closed_matches(self.guild_id)

    async def erase_tiers(self, user_id: int) -> None:
        await self._repository.erase_user_tiers(self.guild_id, user_id)

    async def forget_departed(self, user_id: int) -> None:
        await self._repository.delete_user_warnings(self.guild_id, user_id)

    async def reconcile_membership(self, user_id: int, joined_at: datetime) -> None:
        await self._repository.purge_previous_membership(self.guild_id, user_id, joined_at)

    async def warning_users(self) -> list[int]:
        return await self._repository.warning_user_ids(self.guild_id)

    async def latest_warning(self, user_id: int, reason: WarningReason) -> int | None:
        return await self._repository.latest_warning(self.guild_id, user_id, reason)
