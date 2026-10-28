"""자동 취소와 공지 삭제 취소의 메시지 정리. 이미 없는 메시지는 성공으로 처리한다."""
import logging

import discord

from src.application.rendering import format_match_time
from src.domain.models import CancellationJob

from .common import allowed_mentions_for

logger = logging.getLogger(__name__)


class DiscordCancellationTransport:
    def __init__(self, bot) -> None:
        self._bot = bot

    async def _channel(self, channel_id: int):
        await self._bot.wait_until_ready()
        channel = self._bot.get_channel(channel_id)
        return channel if channel is not None else await self._bot.fetch_channel(channel_id)

    async def cleanup(self, job: CancellationJob) -> bool:
        targets = [(job.announcement_channel_id, job.announcement_message_id)]
        if job.tier_anchor_message_id is not None:
            targets.append((job.tier_channel_id, job.tier_anchor_message_id))
        targets.extend((job.announcement_channel_id, item) for item in job.substitute_message_ids)
        complete = True
        for channel_id, message_id in targets:
            try:
                channel = await self._channel(channel_id)
                message = await channel.fetch_message(message_id)
                await message.delete()
            except discord.NotFound:
                continue
            except discord.HTTPException:
                complete = False
                logger.warning("cancellation message cleanup pending message_id=%s", message_id, exc_info=True)
        return complete

    async def notify(self, job: CancellationJob) -> bool:
        try:
            channel = await self._channel(job.announcement_channel_id)
            await channel.send(
                content=(
                    "**내전 취소**\n"
                    f"{format_match_time(job.starts_at)} 내전이 취소되었습니다."
                    + (f"\n대기실 · {job.lobby_name}" if job.lobby_name else "")
                ),
                allowed_mentions=allowed_mentions_for([]),
            )
            return True
        except discord.HTTPException:
            logger.warning("cancellation notice pending match_id=%s", job.match_id, exc_info=True)
            return False
