from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from owkr_gather_bot.domain.models import PersistenceMutation
from owkr_gather_bot.ports.repositories import MatchRepository


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _FlushBarrier:
    future: asyncio.Future[None]


class _Stop:
    pass


QueueItem = PersistenceMutation | _FlushBarrier | _Stop


class PersistenceWriter:
    def __init__(
        self,
        repository: MatchRepository,
        *,
        flush_interval_seconds: float = 1.0,
        max_batch_size: int = 100,
    ) -> None:
        self._repository = repository
        self._flush_interval = flush_interval_seconds
        self._max_batch_size = max_batch_size
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=5000)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="sqlite-persistence-writer")

    def submit(self, mutation: PersistenceMutation) -> None:
        self._queue.put_nowait(mutation)

    async def flush(self) -> None:
        if self._task is None:
            return
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(_FlushBarrier(future))
        await future

    async def stop(self) -> None:
        if self._task is None:
            return
        await self.flush()
        await self._queue.put(_Stop())
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            if isinstance(first, _Stop):
                self._queue.task_done()
                return
            if isinstance(first, _FlushBarrier):
                first.future.set_result(None)
                self._queue.task_done()
                continue

            batch: list[PersistenceMutation] = [first]
            barriers: list[_FlushBarrier] = []
            should_stop = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._flush_interval

            while len(batch) < self._max_batch_size:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except TimeoutError:
                    break
                if isinstance(item, _Stop):
                    should_stop = True
                    self._queue.task_done()
                    break
                if isinstance(item, _FlushBarrier):
                    barriers.append(item)
                    self._queue.task_done()
                    break
                batch.append(item)

            retry_delay = 0.1
            while True:
                try:
                    await self._repository.apply_mutations(batch)
                    break
                except Exception:
                    logger.exception("failed to persist batch; retrying")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 5.0)

            for _ in batch:
                self._queue.task_done()
            for barrier in barriers:
                if not barrier.future.done():
                    barrier.future.set_result(None)
            if should_stop:
                return
